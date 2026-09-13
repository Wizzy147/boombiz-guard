"""ONNX Runtime on the CPU — the only backend Guard Basic requires.

Threads are capped deliberately: the POS on the same PC must stay responsive
(PRD §15). One session is shared by every camera (Phase 2 §39); ORT sessions
are thread-safe for concurrent run() calls, but Guard serialises them through
the scheduler anyway so two cameras never double the CPU burst.
"""

from __future__ import annotations

import os
import threading

import numpy as np
import onnxruntime as ort

from .base import InferenceBackend


class ONNXCPUBackend(InferenceBackend):
    def __init__(self, threads: int | None = None) -> None:
        cpu = os.cpu_count() or 4
        # Half the logical cores, at most 4: leaves the rest to the POS.
        self.threads = threads or max(1, min(4, cpu // 2))
        self._session: ort.InferenceSession | None = None
        self._input_name = ""
        self._shape = (416, 416)
        self._lock = threading.Lock()

    def load_model(self, model_path: str) -> None:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        self._session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        h, w = inp.shape[2], inp.shape[3]
        self._shape = (int(h), int(w)) if isinstance(h, int) and isinstance(w, int) else (416, 416)

    def infer(self, tensor: np.ndarray) -> list[np.ndarray]:
        if self._session is None:
            raise RuntimeError("model not loaded")
        with self._lock:
            return self._session.run(None, {self._input_name: tensor})

    def get_device_name(self) -> str:
        return f"CPU (ONNX Runtime {ort.__version__}, {self.threads} threads)"

    @property
    def input_shape(self) -> tuple[int, int]:
        return self._shape
