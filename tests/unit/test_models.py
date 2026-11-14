import hashlib
import io
from pathlib import Path
from urllib.error import URLError

import pytest

from tango_posture.config import Config
from tango_posture.models import (
    analysis_model_path,
    fetch,
    model_manifest,
    prepare_analysis_model,
    verify,
)
from tango_posture.pose.mediapipe_backend import MediaPipeEstimator
from tango_posture.references.errors import ImportCancelledError


@pytest.mark.parametrize("variant", ["lite", "full", "heavy"])
def test_analysis_model_downloads_selected_variant_and_reuses_verified_cache(tmp_path, monkeypatch, variant):
    monkeypatch.chdir(tmp_path)
    payload = f"verified {variant} model".encode()
    digest = hashlib.sha256(payload).hexdigest()
    manifest = model_manifest(variant)
    monkeypatch.setitem(manifest, "sha256", digest)
    calls, progress = [], []

    def download(url, **kwargs):
        calls.append(url)
        response = io.BytesIO(payload)
        response.headers = {"Content-Length": str(len(payload))}
        return response

    monkeypatch.setattr("urllib.request.urlopen", download)
    cfg = Config(model_path=str(tmp_path / "live" / "lite.task"),
                 data_dir=str(tmp_path / "data"), analysis_model=variant)
    path = prepare_analysis_model(
        cfg, progress_callback=lambda current, total, message: progress.append((current, message)),
    )
    assert path == analysis_model_path(cfg)
    assert path.read_bytes() == payload
    assert verify(path, variant) == digest
    assert calls == [manifest["source_url"]]
    assert progress[0][0] == 0 and progress[-1][0] == 100
    assert all(variant.capitalize() in message for _, message in progress)
    assert prepare_analysis_model(cfg) == path
    assert len(calls) == 1
    path.write_bytes(b"corrupt cache")
    with pytest.raises(ValueError, match="checksum"):
        prepare_analysis_model(cfg)
    assert len(calls) == 1
    if variant != "lite":
        assert not (tmp_path / "live" / "lite.task").exists()


@pytest.mark.parametrize("variant", ["lite", "full", "heavy"])
@pytest.mark.parametrize("location", ["alongside-live", "project-models", "cache"])
def test_analysis_reuses_installed_model_before_downloading(tmp_path, monkeypatch, variant, location):
    monkeypatch.chdir(tmp_path)
    payload = f"installed {variant} weights".encode()
    monkeypatch.setitem(model_manifest(variant), "sha256", hashlib.sha256(payload).hexdigest())
    cfg = Config(
        model_path=str(tmp_path / "bundled" / "pose_landmarker_lite.task"),
        data_dir=str(tmp_path / "data"), analysis_model=variant,
    )
    filename = model_manifest(variant)["filename"]
    installed = {
        "alongside-live": Path(cfg.model_path).parent / filename,
        "project-models": tmp_path / "models" / filename,
        "cache": tmp_path / "data" / "models" / filename,
    }[location]
    installed.parent.mkdir(parents=True)
    installed.write_bytes(payload)
    messages = []

    def forbidden(*args, **kwargs):
        raise AssertionError("An installed, verified model must not trigger a download")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    path = prepare_analysis_model(
        cfg, progress_callback=lambda current, total, message: messages.append(message),
    )
    assert path.resolve() == installed.resolve()
    assert installed.read_bytes() == payload
    assert "Using verified local" in messages[-1]
    if location != "cache":
        assert not (tmp_path / "data" / "models").exists()


def test_discovered_invalid_model_is_reported_without_download_or_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(model_path=str(tmp_path / "lite.task"), data_dir=str(tmp_path / "data"),
                 analysis_model="full")
    installed = tmp_path / model_manifest("full")["filename"]
    installed.write_bytes(b"unverified local weights")

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid local weights must not be silently overwritten")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    with pytest.raises(ValueError, match="checksum"):
        prepare_analysis_model(cfg)
    assert installed.read_bytes() == b"unverified local weights"


def test_analysis_weights_cannot_replace_live_lite_weights(tmp_path, monkeypatch):
    payload = b"Full model"
    monkeypatch.setitem(model_manifest("full"), "sha256", hashlib.sha256(payload).hexdigest())
    path = tmp_path / "full.task"
    path.write_bytes(payload)
    assert verify(path, "full") == hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError, match="Lite model checksum"):
        MediaPipeEstimator(path)


@pytest.mark.parametrize("failure", ["corrupt", "cancel", "network"])
def test_failed_model_download_preserves_existing_file_and_removes_partial_downloads(
    tmp_path, monkeypatch, failure,
):
    payload = b"verified heavy model"
    monkeypatch.setitem(model_manifest("heavy"), "sha256", hashlib.sha256(payload).hexdigest())
    destination = tmp_path / "heavy.task"
    destination.write_bytes(b"keep existing file")
    cancelled = False

    def download(*args, **kwargs):
        if failure == "network":
            raise URLError("Offline")
        response = io.BytesIO(b"corrupt" if failure == "corrupt" else payload)
        response.headers = {"Content-Length": str(len(payload))}
        return response

    def progress(current, total, message):
        nonlocal cancelled
        if failure == "cancel" and current > 0:
            cancelled = True

    monkeypatch.setattr("urllib.request.urlopen", download)
    error_type = {"corrupt": ValueError, "cancel": ImportCancelledError, "network": URLError}[failure]
    with pytest.raises(error_type):
        fetch(destination, "heavy", progress_callback=progress, cancel_check=lambda: cancelled)
    assert destination.read_bytes() == b"keep existing file"
    assert not list(tmp_path.glob("*.download"))


def test_cancelled_analysis_does_not_start_model_download(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Cancelled analysis must not open a network connection")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    cfg = Config(data_dir=str(tmp_path), analysis_model="heavy")
    with pytest.raises(ImportCancelledError):
        prepare_analysis_model(cfg, cancel_check=lambda: True)
    assert not (tmp_path / "models").exists()
