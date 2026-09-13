"""Body keypoints with RTMPose (SimCC head), CPU / ONNX Runtime.

Used ONLY for concealment: it runs on a person who has just touched a shelf,
never on everyone in view, so its cost stays small. No face, no identity —
17 COCO body points, of which Guard uses shoulders, elbows, wrists and hips.

RTMPose ONNX (mmpose "onnx_sdk" exports): input (1, 3, 256, 192) RGB,
normalised with ImageNet mean/std; outputs simcc_x (1, 17, 384) and
simcc_y (1, 17, 512) — 2× sub-pixel bins; argmax gives the coordinate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np

from ..zones.geometry import BBox
from .backends.onnx import ONNXCPUBackend

MEAN = np.array([123.675, 116.28, 103.53], np.float32)
STD = np.array([58.395, 57.12, 57.375], np.float32)

# COCO-17 indices
L_SHOULDER, R_SHOULDER, L_ELBOW, R_ELBOW, L_WRIST, R_WRIST, L_HIP, R_HIP = 5, 6, 7, 8, 9, 10, 11, 12


@dataclass
class Pose:
    points: np.ndarray  # (17, 2) normalised to the FRAME
    scores: np.ndarray  # (17,)

    def pt(self, i: int, min_score: float = 0.3) -> tuple[float, float] | None:
        return (float(self.points[i, 0]), float(self.points[i, 1])) if self.scores[i] >= min_score else None


class PoseEstimator:
    def __init__(self, backend: ONNXCPUBackend, split_ratio: float = 2.0, padding: float = 1.25) -> None:
        self.backend = backend
        self.split_ratio = split_ratio
        self.padding = padding
        self.last_ms = 0.0

    def estimate(self, frame_bgr: np.ndarray, box: BBox) -> Pose:
        t0 = time.perf_counter()
        ih, iw = self.backend.input_shape  # 256, 192
        fh, fw = frame_bgr.shape[:2]
        cx, cy = (box.x1 + box.x2) / 2 * fw, (box.y1 + box.y2) / 2 * fh
        bw, bh = box.w * fw * self.padding, box.h * fh * self.padding
        # Keep the model's aspect ratio (w:h = 192:256) so the body isn't squashed.
        aspect = iw / ih
        if bw > bh * aspect:
            bh = bw / aspect
        else:
            bw = bh * aspect
        scale = iw / bw
        m = np.array([[scale, 0, iw / 2 - cx * scale], [0, scale, ih / 2 - cy * scale]], np.float32)
        crop = cv2.warpAffine(frame_bgr, m, (iw, ih), flags=cv2.INTER_LINEAR)
        rgb = (crop[:, :, ::-1].astype(np.float32) - MEAN) / STD
        tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None])
        outs = self.backend.infer(tensor)
        sx, sy = sorted(outs, key=lambda a: a.shape[-1])  # x bins (384) < y bins (512)
        x = sx[0].argmax(-1) / self.split_ratio
        y = sy[0].argmax(-1) / self.split_ratio
        score = np.minimum(sx[0].max(-1), sy[0].max(-1))
        # back to frame pixels, then normalise
        px = (x - (iw / 2 - cx * scale)) / scale / fw
        py = (y - (ih / 2 - cy * scale)) / scale / fh
        self.last_ms = (time.perf_counter() - t0) * 1000
        return Pose(np.stack([px, py], 1).astype(np.float64), score.astype(np.float64))
