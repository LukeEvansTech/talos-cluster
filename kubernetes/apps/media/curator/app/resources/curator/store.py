"""Object stores for the ledger.

``Ledger`` needs only four operations, so the backend is an interface rather
than a dependency: a directory on a persistent volume in the cluster, or an
S3 bucket when the engine runs somewhere without one.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


class FileStore:
    """A ledger backend over a directory, for a mounted persistent volume.

    Keys are slash-separated and map onto paths beneath ``root``. They come from
    this codebase rather than from user input, but a key that escaped the root
    would write wherever it liked, so each one is resolved and checked.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """Resolve a key to a path, refusing anything outside the root."""
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError(f"key escapes the ledger root: {key!r}")
        return candidate

    def get(self, key: str) -> bytes | None:
        """Object body, or None when it does not exist."""
        path = self._path(key)
        try:
            return path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return None

    def put(self, key: str, body: bytes) -> None:
        """Write an object.

        Written to a temporary name and renamed, because a run killed midway
        through writing an intent record must not leave a half-written one: the
        next run parses that file to decide whether a deletion is outstanding.
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(body)
        os.replace(tmp, path)

    def delete(self, key: str) -> None:
        """Remove an object, tolerating one that is already gone."""
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def list_keys(self, prefix: str, limit: int = 1000) -> list[str]:
        """Keys under a prefix, in the same shape the S3 backend returns."""
        del limit
        keys = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                keys.append(key)
        return keys

    def usage_bytes(self) -> int:
        """Total size on disk, so a run can report the volume filling up."""
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def free_bytes(self) -> int:
        """Space left on the volume."""
        return shutil.disk_usage(self.root).free
