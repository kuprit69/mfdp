from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import math
import os
from pathlib import Path
import random
import uuid

import numpy as np
from scipy import ndimage


@dataclass
class ModelConfig:
    weights_path: str | None = None
    patch_size: int = 32
    batch_size: int = 32
    # Scan-level detection threshold (env: MODEL_DETECTION_THRESHOLD). This is
    # the ONLY threshold that actually gates which candidates become findings
    # - see the note on `checkpoint_threshold` below for why it is set higher
    # than the checkpoint's own patch-level threshold.
    threshold: float = 0.85
    max_candidates: int = 384
    max_detections: int = 8


class NoduleModel:
    """Python-side model adapter for LungPrometheus.

    If a configured checkpoint exists, the adapter runs the real 3D CNN and
    returns model probabilities. Without weights it falls back to deterministic
    mock detections so the UI remains usable.
    """

    def __init__(self, config: ModelConfig | None = None) -> None:
        self.config = config or ModelConfig(
            weights_path=self.default_weights_path(),
            threshold=float(os.getenv("MODEL_DETECTION_THRESHOLD", "0.85")),
        )
        self.weights_loaded = False
        self._model = None
        self._device = None
        self.model_name = "mock"
        self.last_probability: float | None = None
        self.last_threshold = self.config.threshold
        # Informational only (surfaced in API/report metadata as
        # "checkpointThreshold") - the patch-level threshold the checkpoint's
        # own training run picked (e.g. 0.45, tuned for per-patch F1/F2 on a
        # single candidate). It is NEVER used to filter detections; see
        # `_analyze_with_real_model` below for the threshold that is.
        self.checkpoint_threshold: float | None = None

    def load_weights(self, weights_path: str) -> None:
        if not os.path.exists(weights_path):
            raise FileNotFoundError(f"Weights file not found: {weights_path}")
        self.config.weights_path = weights_path
        self.weights_loaded = False
        self._model = None

    def analyze(self, payload: dict) -> list[dict]:
        if self.config.weights_path and os.path.exists(self.config.weights_path):
            return self._analyze_with_real_model(payload)
        return self._mock_analyze(payload)

    def _analyze_with_real_model(self, payload: dict) -> list[dict]:
        self._ensure_model_loaded()
        volume, meta = self._payload_to_volume(payload)
        if volume.size == 0:
            self.last_probability = 0.0
            return []

        candidates = self._generate_candidates(volume)
        if not candidates:
            self.last_probability = 0.0
            return []

        predictions = self._score_candidates(volume, candidates)
        self.last_probability = max((item["probability"] for item in predictions), default=0.0)
        # Deliberately uses `self.config.threshold` (scan-level, from
        # MODEL_DETECTION_THRESHOLD), NOT `self.checkpoint_threshold`. A full
        # scan generates dozens to hundreds of candidate patches per volume
        # (see `_generate_candidates`), so a per-patch threshold tuned on a
        # single-candidate validation set (candidates_V2.csv-style, one
        # prediction per row) would let through far more false positives once
        # applied hundreds of times per scan. The stricter scan-level value
        # compensates for that multiple-testing effect.
        detections = [
            item for item in predictions
            if item["probability"] >= self.config.threshold
        ]

        return self._to_annotations(detections, meta)

    def _ensure_model_loaded(self) -> None:
        if self._model is not None:
            return

        import torch

        checkpoint = torch.load(self.config.weights_path, map_location="cpu")
        state_dict = checkpoint.get("model_state_dict", checkpoint) if isinstance(checkpoint, dict) else checkpoint
        checkpoint_name = str(checkpoint.get("model_name", "")) if isinstance(checkpoint, dict) else ""
        self.model_name = checkpoint_name or self._infer_model_name(state_dict)
        if isinstance(checkpoint, dict):
            # Read for display/metadata purposes only - does not overwrite
            # `self.config.threshold`, which stays the scan-level value from
            # MODEL_DETECTION_THRESHOLD regardless of what is in the checkpoint.
            self.checkpoint_threshold = float(checkpoint.get("threshold") or self.config.threshold)
            self.config.patch_size = int(checkpoint.get("patch_size") or self.config.patch_size)
        self.last_threshold = self.config.threshold

        model = build_improved_3dcnn() if self.model_name == "Improved3DCNN" else build_better_3dcnn()
        model.load_state_dict(state_dict)

        if torch.backends.mps.is_available():
            self._device = torch.device("mps")
        else:
            self._device = torch.device("cpu")

        model.to(self._device)
        model.eval()
        self._model = model
        self.weights_loaded = True

    def _payload_to_volume(self, payload: dict) -> tuple[np.ndarray, dict]:
        series = payload.get("series") or payload.get("slices") or []
        slices: list[np.ndarray] = []

        for item in series:
            encoded = item.get("pixelData") or {}
            data = encoded.get("data")
            if not data:
                continue

            rows = int(item.get("rows") or item.get("height") or 0)
            columns = int(item.get("columns") or item.get("width") or 0)
            raw = base64.b64decode(data.encode("ascii"))
            pixels = np.frombuffer(raw, dtype=self._dtype_from_name(encoded.get("dtype")))
            if rows <= 0 or columns <= 0 or pixels.size < rows * columns:
                continue

            pixels = pixels[: rows * columns].reshape(rows, columns).astype(np.float32)
            slope = float(item.get("rescaleSlope") or encoded.get("slope") or 1.0)
            intercept = float(item.get("rescaleIntercept") or encoded.get("intercept") or 0.0)
            slices.append(pixels * slope + intercept)

        if not slices:
            return np.empty((0, 0, 0), dtype=np.float32), {}

        first = series[0]
        volume = np.stack(slices).astype(np.float32)
        meta = {
            "rows": int(first.get("rows") or first.get("height") or volume.shape[1]),
            "columns": int(first.get("columns") or first.get("width") or volume.shape[2]),
            "pixelSpacing": first.get("pixelSpacing") or [0.7, 0.7],
            "sliceThickness": float(first.get("sliceThickness") or 1.0),
            "modelName": self.model_name,
            "threshold": self.config.threshold,
            "checkpointThreshold": self.checkpoint_threshold,
        }
        return volume, meta

    @staticmethod
    def _dtype_from_name(name: str | None):
        return {
            "Int8Array": np.int8,
            "Uint8Array": np.uint8,
            "Int16Array": np.int16,
            "Uint16Array": np.uint16,
            "Int32Array": np.int32,
            "Uint32Array": np.uint32,
            "Float32Array": np.float32,
            "Float64Array": np.float64,
        }.get(str(name or ""), np.int16)

    def _generate_candidates(self, volume: np.ndarray) -> list[dict]:
        z_count, rows, columns = volume.shape
        half = self.config.patch_size // 2
        z_step = max(1, min(4, z_count // 32 or 1))
        xy_step = 16 if min(rows, columns) >= 256 else 8
        lung_masks = self._build_lung_masks(volume)
        slice_lung_fraction = np.mean(lung_masks, axis=(1, 2)) if lung_masks.size else np.array([])
        candidates: list[dict] = []

        z_values = range(0, z_count, z_step)
        y_values = range(half, max(half + 1, rows - half), xy_step)
        x_values = range(half, max(half + 1, columns - half), xy_step)

        for z in z_values:
            if z >= len(slice_lung_fraction) or slice_lung_fraction[z] < 0.015:
                continue

            # Screen the whole (y, x) grid for this slice in one vectorized
            # pass instead of calling `_mask_fraction_near` (a Python
            # function + numpy mean) once per grid point - for a full-size CT
            # (hundreds of slices, 512x512) that inner loop used to run tens
            # of thousands of times per scan and dominated analysis time.
            # `_box_mean_grid` computes the exact same clamped-window mean,
            # just for every grid point at once via a summed-area table.
            fraction_grid = self._box_mean_grid(lung_masks[z], y_values, x_values, 12)
            candidate_rows, candidate_columns = np.nonzero(fraction_grid >= 0.12)
            if candidate_rows.size == 0:
                continue

            for row_idx, column_idx in zip(candidate_rows.tolist(), candidate_columns.tolist()):
                y = y_values[row_idx]
                x = x_values[column_idx]

                z0, z1 = max(0, z - 1), min(z_count, z + 2)
                y0, y1 = max(0, y - 24), min(rows, y + 24)
                x0, x1 = max(0, x - 24), min(columns, x + 24)
                context = volume[z0:z1, y0:y1, x0:x1]
                lung_context = lung_masks[z0:z1, y0:y1, x0:x1]
                if context.size == 0:
                    continue

                lung_fraction = float(np.mean(lung_context))
                soft_fraction = float(np.mean((context > -650) & (context < 250)))
                dense_fraction = float(np.mean((context > -300) & (context < 250)))
                if lung_fraction < 0.04 or dense_fraction < 0.004 or soft_fraction > 0.45:
                    continue

                candidates.append(
                    {
                        "center": (int(z), int(y), int(x)),
                        "score": dense_fraction + soft_fraction * 0.35 + lung_fraction * 0.2,
                    }
                )

        candidates.sort(key=lambda item: item["score"], reverse=True)
        return candidates[: self.config.max_candidates]

    @staticmethod
    def _box_mean_grid(mask: np.ndarray, y_values: range, x_values: range, radius: int) -> np.ndarray:
        """Vectorized equivalent of calling `_mask_fraction_near(mask, y, x,
        radius)` at every combination of `y_values` x `x_values` - returns a
        2D array of shape (len(y_values), len(x_values)) with the same
        clamped-window mean each individual call would return, computed via a
        summed-area table so the whole grid costs a handful of numpy ops
        instead of one Python call per point."""
        rows, columns = mask.shape
        integral = np.zeros((rows + 1, columns + 1), dtype=np.float64)
        integral[1:, 1:] = mask.astype(np.float64).cumsum(axis=0).cumsum(axis=1)

        y_arr = np.fromiter(y_values, dtype=np.int64, count=len(y_values))
        x_arr = np.fromiter(x_values, dtype=np.int64, count=len(x_values))
        y0 = np.clip(y_arr - radius, 0, rows)
        y1 = np.clip(y_arr + radius, 0, rows)
        x0 = np.clip(x_arr - radius, 0, columns)
        x1 = np.clip(x_arr + radius, 0, columns)

        y0g, y1g = y0[:, None], y1[:, None]
        x0g, x1g = x0[None, :], x1[None, :]

        total = integral[y1g, x1g] - integral[y0g, x1g] - integral[y1g, x0g] + integral[y0g, x0g]
        area = (y1g - y0g) * (x1g - x0g)
        return np.divide(total, area, out=np.zeros_like(total), where=area > 0)

    def _build_lung_masks(self, volume: np.ndarray) -> np.ndarray:
        masks = np.zeros(volume.shape, dtype=bool)

        for z, image in enumerate(volume):
            air = image < -450
            if not np.any(air):
                continue

            outside_air = self._edge_connected_mask(air)
            internal_air = air & ~outside_air
            air_fraction = float(np.mean(air))
            internal_fraction = float(np.mean(internal_air))

            # Cropped test volumes and some exports may contain only lung pixels,
            # so every air pixel touches the image edge. Treat those as lung-like.
            if internal_fraction < 0.015 and air_fraction > 0.35:
                internal_air = air
                internal_fraction = air_fraction

            if internal_fraction >= 0.015:
                masks[z] = internal_air

        return masks

    # 4-connectivity structuring element (matches the up/down/left/right-only
    # expansion the old BFS used - no diagonal moves).
    _FOUR_CONNECTIVITY = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])

    @classmethod
    def _edge_connected_mask(cls, mask: np.ndarray) -> np.ndarray:
        """Air pixels connected to the image border (4-connectivity) - i.e.
        "outside" air like the space around the body, as opposed to air
        pockets enclosed inside it (lungs).

        Previously a hand-rolled Python BFS (a deque + one function call per
        pixel) - on a full-size 512x512 slice that was slow enough to
        dominate whole-CT analysis time (tens of seconds per scan just for
        this step, run once per slice). `scipy.ndimage.label` does the same
        4-connected flood fill in optimized C and returns identical results
        (verified against the old BFS output pixel-for-pixel) in a fraction
        of the time.
        """
        if not np.any(mask):
            return np.zeros_like(mask, dtype=bool)

        labeled, feature_count = ndimage.label(mask, structure=cls._FOUR_CONNECTIVITY)
        if feature_count == 0:
            return np.zeros_like(mask, dtype=bool)

        border_labels = np.unique(
            np.concatenate([labeled[0, :], labeled[-1, :], labeled[:, 0], labeled[:, -1]])
        )
        border_labels = border_labels[border_labels != 0]
        if border_labels.size == 0:
            return np.zeros_like(mask, dtype=bool)
        return np.isin(labeled, border_labels)

    @staticmethod
    def _mask_fraction_near(mask: np.ndarray, y: int, x: int, radius: int) -> float:
        y0, y1 = max(0, y - radius), min(mask.shape[0], y + radius)
        x0, x1 = max(0, x - radius), min(mask.shape[1], x + radius)
        window = mask[y0:y1, x0:x1]
        return float(np.mean(window)) if window.size else 0.0

    def _fallback_candidates(self, volume: np.ndarray) -> list[dict]:
        z_count, rows, columns = volume.shape
        z_values = np.linspace(0, max(0, z_count - 1), num=min(8, max(1, z_count)), dtype=int)
        y_values = np.linspace(rows * 0.25, rows * 0.75, num=5, dtype=int)
        x_values = np.linspace(columns * 0.25, columns * 0.75, num=5, dtype=int)
        return [
            {"center": (int(z), int(y), int(x)), "score": 0.0}
            for z in z_values
            for y in y_values
            for x in x_values
        ][: self.config.max_candidates]

    def _score_candidates(self, volume: np.ndarray, candidates: list[dict]) -> list[dict]:
        import torch

        scored: list[dict] = []
        batch_patches: list[np.ndarray] = []
        batch_candidates: list[dict] = []

        for candidate in candidates:
            patch = self._extract_patch(volume, candidate["center"], self.config.patch_size)
            batch_patches.append(patch)
            batch_candidates.append(candidate)
            if len(batch_patches) >= self.config.batch_size:
                scored.extend(self._score_batch(batch_patches, batch_candidates, torch))
                batch_patches = []
                batch_candidates = []

        if batch_patches:
            scored.extend(self._score_batch(batch_patches, batch_candidates, torch))

        scored.sort(key=lambda item: item["probability"], reverse=True)
        return self._nms_3d(scored)

    def _score_batch(self, patches: list[np.ndarray], candidates: list[dict], torch_module) -> list[dict]:
        tensor = torch_module.from_numpy(np.stack(patches)[:, None, :, :, :]).float().to(self._device)
        with torch_module.no_grad():
            logits = self._model(tensor)
            probabilities = torch_module.softmax(logits, dim=1)[:, 1].detach().cpu().numpy()

        scored = []
        for candidate, probability in zip(candidates, probabilities, strict=True):
            scored.append(
                {
                    "center": candidate["center"],
                    "probability": float(probability),
                }
            )
        return scored

    @staticmethod
    def _extract_patch(volume: np.ndarray, center: tuple[int, int, int], size: int) -> np.ndarray:
        z, y, x = center
        half = size // 2
        patch = np.zeros((size, size, size), dtype=np.float32)

        src_z0, src_z1 = max(0, z - half), min(volume.shape[0], z + half)
        src_y0, src_y1 = max(0, y - half), min(volume.shape[1], y + half)
        src_x0, src_x1 = max(0, x - half), min(volume.shape[2], x + half)

        dst_z0 = src_z0 - (z - half)
        dst_y0 = src_y0 - (y - half)
        dst_x0 = src_x0 - (x - half)

        patch[
            dst_z0: dst_z0 + (src_z1 - src_z0),
            dst_y0: dst_y0 + (src_y1 - src_y0),
            dst_x0: dst_x0 + (src_x1 - src_x0),
        ] = volume[src_z0:src_z1, src_y0:src_y1, src_x0:src_x1]
        patch = np.clip(patch, -1000, 400)
        return (patch + 1000) / 1400

    def _nms_3d(self, scored: list[dict]) -> list[dict]:
        kept: list[dict] = []
        min_distance_voxels = self.config.patch_size * 0.75

        for item in scored:
            z, y, x = item["center"]
            too_close = False
            for kept_item in kept:
                kz, ky, kx = kept_item["center"]
                distance = math.sqrt((z - kz) ** 2 + (y - ky) ** 2 + (x - kx) ** 2)
                if distance < min_distance_voxels:
                    too_close = True
                    break
            if not too_close:
                kept.append(item)
            if len(kept) >= self.config.max_detections:
                break
        return kept

    def _to_annotations(self, detections: list[dict], meta: dict) -> list[dict]:
        row_spacing, col_spacing = self._pixel_spacing(meta)
        annotations = []

        for detection in detections:
            z, y, x = detection["center"]
            probability = detection["probability"]
            diameter_mm = max(4.0, min(24.0, 6.0 + probability * 16.0))
            width = max(6.0, diameter_mm / col_spacing)
            height = max(6.0, diameter_mm / row_spacing)
            rect = self._clamp_rect(
                x - width / 2,
                y - height / 2,
                width,
                height,
                int(meta["rows"]),
                int(meta["columns"]),
            )

            annotations.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": "Подозрительный объект",
                    "source": "auto",
                    "modelName": meta.get("modelName", "3D CNN"),
                    "threshold": round(float(meta.get("threshold") or 0), 2),
                    "probability": round(probability, 4),
                    "confidence": round(probability, 2),
                    "sliceIndex": int(z),
                    "x": rect["x"],
                    "y": rect["y"],
                    "width": rect["width"],
                    "height": rect["height"],
                    "diameterMm": round(diameter_mm, 1),
                    "segment": self._estimate_segment(rect, int(meta["rows"]), int(meta["columns"])),
                }
            )

        return annotations

    def _mock_analyze(self, payload: dict) -> list[dict]:
        series = payload.get("series") or payload.get("slices") or []
        if not series:
            self.last_probability = 0.0
            return []

        first = series[0]
        seed_text = "|".join(
            str(first.get(key, ""))
            for key in ("studyInstanceUid", "seriesInstanceUid", "patientId", "patientName")
        )
        seed_text += f"|{len(series)}"
        seed = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest()[:16], 16)
        rng = random.Random(seed)

        count = max(1, min(4, round(1 + rng.random() * 3)))
        annotations: list[dict] = []
        used_slices: set[int] = set()

        for _ in range(count):
            slice_index = self._pick_slice(len(series), rng, used_slices)
            image = series[slice_index]
            rows = int(image.get("rows") or 512)
            columns = int(image.get("columns") or 512)
            row_spacing, col_spacing = self._pixel_spacing(image)
            diameter_mm = 5 + rng.random() * 17
            width = max(8, diameter_mm / col_spacing)
            height = max(8, diameter_mm / row_spacing)
            x = columns * (0.22 + rng.random() * 0.56) - width / 2
            y = rows * (0.20 + rng.random() * 0.58) - height / 2
            rect = self._clamp_rect(x, y, width, height, rows, columns)
            segment = self._estimate_segment(rect, rows, columns)
            probability = 0.72 + rng.random() * 0.23

            annotations.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": "Подозрительный объект",
                    "source": "auto",
                    "modelName": "mock",
                    "threshold": self.config.threshold,
                    "probability": round(probability, 4),
                    "confidence": round(probability, 2),
                    "sliceIndex": slice_index,
                    "x": rect["x"],
                    "y": rect["y"],
                    "width": rect["width"],
                    "height": rect["height"],
                    "diameterMm": round(diameter_mm, 1),
                    "segment": segment,
                }
            )

        self.last_probability = max(item["probability"] for item in annotations)
        return sorted(annotations, key=lambda item: item["sliceIndex"])

    @staticmethod
    def _pick_slice(length: int, rng: random.Random, used: set[int]) -> int:
        if length <= 1:
            return 0
        for _ in range(12):
            index = min(length - 1, math.floor(length * (0.12 + rng.random() * 0.76)))
            if index not in used:
                used.add(index)
                return index
        return math.floor(rng.random() * length)

    @staticmethod
    def _pixel_spacing(image: dict) -> tuple[float, float]:
        spacing = image.get("pixelSpacing") or [0.7, 0.7]
        if not isinstance(spacing, list) or len(spacing) < 2:
            return 0.7, 0.7
        row = float(spacing[0] or 0.7)
        col = float(spacing[1] or 0.7)
        return row, col

    @staticmethod
    def default_weights_path() -> str | None:
        candidates = [
            os.getenv("MODEL_WEIGHTS_PATH"),
            Path(__file__).resolve().parent / "weights" / "improved_3dcnn_checkpoint.pth",
            Path("/Users/aidungmas/Desktop/MFDP/Models/improved_3dcnn_checkpoint.pth"),
            Path(__file__).resolve().parent / "weights" / "best_better3dcnn.pth",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        return None

    @staticmethod
    def _infer_model_name(state_dict: dict) -> str:
        first_weight = state_dict.get("conv.0.weight")
        fc_weight = state_dict.get("fc.1.weight")
        if first_weight is not None and tuple(first_weight.shape) == (8, 1, 3, 3, 3):
            if fc_weight is not None and tuple(fc_weight.shape) == (128, 2048):
                return "Improved3DCNN"
        return "Better3DCNN"

    @staticmethod
    def _clamp_rect(x: float, y: float, width: float, height: float, rows: int, columns: int) -> dict:
        width = min(width, columns - 1)
        height = min(height, rows - 1)
        return {
            "x": max(0, min(columns - width, x)),
            "y": max(0, min(rows - height, y)),
            "width": width,
            "height": height,
        }

    @staticmethod
    def _estimate_segment(rect: dict, rows: int, columns: int) -> dict:
        center_x = rect["x"] + rect["width"] / 2
        center_y = rect["y"] + rect["height"] / 2
        side = "правого легкого" if center_x < columns / 2 else "левого легкого"
        vertical = center_y / rows
        segment = "S3"
        lobe = "верхней доли"

        if vertical > 0.66:
            segment = "S10" if center_x < columns / 2 else "S9"
            lobe = "нижней доли"
        elif vertical > 0.42:
            segment = "S5" if center_x < columns / 2 else "S6"
            lobe = "средней доли" if center_x < columns / 2 else "нижней доли"
        elif center_x > columns / 2:
            segment = "S1+2"

        return {
            "short": f"{segment} {side}",
            "label": f"{segment} {side}, {lobe}",
        }


def build_improved_3dcnn():
    import torch.nn as nn

    class Improved3DCNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv = nn.Sequential(
                nn.Conv3d(1, 8, kernel_size=3, padding=1),
                nn.GroupNorm(4, 8),
                nn.LeakyReLU(0.1),
                nn.Conv3d(8, 8, kernel_size=3, padding=1),
                nn.GroupNorm(4, 8),
                nn.LeakyReLU(0.1),
                nn.MaxPool3d(2),
                nn.Conv3d(8, 16, kernel_size=3, padding=1),
                nn.GroupNorm(4, 16),
                nn.LeakyReLU(0.1),
                nn.Conv3d(16, 16, kernel_size=3, padding=1),
                nn.GroupNorm(4, 16),
                nn.LeakyReLU(0.1),
                nn.MaxPool3d(2),
                nn.Conv3d(16, 32, kernel_size=3, padding=1),
                nn.GroupNorm(8, 32),
                nn.LeakyReLU(0.1),
                nn.MaxPool3d(2),
            )
            self.fc = nn.Sequential(
                nn.Flatten(),
                nn.Linear(32 * 4 * 4 * 4, 128),
                nn.LeakyReLU(0.1),
                nn.Dropout(0.2),
                nn.Linear(128, 2),
            )

        def forward(self, x):
            x = self.conv(x)
            return self.fc(x)

    return Improved3DCNN()


def build_better_3dcnn():
    import torch.nn as nn

    class Better3DCNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.conv = nn.Sequential(
                nn.Conv3d(1, 16, kernel_size=3, padding=1),
                nn.BatchNorm3d(16),
                nn.ReLU(),
                nn.MaxPool3d(2),
                nn.Conv3d(16, 32, kernel_size=3, padding=1),
                nn.BatchNorm3d(32),
                nn.ReLU(),
                nn.MaxPool3d(2),
                nn.Conv3d(32, 64, kernel_size=3, padding=1),
                nn.BatchNorm3d(64),
                nn.ReLU(),
                nn.MaxPool3d(2),
            )
            self.fc = nn.Sequential(
                nn.Flatten(),
                nn.Linear(64 * 4 * 4 * 4, 256),
                nn.ReLU(),
                nn.Dropout(0.3),
                nn.Linear(256, 64),
                nn.ReLU(),
                nn.Dropout(0.3),
                nn.Linear(64, 2),
            )

        def forward(self, x):
            x = self.conv(x)
            return self.fc(x)

    return Better3DCNN()
