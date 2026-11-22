"""Webcam access.

Frames exist only as in-memory arrays. Nothing in this package writes an image
or video to disk or sends one anywhere; ``tests/test_privacy.py`` enforces it.
"""

from __future__ import annotations

from typing import Any, Protocol

import numpy as np


class FrameSource(Protocol):
    """Anything that can hand out the most recent camera frame."""

    def open(self) -> bool: ...

    def read(self) -> np.ndarray | None: ...

    def close(self) -> None: ...


class Camera:
    """The laptop webcam, read at a low rate.

    The camera stays open between samples (re-opening takes about a second and
    blinks the LED), so before each read we drop the frames that queued up in
    the driver's buffer and return a fresh one.
    """

    #: Frames to discard before a read so the returned frame is current.
    FLUSH_FRAMES = 2
    #: Frames to discard after opening while auto-exposure settles.
    WARMUP_FRAMES = 8

    def __init__(self, index: int = 0, width: int = 640, height: int = 480) -> None:
        self.index = index
        self.width = width
        self.height = height
        self._cap: Any = None  # cv2.VideoCapture, typed loosely: cv2 is imported lazily

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self) -> bool:
        """Open the camera. Returns False if it is missing or busy."""
        import cv2  # imported lazily: tests and tools that never touch a camera skip it

        if self.is_open:
            return True
        # DirectShow opens much faster than Media Foundation on Windows.
        cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            self._cap = None
            return False
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cap = cap
        for _ in range(self.WARMUP_FRAMES):
            cap.grab()
        return True

    def read(self) -> np.ndarray | None:
        """Return the newest frame as a BGR array, or None if the read failed."""
        if not self.is_open:
            return None
        for _ in range(self.FLUSH_FRAMES):
            self._cap.grab()
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        return frame

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self) -> Camera:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
