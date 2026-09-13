"""Inference backend contract (Phase 2 §5).

The detector never talks to ONNX Runtime directly — it talks to one of
these. CPU first; Coral / Hailo / TensorRT / OpenVINO slot in later as new
subclasses without touching the detector, tracker or rules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class InferenceBackend(ABC):
    @abstractmethod
    def load_model(self, model_path: str) -> None: ...

    @abstractmethod
    def infer(self, tensor: np.ndarray) -> list[np.ndarray]:
        """Run one preprocessed input tensor; return the raw model outputs."""

    @abstractmethod
    def get_device_name(self) -> str: ...

    @property
    @abstractmethod
    def input_shape(self) -> tuple[int, int]:
        """(height, width) the model expects."""
