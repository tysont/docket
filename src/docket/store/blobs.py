"""Content-addressed blob store (TD §4.1): ``blobs/<sha256[0:2]>/<sha256>``."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, hexd: str) -> Path:
        return self.root / hexd[:2] / hexd

    def put(self, data: bytes) -> str:
        hexd = hashlib.sha256(data).hexdigest()
        p = self._path(hexd)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + f".tmp{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        return f"sha256:{hexd}"

    def get(self, ref: str) -> bytes:
        hexd = ref.split(":", 1)[1] if ":" in ref else ref
        return self._path(hexd).read_bytes()

    def exists(self, ref: str) -> bool:
        hexd = ref.split(":", 1)[1] if ":" in ref else ref
        return self._path(hexd).is_file()
