"""Model storage, integrity and versioning (Phase 2 §53–54, §62).

Models live under <data_dir>/models/<kind>/<file>.onnx (ProgramData in
production). A model is loaded ONLY if its SHA-256 matches the manifest
below: a swapped or corrupted file is refused, logged, and the feature that
needed it reports itself unavailable — it never runs an unknown model.

Every event records the model versions that produced it (§53).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    kind: str       # person | fire | interaction
    version: str    # recorded on every event
    filename: str
    sha256: str
    licence: str


# The official YOLOX 0.1.1rc0 release assets (Apache-2.0, Megvii).
MANIFEST: dict[str, ModelSpec] = {
    "person-nano": ModelSpec(
        "person", "guard-person-yolox-nano-0.1.1", "yolox_nano.onnx",
        "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d", "Apache-2.0 (YOLOX, Megvii)",
    ),
    "person-tiny": ModelSpec(
        "person", "guard-person-yolox-tiny-0.1.1", "yolox_tiny.onnx",
        "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7", "Apache-2.0 (YOLOX, Megvii)",
    ),
    # ⚠️ LICENCE REVIEW REQUIRED BEFORE COMMERCIAL LAUNCH (user decision
    # 2026-09-13: pilots only). RTMPose code is Apache-2.0, but these "body7"
    # weights were trained partly on research-only datasets (MPII, AI
    # Challenger). Official OpenMMLab onnx_sdk release, rtmpose-t 256×192.
    "pose-rtm-t": ModelSpec(
        "pose", "guard-pose-rtmpose-t-body7-20230504", "rtmpose-t-body7.onnx",
        "a6c2f6a3896a4d51131d14d7a80a3d08b50f559af5a58a45d5b098aef510a70f",
        "Apache-2.0 code; body7 weights — LICENCE REVIEW REQUIRED before commercial use",
    ),
}

# Heuristic modules have versions too, so a false alarm can be traced to the
# exact rules that raised it.
HEURISTIC_VERSIONS = {
    "shelf": "guard-interaction-heuristic-0.1.0",
    "fire": "guard-fire-heuristic-0.1.0",
    "tracker": "guard-bytetrack-lite-0.1.0",
}


class ModelIntegrityError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ModelManager:
    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir.resolve()

    def path_for(self, key: str) -> Path:
        spec = MANIFEST.get(key)
        if spec is None:
            raise ModelIntegrityError(f"Unknown model '{key}'.")
        # File-path validation (§62): the manifest names the file; nothing
        # from an API request ever becomes part of this path.
        path = (self.models_dir / spec.kind / spec.filename).resolve()
        if self.models_dir not in path.parents:
            raise ModelIntegrityError("Model path escapes the models folder.")
        return path

    def verified_path(self, key: str) -> tuple[Path, ModelSpec]:
        spec = MANIFEST[key]
        path = self.path_for(key)
        if not path.exists():
            raise ModelIntegrityError(f"Model file missing: {spec.kind}/{spec.filename}")
        digest = sha256_file(path)
        if digest != spec.sha256:
            log.error("model_integrity_failed kind=%s file=%s", spec.kind, spec.filename)
            raise ModelIntegrityError(f"Model {spec.filename} failed its integrity check and was not loaded.")
        log.info("model_loaded kind=%s version=%s", spec.kind, spec.version)
        return path, spec

    def versions(self, person_key: str) -> dict[str, str]:
        return {"person_model": MANIFEST[person_key].version, **{f"{k}_model": v for k, v in HEURISTIC_VERSIONS.items()}}
