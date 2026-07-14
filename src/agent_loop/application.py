"""通过显式人工关卡，将验证通过的 Workspace 发布到目标目录。"""

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
    """预览中的单文件变化，before/after 摘要用于发现确认期间的并发修改。"""

    path: str
    operation: str
    before_sha256: str | None
    after_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ApplyPreview:
    """提交给人工确认的完整发布计划，绑定 Run、目标目录和双方摘要。"""

    application_id: str
    run_id: str
    target_dir: str
    workspace_digest: str
    target_overlay_digest: str
    changes: tuple[ApplyChange, ...]


def _hash_bytes(content: bytes) -> str:
    """计算文件内容 SHA-256，统一用于预览比较和写入前复核。"""

    return hashlib.sha256(content).hexdigest()


def _require_safe_open_support() -> None:
    """要求 POSIX 的目录与防跟随标志；平台不支持时宁可拒绝 Apply。"""

    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise ApplyError("safe apply requires POSIX O_NOFOLLOW and O_DIRECTORY support")


def _read_regular_fd(file_fd: int, label: str) -> bytes:
    """读取已经打开的普通文件，并拒绝读取期间发生的 inode 变化。

    读取前后比较设备、inode、大小和 mtime；任一变化都说明内容可能不是同一稳定
    文件。函数始终负责关闭传入 FD，调用者不能再次使用它。
    """

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
    """通过目录文件描述符有界读取 Workspace，全程不跟随符号链接。

    同时限制文件数、单文件大小与总字节数。返回摘要和已读取字节，后续 Apply 可以
    基于同一份扫描结果制作预览，不再通过易受竞态影响的字符串路径重复读取。
    """

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


def _safe_target_root(
    target_dir: str | Path, run_dir: Path
) -> tuple[Path, tuple[int, int]]:
    """校验目标根目录并记录设备/inode 身份。

    目标必须预先存在、不能是符号链接，也不能位于 Run 目录内部，避免把发布结果
    覆盖到状态、证据或 Workspace 自身。
    """

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


def _validate_target_name(root: Path, name: str, protected: Path) -> tuple[str, ...]:
    """校验一个发布相对路径，并阻止它覆盖受保护的 Runs 根目录。"""

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
    """相对可信目录 FD 逐层打开父目录，并拒绝任意层级的符号链接。

    `create=True` 时可以创建缺失目录，但创建后仍会以 O_NOFOLLOW 重新打开，防止
    另一个进程在检查与使用之间把目录替换成符号链接。
    """

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
    """相对目标根 FD 安全读取现有文件；文件不存在返回 None。"""

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
    """在目标父目录内写临时文件并原子替换最终文件。

    写入前再次比较现有内容与 preview.before_sha256，发现目标被并发修改就停止。
    临时文件使用 O_EXCL 创建，写入后 flush + fsync，再通过目录 FD 执行 replace。
    """

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
    """比较冻结的 Workspace 字节与当前目标，生成逐文件变化和目标覆盖摘要。

    v0 只添加或修改 Workspace 中存在的文件，不删除目标目录的额外文件。没有内容
    变化的文件不会出现在 changes 中。
    """

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
    """把同一 Preview 扩展为 prepared/declined/applying/applied/failed 审计记录。"""

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
    """先预览、再确认和复核，最后原子替换目标文件。

    发布阶段依次为：校验 completed 证据 → 扫描并绑定 Workspace → 打开并绑定目标
    根目录 → 写 prepared 记录 → 人工确认 → 二次扫描双方 → 冻结字节 → 逐文件原子
    写入 → 写最终审计。prepared 记录落盘后的失败都会留下结果，且不会改变 Run 终态。
    """

    # completed 只是发布的必要条件；仍需验证证据和 Workspace 摘要共同证明结果未变。
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
    target, expected_target_identity = _safe_target_root(target_dir, run_dir)
    _require_safe_open_support()
    target_fd = os.open(target, _DIRECTORY_FLAGS)
    target_identity = os.fstat(target_fd)
    if (target_identity.st_dev, target_identity.st_ino) != expected_target_identity:
        os.close(target_fd)
        raise ApplyError("target directory changed while it was being opened")
    application_id = uuid.uuid4().hex
    created_at = utc_now()
    try:
        # prepared 记录先于人工确认落盘，即使用户退出也能解释这次 Apply 尝试。
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

        # 确认回调是最终人工关卡；拒绝不会对目标目录产生任何写操作。
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
            # 用户确认期间 Workspace 或目标目录都可能变化，因此必须重新扫描并比较。
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

            # 在第一次目标侧副作用之前冻结已验证字节，后续只写这一份确定内容。
            frozen = {
                change.path: second_files[change.path] for change in second_preview.changes
            }
            if _scan_workspace(workspace_root)[0] != workspace_digest:
                raise ApplyError("workspace changed while preparing the application")

            # 先落 applying，再在内存中逐文件累积 applied_paths；异常记录会写出已影响范围。
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
