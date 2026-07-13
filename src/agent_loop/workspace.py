"""Run-local workspace creation and path confinement."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from .types import PathViolation, WorkspaceSpec


@dataclass(frozen=True)
class Workspace:
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

    @staticmethod
    def _relative_path(value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path in {Path(""), Path(".")}:
            raise PathViolation(f"path must be relative and confined: {value}")
        return path

    def resolve(self, value: str | Path, *, for_write: bool = False) -> Path:
        relative = self._relative_path(value)
        candidate = self.root / relative

        # resolve(strict=False) follows any existing symlink in the prefix.  The
        # containment check therefore rejects both ../ and symlink escapes.
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
        """Hash path names and content so approvals can bind to workspace state."""

        digest = hashlib.sha256()
        for name in self.list_files(limit=100_000):
            path = self.resolve(name)
            if path.is_symlink():
                raise PathViolation(f"cannot digest symlink: {name}")
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
