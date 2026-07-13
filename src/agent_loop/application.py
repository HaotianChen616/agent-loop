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
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
    os, "O_NOFOLLOW", 0
)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)


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


def _require_safe_open_support() -> None:
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise ApplyError("safe apply requires POSIX O_NOFOLLOW and O_DIRECTORY support")


def _read_regular_fd(file_fd: int, label: str) -> bytes:
    """Read one already-opened file and reject a concurrent inode mutation."""

    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ApplyError(f"entry is not a regular file: {label}")
        if before.st_size > MAX_APPLY_BYTES:
            raise ApplyError(f"file exceeds the v0 apply size limit: {label}")
        chunks: list[bytes] = []
        remaining = MAX_APPLY_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(file_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if len(content) > MAX_APPLY_BYTES or identity_before != identity_after:
            raise ApplyError(f"file changed while being read: {label}")
        return content
    finally:
        os.close(file_fd)


def _scan_workspace(root: Path) -> tuple[str, dict[str, bytes]]:
    """Read a bounded workspace through directory FDs without following links."""

    _require_safe_open_support()
    files: dict[str, bytes] = {}
    total_bytes = 0
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        for directory, directories, names, directory_fd in os.fwalk(
            ".", topdown=True, follow_symlinks=False, dir_fd=root_fd
        ):
            directories.sort()
            names.sort()
            base = Path() if directory == "." else Path(directory)
            for child in directories:
                metadata = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise ApplyError(
                        f"workspace contains a symbolic link: {(base / child).as_posix()}"
                    )
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ApplyError(
                        f"workspace contains a special entry: {(base / child).as_posix()}"
                    )
            for name in names:
                relative = (base / name).as_posix()
                try:
                    file_fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
                except OSError as exc:
                    raise ApplyError(f"cannot safely open workspace file: {relative}") from exc
                content = _read_regular_fd(file_fd, relative)
                files[relative] = content
                total_bytes += len(content)
                if len(files) > MAX_APPLY_FILES or total_bytes > MAX_APPLY_BYTES:
                    raise ApplyError("workspace exceeds the v0 apply size limit")
    except OSError as exc:
        raise ApplyError("workspace changed while it was being scanned") from exc
    finally:
        os.close(root_fd)

    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest(), files


def _safe_target_root(target_dir: str | Path, run_dir: Path) -> Path:
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
    return root


def _validate_target_name(root: Path, name: str, protected: Path) -> tuple[str, ...]:
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ApplyError(f"invalid target path: {name}")
    candidate = root.joinpath(*relative.parts)
    if candidate == protected or candidate.is_relative_to(protected):
        raise ApplyError(f"change would overwrite Run records: {name}")
    return relative.parts


def _open_parent(
    root_fd: int, parts: tuple[str, ...], *, create: bool
) -> tuple[int, str] | None:
    """Open each parent relative to a trusted FD, rejecting every symlink."""

    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    os.close(parent_fd)
                    return None
                try:
                    os.mkdir(part, mode=0o755, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise ApplyError("target parent is not a safe directory") from exc
            os.close(parent_fd)
            parent_fd = next_fd
        return parent_fd, parts[-1]
    except Exception:
        try:
            os.close(parent_fd)
        except OSError:
            pass
        raise


def _read_target(root_fd: int, parts: tuple[str, ...], label: str) -> bytes | None:
    opened = _open_parent(root_fd, parts, create=False)
    if opened is None:
        return None
    parent_fd, leaf = opened
    try:
        try:
            file_fd = os.open(leaf, _FILE_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ApplyError(f"target entry is not a safe regular file: {label}") from exc
        return _read_regular_fd(file_fd, label)
    finally:
        os.close(parent_fd)


def _write_target(
    root_fd: int,
    parts: tuple[str, ...],
    change: ApplyChange,
    content: bytes,
    application_id: str,
) -> None:
    opened = _open_parent(root_fd, parts, create=True)
    assert opened is not None
    parent_fd, leaf = opened
    temporary = f".{leaf}.{application_id}.tmp"
    try:
        before = _read_target(parent_fd, (leaf,), change.path)
        before_hash = _hash_bytes(before) if before is not None else None
        if before_hash != change.before_sha256:
            raise ApplyError(f"target changed before writing: {change.path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(file_fd, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(
                temporary,
                leaf,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        finally:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent_fd)


def _build_preview(
    application_id: str,
    run_id: str,
    root: Path,
    root_fd: int,
    protected: Path,
    workspace_digest: str,
    files: dict[str, bytes],
) -> ApplyPreview:
    changes: list[ApplyChange] = []
    overlay = hashlib.sha256()
    for name, content in sorted(files.items()):
        parts = _validate_target_name(root, name, protected)
        before = _read_target(root_fd, parts, name)
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
    target = _safe_target_root(target_dir, run_dir)
    _require_safe_open_support()
    target_fd = os.open(target, _DIRECTORY_FLAGS)
    target_identity = os.fstat(target_fd)
    application_id = uuid.uuid4().hex
    created_at = utc_now()
    try:
        preview = _build_preview(
            application_id,
            run_id,
            target,
            target_fd,
            store.root,
            workspace_digest,
            files,
        )
        store.write_application(
            run_id,
            application_id,
            _record(preview, created_at, "prepared", confirmed=False),
        )

        if not confirm(preview):
            record = _record(
                preview, created_at, "declined", confirmed=False, completed_at=utc_now()
            )
            store.write_application(run_id, application_id, record)
            return record

        try:
            current_fd = os.open(target, _DIRECTORY_FLAGS)
            try:
                current = os.fstat(current_fd)
                if (current.st_dev, current.st_ino) != (
                    target_identity.st_dev,
                    target_identity.st_ino,
                ):
                    raise ApplyError("target directory changed during confirmation")
            finally:
                os.close(current_fd)
            second_digest, second_files = _scan_workspace(workspace_root)
            second_preview = _build_preview(
                application_id,
                run_id,
                target,
                target_fd,
                store.root,
                second_digest,
                second_files,
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
                parts = _validate_target_name(target, change.path, store.root)
                _write_target(
                    target_fd,
                    parts,
                    change,
                    frozen[change.path],
                    application_id,
                )
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
    finally:
        os.close(target_fd)
