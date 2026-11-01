import hashlib
import json
import tempfile
import urllib.request
from importlib.resources import files
from pathlib import Path

MANIFEST = json.loads(files("tango_posture").joinpath("model_manifest.json").read_text())


def verify(path: Path) -> str:
    if not path.is_file():
        raise ValueError(
            "Model missing: run 'tango-posture models fetch' during setup, or supply --model-path"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != MANIFEST["sha256"]:
        raise ValueError(
            "Model checksum mismatch; obtain the exact model identified in models/manifest.json"
        )
    return digest


def fetch(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".download", delete=False) as f:
        temporary = Path(f.name)
    try:
        with urllib.request.urlopen(MANIFEST["source_url"], timeout=60) as response:
            temporary.write_bytes(response.read())
        verify(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
