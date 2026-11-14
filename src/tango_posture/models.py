from __future__ import annotations

import hashlib
import json
import tempfile
import urllib.request
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .config import Config

ModelVariant = Literal["lite", "full", "heavy"]

MANIFEST = json.loads(files("tango_posture").joinpath("model_manifest.json").read_text())


def model_manifest(variant: ModelVariant = "lite") -> dict[str, Any]:
    if variant == "lite":
        return MANIFEST
    return MANIFEST["analysis_variants"][variant]


def analysis_model_path(cfg: Config) -> Path:
    filename = model_manifest(cfg.analysis_model)["filename"]
    destination = (
        Path(cfg.model_path) if cfg.analysis_model == "lite" else
        Path(cfg.data_dir) / "models" / filename
    )
    candidates = (
        destination,
        Path(cfg.data_dir) / "models" / filename,
        Path(cfg.model_path).parent / filename,
        Path("models") / filename,
    )
    return next((path for path in candidates if path.is_file()), destination)


def verify(path: Path, variant: ModelVariant = "lite") -> str:
    if not path.is_file():
        raise ValueError(
            "Model missing: run 'tango-posture models fetch' for Live Practice, "
            "or start analysis to download the selected analysis model"
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != model_manifest(variant)["sha256"]:
        raise ValueError(
            f"{variant.capitalize()} model checksum mismatch; obtain its exact pinned file "
            "identified in models/manifest.json"
        )
    return digest


def fetch(
    destination: Path, variant: ModelVariant = "lite", *,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    def check_cancel() -> None:
        if cancel_check is not None and cancel_check():
            from .references.errors import ImportCancelledError

            raise ImportCancelledError("Model download was cancelled")

    check_cancel()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".download", delete=False) as f:
        temporary = Path(f.name)
    try:
        label = f"Downloading {variant.capitalize()} analysis model"
        if progress_callback is not None:
            progress_callback(0, 100, f"{label}...")
        check_cancel()
        with urllib.request.urlopen(model_manifest(variant)["source_url"], timeout=60) as response:
            total = int(getattr(response, "headers", {}).get("Content-Length", 0))
            downloaded = 0
            with temporary.open("wb") as output:
                while True:
                    check_cancel()
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback is not None:
                        pct = min(99, round(downloaded / total * 100)) if total else 0
                        progress_callback(pct, 100, f"{label} ({downloaded / 1048576:.1f} MB)...")
        check_cancel()
        verify(temporary, variant)
        check_cancel()
        temporary.replace(destination)
        if progress_callback is not None:
            progress_callback(100, 100, f"{variant.capitalize()} analysis model verified and cached.")
    finally:
        temporary.unlink(missing_ok=True)


def prepare_analysis_model(
    cfg: Config, *,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    if cancel_check is not None and cancel_check():
        from .references.errors import ImportCancelledError

        raise ImportCancelledError("Model preparation was cancelled")
    path = analysis_model_path(cfg)
    if not path.is_file():
        fetch(path, cfg.analysis_model, progress_callback=progress_callback, cancel_check=cancel_check)
    else:
        verify(path, cfg.analysis_model)
        if progress_callback is not None:
            progress_callback(
                100, 100, f"Using verified local {cfg.analysis_model.capitalize()} model: {path}",
            )
    return path
