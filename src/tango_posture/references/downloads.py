"""Cancellable YouTube and Facebook download adapters."""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from .constants import MAX_IMPORT_VIDEO_BYTES, SUPPORTED_VIDEO_EXTENSIONS
from .errors import ImportCancelledError
from .links import (
    FacebookReelSourceLink,
    YouTubeSourceLink,
    parse_facebook_reel_url,
    parse_youtube_url,
)
from .video import probe_import_video


def imports_directory(data_dir: str) -> Path:
    """Directory where YouTube-imported videos are stored locally."""
    return Path("videos") / "imports" if data_dir == "data" else Path(data_dir) / "imports"


_YTDLP_FORMAT = "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best"


_YTDLP_MISSING_MESSAGE = (
    "Cannot download from YouTube: 'yt-dlp' is not installed. Install it with "
    "'brew install yt-dlp' or 'uv tool install yt-dlp', or download the MP4 locally "
    "and choose it with 'Choose file…'."
)


_YTDLP_TIMEOUT_SECONDS: float = 1800.0


def _trusted_ca_bundle() -> Path | None:
    for variable in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        value = os.environ.get(variable)
        if value and Path(value).is_file():
            return Path(value).resolve()
    defaults = ssl.get_default_verify_paths()
    candidates = (
        Path("/opt/homebrew/etc/openssl@3/cert.pem"),
        Path("/usr/local/etc/openssl@3/cert.pem"),
        Path(defaults.cafile) if defaults.cafile else None,
    )
    return next((path.resolve() for path in candidates if path is not None and path.is_file()), None)


def _find_ytdlp_command() -> list[str] | None:
    """Locate a `yt-dlp` binary, then `uv tool run yt-dlp`, without any shell execution."""
    candidates = [
        shutil.which("yt-dlp"),
        str(Path(sys.executable).parent / "yt-dlp"),
        "/opt/homebrew/bin/yt-dlp",
        "/usr/local/bin/yt-dlp",
        str(Path.home() / ".local" / "bin" / "yt-dlp"),
    ]
    for cand in candidates:
        if cand and Path(cand).is_file():
            return [cand]
    uv_candidates = [
        shutil.which("uv"),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
        str(Path.home() / ".local" / "bin" / "uv"),
    ]
    for uv_cand in uv_candidates:
        if uv_cand and Path(uv_cand).is_file():
            return [uv_cand, "tool", "run", "yt-dlp"]
    return None


def _partial_download_bytes(folder: Path, cache_stem: str) -> int:
    total = 0
    for p in folder.glob(f"{cache_stem}.*"):
        with suppress(OSError):
            total += p.stat().st_size
    return total


def _discard_partial_downloads(folder: Path, cache_stem: str, keep: Path | None = None) -> None:
    for p in folder.glob(f"{cache_stem}.*"):
        if keep is not None and p == keep:
            continue
        with suppress(OSError):
            p.unlink()


def _download_with_python_module(
    yt_dlp_module: Any,
    link: YouTubeSourceLink | FacebookReelSourceLink,
    target_mp4: Path,
    cancel_check: Callable[[], bool] | None,
    provider_label: str,
) -> None:
    cancelled = False
    ca_bundle = _trusted_ca_bundle()
    if ca_bundle is not None:
        os.environ["SSL_CERT_FILE"] = str(ca_bundle)

    def hook(status: dict[str, Any]) -> None:
        nonlocal cancelled
        if cancel_check is not None and cancel_check():
            cancelled = True
            raise ImportCancelledError(f"{provider_label} download was cancelled")
        downloaded = status.get("downloaded_bytes")
        if isinstance(downloaded, int | float) and downloaded > MAX_IMPORT_VIDEO_BYTES:
            raise ValueError(f"{provider_label} video exceeds the 2 GB import limit")

    opts: dict[str, Any] = {
        "format": _YTDLP_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(target_mp4),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    try:
        with yt_dlp_module.YoutubeDL(opts) as ydl:
            ydl.download([link.canonical_url])
    except ImportCancelledError:
        raise
    except Exception as exc:
        if cancelled:
            raise ImportCancelledError(f"{provider_label} download was cancelled") from exc
        raise ValueError(f"{provider_label} download failed: {exc}") from exc


def _download_with_subprocess(
    cmd_prefix: list[str],
    link: YouTubeSourceLink | FacebookReelSourceLink,
    target_mp4: Path,
    cancel_check: Callable[[], bool] | None,
    cache_stem: str,
    provider_label: str,
) -> None:
    ca_bundle = _trusted_ca_bundle()
    cmd = [
        *cmd_prefix,
        "--no-playlist",
        "--no-warnings",
        "-f",
        _YTDLP_FORMAT,
        "--merge-output-format",
        "mp4",
        "-o",
        str(target_mp4),
        link.canonical_url,
    ]
    log_path = target_mp4.parent / f".{cache_stem}.log"
    try:
        with log_path.open("w+b") as log:
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=(
                        {**os.environ, "SSL_CERT_FILE": str(ca_bundle)}
                        if ca_bundle is not None
                        else None
                    ),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"Failed to launch {provider_label} downloader: {exc}") from exc
            started = time.monotonic()
            try:
                while True:
                    try:
                        proc.wait(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if cancel_check is not None and cancel_check():
                        raise ImportCancelledError(f"{provider_label} download was cancelled")
                    if _partial_download_bytes(target_mp4.parent, cache_stem) > (
                        MAX_IMPORT_VIDEO_BYTES
                    ):
                        raise ValueError(f"{provider_label} video exceeds the 2 GB import limit")
                    if time.monotonic() - started > _YTDLP_TIMEOUT_SECONDS:
                        raise ValueError(f"{provider_label} video download timed out")
            except BaseException:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                _discard_partial_downloads(target_mp4.parent, cache_stem)
                raise
            if proc.returncode != 0:
                log.seek(0)
                text = log.read().decode("utf-8", errors="replace")
                err_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                detail = err_lines[-1] if err_lines else f"exit code {proc.returncode}"
                raise ValueError(f"{provider_label} download failed: {detail}")
    finally:
        with suppress(OSError):
            log_path.unlink()


def _download_source_video(
    link: YouTubeSourceLink | FacebookReelSourceLink,
    output_dir: Path | str,
    *,
    cache_stem: str,
    provider_label: str,
    missing_message: str,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    downloader: Callable[[str, Path], Path] | None = None,
) -> Path:
    out_folder = Path(output_dir)
    out_folder.mkdir(parents=True, exist_ok=True)
    target_mp4 = out_folder / f"{cache_stem}.mp4"

    if target_mp4.is_file() and target_mp4.stat().st_size > 0:
        with suppress(ValueError):
            probe_import_video(target_mp4)
            return target_mp4.resolve()

    if cancel_check is not None and cancel_check():
        raise ImportCancelledError(f"{provider_label} video download was cancelled")

    if progress_callback is not None:
        progress_callback(2, 100, f"Downloading {provider_label} video…")

    if downloader is not None:
        downloaded = Path(downloader(link.canonical_url, target_mp4))
        if downloaded.is_file() and downloaded.stat().st_size > MAX_IMPORT_VIDEO_BYTES:
            raise ValueError(f"{provider_label} video exceeds the 2 GB import limit")
        probe_import_video(downloaded)
        return downloaded.resolve()

    yt_dlp_module: Any = None
    try:
        import yt_dlp as yt_dlp_module  # type: ignore[import-not-found,no-redef]
    except ImportError:
        yt_dlp_module = None

    if yt_dlp_module is not None:
        _download_with_python_module(
            yt_dlp_module, link, target_mp4, cancel_check, provider_label
        )
    else:
        cmd_prefix = _find_ytdlp_command()
        if cmd_prefix is None:
            raise ValueError(missing_message)
        _download_with_subprocess(
            cmd_prefix,
            link,
            target_mp4,
            cancel_check,
            cache_stem,
            provider_label,
        )

    if cancel_check is not None and cancel_check():
        raise ImportCancelledError(f"{provider_label} video download was cancelled")

    result: Path | None = target_mp4 if target_mp4.is_file() else None
    if result is None:
        matches = sorted(
            (
                p
                for p in out_folder.glob(f"{cache_stem}.*")
                if p.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            ),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
        result = matches[0] if matches else None
    if result is None:
        raise ValueError(
            f"{provider_label} download completed, but no playable local video file was produced"
        )
    if result.stat().st_size > MAX_IMPORT_VIDEO_BYTES:
        result.unlink(missing_ok=True)
        raise ValueError(f"{provider_label} video exceeds the 2 GB import limit")
    probe_import_video(result)
    return result.resolve()


def download_youtube_video(
    youtube_url: str,
    output_dir: Path | str,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    downloader: Callable[[str, Path], Path] | None = None,
) -> tuple[Path, YouTubeSourceLink]:
    """Validate and download a YouTube video, reusing a validated local cache."""
    link = parse_youtube_url(youtube_url)
    if link is None:
        raise ValueError("Enter a valid HTTPS YouTube URL to import")
    path = _download_source_video(
        link,
        output_dir,
        cache_stem=f"youtube_{link.video_id}",
        provider_label="YouTube",
        missing_message=_YTDLP_MISSING_MESSAGE,
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        downloader=downloader,
    )
    return path, link


def download_facebook_reel(
    facebook_url: str,
    output_dir: Path | str,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    downloader: Callable[[str, Path], Path] | None = None,
) -> tuple[Path, FacebookReelSourceLink]:
    """Validate and download a Facebook Reel, reusing its validated local cache."""
    link = parse_facebook_reel_url(facebook_url)
    if link is None:
        raise ValueError("Enter a valid HTTPS Facebook Reel URL to import")
    path = _download_source_video(
        link,
        output_dir,
        cache_stem=f"facebook_reel_{link.reel_id}",
        provider_label="Facebook Reel",
        missing_message=(
            "Cannot download from Facebook: 'yt-dlp' is not installed. Install it with "
            "'brew install yt-dlp' or 'uv tool install yt-dlp', or download the video locally."
        ),
        progress_callback=progress_callback,
        cancel_check=cancel_check,
        downloader=downloader,
    )
    return path, link
