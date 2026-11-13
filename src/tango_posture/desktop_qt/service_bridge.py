"""Bridge connecting Qt screens to in-process services and background workers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QIODevice, QObject, QSaveFile, Signal
from PySide6.QtGui import QImage

from ..config import Config, RangeDefault, export_toml
from ..core.entities import ReferenceSummary
from ..core.session_state import SessionState
from ..core.types import ArchiveKind

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from ..calibration.models import RangeProfile


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
    config_changed = Signal(object)
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
        self.config_changed.emit(new_cfg)
        self.status_message.emit("Settings updated.")

    def use_calibration_defaults(self, profile: RangeProfile, parent: QWidget) -> bool:
        from PySide6.QtWidgets import QMessageBox

        destination = Path("settings.toml").resolve()
        names = ", ".join(profile.metrics)
        answer = QMessageBox.question(
            parent, "Overwrite Settings defaults?",
            f"Replace the default ranges with {profile.name} ({names}) and save all current "
            f"Settings to:\n{destination}\n\nAny existing settings.toml will be overwritten. "
            "Skipped metrics stay unavailable; camera scoring approval is not transferred.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        try:
            data = self.cfg.model_dump(exclude={"axis_curve"})
            data["calibration_defaults"] = {
                key: RangeDefault(
                    lower=target.lower, upper=target.upper, unit=target.unit, view=target.view,
                ).model_dump()
                for key, target in profile.metrics.items()
            }
            cfg = Config.model_validate(data)
            self.save_settings(cfg, destination)
        except (OSError, ValueError) as error:
            self.status_message.emit(f"Could not save calibration defaults: {error}")
            return False
        self.status_message.emit(f"Calibration defaults saved to {destination}")
        return True

    def save_settings(self, cfg: Config, destination: Path) -> None:
        payload = export_toml(cfg).encode("utf-8")
        output = QSaveFile(str(destination))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(output.errorString())
        if output.write(payload) != len(payload):
            error = output.errorString()
            output.cancelWriting()
            raise OSError(error)
        if not output.commit():
            raise OSError(output.errorString())
        self.update_config(cfg)

    def list_references(self) -> list[ReferenceSummary]:
        """Discover existing .tpc and legacy JSON calibrations."""
        from ..calibration.baseline import load_calibration_file
        from ..calibration.models import profile_from_calibration
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
                        ranges = profile_from_calibration(ref)
                        results[ref.id] = ReferenceSummary(
                            id=ref.id,
                            name=getattr(ref, "name", path.stem),
                            path=path.resolve(),
                            side=prof.get("side", "left"),
                            facing=prof.get("facing", "right"),
                            camera=str(prof.get("camera", 0)),
                            is_archive=path.suffix.lower() == ".tpc",
                            scoring_ready=ranges is None or any(
                                value.camera_check_passed for value in ranges.metrics.values()
                            ),
                        )
                except (OSError, ValueError, KeyError):
                    continue

        return list(results.values())

    def list_practices(self) -> list[dict[str, Any]]:
        """List practice archives (.tp) using their persisted display names."""
        from ..archive import practice_directory, read_manifest

        folder = practice_directory(self.cfg.data_dir)
        items = []
        if folder.is_dir():
            for archive_path in sorted(folder.glob("*.tp"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
                try:
                    manifest = read_manifest(archive_path)
                    segments = manifest.get("segments", [])
                    items.append({
                        "name": manifest.get("name", archive_path.stem),
                        "path": archive_path.resolve(),
                        "kind": ArchiveKind.PRACTICE,
                        "created_at": manifest.get("created_at_utc", ""),
                        "segments_count": len(segments),
                        "segments": segments,
                    })
                except (OSError, ValueError, KeyError) as error:
                    self.status_message.emit(f"Library archive unavailable: {archive_path.name}: {error}")
        return items

    def list_library_archives(self) -> list[dict[str, Any]]:
        from ..archive import calibration_directory, read_manifest
        from ..references.archives import reference_directory

        items = self.list_practices()
        for folder, suffix, kind in (
            (calibration_directory(self.cfg.data_dir), "*.tpc", ArchiveKind.CALIBRATION),
            (reference_directory(self.cfg.data_dir), "*.tpr", ArchiveKind.REFERENCE),
        ):
            for path in sorted(folder.glob(suffix)):
                try:
                    manifest = read_manifest(path)
                    items.append({
                        "name": manifest.get("name", path.stem),
                        "path": path.resolve(),
                        "kind": kind,
                        "version": manifest["version"],
                        "created_at": manifest.get("created_at_utc", ""),
                        "segments_count": len(manifest["segments"]),
                    })
                except (OSError, ValueError) as error:
                    self.status_message.emit(f"Library archive unavailable: {path.name}: {error}")
        return items

    def choose_library_archive(self, parent: Any, title: str) -> Path | None:
        from PySide6.QtWidgets import QInputDialog

        items = [item for item in self.list_library_archives() if item["segments_count"]]
        if not items:
            self.status_message.emit("Library is empty. Import a video through Library first.")
            return None
        labels = [f"{item['name']} — {item['path'].name}" for item in items]
        selected, accepted = QInputDialog.getItem(parent, title, "Library archive:", labels, 0, False)
        return items[labels.index(selected)]["path"] if accepted else None

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

        self.update_config(Config.model_validate(tomllib.loads(source.read_text(encoding="utf-8"))))
        self.status_message.emit(f"Loaded settings from {source.name}")
