from importlib.metadata import distributions
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH).parent
datas, binaries, hiddenimports = collect_all("mediapipe")
datas += copy_metadata("tango-posture")
datas += [
    (str(root / "src/tango_posture/model_manifest.json"), "tango_posture"),
    (str(root / "build/model/pose_landmarker_lite.task"), "tango_posture"),
    (str(root / "logos/TangoPosture_logo_under_1MB.png"), "tango_posture"),
    (str(root / "build/desktop-resources/TANGO_POSTURE_MANUAL.md"), "tango_posture"),
    (str(root / "LICENSE"), "notices"),
    (str(root / "NOTICE"), "notices"),
    (str(root / "THIRD_PARTY_NOTICES.md"), "notices"),
]
for distribution in distributions():
    for entry in distribution.files or ():
        parts = Path(str(entry)).parts
        if "licenses" in (part.lower() for part in parts) or Path(str(entry)).name.upper().startswith(
            ("LICENSE", "COPYING", "NOTICE", "AUTHORS")
        ):
            source = Path(distribution.locate_file(entry))
            if source.is_file() and ".." not in parts:
                target = Path("notices") / distribution.metadata["Name"] / Path(str(entry)).parent
                datas.append((str(source), str(target)))

analysis = Analysis(
    [str(root / "scripts/desktop_entry.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="TangoPosture",
    console=False,
)
collection = COLLECT(exe, analysis.binaries, analysis.datas, name="TangoPosture")
if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="TangoPosture.app",
        bundle_identifier="org.tangoposture.desktop",
        info_plist={
            "NSCameraUsageDescription": "TangoPosture uses the camera to record and analyse practice.",
            "NSMicrophoneUsageDescription": "TangoPosture uses the microphone for optional music tracking and recording.",
            "NSHighResolutionCapable": True,
        },
    )
