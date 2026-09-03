from __future__ import annotations

import gzip
import json
import os
from pathlib import Path


class SliceStore:
    """Persists uploaded CT slice payloads (pixel data + metadata) on disk.

    Without this, the pixel data parsed by the browser only ever existed in
    that browser tab: the async analysis worker had nothing to run the real
    model on, and reopening a study from history could never show the image
    again. Each study's slices are stored as one gzip-compressed JSON file,
    in exactly the shape the model adapter already expects.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        default_dir = Path(__file__).resolve().parents[1] / "data" / "studies"
        self.base_dir = Path(base_dir or os.getenv("STUDY_FILES_DIR") or default_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, study_id: str) -> Path:
        return self.base_dir / f"{study_id}.json.gz"

    def save(self, study_id: str, slices: list[dict]) -> None:
        payload = json.dumps({"slices": slices}, ensure_ascii=False).encode("utf-8")
        tmp_path = self._path(study_id).with_suffix(".json.gz.tmp")
        with gzip.open(tmp_path, "wb") as handle:
            handle.write(payload)
        tmp_path.replace(self._path(study_id))

    def load(self, study_id: str) -> list[dict] | None:
        path = self._path(study_id)
        if not path.exists():
            return None
        with gzip.open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
        return data.get("slices") or []

    def exists(self, study_id: str) -> bool:
        return self._path(study_id).exists()

    def delete(self, study_id: str) -> None:
        path = self._path(study_id)
        if path.exists():
            path.unlink()
