"""Settings management screen with TOML import and export."""

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
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...config import Config
from ..router import Router
from ..service_bridge import ServiceBridge


class SettingsScreen(QScrollArea):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        container = QWidget()
        self.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(20)

        # Header
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("Application Settings")
        title.setProperty("class", "title")
        subtitle = QLabel("Configure camera capture, measurement thresholds, and Ollama integration.")
        subtitle.setProperty("class", "subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        export_btn = QPushButton("Export TOML…")
        export_btn.clicked.connect(self._export_toml)
        import_btn = QPushButton("Import TOML…")
        import_btn.clicked.connect(self._import_toml)
        reset_btn = QPushButton("Reset Defaults")
        reset_btn.clicked.connect(self._reset_defaults)

        header.addWidget(export_btn)
        header.addWidget(import_btn)
        header.addWidget(reset_btn)
        layout.addLayout(header)

        cfg = self.bridge.config

        # 1. Camera & View Group
        cam_group = QGroupBox("Camera & Capture Setup")
        cam_layout = QFormLayout(cam_group)
        self.cam_spin = QSpinBox()
        self.cam_spin.setRange(0, 16)
        self.cam_spin.setValue(cfg.camera)
        cam_layout.addRow("Camera Device Index:", self.cam_spin)

        self.side_combo = QComboBox()
        self.side_combo.addItems(["left", "right"])
        self.side_combo.setCurrentText(cfg.side)
        cam_layout.addRow("Measurement Side:", self.side_combo)

        self.facing_combo = QComboBox()
        self.facing_combo.addItems(["right", "left"])
        self.facing_combo.setCurrentText(cfg.facing)
        cam_layout.addRow("Facing Direction:", self.facing_combo)

        self.mirror_cb = QCheckBox("Mirror camera preview")
        self.mirror_cb.setChecked(cfg.mirror)
        cam_layout.addRow("", self.mirror_cb)
        layout.addWidget(cam_group)

        # 2. Tracking & Quality Group
        track_group = QGroupBox("Tracking & Quality Thresholds")
        track_layout = QFormLayout(track_group)
        self.conf_spin = QDoubleSpinBox()
        self.conf_spin.setRange(0.1, 1.0)
        self.conf_spin.setSingleStep(0.05)
        self.conf_spin.setValue(cfg.confidence)
        track_layout.addRow("Landmark Confidence:", self.conf_spin)

        self.smooth_spin = QDoubleSpinBox()
        self.smooth_spin.setRange(0.05, 2.0)
        self.smooth_spin.setSingleStep(0.05)
        self.smooth_spin.setValue(cfg.smoothing)
        track_layout.addRow("Measurement Smoothing (seconds):", self.smooth_spin)

        self.head_angle_spin = QDoubleSpinBox()
        self.head_angle_spin.setRange(-45.0, -5.0)
        self.head_angle_spin.setValue(cfg.head_down_angle)
        track_layout.addRow("Head Down Angle Limit (degrees):", self.head_angle_spin)

        self.shoulder_spin = QDoubleSpinBox()
        self.shoulder_spin.setRange(2.0, 50.0)
        self.shoulder_spin.setValue(cfg.shoulder_raise_threshold)
        track_layout.addRow("Shoulder Tension Limit (%):", self.shoulder_spin)
        layout.addWidget(track_group)

        # 3. Music & Tempo
        music_group = QGroupBox("Musicality & Beat Tracking")
        music_layout = QFormLayout(music_group)
        self.beat_cb = QCheckBox("Enable beat tracking")
        self.beat_cb.setChecked(cfg.beat_tracking)
        music_layout.addRow("", self.beat_cb)

        self.bpm_spin = QDoubleSpinBox()
        self.bpm_spin.setRange(40.0, 240.0)
        self.bpm_spin.setValue(cfg.tango_bpm)
        music_layout.addRow("Starting Tempo (BPM):", self.bpm_spin)
        layout.addWidget(music_group)

        # 4. Ollama Server Group
        ollama_group = QGroupBox("Ollama Local AI Integration")
        ollama_layout = QFormLayout(ollama_group)
        self.ollama_url = QLineEdit(cfg.ollama_base_url)
        ollama_layout.addRow("Ollama Server URL:", self.ollama_url)

        self.ollama_model = QLineEdit(cfg.ollama_model)
        ollama_layout.addRow("Model Name:", self.ollama_model)
        layout.addWidget(ollama_group)

        # Save Button
        save_btn = QPushButton("Save Settings")
        save_btn.setProperty("class", "primary")
        save_btn.setFixedHeight(38)
        save_btn.clicked.connect(self._save_settings)
        layout.addWidget(save_btn)

    def _save_settings(self) -> None:
        cfg = self.bridge.config
        cfg.camera = self.cam_spin.value()
        cfg.side = self.side_combo.currentText()  # type: ignore
        cfg.facing = self.facing_combo.currentText()  # type: ignore
        cfg.mirror = self.mirror_cb.isChecked()
        cfg.confidence = self.conf_spin.value()
        cfg.smoothing = self.smooth_spin.value()
        cfg.head_down_angle = self.head_angle_spin.value()
        cfg.shoulder_raise_threshold = self.shoulder_spin.value()
        cfg.beat_tracking = self.beat_cb.isChecked()
        cfg.tango_bpm = self.bpm_spin.value()
        cfg.ollama_base_url = self.ollama_url.text().strip()
        cfg.ollama_model = self.ollama_model.text().strip()
        self.bridge.update_config(cfg)

    def _reset_defaults(self) -> None:
        default_cfg = Config()
        self.bridge.update_config(default_cfg)
        self._apply_config(default_cfg)

    def _apply_config(self, cfg: Config) -> None:
        self.cam_spin.setValue(cfg.camera)
        self.side_combo.setCurrentText(cfg.side)
        self.facing_combo.setCurrentText(cfg.facing)
        self.mirror_cb.setChecked(cfg.mirror)
        self.conf_spin.setValue(cfg.confidence)
        self.smooth_spin.setValue(cfg.smoothing)
        self.head_angle_spin.setValue(cfg.head_down_angle)
        self.shoulder_spin.setValue(cfg.shoulder_raise_threshold)
        self.beat_cb.setChecked(cfg.beat_tracking)
        self.bpm_spin.setValue(cfg.tango_bpm)
        self.ollama_url.setText(cfg.ollama_base_url)
        self.ollama_model.setText(cfg.ollama_model)

    def _export_toml(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export TOML Configuration", "settings.toml", "TOML (*.toml)")
        if path:
            self.bridge.export_settings_toml(Path(path))

    def _import_toml(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import TOML Configuration", "", "TOML (*.toml)")
        if path:
            try:
                self.bridge.import_settings_toml(Path(path))
            except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValidationError) as error:
                self.bridge.status_message.emit(f"Could not import settings: {error}")
                return
            self._apply_config(self.bridge.config)
