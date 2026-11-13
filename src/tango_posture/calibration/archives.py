"""Atomic video-independent range profile archives."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from .models import RangeProfile


def save_range_profile(path: Path, profile: RangeProfile) -> None:
    from ..archive import _archive_lock, _replace_archive

    if path.suffix.lower() != ".tpc":
        raise ValueError("Calibration profiles must use the .tpc extension")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _archive_lock(path):
        if path.exists():
            raise FileExistsError("Choose a new profile name; existing profiles are not overwritten")
        with NamedTemporaryFile(dir=path.parent, suffix=".tpc.tmp", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("profile.json", profile.model_dump_json(indent=2))
                archive.writestr("manifest.json", json.dumps({
                    "version": 2, "kind": "calibration", "name": profile.name,
                    "profile": "profile.json", "segments": [],
                    "created_at_utc": profile.created_at_utc,
                }))
            _replace_archive(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_range_profile(path: Path) -> RangeProfile:
    if path.suffix.lower() != ".tpc":
        raise ValueError("Calibration profiles must use the .tpc extension")
    try:
        return _read_range_profile(path)
    except (BadZipFile, KeyError) as error:
        raise ValueError("Invalid range calibration archive") from error


def _read_range_profile(path: Path) -> RangeProfile:
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("version") != 2 or manifest.get("kind") != "calibration"
            or manifest.get("profile") != "profile.json"
            or manifest.get("segments") != []
        ):
            raise ValueError("Not a version-2 range calibration profile")
        result = RangeProfile.model_validate_json(archive.read("profile.json"))
        name = manifest.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Calibration profile name is missing")
        return result.model_copy(update={"name": name})
