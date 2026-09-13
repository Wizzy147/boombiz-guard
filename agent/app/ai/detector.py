"""Person detection with YOLOX (Apache-2.0) — Phase 2 §7.

Only COCO class 0 (person) is kept; the other 79 classes are thrown away
before NMS so no CPU is spent sorting chairs and bottles.

YOLOX 0.1.1 ONNX exports take a BGR, 0–255, letterboxed (pad value 114)
NCHW float tensor, and return raw grid outputs (1, N, 85) that still need
decoding with strides 8/16/32. Both steps mirror the official demo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np

from ..zones.geometry import BBox
from .backends.base import InferenceBackend

PERSON = 0


@dataclass
class Detection:
    bbox: BBox  # normalised to the ORIGINAL frame
    confidence: float

    def to_dict(self) -> dict:
        return {"class": "person", "confidence": round(self.confidence, 3),
                "bbox": {"x1": self.bbox.x1, "y1": self.bbox.y1, "x2": self.bbox.x2, "y2": self.bbox.y2}}


def _grids(h: int, w: int, strides=(8, 16, 32)) -> tuple[np.ndarray, np.ndarray]:
    grids, expanded = [], []
    for s in strides:
        hs, ws = h // s, w // s
        xv, yv = np.meshgrid(np.arange(ws), np.arange(hs))
        g = np.stack((xv, yv), 2).reshape(1, -1, 2)
        grids.append(g)
        expanded.append(np.full((*g.shape[:2], 1), s))
    return np.concatenate(grids, 1).astype(np.float32), np.concatenate(expanded, 1).astype(np.float32)


class PersonDetector:
    def __init__(self, backend: InferenceBackend, *, threshold: float = 0.5, nms: float = 0.45,
                 min_box_frac: float = 0.02) -> None:
        self.backend = backend
        self.threshold = threshold
        self.nms = nms
        self.min_box_frac = min_box_frac  # drop boxes shorter than 2 % of the frame
        h, w = backend.input_shape
        self._grid, self._stride = _grids(h, w)
        self.last_ms = 0.0

    def _preprocess(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        ih, iw = self.backend.input_shape
        r = min(ih / frame.shape[0], iw / frame.shape[1])
        resized = cv2.resize(frame, (int(frame.shape[1] * r), int(frame.shape[0] * r)), interpolation=cv2.INTER_LINEAR)
        padded = np.full((ih, iw, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        tensor = padded.transpose(2, 0, 1)[None].astype(np.float32)
        return np.ascontiguousarray(tensor), r

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        t0 = time.perf_counter()
        tensor, ratio = self._preprocess(frame_bgr)
        out = self.backend.infer(tensor)[0]  # (1, N, 85)
        out = out.copy()
        out[..., :2] = (out[..., :2] + self._grid) * self._stride
        out[..., 2:4] = np.exp(out[..., 2:4]) * self._stride
        pred = out[0]
        scores = pred[:, 4] * pred[:, 5 + PERSON]
        keep = scores >= self.threshold
        pred, scores = pred[keep], scores[keep]
        dets: list[Detection] = []
        if len(pred):
            cx, cy, w, h = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
            boxes = np.stack([cx - w / 2, cy - h / 2, w, h], 1) / ratio
            idx = cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), self.threshold, self.nms)
            fh, fw = frame_bgr.shape[:2]
            for i in np.array(idx).reshape(-1):
                # Plain Python floats: NumPy scalars leak into tracks, events
                # and API responses, and FastAPI cannot serialise them.
                x, y, bw, bh = (float(v) for v in boxes[i])
                b = BBox(max(0.0, x / fw), max(0.0, y / fh), min(1.0, (x + bw) / fw), min(1.0, (y + bh) / fh))
                if b.h >= self.min_box_frac:
                    dets.append(Detection(b, float(scores[i])))
        self.last_ms = (time.perf_counter() - t0) * 1000
        return dets
