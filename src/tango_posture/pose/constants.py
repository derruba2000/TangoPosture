"""MediaPipe Pose landmark mapping: official Pose Landmarker model card."""

NAMES = {
    "nose": 0,
    "left_ear": 7,
    "right_ear": 8,
    "left_shoulder": 11,
    "right_shoulder": 12,
    "left_elbow": 13,
    "right_elbow": 14,
    "left_wrist": 15,
    "right_wrist": 16,
    "left_hip": 23,
    "right_hip": 24,
    "left_knee": 25,
    "right_knee": 26,
    "left_ankle": 27,
    "right_ankle": 28,
    "left_heel": 29,
    "right_heel": 30,
    "left_foot_index": 31,
    "right_foot_index": 32,
}
CONNECTIONS = [
    (f"{side}_{a}", f"{side}_{b}")
    for side in ("left", "right")
    for a, b in (
        ("ear", "shoulder"),
        ("shoulder", "hip"),
        ("shoulder", "elbow"),
        ("elbow", "wrist"),
        ("hip", "knee"),
        ("knee", "ankle"),
    )
]
