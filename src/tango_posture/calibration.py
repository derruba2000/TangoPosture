import itertools
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np

from .config import Config
from .domain import METRIC_VERSION, METRICS, Calibration, MetricObservation
from .geometry import wrapped


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def profile(cfg: Config, width: int, height: int, source: str) -> dict:
    return {
        "side": cfg.side,
        "facing": cfg.facing,
        "width": width,
        "height": height,
        "aspect_ratio": width / height,
        "capture_profile": cfg.capture_profile,
        "source": source,
    }


def compatible(ref: Calibration, current: dict, model_hash: str) -> None:
    if ref.model_hash != model_hash or ref.metric_version != METRIC_VERSION:
        raise ValueError("Reference model/metric version changed; recalibrate")
    if ref.profile != current:
        raise ValueError(
            "Reference capture profile, resolution, side or direction changed; recalibrate"
        )
    from .range_profiles import profile_from_calibration

    ranges = profile_from_calibration(ref)
    if ranges is not None and not any(target.camera_check_passed for target in ranges.metrics.values()):
        raise ValueError("Comparison-only profile: use Library > Check camera before enabling scoring")


def calibrate(
    samples: list[MetricObservation],
    start: float,
    cfg: Config,
    capture_profile: dict,
    model_hash: str,
    name: str,
    review: dict | None = None,
) -> Calibration:
    end = start + cfg.calibration_duration
    window = [s for s in samples if start <= s.timestamp <= end]
    valid = [s for s in window if all(s.quality[m].valid for m in METRICS)]
    duration = sum(
        b.timestamp - a.timestamp
        for a, b in itertools.pairwise(window)
        if 0 < b.timestamp - a.timestamp <= cfg.max_gap
        and all(a.quality[m].valid and b.quality[m].valid for m in METRICS)
    )
    if len(valid) < cfg.calibration_samples or duration + 1e-8 < cfg.calibration_valid_duration:
        reasons = [
            s.quality[m].reason
            for s in window
            for m in METRICS
            if not s.quality[m].valid and s.quality[m].reason
        ]
        reason_suffix = ""
        if reasons:
            top_reason = max(set(reasons), key=reasons.count)
            reason_suffix = f" ({top_reason})"
        raise ValueError(
            f"Calibration rejected: {duration:.2f}s valid, {len(valid)} samples; "
            f"need {cfg.calibration_valid_duration}s and {cfg.calibration_samples}{reason_suffix}"
        )
    baseline, mad = {}, {}
    for m in METRICS:
        values = np.array([s.values[m] for s in valid], dtype=float)
        anchor = float(values[0])
        if m != "head_forward":
            values = np.array([anchor + wrapped(float(x) - anchor) for x in values])
        med = float(np.median(values))
        mad[m] = float(np.median(np.abs(values - med)))
        baseline[m] = med if m == "head_forward" else wrapped(med)
        if mad[m] > (cfg.offset_mad if m == "head_forward" else cfg.angle_mad):
            raise ValueError(f"Calibration rejected: unstable {m} (MAD {mad[m]:.3f})")
    widths = [s.view_width for s in valid if s.view_width is not None]
    return Calibration(
        str(uuid4()),
        utc_now(),
        name,
        capture_profile,
        baseline,
        {
            "mad": mad,
            "valid_duration": duration,
            "valid_coverage": duration / cfg.calibration_duration,
            "samples": len(valid),
            "view_range": [min(widths), max(widths)],
        },
        model_hash,
        review=review or {"label": "self-selected"},
    )


def save_calibration_file(
    cal: Calibration, folder: Path = Path("calibrations")
) -> Path:
    """Save accepted calibration to `calibrations/calibration_<timestamp>.json` and `latest_calibration.json`."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    file_path = folder / f"calibration_{stamp}_{cal.id[:8]}.json"
    latest_path = folder / "latest_calibration.json"
    payload = json.dumps(asdict(cal), indent=2) + "\n"
    file_path.write_text(payload, encoding="utf-8")
    latest_path.write_text(payload, encoding="utf-8")
    return file_path


def load_calibration_file(
    identifier: str, folder: Path = Path("calibrations")
) -> Calibration | None:
    """Load a Calibration from a JSON file path, filename in `calibrations/`, 'latest', or UUID."""
    candidates: list[Path] = []
    if identifier.lower() == "latest":
        archives = sorted(folder.glob("*.tpc"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        if archives:
            candidates.append(archives[0])
        candidates.append(folder / "latest_calibration.json")
    raw_path = Path(identifier)
    candidates.extend([raw_path, folder / raw_path.name, folder / f"{identifier}.json"])
    for cand in candidates:
        if cand.is_file():
            if cand.suffix.lower() == ".tpc":
                from .archive import read_calibration_archive

                return read_calibration_archive(cand)
            data = json.loads(cand.read_text(encoding="utf-8"))
            return Calibration(**data)
    if folder.is_dir():
        for archive_file in sorted(folder.glob("*.tpc"), reverse=True):
            try:
                from .archive import read_calibration_archive

                reference = read_calibration_archive(archive_file)
                if reference.id == identifier or reference.id.startswith(identifier):
                    return reference
            except (OSError, ValueError, TypeError):
                continue
        for json_file in sorted(folder.glob("calibration_*.json"), reverse=True):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                if data.get("id") == identifier or data.get("id", "").startswith(identifier):
                    return Calibration(**data)
            except (OSError, ValueError, TypeError):
                continue
    return None
