"""Content-addressable artifact store backed by state/artifacts/."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from models import Artifact

ARTIFACTS_DIR = Path(__file__).parent / "state" / "artifacts"


class ArtifactStore:
    def __init__(self):
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    def put(self, blob: bytes, *, content_type: str, source: str, descriptor: str) -> str:
        sha = hashlib.sha256(blob).hexdigest()[:16]
        aid = f"art:{sha}"
        bin_path = ARTIFACTS_DIR / f"{sha}.bin"
        meta_path = ARTIFACTS_DIR / f"{sha}.json"
        if not bin_path.exists():
            bin_path.write_bytes(blob)
            meta = Artifact(
                id=aid,
                content_type=content_type,
                size_bytes=len(blob),
                source=source,
                descriptor=descriptor[:500],
            )
            meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        return aid

    def get_bytes(self, artifact_id: str) -> bytes:
        sha = artifact_id.removeprefix("art:")
        return (ARTIFACTS_DIR / f"{sha}.bin").read_bytes()

    def get_meta(self, artifact_id: str) -> Artifact:
        sha = artifact_id.removeprefix("art:")
        data = json.loads((ARTIFACTS_DIR / f"{sha}.json").read_text(encoding="utf-8"))
        return Artifact(**data)

    def exists(self, artifact_id: str) -> bool:
        sha = artifact_id.removeprefix("art:")
        return (ARTIFACTS_DIR / f"{sha}.bin").exists()
