"""创建 Run 独享的 Workspace，并把所有文件访问限制在其中。"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from .types import PathViolation, WorkspaceSpec


@dataclass(frozen=True)
class Workspace:
    """Agent 的隔离工作目录及其只读路径集合。"""

    root: Path
    read_only: frozenset[Path]

    @classmethod
    def create(cls, spec: WorkspaceSpec, destination: str | Path) -> "Workspace":
        root = Path(destination).resolve()
        if root.exists():
            raise PathViolation(f"workspace already exists: {root}")
        root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(spec.seed, root, symlinks=True)
        protected = frozenset(cls._relative_path(value) for value in spec.read_only)
        return cls(root, protected)

    @classmethod
    def open(cls, spec: WorkspaceSpec, root: str | Path) -> "Workspace":
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise PathViolation(f"workspace does not exist: {resolved}")
        protected = frozenset(cls._relative_path(value) for value in spec.read_only)
        return cls(resolved, protected)

    @staticmethod
    def _relative_path(value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path in {Path(""), Path(".")}:
            raise PathViolation(f"path must be relative and confined: {value}")
        return path

    def resolve(self, value: str | Path, *, for_write: bool = False) -> Path:
        relative = self._relative_path(value)
        candidate = self.root / relative

        # resolve(strict=False) 会解析路径前缀中已经存在的符号链接；随后再做
        # 包含关系检查，能够同时拒绝 `../` 和借助符号链接逃出 Workspace。
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise PathViolation(f"path escapes workspace: {value}")

        if for_write and any(relative == item or item in relative.parents for item in self.read_only):
            raise PathViolation(f"path is read-only: {value}")
        return resolved

    def list_files(self, *, limit: int = 200) -> tuple[str, ...]:
        files: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() or path.is_symlink():
                files.append(path.relative_to(self.root).as_posix())
                if len(files) >= limit:
                    break
        return tuple(files)

    def read_text(self, value: str | Path, *, max_chars: int = 100_000) -> str:
        path = self.resolve(value)
        if not path.is_file():
            raise FileNotFoundError(f"file does not exist: {value}")
        text = path.read_text(encoding="utf-8")
        if len(text) > max_chars:
            raise PathViolation(f"file exceeds read limit: {value}")
        return text

    def write_text(self, value: str | Path, content: str, *, max_chars: int = 100_000) -> int:
        if not isinstance(content, str) or len(content) > max_chars:
            raise PathViolation("write content must be bounded text")
        path = self.resolve(value, for_write=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))

    def digest(self) -> str:
        """哈希文件名和内容，使审批与验证能够绑定到具体 Workspace 状态。"""

        digest = hashlib.sha256()
        for name in self.list_files(limit=100_000):
            # 必须检查尚未解析的目录项。若只检查解析后的路径，会漏掉目标仍在
            # Workspace 内部的符号链接，进而让快照身份产生歧义。
            if (self.root / name).is_symlink():
                raise PathViolation(f"cannot digest symlink: {name}")
            path = self.resolve(name)
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    def copy_snapshot(self, destination: str | Path) -> Path:
        target = Path(destination).resolve()
        if target.exists():
            raise PathViolation(f"snapshot already exists: {target}")
        shutil.copytree(self.root, target, symlinks=True)
        return target
