"""Argentine Tango 8-beat phrase tracker (microphone spectral-flux + pause detection + B-key sync)."""

import ctypes
import ctypes.util
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from statistics import median
from typing import Any

import numpy as np


def _mac_mic_status(request_if_needed: bool = False) -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        msg_str = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p
        )(("objc_msgSend", objc))
        media = msg_str(
            objc.objc_getClass(b"NSString"),
            objc.sel_registerName(b"stringWithUTF8String:"),
            b"soun",
        )
        msg_status = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(("objc_msgSend", objc))
        av_device_cls = objc.objc_getClass(b"AVCaptureDevice")
        status = int(
            msg_status(
                av_device_cls,
                objc.sel_registerName(b"authorizationStatusForMediaType:"),
                media,
            )
        )
        if status == 0 and request_if_needed:
            msg_dev = ctypes.CFUNCTYPE(
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
            )(("objc_msgSend", objc))
            dev = msg_dev(
                av_device_cls,
                objc.sel_registerName(b"defaultDeviceWithMediaType:"),
                media,
            )
            if dev:
                msg_input = ctypes.CFUNCTYPE(
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                    ctypes.c_void_p,
                )(("objc_msgSend", objc))
                msg_input(
                    objc.objc_getClass(b"AVCaptureDeviceInput"),
                    objc.sel_registerName(b"deviceInputWithDevice:error:"),
                    dev,
                    None,
                )
        return status
    except (OSError, AttributeError):
        return None


class BeatTracker:
    """Tracks an 8-beat tango phrase (1..8 -> 1..8) from live microphone audio and stops when paused."""

    def __init__(
        self,
        bpm: float = 120.0,
        enable_audio: bool = False,
        require_audio: bool | None = None,
    ):
        self.lock = threading.Lock()
        self.bpm = float(np.clip(bpm, 60.0, 180.0))
        self.require_audio = enable_audio if require_audio is None else require_audio
        self.beat_position = 0.0
        self.last_state_time: float | None = None
        self.was_playing = False
        self.taps: deque[float] = deque(maxlen=6)
        self.onsets: deque[float] = deque(maxlen=12)
        self.flux_history: deque[float] = deque(maxlen=60)
        self.rms_history: deque[float] = deque(maxlen=60)
        self.prev_spectrum: np.ndarray | None = None
        self.last_onset = -10.0
        self.last_energy_time = -10.0
        self.last_tap_time = -10.0
        self.consecutive_onsets = 0
        self.audio_active = False
        self.mic_permission_denied = False
        self.using_live_stream = False
        self.stream: Any = None
        self.sample_rate = 16000
        self.blocksize = 1024
        self.window = np.hanning(self.blocksize)
        freqs = np.fft.rfftfreq(self.blocksize, 1.0 / self.sample_rate)
        # Tango instrumentation band: contrabajo, piano, bandoneon, violin (45 Hz - 2500 Hz)
        self.band_mask = (freqs >= 45.0) & (freqs <= 2500.0)
        self.wall_start = time.monotonic()
        if enable_audio:
            self._start_audio()

    def _start_audio(self) -> None:
        mic_status = _mac_mic_status(request_if_needed=True)
        if mic_status == 0:
            deadline = time.monotonic() + 15.0
            while _mac_mic_status(request_if_needed=False) == 0 and time.monotonic() < deadline:
                time.sleep(0.2)
            mic_status = _mac_mic_status(request_if_needed=False)
        if mic_status in (1, 2):
            self.mic_permission_denied = True
            return
        try:
            import sounddevice as sd

            self.stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
            )
            self.stream.start()
            self.using_live_stream = True
        except Exception:  # noqa: BLE001
            self.stream = None
            self.using_live_stream = False

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if frames != self.blocksize:
            return
        mono = indata[:, 0]
        rms = float(np.sqrt(np.mean(mono * mono)))
        now = time.monotonic() - self.wall_start
        if rms < 1e-5:
            self.process_flux(0.0, now, rms=rms)
            return
        spectrum = np.abs(np.fft.rfft(mono * self.window))[self.band_mask]
        if self.prev_spectrum is None:
            self.prev_spectrum = spectrum
            self.process_flux(0.0, now, rms=rms)
            return
        diff = spectrum - self.prev_spectrum
        self.prev_spectrum = spectrum
        flux = float(np.sum(np.maximum(0.0, diff)))
        self.process_flux(flux, now, rms=rms)

    def _is_playing_unlocked(self, now: float) -> bool:
        if not self.require_audio:
            return True
        if (now - self.last_tap_time) <= 1.2:
            return True
        energy_recent = (now - self.last_energy_time) <= 0.38
        onset_recent = (now - self.last_onset) <= 1.65
        if not energy_recent or not onset_recent:
            if (now - self.last_energy_time) > 0.38 or (now - self.last_onset) > 1.65:
                self.consecutive_onsets = 0
            return False
        return self.consecutive_onsets >= 2

    def process_flux(self, flux: float, timestamp: float, rms: float | None = None) -> bool:
        """Process one spectral-flux + RMS observation; return True if a beat onset occurred."""
        with self.lock:
            effective_rms = (flux * 0.05) if rms is None else rms
            self.rms_history.append(effective_rms)
            noise_floor = (
                float(np.clip(np.percentile(self.rms_history, 20), 2e-4, 0.015))
                if len(self.rms_history) >= 10
                else 8e-4
            )
            has_energy = effective_rms > max(0.0012, noise_floor * 1.75)
            if has_energy:
                self.last_energy_time = timestamp

            self.flux_history.append(flux)
            if len(self.flux_history) < 8 or not has_energy:
                return False

            arr = np.array(self.flux_history, dtype=float)
            threshold = float(np.mean(arr) + 1.35 * np.std(arr))
            if flux <= threshold or flux <= 1e-3 or (timestamp - self.last_onset) < 0.27:
                return False

            prev_onset = self.last_onset
            self.last_onset = timestamp
            self.audio_active = True

            if prev_onset >= 0 and (timestamp - prev_onset) <= 1.65:
                ioi = timestamp - prev_onset
                self.consecutive_onsets += 1
                # Double walking pulse (0.76s-1.35s) into 8-count subdivision if needed
                if 0.76 <= ioi <= 1.35:
                    ioi /= 2.0
                if 0.33 <= ioi <= 0.75:
                    inst_bpm = 60.0 / ioi
                    self.bpm = float(np.clip(0.85 * self.bpm + 0.15 * inst_bpm, 80.0, 160.0))
            else:
                self.consecutive_onsets = 1

            self.onsets.append(timestamp)
            if self._is_playing_unlocked(timestamp):
                # Phase-locked loop nudge toward the nearest integer beat boundary
                frac = ((self.beat_position + 0.5) % 1.0) - 0.5
                self.beat_position = max(0.0, self.beat_position - 0.25 * frac)
            return True

    def tap(self, timestamp: float) -> None:
        """Snap to Beat 1 of the current phrase and refine BPM when tapped on consecutive beats."""
        with self.lock:
            now = time.monotonic() - self.wall_start if self.using_live_stream else timestamp
            if self.taps and (now - self.taps[-1]) > 2.5:
                self.taps.clear()
            self.taps.append(now)
            if len(self.taps) >= 2:
                intervals = [
                    b - a
                    for a, b in zip(self.taps, list(self.taps)[1:], strict=False)
                    if 0.28 <= (b - a) <= 1.5
                ]
                if intervals:
                    med_ioi = median(intervals)
                    self.bpm = float(np.clip(60.0 / med_ioi, 60.0, 180.0))
            self.last_tap_time = now
            self.last_energy_time = now
            self.last_onset = now
            self.consecutive_onsets = max(self.consecutive_onsets, 2)
            # Snap to Beat 1 of the current phrase
            current_phrase_index = int(self.beat_position // 8)
            self.beat_position = float(current_phrase_index * 8)
            self.last_state_time = now
            self.was_playing = True

    def state_at(self, timestamp: float) -> dict[str, Any]:
        """Return current beat (1..8), phrase (1..N), playing status, BPM, and sync source."""
        with self.lock:
            now = time.monotonic() - self.wall_start if self.using_live_stream else timestamp
            playing = self._is_playing_unlocked(now)
            if self.last_state_time is not None and playing and self.was_playing:
                dt = max(0.0, now - self.last_state_time)
                period = 60.0 / self.bpm
                self.beat_position += dt / period
            self.last_state_time = now
            self.was_playing = playing

            beat_index = int(self.beat_position) % 8
            beat = beat_index + 1
            phrase = int(self.beat_position // 8) + 1
            phase = float(self.beat_position - int(self.beat_position))
            if self.mic_permission_denied:
                source = "mic-denied"
            elif not self.require_audio:
                source = "mic-pll" if self.audio_active else ("tap-sync" if self.taps else "clock")
            elif playing:
                source = "mic-live" if self.audio_active else "tap-sync"
            elif self.audio_active:
                source = "mic-paused"
            else:
                source = "mic-waiting"
            return {
                "beat": beat,
                "phrase": phrase,
                "phase": phase,
                "bpm": round(self.bpm, 1),
                "playing": playing,
                "source": source,
            }

    def close(self) -> None:
        if self.stream is not None:
            with suppress(Exception):
                self.stream.stop()
                self.stream.close()
            self.stream = None
