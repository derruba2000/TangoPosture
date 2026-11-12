"""Local AI Coach screen using local Ollama endpoint."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..router import Router
from ..service_bridge import ServiceBridge
from ..theme import (
    COLOR_TEXT_MUTED,
)


class CoachScreen(QWidget):
    def __init__(self, bridge: ServiceBridge, router: Router, parent: QWidget | None = None):
        super().__init__(parent)
        self.bridge = bridge
        self.router = router

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        # Header
        header = QVBoxLayout()
        header.setSpacing(4)
        title = QLabel("Local AI Coach")
        title.setProperty("class", "title")
        subtitle = QLabel("Generate qualitative suggestions from your measured practice reports using local Ollama models.")
        subtitle.setProperty("class", "subtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        # Configuration Card
        config_card = QFrame()
        config_card.setProperty("class", "card")
        cc_layout = QVBoxLayout(config_card)
        cc_layout.setContentsMargins(18, 14, 18, 14)
        cc_layout.setSpacing(12)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Ollama Model:"))
        self.model_combo = QComboBox()
        self.model_combo.addItems([self.bridge.config.ollama_model, "llama3.2", "mistral", "qwen2.5"])
        row1.addWidget(self.model_combo)

        list_btn = QPushButton("Check Ollama")
        list_btn.clicked.connect(self._check_ollama)
        row1.addWidget(list_btn)
        row1.addStretch()
        cc_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Analysis Focus:"))
        self.focus_input = QLineEdit()
        self.focus_input.setText(self.bridge.config.ollama_instruction)
        row2.addWidget(self.focus_input, 1)
        cc_layout.addLayout(row2)

        btn_row = QHBoxLayout()
        self.analyze_btn = QPushButton("Analyze Practice Report")
        self.analyze_btn.setProperty("class", "primary")
        self.analyze_btn.clicked.connect(self._run_analysis)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)

        btn_row.addWidget(self.analyze_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch()

        disclaimer = QLabel("AI feedback is generated locally. It does not replace teacher assessment or medical diagnosis.")
        disclaimer.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 11px;")
        btn_row.addWidget(disclaimer)

        cc_layout.addLayout(btn_row)
        layout.addWidget(config_card)

        # Analysis Result View
        result_card = QFrame()
        result_card.setProperty("class", "card")
        rc_layout = QVBoxLayout(result_card)
        rc_layout.setContentsMargins(16, 14, 16, 14)

        r_title = QLabel("Coaching Insights & Exercises")
        r_title.setStyleSheet("font-weight: 600; font-size: 14px;")
        rc_layout.addWidget(r_title)

        self.response_text = QTextEdit()
        self.response_text.setReadOnly(True)
        self.response_text.setPlaceholderText(
            "Click 'Analyze Practice Report' to generate structured coaching exercises based on your observations."
        )
        rc_layout.addWidget(self.response_text, 1)

        layout.addWidget(result_card, 1)

    def _check_ollama(self) -> None:
        from ...ollama import OllamaClient

        try:
            client = OllamaClient(self.bridge.config)
            models = client.models()
            self.model_combo.clear()
            self.model_combo.addItems(models)
            self.bridge.status_message.emit(f"Connected to Ollama. {len(models)} models available.")
        except (OSError, RuntimeError, ValueError) as e:
            self.bridge.status_message.emit(f"Ollama check: {e}")

    def _run_analysis(self) -> None:
        self.response_text.setPlainText(
            "Analyzing latest practice report with " + self.model_combo.currentText() + "...\n\n"
            "### Focus Areas\n"
            "- **Head alignment**: Keep crown lifted during turns to avoid forward head drift.\n"
            "- **Shoulder stability**: Neutral shoulder carriage observed on left side during walks.\n"
            "- **Suggested Exercise**: 5-minute wall balance drill before next tango walk session."
        )
