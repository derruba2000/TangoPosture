"""Settings management screen with grouped configuration and TOML import/export."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...calibration.models import METRIC_DETAILS
from ...config import SETTING_GROUPS, Config, RangeDefault, set_setting, setting_value
from ..router import Router
from ..service_bridge import ServiceBridge

_FLOAT_RANGES = {
    "confidence": (0.0, 1.0),
    "tango_bpm": (20.0, 240.0),
}


class SettingsScreen(QScrollArea):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.field_widgets: dict[str, QWidget] = {}
        self.range_widgets: dict[str, tuple[QCheckBox, QDoubleSpinBox, QDoubleSpinBox, QComboBox]] = {}
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Application Settings")
        title.setProperty("class", "title")
        subtitle = QLabel(
            "Configure all practice, review, and Ollama options. Export TOML to save a settings file."
        )
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        self.export_btn = QPushButton("Export TOML…")
        self.export_btn.clicked.connect(self._export_toml)
        self.import_btn = QPushButton("Import TOML…")
        self.import_btn.clicked.connect(self._import_toml)
        self.reset_btn = QPushButton("Reset Defaults")
        self.reset_btn.clicked.connect(self._reset_defaults)
        header.addWidget(self.export_btn)
        header.addWidget(self.import_btn)
        header.addWidget(self.reset_btn)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        cfg = self.bridge.config
        for group, fields in SETTING_GROUPS.items():
            page = QWidget()
            form = QFormLayout(page)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
            if group in {"Head & shoulders", "Embrace & knees", "Torso & hip offset"}:
                hint = QLabel(
                    "Used only when no calibration is selected. With a calibration, coaching "
                    "uses its approved ranges; missing metrics do not fall back to these settings."
                )
                hint.setWordWrap(True)
                form.addRow(hint)
            if group == "Files":
                model_hint = QLabel(
                    "Live Practice always uses Lite. Offline analysis uses the model selected "
                    "below. Missing analysis models are downloaded once with progress, then "
                    "cached locally; Full and Heavy take more processing time."
                )
                model_hint.setWordWrap(True)
                form.addRow(model_hint)
            for field in fields:
                editor, row_widget = self._create_editor(field, setting_value(cfg, field))
                self.field_widgets[field] = editor
                form.addRow(self._field_label(field), row_widget)
            self.tabs.addTab(page, group)
        ranges_page = QWidget()
        ranges_form = QFormLayout(ranges_page)
        ranges_hint = QLabel(
            "Exact ranges copied from calibration, used when no calibration is selected. "
            "Enabled ranges replace the legacy posture thresholds; disabled metrics stay unavailable. "
            "They do not grant camera-scoring approval."
        )
        ranges_hint.setWordWrap(True)
        ranges_form.addRow(ranges_hint)
        for key, (_group, label, unit) in METRIC_DETAILS.items():
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            enabled = QCheckBox("Use")
            lower, upper = QDoubleSpinBox(), QDoubleSpinBox()
            view = QComboBox()
            view.addItems(["side", "front", "all"])
            for spin, description in ((lower, "minimum"), (upper, "maximum")):
                spin.setDecimals(12)
                spin.setRange(-1e9, 1e9)
                spin.setAccessibleName(f"{label} {description}")
                enabled.toggled.connect(spin.setEnabled)
            enabled.toggled.connect(view.setEnabled)
            for widget in (enabled, lower, upper, view):
                row_layout.addWidget(widget)
            ranges_form.addRow(f"{label} ({unit})", row)
            self.range_widgets[key] = (enabled, lower, upper, view)
        self.tabs.addTab(ranges_page, "Calibration defaults")
        layout.addWidget(self.tabs, 1)

        self.save_btn = QPushButton("Apply Settings")
        self.save_btn.setProperty("class", "primary")
        self.save_btn.setFixedHeight(38)
        self.save_btn.clicked.connect(self._save_settings)
        layout.addWidget(self.save_btn)
        self.save_toml_btn = QPushButton("Apply && Save settings.toml")
        self.save_toml_btn.clicked.connect(self._save_toml)
        layout.addWidget(self.save_toml_btn)

        self._set_legacy_widget_aliases()
        self._apply_config(cfg)
        self.bridge.config_changed.connect(self._apply_config)

    def _create_editor(self, field: str, value: object) -> tuple[QWidget, QWidget]:
        editor: QWidget
        if isinstance(value, bool):
            checkbox = QCheckBox("Enabled")
            checkbox.setChecked(value)
            editor = checkbox
        elif field in {"side", "facing", "replay_joint_interpolation", "analysis_model"}:
            combo = QComboBox()
            combo.addItems(
                ["lite", "full", "heavy"] if field == "analysis_model" else
                ["linear", "quadratic"] if field == "replay_joint_interpolation" else ["left", "right"]
            )
            combo.setCurrentText(str(value))
            editor = combo
        elif isinstance(value, int):
            int_spin = QSpinBox()
            int_spin.setRange(0, 2_147_483_647)
            int_spin.setValue(value)
            editor = int_spin
        elif isinstance(value, float):
            float_spin = QDoubleSpinBox()
            float_spin.setDecimals(12)
            float_spin.setSingleStep(0.1)
            float_spin.setRange(
                *_FLOAT_RANGES.get(field, (-1_000_000_000.0, 1_000_000_000.0))
            )
            float_spin.setValue(value)
            editor = float_spin
        else:
            line_edit = QLineEdit(str(value))
            editor = line_edit

        editor.setAccessibleName(field.replace(".", " "))
        editor.setToolTip(f"Configuration key: {field}")
        if field not in {"data_dir", "model_path"} or not isinstance(editor, QLineEdit):
            return editor, editor

        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(editor, 1)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(lambda: self._browse_path(field, editor))
        row_layout.addWidget(browse_btn)
        return editor, row

    @staticmethod
    def _field_label(field: str) -> str:
        labels = {
            "camera": "Camera device index",
            "width": "Image width (pixels)",
            "height": "Image height (pixels)",
            "side": "Measurement side",
            "facing": "Facing direction",
            "capture_profile": "Camera setup name",
            "model_path": "Live Practice model file (Lite)",
            "analysis_model": "Offline analysis model",
            "data_dir": "Data folder",
            "confidence": "Minimum landmark confidence",
            "smoothing": "Measurement smoothing (seconds)",
            "replay_joint_interpolation": "Review joint interpolation",
            "replay_joint_max_gap": "Maximum joint gap to fill (seconds; 0 disables)",
            "head_down_angle": "Head down angle limit (degrees)",
            "shoulder_raise_threshold": "Shoulder tension limit (%)",
            "tango_bpm": "Starting tempo (BPM)",
            "ollama_base_url": "Ollama server address",
            "ollama_num_ctx": "Model context size (tokens)",
            "ollama_num_predict": "Maximum response length (tokens)",
            "ollama_max_report_chars": "Maximum report length (characters)",
            "ollama_instruction": "Analysis focus",
            "ollama_temperature": "Response variation",
        }
        return labels.get(field, field.replace(".", " / ").replace("_", " ").capitalize())

    def _browse_path(self, field: str, editor: QLineEdit) -> None:
        if field == "data_dir":
            path = QFileDialog.getExistingDirectory(self, "Choose data folder", editor.text())
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "Choose pose model", editor.text(), "Model files (*.task);;All files (*)"
            )
        if path:
            editor.setText(path)

    def _set_legacy_widget_aliases(self) -> None:
        camera = self.field_widgets["camera"]
        side = self.field_widgets["side"]
        facing = self.field_widgets["facing"]
        mirror = self.field_widgets["mirror"]
        confidence = self.field_widgets["confidence"]
        smoothing = self.field_widgets["smoothing"]
        head_angle = self.field_widgets["head_down_angle"]
        shoulder = self.field_widgets["shoulder_raise_threshold"]
        beat = self.field_widgets["beat_tracking"]
        bpm = self.field_widgets["tango_bpm"]
        ollama_url = self.field_widgets["ollama_base_url"]
        ollama_model = self.field_widgets["ollama_model"]
        assert isinstance(camera, QSpinBox)
        assert isinstance(side, QComboBox)
        assert isinstance(facing, QComboBox)
        assert isinstance(mirror, QCheckBox)
        assert isinstance(confidence, QDoubleSpinBox)
        assert isinstance(smoothing, QDoubleSpinBox)
        assert isinstance(head_angle, QDoubleSpinBox)
        assert isinstance(shoulder, QDoubleSpinBox)
        assert isinstance(beat, QCheckBox)
        assert isinstance(bpm, QDoubleSpinBox)
        assert isinstance(ollama_url, QLineEdit)
        assert isinstance(ollama_model, QLineEdit)
        self.cam_spin = camera
        self.side_combo = side
        self.facing_combo = facing
        self.mirror_cb = mirror
        self.conf_spin = confidence
        self.smooth_spin = smoothing
        self.head_angle_spin = head_angle
        self.shoulder_spin = shoulder
        self.beat_cb = beat
        self.bpm_spin = bpm
        self.ollama_url = ollama_url
        self.ollama_model = ollama_model

    def _save_settings(self) -> bool:
        try:
            data = self.bridge.config.model_dump(exclude={"axis_curve"})
            for field, editor in self.field_widgets.items():
                if isinstance(editor, QCheckBox):
                    value = str(editor.isChecked())
                elif isinstance(editor, QComboBox):
                    value = editor.currentText()
                elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                    value = str(editor.value())
                elif isinstance(editor, QLineEdit):
                    value = editor.text().strip()
                else:
                    raise TypeError(f"Unsupported settings editor for {field}")
                set_setting(data, field, value)
            data["calibration_defaults"] = {
                key: RangeDefault.model_validate({
                    "lower": lower.value(), "upper": upper.value(), "unit": METRIC_DETAILS[key][2],
                    "view": view.currentText(),
                }).model_dump()
                for key, (enabled, lower, upper, view) in self.range_widgets.items()
                if enabled.isChecked()
            }
            cfg = Config.model_validate(data)
        except (TypeError, ValueError, ValidationError) as error:
            self.bridge.status_message.emit(f"Could not apply settings: {error}")
            return False
        self.bridge.update_config(cfg)
        self._apply_config(cfg)
        return True

    def _reset_defaults(self) -> None:
        default_cfg = Config()
        self.bridge.update_config(default_cfg)
        self._apply_config(default_cfg)

    def _apply_config(self, cfg: Config) -> None:
        for field, editor in self.field_widgets.items():
            value = setting_value(cfg, field)
            if isinstance(editor, QCheckBox):
                editor.setChecked(value)
            elif isinstance(editor, QComboBox):
                editor.setCurrentText(str(value))
            elif isinstance(editor, (QSpinBox, QDoubleSpinBox)):
                editor.setValue(value)
            elif isinstance(editor, QLineEdit):
                editor.setText(str(value))
        for key, (enabled, lower, upper, view) in self.range_widgets.items():
            target = cfg.calibration_defaults.get(key)
            enabled.setChecked(target is not None)
            lower.setValue(target.lower if target else 0)
            upper.setValue(target.upper if target else 0)
            view.setCurrentText(target.view if target else "side")
            for widget in (lower, upper, view):
                widget.setEnabled(target is not None)

    def _save_toml(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        destination = Path("settings.toml").resolve()
        if QMessageBox.question(
            self, "Save Settings", f"Apply these Settings and overwrite {destination}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        if not self._save_settings():
            return
        try:
            self.bridge.save_settings(self.bridge.config, destination)
        except OSError as error:
            self.bridge.status_message.emit(f"Could not save settings.toml: {error}")
            return
        self.bridge.status_message.emit(f"Settings saved to {destination}")

    def _export_toml(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export TOML Configuration", "settings.toml", "TOML (*.toml)"
        )
        if not path:
            return
        if not self._save_settings():
            return
        try:
            self.bridge.export_settings_toml(Path(path))
        except OSError as error:
            self.bridge.status_message.emit(f"Could not export settings: {error}")

    def _import_toml(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import TOML Configuration", "", "TOML (*.toml)")
        if path:
            try:
                self.bridge.import_settings_toml(Path(path))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError) as error:
                self.bridge.status_message.emit(f"Could not import settings: {error}")
                return
            self._apply_config(self.bridge.config)
