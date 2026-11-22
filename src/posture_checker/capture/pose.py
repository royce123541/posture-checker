"""Pose estimation with MediaPipe Pose Landmarker (BlazePose), on the CPU.

This model is pretrained and frozen. The part of the system that learns is the
small posture classifier that runs on top of its landmarks.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from posture_checker import paths


@dataclass(frozen=True)
class PoseModelSpec:
    name: str
    url: str
    sha256: str

    @property
    def path(self) -> Path:
        return paths.models_dir() / f"{self.name}.task"


# Pinned to version 1 of Google's published model, with its checksum, so every
# install runs exactly the same weights.
POSE_LITE = PoseModelSpec(
    name="pose_landmarker_lite",
    url=(
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
    ),
    sha256="59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(spec: PoseModelSpec = POSE_LITE) -> Path:
    """Download the pose model if it is missing, and verify its checksum.

    This is the only network access in the whole project, and it happens at
    install time, never while monitoring.
    """
    path = spec.path
    if not path.exists():
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(spec.url, tmp)
        tmp.replace(path)
    actual = file_sha256(path)
    if actual != spec.sha256:
        path.unlink()
        raise RuntimeError(
            f"{spec.name}: checksum mismatch (got {actual}); the file was removed, run "
            "`posture-checker fetch-model` again"
        )
    return path


@dataclass(frozen=True)
class PoseResult:
    #: ``(33, 4)`` array of x, y, z, visibility; None when nobody was found.
    landmarks: np.ndarray | None
    latency_ms: float


class PoseBackend(Protocol):
    name: str

    def estimate(self, frame_bgr: np.ndarray) -> PoseResult: ...

    def close(self) -> None: ...


class PoseEstimator:
    """MediaPipe Pose Landmarker in single-image mode.

    Samples are seconds apart, so each frame is processed independently rather
    than in video-tracking mode.
    """

    def __init__(self, spec: PoseModelSpec = POSE_LITE) -> None:
        # Keep MediaPipe's native start-up chatter out of the console.
        os.environ.setdefault("GLOG_minloglevel", "2")
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self.name = spec.name
        self._mp = mp
        options = vision.PoseLandmarkerOptions(
            # CPU delegate on purpose: waking the discrete GPU would cost far
            # more battery than this model's few milliseconds of CPU time.
            base_options=BaseOptions(
                model_asset_path=str(ensure_model(spec)),
                delegate=BaseOptions.Delegate.CPU,
            ),
            running_mode=vision.RunningMode.IMAGE,
            num_poses=1,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)

    def estimate(self, frame_bgr: np.ndarray) -> PoseResult:
        start = time.perf_counter()
        rgb = np.ascontiguousarray(frame_bgr[:, :, ::-1])
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect(image)
        latency_ms = (time.perf_counter() - start) * 1000
        if not result.pose_landmarks:
            return PoseResult(None, latency_ms)
        points = result.pose_landmarks[0]
        landmarks = np.array(
            [[p.x, p.y, p.z, p.visibility if p.visibility is not None else 0.0] for p in points],
            dtype=np.float32,
        )
        return PoseResult(landmarks, latency_ms)

    def close(self) -> None:
        self._landmarker.close()
