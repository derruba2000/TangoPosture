import pytest

from tango_posture.calibration import calibrate, profile
from tango_posture.config import Config
from tango_posture.domain import FrameObservation, Landmark
from tango_posture.metrics import measure
from tango_posture.models import MANIFEST


@pytest.fixture
def cfg():
    return Config()


def frame(timestamp=0.0, forward=0.0, width=640, height=480, side="left", facing="right"):
    # Torso is 160px, head segment 60px; widths 10px. Input geometry in pixels.
    coords = {
        "nose": (320, 65),
        "left_ear": (320, 100),
        "right_ear": (330, 100),
        "left_shoulder": (320, 160),
        "right_shoulder": (330, 160),
        "left_hip": (320, 320),
        "right_hip": (330, 320),
        "left_ankle": (320, 450),
        "right_ankle": (330, 450),
    }
    x, y = coords[side + "_ear"]
    coords[side + "_ear"] = (x + forward * 160 * (1 if facing == "right" else -1), y)
    return FrameObservation(
        timestamp,
        width,
        height,
        [{k: Landmark(x / width, y / height, 0.99, 0.99) for k, (x, y) in coords.items()}],
    )


@pytest.fixture
def reference(cfg):
    samples = [measure(frame(i / 10), cfg) for i in range(121)]
    return calibrate(
        samples,
        0,
        cfg,
        profile(cfg, 640, 480, "video"),
        MANIFEST["sha256"],
        "Synthetic test reference",
    )
