"""Framework-independent cancellable reference analysis workers."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from .downloads import download_facebook_reel, download_youtube_video
from .errors import ImportCancelledError
from .pipeline import analyse_imported_clip


class ReferenceImportWorker:
    """Cancellable background worker for importing and analysing reference video clips."""

    def __init__(self, event_sink: Callable[[tuple[Any, ...]], None]) -> None:
        self.event_sink = event_sink
        self._lock = threading.Lock()
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._active_job_id: str | None = None

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, **kwargs: Any) -> str:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A reference video import is already running")
            job_id = uuid4().hex
            cancel_ev = threading.Event()
            self._cancel = cancel_ev
            self._active_job_id = job_id

            def _run() -> None:
                try:
                    result = analyse_imported_clip(
                        **kwargs,
                        progress_callback=lambda cur, tot, msg: self.event_sink(
                            ("import_progress", job_id, cur, tot, msg)
                        ),
                        cancel_check=cancel_ev.is_set,
                    )
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("import_done", job_id, result))
                except ImportCancelledError:
                    self.event_sink(("import_cancelled", job_id))
                except Exception as exc:  # noqa: BLE001 - worker boundary reports error to UI
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("import_error", job_id, str(exc)))

            thread = threading.Thread(target=_run, name="reference-import-worker", daemon=True)
            self._thread = thread
            thread.start()
            return job_id

    def start_download(
        self,
        youtube_url: str,
        output_dir: Path,
        *,
        downloader: Callable[[str, Path], Path] | None = None,
    ) -> str:
        """Download a YouTube video in the background without analysing it."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A reference video import is already running")
            job_id = uuid4().hex
            cancel_ev = threading.Event()
            self._cancel = cancel_ev
            self._active_job_id = job_id

            def _run() -> None:
                try:
                    path, link = download_youtube_video(
                        youtube_url,
                        output_dir,
                        progress_callback=lambda cur, tot, msg: self.event_sink(
                            ("import_progress", job_id, cur, tot, msg)
                        ),
                        cancel_check=cancel_ev.is_set,
                        downloader=downloader,
                    )
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("youtube_downloaded", job_id, path, link))
                except ImportCancelledError:
                    self.event_sink(("import_cancelled", job_id))
                except Exception as exc:  # noqa: BLE001 - worker boundary reports error to UI
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("import_error", job_id, str(exc)))

            thread = threading.Thread(target=_run, name="youtube-download-worker", daemon=True)
            self._thread = thread
            thread.start()
            return job_id

    def start_facebook_download(
        self,
        facebook_url: str,
        output_dir: Path,
        *,
        downloader: Callable[[str, Path], Path] | None = None,
    ) -> str:
        """Download a Facebook Reel in the background without analysing it."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A reference video import is already running")
            job_id = uuid4().hex
            cancel_ev = threading.Event()
            self._cancel = cancel_ev
            self._active_job_id = job_id

            def _run() -> None:
                try:
                    path, link = download_facebook_reel(
                        facebook_url,
                        output_dir,
                        progress_callback=lambda cur, tot, msg: self.event_sink(
                            ("import_progress", job_id, cur, tot, msg)
                        ),
                        cancel_check=cancel_ev.is_set,
                        downloader=downloader,
                    )
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("facebook_downloaded", job_id, path, link))
                except ImportCancelledError:
                    self.event_sink(("import_cancelled", job_id))
                except Exception as exc:  # noqa: BLE001 - worker boundary reports error to UI
                    if cancel_ev.is_set():
                        self.event_sink(("import_cancelled", job_id))
                    else:
                        self.event_sink(("import_error", job_id, str(exc)))

            thread = threading.Thread(target=_run, name="facebook-download-worker", daemon=True)
            self._thread = thread
            thread.start()
            return job_id

    def cancel(self) -> bool:
        with self._lock:
            if self._cancel is not None:
                self._cancel.set()
                return True
            return False

    def close(self) -> None:
        self.cancel()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
