from pathlib import Path
from typing import Any

from ..domain import FrameObservation, Landmark
from ..models import verify
from .constants import NAMES


class MediaPipeEstimator:
    def __init__(self, model_path: Path):
        self.model_hash = verify(model_path)
        import mediapipe as mp

        self.mp = mp
        self.last_ms = -1
        self.detector = mp.tasks.vision.PoseLandmarker.create_from_options(
            mp.tasks.vision.PoseLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(
                    model_asset_path=str(model_path), delegate=mp.tasks.BaseOptions.Delegate.CPU
                ),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_poses=2,
            )
        )

    def estimate(self, image: Any, timestamp: float) -> FrameObservation:
        import cv2

        ms = max(self.last_ms + 1, round(timestamp * 1000))
        self.last_ms = ms
        result = self.detector.detect_for_video(
            self.mp.Image(
                image_format=self.mp.ImageFormat.SRGB, data=cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            ),
            ms,
        )
        poses = []
        for landmarks in result.pose_landmarks:
            poses.append(
                {
                    name: Landmark(
                        landmarks[i].x,
                        landmarks[i].y,
                        landmarks[i].visibility,
                        landmarks[i].presence,
                    )
                    for name, i in NAMES.items()
                }
            )
        return FrameObservation(timestamp, image.shape[1], image.shape[0], poses)

    def close(self) -> None:
        self.detector.close()
