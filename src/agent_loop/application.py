"""Explicitly apply a verified workspace after a human gate."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .storage import StateStore, utc_now
from .types import ApplyError, RunStatus


MAX_APPLY_FILES = 1_000
MAX_APPLY_BYTES = 10_000_000


@dataclass(frozen=True)
class ApplyChange:
    path: str
    operation: str
    before_sha256: str | None
    after_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ApplyPreview:
    application_id: str
    run_id: str
    target_dir: str
    workspace_digest: str
    target_overlay_digest: str
    changes: tuple[ApplyChange, ...]


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _scan_workspace(root: Path) -> tuple[str, dict[str, bytes]]:
    """Read a bounded, link-free workspace and reproduce Workspace.digest()."""

    digest = hashlib.sha256()
    files: dict[str, bytes] = {}
    total_bytes = 0
    for entry in sorted(root.rglob("*")):
        name = entry.relative_to(root).as_posix()
        mode = entry.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ApplyError(f"workspace contains a symbolic link: {name}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ApplyError(f"workspace contains a special file: {name}")
        content = entry.read_bytes()
        files[name] = content
        total_bytes += len(content)
        if len(files) > MAX_APPLY_FILES or total_bytes > MAX_APPLY_BYTES:
            raise ApplyError("workspace exceeds the v0 apply size limit")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest(), files


def _safe_target_root(target_dir: str | Path, run_dir: Path) -> tuple[Path, tuple[int, int]]:
    raw = Path(target_dir).expanduser()
    if raw.is_symlink():
        raise ApplyError("target directory cannot be a symbolic link")
    try:
        root = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ApplyError("target directory must already exist") from exc
    if not root.is_dir():
        raise ApplyError("target must be a directory")
    if root == run_dir or root.is_relative_to(run_dir):
        raise ApplyError("target directory cannot be inside the Run directory")
    metadata = root.stat()
    return root, (metadata.st_dev, metadata.st_ino)


def _target_path(root: Path, name: str, protected: Path) -> Path:
    candidate = root / Path(name)
    # Existing parents and files must be real entries, never links.  This also
    # rejects a link that happens to resolve back inside the target directory.
    current = root
    for part in Path(name).parts:
        current = current / part
        if current.is_symlink():
            raise ApplyError(f"target path contains a symbolic link: {name}")
        if current.exists() and current != candidate and not current.is_dir():
            raise ApplyError(f"target parent is not a directory: {name}")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ApplyError(f"target path escapes the target directory: {name}")
    if resolved == protected or resolved.is_relative_to(protected):
        raise ApplyError(f"change would overwrite Run records: {name}")
    return resolved


def _build_preview(
    application_id: str,
    run_id: str,
    root: Path,
    protected: Path,
    workspace_digest: str,
    files: dict[str, bytes],
) -> ApplyPreview:
    changes: list[ApplyChange] = []
    overlay = hashlib.sha256()
    for name, content in files.items():
        target = _target_path(root, name, protected)
        if target.exists() and not target.is_file():
            raise ApplyError(f"target entry is not a regular file: {name}")
        before = target.read_bytes() if target.exists() else None
        before_hash = _hash_bytes(before) if before is not None else None
        after_hash = _hash_bytes(content)
        if before_hash == after_hash:
            continue
        changes.append(
            ApplyChange(
                name,
                "modify" if before is not None else "add",
                before_hash,
                after_hash,
                len(content),
            )
        )
        overlay.update(name.encode("utf-8"))
        overlay.update(b"\0")
        overlay.update((before_hash or "missing").encode("ascii"))
        overlay.update(b"\0")
    return ApplyPreview(
        application_id,
        run_id,
        str(root),
        workspace_digest,
        overlay.hexdigest(),
        tuple(changes),
    )


def _record(preview: ApplyPreview, created_at: str, status: str, **extra) -> dict:
    return {
        "schema_version": 1,
        **asdict(preview),
        "created_at": created_at,
        "status": status,
        **extra,
    }


def apply_run(
    store: StateStore,
    run_id: str,
    target_dir: str | Path,
    confirm: Callable[[ApplyPreview], bool],
) -> dict:
    """Preview, confirm, revalidate, then atomically replace target files."""

    state = store.load(run_id)
    manifest = store.load_manifest(run_id)
    if state.status is not RunStatus.COMPLETED:
        raise ApplyError("only a completed Run can be applied")
    if not state.last_verification or state.last_verification.get("verdict") != "pass":
        raise ApplyError("completed Run is missing passing verification evidence")
    if state.pending_approval or state.in_flight_action:
        raise ApplyError("Run still has an unresolved action")
    if manifest["scenario"]["digest"] != state.scenario_digest:
        raise ApplyError("Run manifest and state disagree on the Scenario")

    run_dir = store.run_dir(run_id).resolve()
    workspace_root = run_dir / "workspace"
    workspace_digest, files = _scan_workspace(workspace_root)
    if workspace_digest != state.last_workspace_digest:
        raise ApplyError("workspace changed after its passing verification")
    target, target_identity = _safe_target_root(target_dir, run_dir)
    application_id = uuid.uuid4().hex
    preview = _build_preview(
        application_id, run_id, target, store.root, workspace_digest, files
    )
    created_at = utc_now()
    store.write_application(
        run_id, application_id, _record(preview, created_at, "prepared", confirmed=False)
    )

    if not confirm(preview):
        record = _record(
            preview, created_at, "declined", confirmed=False, completed_at=utc_now()
        )
        store.write_application(run_id, application_id, record)
        return record

    try:
        current_identity = target.stat()
        if (current_identity.st_dev, current_identity.st_ino) != target_identity:
            raise ApplyError("target directory changed during confirmation")
        second_digest, second_files = _scan_workspace(workspace_root)
        second_preview = _build_preview(
            application_id, run_id, target, store.root, second_digest, second_files
        )
        if second_digest != workspace_digest or second_preview != preview:
            raise ApplyError("workspace or target changed during confirmation")

        # Freeze the verified bytes before the first target-side effect.
        frozen = {
            change.path: second_files[change.path] for change in second_preview.changes
        }
        if _scan_workspace(workspace_root)[0] != workspace_digest:
            raise ApplyError("workspace changed while preparing the application")

        applied: list[str] = []
        store.write_application(
            run_id,
            application_id,
            _record(preview, created_at, "applying", confirmed=True, applied_paths=[]),
        )
        for change in preview.changes:
            destination = _target_path(target, change.path, store.root)
            before = _hash_bytes(destination.read_bytes()) if destination.exists() else None
            if before != change.before_sha256:
                raise ApplyError(f"target changed before writing: {change.path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{application_id}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(frozen[change.path])
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            applied.append(change.path)
        record = _record(
            preview,
            created_at,
            "applied",
            confirmed=True,
            applied_paths=applied,
            completed_at=utc_now(),
        )
    except Exception as exc:
        record = _record(
            preview,
            created_at,
            "failed",
            confirmed=True,
            applied_paths=locals().get("applied", []),
            completed_at=utc_now(),
            error=str(exc),
        )
        store.write_application(run_id, application_id, record)
        if isinstance(exc, ApplyError):
            raise
        raise ApplyError(f"apply failed: {exc}") from exc

    store.write_application(run_id, application_id, record)
    return record
