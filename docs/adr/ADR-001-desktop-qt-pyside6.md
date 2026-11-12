# ADR-001: Adoption of PySide6 for Desktop UI Architecture

## Status
Accepted

## Context
TangoPosture's original graphical interface was written in Tkinter (`gui.py`, `review_ui.py`, `notes_ui.py`). As capabilities grew (multi-stream synchronized replay, analytics charts, side-by-side comparison, YouTube/Facebook reference import, Ollama streaming, and movement overlays), Tkinter showed distinct limitations:
1. Nested tab widgets creating poor UX navigation.
2. Cluttered modal dialogs and secondary toplevel windows for analytics, live coach, and cut operations.
3. Lack of hardware-accelerated video/canvas rendering leading to CPU-bound frame redrawing.
4. Rigid widget theming requiring low-level Tk Canvas workarounds for modern accessibility and dark styling.

The product requirements specify a **desktop-only** redesign (no web, no cloud backend, no local HTTP/REST server), targeting macOS Apple Silicon and Windows x64.

## Decision
We adopt **PySide6 (Qt 6.10+)** as the primary graphical framework for the modern TangoPosture desktop application.

Key architectural decisions:
1. **In-Process Python Services with Qt Signal Bridge:**
   - Qt screens and viewmodels interact with pure Python domain services via typed commands and events.
   - Core computational engines (`geometry`, `metrics`, `movement`, `calibration`, `beat`) remain strictly decoupled from Qt.
2. **Video & Frame Display Strategy:**
   - Real-time video preview and playback frames are converted directly from NumPy/OpenCV arrays (`BGR` to `RGB`) to `QImage` using `QImage.Format_RGB888`.
   - Zero-copy / ownership-safe memory buffers are displayed via custom `QPainter` widgets (`VideoStage`) ensuring aspect-ratio preservation without blocking Qt's main event loop.
3. **Threading & Concurrency:**
   - GUI thread runs the PySide6 event loop.
   - Long-running tasks (MediaPipe pose estimation, video cutting, archive extraction, Ollama streaming) execute in worker threads communicating back via Qt signals/slots or thread-safe message queues.
4. **Headless & Offscreen Testability:**
   - All Qt widgets, viewmodels, and routers can be tested without a physical window display using `-platform offscreen` with `pytest-qt` or standard `QApplication` fixtures.
5. **Preservation of CLI & Legacy Entrypoint:**
   - The CLI `tango-posture` command continues to function identically.
   - Legacy Tk GUI remains available via `tango-posture gui --legacy` or fallback, while `tango-posture gui` / `tango-posture qt` launches the modern PySide6 desktop experience.

## Consequences
- PySide6 is added as a runtime dependency.
- Modern dark palette and ergonomic layout: persistent left navigation rail (Home, Practice, References, Library, Review, Analytics, AI Coach, Settings, Help), centralized video stage, collapsible inspector, and responsive layouts.
- Packaging bundles PySide6 binaries natively in PyInstaller / `.dmg` / installer packages.
