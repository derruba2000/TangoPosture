"""Bridge connecting Qt screens to in-process services and background workers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage

from ..config import Config, export_toml
from ..core.entities import ReferenceSummary
from ..core.session_state import SessionState
from ..core.types import ArchiveKind


def numpy_to_qimage(image_bgr: Any) -> QImage:
    """Convert an OpenCV BGR numpy array to a QImage without external file writes."""
    import cv2

    height, width, channels = image_bgr.shape
    bytes_per_line = channels * width
    # OpenCV BGR -> RGB
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    # Note: store buffer reference so memory isn't collected prematurely
    qimg = QImage(rgb.data, width, height, bytes_per_line, QImage.Format.Format_RGB888)
    return qimg.copy()


class ServiceBridge(QObject):
    """Coordinates local storage, config, and background tasks."""

    status_message = Signal(str)
    session_state_changed = Signal(object)  # SessionState
    frame_emitted = Signal(QImage, str)     # video frame, frame note summary
    beat_emitted = Signal(float, int, bool) # bpm, beat_num, is_active
    practice_finished = Signal(int, str, object)  # code, message, Path | None

    def __init__(self, cfg: Config, parent: QObject | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self._active_session: SessionState = SessionState()
        self._running_worker = None

    @property
    def config(self) -> Config:
        return self.cfg

    def update_config(self, new_cfg: Config) -> None:
        self.cfg = new_cfg
        self.status_message.emit("Settings updated.")

    def list_references(self) -> list[ReferenceSummary]:
        """Discover existing .tpc and legacy JSON calibrations."""
        from ..calibration import load_calibration_file
        from ..storage import Storage

        data_dir = Path(self.cfg.data_dir)
        folder = Path("calibrations") if self.cfg.data_dir == "data" else data_dir / "calibrations"
        results: dict[str, ReferenceSummary] = {}

        if (data_dir / "tango.sqlite3").is_file():
            storage = Storage(data_dir)
            try:
                for row in storage.list("calibrations"):
                    results[row["id"]] = ReferenceSummary(
                        id=row["id"],
                        name=row.get("name", "Reference"),
                        path=folder / f"{row['id']}.tpc",
                        side=row.get("side", "left"),
                        facing=row.get("facing", "right"),
                        camera=str(row.get("camera", 0)),
                        created_at=row.get("created_at_utc", ""),
                        is_archive=True,
                    )
            except (OSError, ValueError, KeyError):
                pass
            finally:
                storage.close()

        if folder.is_dir():
            for path in sorted([*folder.glob("calibration_*.json"), *folder.glob("*.tpc")], reverse=True):
                try:
                    ref = load_calibration_file(str(path), folder)
                    if ref:
                        prof = getattr(ref, "profile", {})
                        results[ref.id] = ReferenceSummary(
                            id=ref.id,
                            name=getattr(ref, "name", path.stem),
                            path=path.resolve(),
                            side=prof.get("side", "left"),
                            facing=prof.get("facing", "right"),
                            camera=str(prof.get("camera", 0)),
                            is_archive=path.suffix.lower() == ".tpc",
                        )
                except (OSError, ValueError, KeyError):
                    continue

        return list(results.values())

    def list_practices(self) -> list[dict[str, Any]]:
        """List practice archives (.tp) and reports."""
        from ..archive import practice_directory, read_manifest

        folder = practice_directory(self.cfg.data_dir)
        items = []
        if folder.is_dir():
            for archive_path in sorted(folder.glob("*.tp"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
                try:
                    manifest = read_manifest(archive_path)
                    segments = manifest.get("segments", [])
                    items.append({
                        "name": archive_path.stem,
                        "path": archive_path.resolve(),
                        "kind": ArchiveKind.PRACTICE,
                        "created_at": manifest.get("created_at_utc", ""),
                        "segments_count": len(segments),
                        "segments": segments,
                    })
                except (OSError, ValueError, KeyError):
                    continue
        return items

    def list_reports(self) -> list[Path]:
        from ..review import report_directory

        folder = report_directory(self.cfg.data_dir)
        if folder.is_dir():
            return sorted(folder.glob("report_*.md"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        return []

    def export_settings_toml(self, destination: Path) -> None:
        destination.write_text(export_toml(self.cfg), encoding="utf-8")
        self.status_message.emit(f"Settings saved to {destination.name}")

    def import_settings_toml(self, source: Path) -> None:
        import tomllib

        self.cfg = Config.model_validate(tomllib.loads(source.read_text(encoding="utf-8")))
        self.status_message.emit(f"Loaded settings from {source.name}")
