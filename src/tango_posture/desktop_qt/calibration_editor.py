"""Shared evidence and explicit range approval for video and camera calibrations."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..archive import PracticeArchive, calibration_directory, calibration_path, file_sha256
from ..config import Config
from ..domain import FrameObservation
from ..overlay import draw
from ..quality import reliable
from ..range_profiles import (
    METRIC_DETAILS,
    STEPS,
    ApprovedRange,
    Passage,
    RangeProfile,
    cached_evidence,
    dancer_frames,
    metric_samples,
    repeat_check,
    save_range_profile,
    summarize_examples,
)
from ..replay import VideoReader
from .analytics_data import MetricData
from .components.video_stage import VideoStage
from .screens.analytics import MetricChart
from .service_bridge import numpy_to_qimage


class CalibrationEditor(QDialog):
    def __init__(
        self, path: Path, cfg: Config, parent: QWidget | None = None, *,
        holds: dict[str, list[Passage]] | None = None,
        base_profile: RangeProfile | None = None,
    ) -> None:
        super().__init__(parent)
        if path.suffix.lower() != ".tpr":
            raise ValueError("Create / migrate a .tpr reference before approving calibration ranges")
        self.setWindowTitle("Review evidence & approve calibration ranges")
        self.resize(1180, 800)
        self.cfg = cfg.model_copy(deep=True)
        self.evidence = cached_evidence(path)
        self._source_hash = self.evidence.source["archive_sha256"]
        self.holds = holds or {}
        self.base_profile = base_profile
        if base_profile is not None and base_profile.model_hash != self.evidence.model_hash:
            raise ValueError("Reference and camera measurement models differ; reanalyze the reference")
        self.saved_path: Path | None = None
        self.approved: dict[str, ApprovedRange] = {}
        self._stats: dict[str, Any] | None = None
        self._track_start = 0
        self._anchors: dict[int, int] = {0: 0}
        capture = self.evidence.capture_profile
        if not capture:
            raise ValueError("Reference capture settings are missing; reanalyze it in Review")
        self.cfg = Config.model_validate({
            **self.cfg.model_dump(),
            **{key: capture[key] for key in ("side", "facing", "width", "height", "capture_profile")},
        })
        self._archive = PracticeArchive(path)
        try:
            if file_sha256(path) != self._source_hash:
                raise ValueError("Reference changed while loading evidence; reopen it")
            segment = self._archive.manifest["segments"][0]
            descriptor = json.loads((self._archive.folder / "reference.json").read_text())
            anchors = descriptor.get("dancer_anchors")
            if anchors is not None:
                if not isinstance(anchors, list) or not anchors:
                    raise ValueError("Invalid saved reference dancer selection")
                self._anchors = {}
                for anchor in anchors:
                    if (
                        not isinstance(anchor, dict) or type(anchor.get("frame")) is not int
                        or type(anchor.get("dancer")) is not int
                        or not 0 <= anchor["frame"] < len(self.evidence.frames)
                        or not 0 <= anchor["dancer"] < len(self.evidence.frames[anchor["frame"]].poses)
                    ):
                        raise ValueError("Saved dancer selection does not match the cached poses")
                    self._anchors[anchor["frame"]] = anchor["dancer"]
            self._reader = VideoReader(self._archive.folder / segment["video"])
            if self._reader.count != len(self.evidence.frames):
                self._reader.close()
                raise ValueError("Landmark cache and reference video frame counts differ; reanalyze")
        except Exception:
            self._archive.close()
            raise

        layout = QVBoxLayout(self)
        form = QHBoxLayout()
        self.name_input = QLineEdit(
            f"{path.stem} calibration" if base_profile is None else f"{base_profile.name} camera check"
        )
        self.context_combo = QComboBox()
        self.context_combo.addItems(["Neutral posture", "Solo walking", "Close embrace"])
        if base_profile is not None:
            self.context_combo.setEditable(True)
            self.context_combo.setCurrentText(base_profile.context)
        form.addWidget(QLabel("Profile name:"))
        form.addWidget(self.name_input, 1)
        form.addWidget(QLabel("Practice context:"))
        form.addWidget(self.context_combo)
        layout.addLayout(form)
        warning = QLabel(
            "A reference describes this dancer and passage, not a universal technique standard. "
            "Separate standing/walking passages when their distributions differ. "
            "Examples and acceptable limits are independent; only explicitly approved metrics are saved."
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)
        video = QWidget()
        video_layout = QVBoxLayout(video)
        self.stage = VideoStage()
        video_layout.addWidget(self.stage, 1)
        playback = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self._toggle_play)
        playback.addWidget(self.play_button)
        self.time_label = QLabel()
        playback.addWidget(self.time_label)
        video_layout.addLayout(playback)
        self.play_timer = QTimer(self)
        self.play_timer.setSingleShot(True)
        self.play_timer.timeout.connect(self._advance_frame)
        self.scrubber = QSlider(Qt.Orientation.Horizontal)
        self.scrubber.setRange(0, self._reader.count - 1)
        self.scrubber.valueChanged.connect(self._seek_frame)
        video_layout.addWidget(self.scrubber)
        selection = QHBoxLayout()
        self.dancer_spin = QSpinBox()
        self.dancer_spin.setRange(1, max(len(frame.poses) for frame in self.evidence.frames))
        self.dancer_spin.setValue(min(self._anchors.items())[1] + 1)
        self.dancer_spin.valueChanged.connect(self._restart_track)
        restart = QPushButton("Select dancer / restart track here")
        restart.clicked.connect(self._restart_track)
        selection.addWidget(QLabel("Dancer at playhead:"))
        selection.addWidget(self.dancer_spin)
        selection.addWidget(restart)
        video_layout.addLayout(selection)
        self.confirm_identity = QCheckBox("I confirm all manually linked passages follow the same dancer")
        self.confirm_identity.setChecked(len(self._anchors) == 1)
        video_layout.addWidget(self.confirm_identity)
        save_selection = QPushButton("Save dancer selection in reference (.tpr)")
        save_selection.clicked.connect(self._save_selection)
        video_layout.addWidget(save_selection)
        self.chart = MetricChart()
        self.chart.setMinimumHeight(140)
        self.chart.seek_requested.connect(self._seek_timestamp)
        video_layout.addWidget(self.chart)
        splitter.addWidget(video)
        inspector = QWidget()
        inspector.setMaximumWidth(360)
        controls = QVBoxLayout(inspector)
        self.step_combo = QComboBox()
        self.step_combo.addItems(STEPS)
        self.metric_combo = QComboBox()
        self.step_combo.currentTextChanged.connect(self._change_step)
        self.metric_combo.currentIndexChanged.connect(self._change_metric)
        controls.addWidget(self.step_combo)
        controls.addWidget(self.metric_combo)
        options = QFormLayout()
        self.view_combo = QComboBox()
        self.view_combo.addItems(["side", "front", "all"])
        options.addRow("Applicable view:", self.view_combo)
        self.passages_input = QLineEdit()
        self.passages_input.setPlaceholderText("Example seconds: 0-3; 5-9")
        options.addRow("Example passages:", self.passages_input)
        self.pose_combo = QComboBox()
        self.pose_combo.addItems(["Neutral hold", "Changed hold", "Return hold", "Fresh repeat", "Walking drill"])
        self.pose_combo.setVisible(bool(self.holds))
        self.pose_combo.currentIndexChanged.connect(self._select_hold)
        options.addRow("Use captured examples:", self.pose_combo)
        self.lower_spin, self.upper_spin = QDoubleSpinBox(), QDoubleSpinBox()
        for field in (self.lower_spin, self.upper_spin):
            field.setRange(-10000, 10000)
            field.setDecimals(5)
        options.addRow("Acceptable minimum:", self.lower_spin)
        options.addRow("Acceptable maximum:", self.upper_spin)
        controls.addLayout(options)
        suggest = QPushButton("Suggest middle 80% of examples")
        suggest.clicked.connect(lambda: self._update_stats(suggest=True))
        controls.addWidget(suggest)
        controls.addWidget(QLabel("Distribution · tinted area = acceptable range"))
        self.distribution = QLabel()
        controls.addWidget(self.distribution)
        self.stats_label = QLabel()
        self.stats_label.setWordWrap(True)
        controls.addWidget(self.stats_label)
        extreme_row = QHBoxLayout()
        for text, key in (("Show low example", "minimum_time"), ("Show high example", "maximum_time")):
            button = QPushButton(text)
            button.clicked.connect(
                lambda _, key=key: self._seek_timestamp(self._stats[key]) if self._stats else None
            )
            extreme_row.addWidget(button)
        controls.addLayout(extreme_row)
        self.check_label = QLabel()
        self.check_label.setWordWrap(True)
        controls.addWidget(self.check_label)
        self.live_check = QCheckBox("Approve this metric for this checked camera")
        self.live_check.setToolTip(
            "I confirm that the camera view and movement make this metric comparable to the reference. "
            "A repeat check demonstrates response, not universal cross-camera accuracy."
        )
        controls.addWidget(self.live_check)
        approve = QPushButton("Approve metric")
        approve.clicked.connect(self._approve)
        skip = QPushButton("Skip / remove metric")
        skip.clicked.connect(self._skip)
        controls.addWidget(approve)
        controls.addWidget(skip)
        next_step = QPushButton("Next: head → shoulders → hips → torso → knees")
        next_step.clicked.connect(
            lambda: self.step_combo.setCurrentIndex((self.step_combo.currentIndex() + 1) % len(STEPS))
        )
        controls.addWidget(next_step)
        self.approved_label = QLabel("No metrics approved")
        self.approved_label.setWordWrap(True)
        controls.addWidget(self.approved_label)
        controls.addStretch()
        save = QPushButton("Save approved .tpc profile")
        save.clicked.connect(self._save)
        controls.addWidget(save)
        inspector_scroll = QScrollArea()
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setMinimumWidth(340)
        inspector_scroll.setMaximumWidth(380)
        inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inspector_scroll.setWidget(inspector)
        splitter.addWidget(inspector_scroll)
        splitter.setSizes([820, 340])
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.passages_input.editingFinished.connect(lambda: self._update_stats())
        self.lower_spin.valueChanged.connect(lambda: self._update_stats())
        self.upper_spin.valueChanged.connect(lambda: self._update_stats())
        self.view_combo.currentIndexChanged.connect(self._recompute)
        self._samples: dict[str, list[tuple[float, float | None]]] = {}
        self._recompute()
        self._change_step(STEPS[0])
        self._seek_frame(0)
        self.finished.connect(self._release)

    def _release(self, *_args: object) -> None:
        self.play_timer.stop()
        self._reader.close()
        self._archive.close()

    def _restart_track(self, *_args: object) -> None:
        self._track_start = self.scrubber.value()
        self._anchors[self._track_start] = self.dancer_spin.value() - 1
        self.confirm_identity.setChecked(False)
        self.approved.clear()
        self.approved_label.setText("Dancer selection changed; approve the new evidence again")
        self._recompute()

    def _recompute(self, *_args: object) -> None:
        from ..range_profiles import Evidence

        anchors = sorted(self._anchors.items())
        frames: list[FrameObservation] = []
        if anchors[0][0] > 0:
            frames.extend(FrameObservation(frame.timestamp, frame.width, frame.height, [])
                          for frame in self.evidence.frames[:anchors[0][0]])
        for position, (start, dancer) in enumerate(anchors):
            end = anchors[position + 1][0] if position + 1 < len(anchors) else len(self.evidence.frames)
            evidence = Evidence(
                self.evidence.frames[start:end], self.evidence.model_hash,
                self.evidence.capture_profile, self.evidence.source, self.evidence.max_gap,
            )
            frames.extend(dancer_frames(evidence, dancer, self.cfg.confidence))
        cfg = self.cfg.model_copy(update={"allow_all_angles": self.view_combo.currentText() != "side"})
        self._samples = metric_samples(frames, cfg)
        if self.metric_combo.currentData():
            self._update_stats(suggest=True)

    def _change_step(self, step: str) -> None:
        self.metric_combo.blockSignals(True)
        self.metric_combo.clear()
        for key, (group, label, _unit) in METRIC_DETAILS.items():
            if group == step:
                self.metric_combo.addItem(label, key)
        self.metric_combo.blockSignals(False)
        self._change_metric()

    def _change_metric(self, *_args: object) -> None:
        key = self.metric_combo.currentData()
        if key is None:
            return
        first, last = self.evidence.frames[0].timestamp, self.evidence.frames[-1].timestamp
        holds = self.holds.get(self.step_combo.currentText())
        passages = [holds[0]] if holds else [Passage(start=first, end=last + 1 / self._reader.fps)]
        target = self.approved.get(key) or (
            self.base_profile.metrics.get(key) if self.base_profile is not None else None
        )
        self.passages_input.setText("; ".join(f"{p.start:.6f}-{p.end:.6f}" for p in passages))
        self._update_stats(suggest=target is None)
        if target is not None:
            self.view_combo.setCurrentText(target.view)
            self.lower_spin.setValue(target.lower)
            self.upper_spin.setValue(target.upper)
        self.live_check.setChecked(False)

    def _select_hold(self, index: int) -> None:
        holds = (
            self.holds.get("Walking", []) if index == 4 else
            self.holds.get(self.step_combo.currentText(), [])[index:index + 1]
        )
        if not holds:
            self.status_label.setText("That hold / drill was skipped or is unavailable.")
            return
        self.passages_input.setText("; ".join(f"{p.start:.6f}-{p.end:.6f}" for p in holds))
        self._update_stats()

    def _passages(self) -> list[Passage]:
        passages = []
        origin = self.evidence.frames[0].timestamp
        end = self.evidence.frames[-1].timestamp + 1 / self._reader.fps
        for value in self.passages_input.text().split(";"):
            start_text, end_text = value.strip().split("-")
            passage = Passage(start=float(start_text), end=float(end_text))
            if passage.start < origin or passage.end > end + 1e-6:
                raise ValueError("Example passage is outside the reference timeline")
            passages.append(passage)
        return passages

    def _update_stats(self, *, suggest: bool = False) -> None:
        key = self.metric_combo.currentData()
        if key is None or not self._samples:
            return
        self._stats = None
        try:
            stats = summarize_examples(
                self._samples[key], self._passages(), self.evidence.max_gap,
                None if suggest else self.lower_spin.value(),
                None if suggest else self.upper_spin.value(),
            )
            self._stats = stats
            if suggest:
                for field, value in ((self.lower_spin, stats["lower"]), (self.upper_spin, stats["upper"])):
                    field.blockSignals(True)
                    field.setValue(value)
                    field.blockSignals(False)
            self.stats_label.setText(
                f"Median: {stats['median']:.4g} {METRIC_DETAILS[key][2]}\n"
                f"Observed middle 80%: {stats['p10']:.4g} … {stats['p90']:.4g}\n"
                f"Usable: {stats['valid_duration']:.2f} / {stats['selected_duration']:.2f}s "
                f"({stats['samples']} samples)\nInside edited range: {stats['inside_fraction']:.1%}"
            )
            self._draw_distribution(key)
            origin = self.evidence.frames[0].timestamp
            points = [(t - origin, value) for t, value in self._samples[key]]
            segments: list[list[tuple[float, float]]] = []
            segment: list[tuple[float, float]] = []
            for timestamp, value in points:
                if value is None or (segment and timestamp - segment[-1][0] > self.evidence.max_gap):
                    if segment:
                        segments.append(segment)
                    segment = []
                if value is not None:
                    segment.append((timestamp, value))
            if segment:
                segments.append(segment)
            self.chart.set_data(MetricData(points, segments, {}, 0, 0), points[-1][0], origin)
            holds = self.holds.get(self.step_combo.currentText(), [])
            passed, reason = repeat_check(self._samples[key], holds, self.evidence.max_gap)
            is_camera = str(self.evidence.capture_profile.get("source", "")).startswith("camera:")
            self.live_check.setEnabled(passed and is_camera)
            if not passed or not is_camera:
                self.live_check.setChecked(False)
            self.check_label.setText(
                reason if is_camera else
                "Comparison only. A guided camera repeat check is required before live scoring."
            )
            self.status_label.setText("Edit examples independently from target limits, then approve this metric.")
        except (ValueError, KeyError) as error:
            self.stats_label.setText(f"Unavailable: {error}")
            self.distribution.clear()
            self.live_check.setEnabled(False)
            self.chart.set_data(None)

    def _draw_distribution(self, key: str) -> None:
        passages = self._passages()
        values = [value for timestamp, value in self._samples[key]
                  if value is not None and math.isfinite(value)
                  and any(p.start <= timestamp < p.end for p in passages)]
        counts, edges = np.histogram(values, bins=14)
        pixmap = QPixmap(320, 100)
        pixmap.fill(QColor("#19151b"))
        painter = QPainter(pixmap)
        left, right = float(edges[0]), float(edges[-1])
        project = lambda value: max(8.0, min(312.0, 8 + (value - left) / (right - left) * 304))
        low, high = project(self.lower_spin.value()), project(self.upper_spin.value())
        painter.fillRect(QRectF(low, 3, max(0, high - low), 73), QColor("#315748"))
        peak = int(max(counts))
        for index, count in enumerate(counts):
            height = float(count) / peak * 66
            painter.fillRect(QRectF(8 + index * 304 / 14, 76 - height, 304 / 14 - 2, height),
                             QColor("#bb9974"))
        painter.setPen(QColor("#ddd5d0"))
        painter.drawText(8, 94, f"{left:.3g}")
        painter.drawText(260, 94, f"{right:.3g}")
        painter.end()
        self.distribution.setPixmap(pixmap)

    def _approve(self) -> None:
        self._update_stats()
        if self._stats is None:
            return
        key = self.metric_combo.currentData()
        stats = {name: value for name, value in self._stats.items()
                 if name not in ("minimum_time", "maximum_time")}
        self.approved[key] = ApprovedRange.model_validate({
            **stats, "unit": METRIC_DETAILS[key][2], "view": self.view_combo.currentText(),
            "passages": self._passages(), "camera_check_passed": self.live_check.isChecked(),
        })
        self.approved_label.setText("Approved: " + ", ".join(METRIC_DETAILS[k][1] for k in self.approved))

    def _skip(self) -> None:
        self.approved.pop(self.metric_combo.currentData(), None)
        self.approved_label.setText("Approved: " + ", ".join(METRIC_DETAILS[k][1] for k in self.approved))

    def _save(self) -> None:
        try:
            if file_sha256(self._archive.path) != self._source_hash:
                raise ValueError("Reference changed while reviewing; reopen it before saving a profile")
            if not self.confirm_identity.isChecked():
                raise ValueError("Confirm that manually linked passages follow the same dancer")
            if not self.approved:
                raise ValueError("Approve at least one metric; skipped metrics will remain unavailable")
            result = RangeProfile(
                name=self.name_input.text().strip(), context=self.context_combo.currentText(),
                model_hash=self.evidence.model_hash, capture_profile=self.evidence.capture_profile,
                source={
                    **self.evidence.source, "dancer_at_track_start": self.dancer_spin.value() - 1,
                    "track_start_frame": self._track_start,
                    "dancer_anchors": [
                        {"frame": frame, "dancer": dancer} for frame, dancer in sorted(self._anchors.items())
                    ],
                    **({"target_reference": self.base_profile.source} if self.base_profile else {}),
                },
                metrics=self.approved,
                camera_checks={"holds": {
                    key: [hold.model_dump() for hold in holds] for key, holds in self.holds.items()
                }},
            )
            path = calibration_path(calibration_directory(self.cfg.data_dir), result.name)
            save_range_profile(path, result)
            self.saved_path = path
            self.accept()
        except (OSError, ValueError, TypeError) as error:
            self.status_label.setText(f"Calibration not saved: {error}")

    def _save_selection(self) -> None:
        from ..archive import _archive_lock, _rewrite_members

        try:
            if not self.confirm_identity.isChecked():
                raise ValueError("Confirm that the linked passages follow the same dancer first")
            path = self._archive.path
            with _archive_lock(path):
                if file_sha256(path) != self._source_hash:
                    raise ValueError("Reference changed while reviewing; reopen before saving selection")
                from zipfile import ZipFile

                with ZipFile(path) as archive:
                    descriptor = json.loads(archive.read("reference.json"))
                descriptor["selected_dancer"] = min(self._anchors.items())[1]
                descriptor["dancer_anchors"] = [
                    {"frame": frame, "dancer": dancer} for frame, dancer in sorted(self._anchors.items())
                ]
                _rewrite_members(path, {"reference.json": json.dumps(descriptor).encode()})
                self._source_hash = file_sha256(path)
                self.evidence.source["archive_sha256"] = self._source_hash
            self.status_label.setText("Dancer selection saved. Measurements and raw video are unchanged.")
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.status_label.setText(f"Reference selection not saved: {error}")
    def _seek_timestamp(self, timestamp: float) -> None:
        index = min(
            range(len(self.evidence.frames)),
            key=lambda index: abs(self.evidence.frames[index].timestamp - timestamp),
        )
        self.scrubber.setValue(index)

    def _seek_frame(self, index: int) -> None:
        try:
            frame = self.evidence.frames[index]
            image = draw(
                self._reader.frame(index), frame, False, [], "", self.cfg.confidence,
                show_vertical_axis=False, show_footer=False,
            )
            height, width = image.shape[:2]
            for number, pose in enumerate(frame.poses, start=1):
                hip = pose.get("left_hip") or pose.get("right_hip")
                if hip is not None and reliable(hip, self.cfg.confidence):
                    position = (round(hip.x * width), round(hip.y * height))
                    color = (90, 230, 150) if number == self.dancer_spin.value() else (230, 200, 150)
                    cv2.putText(image, str(number), position, cv2.FONT_HERSHEY_SIMPLEX, 1, color, 3)
            self.stage.set_frame(numpy_to_qimage(image))
            self.time_label.setText(f"{frame.timestamp:.3f}s · frame {index}")
            if self.play_button.text() == "Pause":
                if index + 1 == len(self.evidence.frames):
                    self.play_timer.stop()
                    self.play_button.setText("Play")
                else:
                    gap = self.evidence.frames[index + 1].timestamp - frame.timestamp
                    self.play_timer.start(max(1, round(gap * 1000)))
        except (OSError, ValueError, RuntimeError) as error:
            self.status_label.setText(f"Cannot display reference frame: {error}")

    def _toggle_play(self) -> None:
        if self.play_button.text() == "Pause":
            self.play_timer.stop()
            self.play_button.setText("Play")
        else:
            self.play_button.setText("Pause")
            self._seek_frame(self.scrubber.value())

    def _advance_frame(self) -> None:
        self.scrubber.setValue(min(self.scrubber.maximum(), self.scrubber.value() + 1))
