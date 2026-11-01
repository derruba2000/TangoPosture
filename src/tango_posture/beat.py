"""Argentine Tango 8-beat phrase tracker with 30s phrase-start analysis and WAV recording."""

import ctypes
import ctypes.util
import sys
import threading
import time
import wave
from collections import deque
from contextlib import suppress
from pathlib import Path
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


def mux_audio_into_video(video_path: Path, wav_path: Path) -> bool:
    """Embed `wav_path` audio track into `video_path` (.mp4) in-place using macOS AVFoundation."""
    if (
        sys.platform != "darwin"
        or not video_path.is_file()
        or not wav_path.is_file()
        or video_path.suffix.lower() not in (".mp4", ".mov", ".m4v")
    ):
        return False
    tmp_out = video_path.with_name(f".{video_path.stem}_muxed{video_path.suffix}")
    with suppress(OSError):
        tmp_out.unlink()
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "libobjc.dylib")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreMedia.framework/CoreMedia")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        sys_lib = ctypes.cdll.LoadLibrary("/usr/lib/libSystem.B.dylib")

        class CMTime(ctypes.Structure):
            _fields_ = [
                ("value", ctypes.c_int64),
                ("timescale", ctypes.c_int32),
                ("flags", ctypes.c_uint32),
                ("epoch", ctypes.c_int64),
            ]

        class CMTimeRange(ctypes.Structure):
            _fields_ = [("start", CMTime), ("duration", CMTime)]

        class BlockDescriptor(ctypes.Structure):
            _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]

        class BlockLiteral(ctypes.Structure):
            _fields_ = [
                ("isa", ctypes.c_void_p),
                ("flags", ctypes.c_int),
                ("reserved", ctypes.c_int),
                ("invoke", ctypes.c_void_p),
                ("descriptor", ctypes.POINTER(BlockDescriptor)),
            ]

        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        def sel(name: str) -> Any:
            return objc.sel_registerName(name.encode("utf-8"))

        def cls(name: str) -> Any:
            return objc.objc_getClass(name.encode("utf-8"))

        msg_p_c = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p
        )(("objc_msgSend", objc))
        msg_p_p = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(("objc_msgSend", objc))
        msg_p_pp = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )(("objc_msgSend", objc))
        msg_p = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_l = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_p_l = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long
        )(("objc_msgSend", objc))
        msg_p_pl = ctypes.CFUNCTYPE(
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32
        )(("objc_msgSend", objc))
        msg_v_p = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_v_b = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(
            ("objc_msgSend", objc)
        )
        msg_time = ctypes.CFUNCTYPE(CMTime, ctypes.c_void_p, ctypes.c_void_p)(
            ("objc_msgSend", objc)
        )
        msg_insert = ctypes.CFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
            CMTimeRange,
            ctypes.c_void_p,
            CMTime,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))
        msg_scale = ctypes.CFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, CMTimeRange, CMTime
        )(("objc_msgSend", objc))

        def nsstr(s: str) -> Any:
            return msg_p_c(cls("NSString"), sel("stringWithUTF8String:"), s.encode("utf-8"))

        def nsurl(p: Path) -> Any:
            return msg_p_p(cls("NSURL"), sel("fileURLWithPath:"), nsstr(str(p.resolve())))

        v_asset = msg_p_p(cls("AVURLAsset"), sel("assetWithURL:"), nsurl(video_path))
        a_asset = msg_p_p(cls("AVURLAsset"), sel("assetWithURL:"), nsurl(wav_path))
        v_tracks = msg_p_p(v_asset, sel("tracksWithMediaType:"), nsstr("vide"))
        a_tracks = msg_p_p(a_asset, sel("tracksWithMediaType:"), nsstr("soun"))
        if msg_l(v_tracks, sel("count")) < 1 or msg_l(a_tracks, sel("count")) < 1:
            return False

        v_track = msg_p_l(v_tracks, sel("objectAtIndex:"), 0)
        a_track = msg_p_l(a_tracks, sel("objectAtIndex:"), 0)
        v_dur = msg_time(v_asset, sel("duration"))
        a_dur = msg_time(a_asset, sel("duration"))
        if v_dur.value <= 0 or a_dur.value <= 0:
            return False

        comp = msg_p(cls("AVMutableComposition"), sel("composition"))
        zero = CMTime(0, 600, 1, 0)
        cv_track = msg_p_pl(
            comp, sel("addMutableTrackWithMediaType:preferredTrackID:"), nsstr("vide"), 1
        )
        ca_track = msg_p_pl(
            comp, sel("addMutableTrackWithMediaType:preferredTrackID:"), nsstr("soun"), 2
        )
        msg_insert(
            cv_track,
            sel("insertTimeRange:ofTrack:atTime:error:"),
            CMTimeRange(zero, v_dur),
            v_track,
            zero,
            None,
        )
        # Scale video presentation duration to match real-time audio duration
        msg_scale(cv_track, sel("scaleTimeRange:toDuration:"), CMTimeRange(zero, v_dur), a_dur)
        msg_insert(
            ca_track,
            sel("insertTimeRange:ofTrack:atTime:error:"),
            CMTimeRange(zero, a_dur),
            a_track,
            zero,
            None,
        )

        exp = msg_p_pp(
            cls("AVAssetExportSession"),
            sel("exportSessionWithAsset:presetName:"),
            comp,
            nsstr("AVAssetExportPresetHighestQuality"),
        )
        if not exp:
            return False
        msg_v_p(exp, sel("setOutputURL:"), nsurl(tmp_out))
        msg_v_p(exp, sel("setOutputFileType:"), nsstr("public.mpeg-4"))
        msg_v_b(exp, sel("setShouldOptimizeForNetworkUse:"), True)

        cb_type = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
        cb_fn = cb_type(lambda _b: None)
        desc = BlockDescriptor(0, ctypes.sizeof(BlockLiteral))
        blk = BlockLiteral(
            ctypes.cast(sys_lib._NSConcreteGlobalBlock, ctypes.c_void_p),
            1 << 28,
            0,
            ctypes.cast(cb_fn, ctypes.c_void_p),
            ctypes.pointer(desc),
        )
        msg_v_p(exp, sel("exportAsynchronouslyWithCompletionHandler:"), ctypes.byref(blk))
        deadline = time.monotonic() + 30.0
        while msg_l(exp, sel("status")) in (0, 1, 2) and time.monotonic() < deadline:
            time.sleep(0.02)
        if msg_l(exp, sel("status")) == 3 and tmp_out.is_file() and tmp_out.stat().st_size > 0:
            tmp_out.replace(video_path)
            return True
    except (OSError, AttributeError):
        pass
    with suppress(OSError):
        tmp_out.unlink()
    return False


def _zscore(x: np.ndarray) -> np.ndarray:
    if len(x) == 0:
        return x
    std = float(np.std(x))
    if std < 1e-8:
        return np.zeros_like(x)
    return (x - float(np.mean(x))) / std


class BeatTracker:
    """
    Records microphone audio, analyzes the first `analysis_seconds` (default 30s) of active music
    to identify both the tempo (BPM) and the exact beginning of each 8-beat tango phrase (Beat 1),
    then tracks Beat 1..8 -> 1..8 and pauses immediately when the music stops.
    """

    def __init__(
        self,
        bpm: float = 120.0,
        enable_audio: bool = False,
        require_audio: bool | None = None,
        analysis_seconds: float = 30.0,
        audio_save_path: Path | None = None,
    ):
        self.lock = threading.Lock()
        self.initial_bpm = float(np.clip(bpm, 60.0, 180.0))
        self.bpm = self.initial_bpm
        self.require_audio = enable_audio if require_audio is None else require_audio
        self.analysis_seconds = max(4.0, float(analysis_seconds))
        self.audio_save_path = audio_save_path
        self.saved_audio_files: list[Path] = []

        # Continuous 16 kHz audio + 100 Hz (10 ms hop) multi-band feature buffers
        self.sample_rate = 16000
        self.n_fft = 512
        self.hop = 160  # 10 ms per feature frame -> 100 Hz envelope rate
        self.frame_rate = self.sample_rate / self.hop  # 100.0 Hz
        self.blocksize = 1024
        self.stft_window = np.hanning(self.n_fft).astype(np.float32)
        freqs = np.fft.rfftfreq(self.n_fft, 1.0 / self.sample_rate)
        self.bass_mask = (freqs >= 55.0) & (freqs <= 320.0)
        self.mid_mask = (freqs > 320.0) & (freqs <= 2600.0)
        band_edges = [(55, 130), (130, 260), (260, 520), (520, 1040), (1040, 2080), (2080, 4000)]
        self.subband_masks = [(freqs >= lo) & (freqs < hi) for lo, hi in band_edges]

        self.recorded_chunks: list[np.ndarray] = []
        self.staging_audio = np.zeros(0, dtype=np.float32)
        self.prev_mag: np.ndarray | None = None

        # 100 Hz feature time series for active music
        self.feat_rms: list[float] = []
        self.feat_bass_flux: list[float] = []
        self.feat_mid_flux: list[float] = []
        self.feat_subbands: list[np.ndarray] = []
        self.feat_times: list[float] = []

        # Phrase model state (in 100 Hz active-music frame coordinates)
        self.tau_frames: float = (60.0 / self.bpm) * self.frame_rate
        self.phrase_anchor_frame: float = 0.0  # Frame index where Beat 1 (Phrase 1) starts
        self.selected_m: int | None = None
        self.phrase_locked = False
        self.phrase_confidence = 0.0
        self.last_analysis_frames = 0

        # Real-time playback & pause tracking
        self.beat_position = 0.0
        self.last_state_time: float | None = None
        self.was_playing = False
        self.taps: deque[float] = deque(maxlen=6)
        self.onsets: deque[float] = deque(maxlen=16)
        self.flux_history: deque[float] = deque(maxlen=60)
        self.rms_history: deque[float] = deque(maxlen=80)
        self.last_onset = -10.0
        self.last_energy_time = -10.0
        self.last_tap_time = -10.0
        self.consecutive_onsets = 0
        self.audio_active = False
        self.mic_permission_denied = False
        self.using_live_stream = False
        self.stream: Any = None
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
        if frames <= 0:
            return
        mono = indata[:, 0].copy()
        now = time.monotonic() - self.wall_start
        self.feed_samples(mono, now)

    def feed_samples(self, samples: np.ndarray, timestamp: float) -> None:
        """Record raw mono audio samples and extract 100 Hz phrase-analysis features."""
        mono = np.asarray(samples, dtype=np.float32).ravel()
        if len(mono) == 0:
            return
        with self.lock:
            self.recorded_chunks.append(mono)
            self.staging_audio = np.concatenate([self.staging_audio, mono])

            n_avail = len(self.staging_audio)
            if n_avail < self.n_fft:
                return
            num_hops = 1 + (n_avail - self.n_fft) // self.hop
            block_bass_flux = 0.0
            block_mid_flux = 0.0
            block_rms = 0.0

            for h in range(num_hops):
                start = h * self.hop
                frame = self.staging_audio[start : start + self.n_fft]
                frame_time = timestamp - (num_hops - 1 - h) * (self.hop / self.sample_rate)
                rms = float(np.sqrt(np.mean(frame * frame)))
                block_rms = max(block_rms, rms)

                mag = np.abs(np.fft.rfft(frame * self.stft_window))
                if self.prev_mag is None:
                    self.prev_mag = mag
                    bass_fl = 0.0
                    mid_fl = 0.0
                else:
                    # Log-compressed spectral flux emphasizes rhythmic note attacks over vibrato
                    log_curr = np.log1p(20.0 * mag)
                    log_prev = np.log1p(20.0 * self.prev_mag)
                    diff = np.maximum(0.0, log_curr - log_prev)
                    self.prev_mag = mag
                    bass_fl = float(np.sum(diff[self.bass_mask]))
                    mid_fl = float(np.sum(diff[self.mid_mask]))

                block_bass_flux = max(block_bass_flux, bass_fl)
                block_mid_flux = max(block_mid_flux, mid_fl)

                # Check if this 10 ms frame has active music energy above ambient noise floor
                self.rms_history.append(rms)
                noise_floor = (
                    float(np.clip(np.percentile(self.rms_history, 15), 1.5e-4, 0.008))
                    if len(self.rms_history) >= 50
                    else 5e-4
                )
                frame_has_sound = rms > max(0.0012, noise_floor * 1.8)
                if frame_has_sound:
                    self.last_energy_time = frame_time

                # Keep 100.0 Hz time axis contiguous across staccato gaps / respiros while music plays
                music_window_active = frame_has_sound or (
                    self.last_energy_time >= 0 and (frame_time - self.last_energy_time) <= 0.45
                )
                if music_window_active:
                    sub = np.array(
                        [
                            float(np.log1p(np.mean(mag[mask]))) if np.any(mask) else 0.0
                            for mask in self.subband_masks
                        ],
                        dtype=np.float32,
                    )
                    norm = float(np.linalg.norm(sub))
                    if norm > 1e-6:
                        sub /= norm
                    self.feat_rms.append(rms)
                    self.feat_bass_flux.append(bass_fl)
                    self.feat_mid_flux.append(mid_fl)
                    self.feat_subbands.append(sub)
                    self.feat_times.append(frame_time)

            self.staging_audio = self.staging_audio[num_hops * self.hop :]
            self._update_onset_state_unlocked(
                block_bass_flux + 0.6 * block_mid_flux, timestamp, block_rms
            )
            self._maybe_analyze_phrases_unlocked()

    def _update_onset_state_unlocked(self, flux: float, timestamp: float, rms: float) -> bool:
        noise_floor = (
            float(np.clip(np.percentile(self.rms_history, 15), 1.5e-4, 0.012))
            if len(self.rms_history) >= 10
            else 6e-4
        )
        has_energy = rms > max(0.0010, noise_floor * 1.75)
        if has_energy:
            self.last_energy_time = timestamp

        self.flux_history.append(flux)
        if len(self.flux_history) < 8 or not has_energy:
            return False

        arr = np.array(self.flux_history, dtype=float)
        threshold = float(np.mean(arr) + 1.25 * np.std(arr))
        if flux <= threshold or flux <= 1e-3 or (timestamp - self.last_onset) < 0.26:
            return False

        prev_onset = self.last_onset
        self.last_onset = timestamp
        self.audio_active = True
        if prev_onset >= 0 and (timestamp - prev_onset) <= 1.75:
            self.consecutive_onsets += 1
            ioi = timestamp - prev_onset
            if 0.76 <= ioi <= 1.35:
                ioi /= 2.0
            if 0.33 <= ioi <= 0.75 and len(self.feat_rms) < int(4.0 * self.frame_rate):
                inst_bpm = 60.0 / ioi
                self.bpm = float(np.clip(0.85 * self.bpm + 0.15 * inst_bpm, 80.0, 160.0))
                self.tau_frames = (60.0 / self.bpm) * self.frame_rate
        else:
            self.consecutive_onsets = 1
        self.onsets.append(timestamp)
        return True

    def _maybe_analyze_phrases_unlocked(self) -> None:
        """
        During the first `analysis_seconds` (30s) of active music, analyze the recorded
        multi-band envelope to lock onto both the tempo and Beat 1 of each 8-beat phrase.
        """
        n_frames = len(self.feat_rms)
        target_frames = int(self.analysis_seconds * self.frame_rate)
        min_frames = int(4.0 * self.frame_rate)  # Need at least 1 phrase (4s) to start analysis
        if n_frames < min_frames:
            return
        if self.phrase_locked:
            return
        # Re-evaluate every 1.0s during the 30s window, and one final time when reaching 30s
        if (n_frames - self.last_analysis_frames) < int(
            1.0 * self.frame_rate
        ) and n_frames < target_frames:
            return

        self.last_analysis_frames = n_frames
        window_len = min(n_frames, target_frames)
        self._analyze_phrase_structure_unlocked(window_len)
        if n_frames >= target_frames:
            self.phrase_locked = True

    def _analyze_phrase_structure_unlocked(self, window_len: int) -> None:
        bass = np.array(self.feat_bass_flux[:window_len], dtype=np.float64)
        mid = np.array(self.feat_mid_flux[:window_len], dtype=np.float64)
        rms = np.array(self.feat_rms[:window_len], dtype=np.float64)
        subbands = np.stack(self.feat_subbands[:window_len], axis=0)

        p90_b = float(np.percentile(bass, 90)) or 1.0
        p90_m = float(np.percentile(mid, 90)) or 1.0
        bass_norm = bass / p90_b
        mid_norm = mid / p90_m
        raw_env = 0.62 * bass_norm + 0.38 * mid_norm

        # Subtract 0.40s local moving average and smooth with a 50ms Hann kernel
        kernel_ma = np.ones(41, dtype=np.float64) / 41.0
        local_mean = np.convolve(raw_env, kernel_ma, mode="same")
        onset_env = np.maximum(0.0, raw_env - local_mean)
        smooth_k = np.array([0.25, 0.5, 1.0, 0.5, 0.25], dtype=np.float64)
        smooth_k /= np.sum(smooth_k)
        onset_env = np.convolve(onset_env, smooth_k, mode="same")

        # Step 1: Global Tempo via Phrase-Harmonic Comb Autocorrelation (1x, 2x, 4x, 8x beat period)
        n_fft_acf = 1 << (2 * window_len - 1).bit_length()
        spec = np.fft.rfft(onset_env, n=n_fft_acf)
        acf = np.fft.irfft(spec * np.conj(spec), n=n_fft_acf)[:window_len]
        if acf[0] > 0:
            acf /= acf[0]

        # Candidate beat periods: 96 BPM (62.5 frames) to 148 BPM (40.5 frames)
        candidate_taus = np.arange(40.5, 62.75, 0.25)
        lags = np.arange(window_len, dtype=np.float64)
        best_tau = self.tau_frames
        best_comb = -1e9
        for tau in candidate_taus:
            cand_bpm = 6000.0 / float(tau)
            # Soft prior centered around configured tango walking BPM (default 120 BPM)
            prior = float(np.exp(-0.5 * ((cand_bpm - self.initial_bpm) / 24.0) ** 2))
            score = float(np.interp(tau, lags, acf))
            if 2.0 * tau < window_len - 10:
                score += 0.85 * float(np.interp(2.0 * tau, lags, acf))
            if 4.0 * tau < window_len - 10:
                score += 0.75 * float(np.interp(4.0 * tau, lags, acf))
            if 8.0 * tau < window_len - 10:
                score += 0.95 * float(np.interp(8.0 * tau, lags, acf))
            score *= 0.65 + 0.35 * prior
            if score > best_comb:
                best_comb = score
                best_tau = float(tau)

        self.tau_frames = best_tau
        self.bpm = float(np.clip(6000.0 / best_tau, 80.0, 160.0))

        # Step 2: Beat Grid Phase Alignment phi in [0, best_tau)
        candidate_phis = np.arange(0.0, best_tau, 0.5)
        best_phi = 0.0
        best_phi_score = -1e9
        combined_pulse = onset_env + 0.50 * bass_norm
        for phi in candidate_phis:
            beat_indices = np.round(np.arange(phi, window_len - 3, best_tau)).astype(int)
            if len(beat_indices) == 0:
                continue
            # Local max within +-20ms of each grid point
            vals = np.maximum.reduce(
                [combined_pulse[np.clip(beat_indices + d, 0, window_len - 1)] for d in (-2, -1, 0, 1, 2)]
            )
            sc = float(np.mean(vals))
            if sc > best_phi_score:
                best_phi_score = sc
                best_phi = float(phi)

        # Step 3: Identify which of the 8 beats m in {0..7} is BEAT 1 (Phrase Start)
        beat_frames = np.round(np.arange(best_phi, window_len - 3, best_tau)).astype(int)
        num_beats = len(beat_frames)
        if num_beats < 8:
            self.phrase_anchor_frame = best_phi
            return

        attack = np.zeros(num_beats, dtype=np.float64)
        cadence_jump = np.zeros(num_beats, dtype=np.float64)
        novelty = np.zeros(num_beats, dtype=np.float64)
        tau_i = max(4, round(best_tau))

        for k, bf in enumerate(beat_frames):
            lo = max(0, bf - 3)
            hi = min(window_len, bf + 4)
            attack[k] = float(np.max(onset_env[lo:hi] + 0.65 * bass_norm[lo:hi]))

            # Post-cadence jump: strong attack on Beat 1 after weaker Beat 8 / phrase breath
            prev_att = attack[k - 1] if k > 0 else 0.0
            tail_lo = max(0, bf - int(0.6 * tau_i))
            tail_rms = float(np.mean(rms[tail_lo:bf])) if bf > tail_lo else 0.0
            head_hi = min(window_len, bf + int(0.8 * tau_i))
            head_rms = float(np.mean(rms[bf:head_hi])) if head_hi > bf else 0.0
            cadence_jump[k] = (attack[k] - 0.65 * prev_att) + 2.0 * max(0.0, head_rms - tail_rms)

            # Harmonic & orchestration novelty across 4-beat span before vs after beat k
            span = min(4 * tau_i, bf, window_len - bf)
            if span >= tau_i:
                before_vec = np.mean(subbands[bf - span : bf], axis=0)
                after_vec = np.mean(subbands[bf : bf + span], axis=0)
                novelty[k] = float(np.linalg.norm(after_vec - before_vec))

        z_att = _zscore(attack)
        z_jmp = _zscore(cadence_jump)
        z_nov = _zscore(novelty)

        # Find the first prominent beat in the recording (music entrance cue)
        significant = np.where(attack >= 0.45 * float(np.max(attack)))[0]
        first_sig_beat = int(significant[0]) if len(significant) > 0 else 0

        best_m = 0
        best_m_score = -1e9
        scores = np.zeros(8, dtype=np.float64)
        for m in range(8):
            idx_1 = np.arange(m, num_beats, 8)
            idx_5 = np.arange((m + 4) % 8, num_beats, 8)
            idx_37 = np.sort(
                np.concatenate(
                    [np.arange((m + 2) % 8, num_beats, 8), np.arange((m + 6) % 8, num_beats, 8)]
                )
            )
            idx_8 = np.arange((m + 7) % 8, num_beats, 8)

            s1 = float(np.mean(1.30 * z_att[idx_1] + 1.60 * z_jmp[idx_1] + 1.85 * z_nov[idx_1]))
            s5 = (
                float(np.mean(0.55 * z_att[idx_5] + 0.45 * z_nov[idx_5]))
                if len(idx_5) > 0
                else 0.0
            )
            s37 = float(np.mean(0.30 * z_att[idx_37])) if len(idx_37) > 0 else 0.0
            s8 = float(np.mean(0.55 * z_att[idx_8])) if len(idx_8) > 0 else 0.0

            entrance_bonus = 0.45 if (first_sig_beat % 8) == m else 0.0
            stability_bonus = 0.20 if self.selected_m == m else 0.0
            total = s1 + s5 + s37 - s8 + entrance_bonus + stability_bonus
            scores[m] = total
            if total > best_m_score:
                best_m_score = total
                best_m = m

        self.selected_m = best_m
        self.phrase_anchor_frame = float((best_phi + best_m * best_tau) % (8.0 * best_tau))
        sorted_scores = np.sort(scores)
        self.phrase_confidence = float(
            np.clip((sorted_scores[-1] - sorted_scores[-2]) / 2.0, 0.1, 1.0)
        )

    def _is_playing_unlocked(self, now: float) -> bool:
        if not self.require_audio:
            return True
        if (now - self.last_tap_time) <= 1.2:
            return True
        energy_recent = (now - self.last_energy_time) <= 0.38
        onset_recent = (now - self.last_onset) <= 1.80
        if not energy_recent or not onset_recent:
            if (now - self.last_energy_time) > 0.38 or (now - self.last_onset) > 1.80:
                self.consecutive_onsets = 0
            return False
        return self.consecutive_onsets >= 2 or len(self.feat_rms) >= int(2.0 * self.frame_rate)

    def process_flux(self, flux: float, timestamp: float, rms: float | None = None) -> bool:
        """Process one synthetic or external spectral-flux observation."""
        with self.lock:
            effective_rms = (flux * 0.05) if rms is None else rms
            self.rms_history.append(effective_rms)
            detected = self._update_onset_state_unlocked(flux, timestamp, effective_rms)
            if detected and self._is_playing_unlocked(timestamp):
                frac = ((self.beat_position + 0.5) % 1.0) - 0.5
                self.beat_position = max(0.0, self.beat_position - 0.25 * frac)
            return detected

    def tap(self, timestamp: float) -> None:
        """Snap Beat 1 to `timestamp` and lock the phrase start immediately."""
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
                    self.tau_frames = (60.0 / self.bpm) * self.frame_rate
            self.last_tap_time = now
            self.last_energy_time = now
            self.last_onset = now
            self.consecutive_onsets = max(self.consecutive_onsets, 2)
            if len(self.feat_rms) > 0:
                self.phrase_anchor_frame = float(len(self.feat_rms))
                self.phrase_locked = True
            current_phrase_index = int(self.beat_position // 8)
            self.beat_position = float(current_phrase_index * 8)
            self.last_state_time = now
            self.was_playing = True

    def state_at(self, timestamp: float) -> dict[str, Any]:
        """Return current beat (1..8), phrase (1..N), analysis progress, playing status, and BPM."""
        with self.lock:
            now = time.monotonic() - self.wall_start if self.using_live_stream else timestamp
            playing = self._is_playing_unlocked(now)
            analyzed_sec = min(self.analysis_seconds, len(self.feat_rms) / self.frame_rate)

            if len(self.feat_rms) >= int(4.0 * self.frame_rate) and self.tau_frames > 0:
                # Driven directly by the phrase-start model anchored on Beat 1
                current_frame = float(len(self.feat_rms))
                if playing and self.feat_times:
                    extra_dt = max(0.0, min(now - self.feat_times[-1], 0.15))
                    current_frame += extra_dt * self.frame_rate
                rel_beats = (current_frame - self.phrase_anchor_frame) / self.tau_frames
                self.beat_position = max(0.0, rel_beats)
                beat_index = int(np.floor(rel_beats)) % 8
                beat = beat_index + 1
                phrase = max(1, int(np.floor(rel_beats / 8.0)) + 1)
                phase = float(rel_beats - np.floor(rel_beats))
            else:
                if self.last_state_time is not None and playing and self.was_playing:
                    dt = max(0.0, now - self.last_state_time)
                    period = 60.0 / self.bpm
                    self.beat_position += dt / period
                beat_index = int(self.beat_position) % 8
                beat = beat_index + 1
                phrase = int(self.beat_position // 8) + 1
                phase = float(self.beat_position - int(self.beat_position))

            self.last_state_time = now
            self.was_playing = playing

            if self.mic_permission_denied:
                source = "mic-denied"
            elif not self.require_audio:
                source = "mic-pll" if self.audio_active else ("tap-sync" if self.taps else "clock")
            elif not playing:
                source = "mic-paused" if self.audio_active else "mic-waiting"
            elif self.phrase_locked:
                source = "phrase-locked"
            else:
                source = f"analyzing-{int(analyzed_sec)}/{int(self.analysis_seconds)}s"

            return {
                "beat": beat,
                "phrase": phrase,
                "phase": phase,
                "bpm": round(self.bpm, 1),
                "playing": playing,
                "analyzing": self.require_audio and not self.phrase_locked and playing,
                "analyzed_seconds": round(analyzed_sec, 1),
                "analysis_target_seconds": round(self.analysis_seconds, 1),
                "phrase_locked": self.phrase_locked,
                "source": source,
            }

    def save_audio(self, output_path: Path | None = None) -> list[Path]:
        """Write recorded microphone audio to 16-bit PCM WAV inside `videos/`."""
        with self.lock:
            if not self.recorded_chunks:
                return []
            full_audio = np.concatenate(self.recorded_chunks)
        if len(full_audio) == 0:
            return []

        peak = float(np.max(np.abs(full_audio)))
        if peak > 1.0:
            full_audio = full_audio / peak
        pcm16 = np.clip(full_audio * 32767.0, -32768, 32767).astype(np.int16)

        videos_dir = Path("videos")
        videos_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        primary = output_path or self.audio_save_path or (videos_dir / f"audio_{stamp}.wav")
        if not primary.is_absolute() and primary.parts[:1] != ("videos",):
            primary = videos_dir / primary
        primary.parent.mkdir(parents=True, exist_ok=True)
        latest = primary.parent / "latest_audio.wav"

        written: list[Path] = []
        for target in (primary, latest):
            with wave.open(str(target), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.sample_rate)
                wf.writeframes(pcm16.tobytes())
            if target not in written:
                written.append(target)
        self.saved_audio_files = written
        return written

    def mux_into_videos(self, video_paths: list[Path]) -> list[Path]:
        """Mux recorded microphone audio directly into recorded MP4 video/overlay files."""
        valid_videos = [p for p in video_paths if p is not None and p.is_file()]
        if not valid_videos or not self.recorded_chunks:
            return []
        tmp_wav = valid_videos[0].parent / ".tmp_session_audio.wav"
        with self.lock:
            full_audio = np.concatenate(self.recorded_chunks)
        if len(full_audio) == 0:
            return []
        peak = float(np.max(np.abs(full_audio)))
        if peak > 1.0:
            full_audio = full_audio / peak
        pcm16 = np.clip(full_audio * 32767.0, -32768, 32767).astype(np.int16)
        tmp_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(tmp_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(pcm16.tobytes())
        muxed: list[Path] = []
        try:
            for vpath in valid_videos:
                if mux_audio_into_video(vpath, tmp_wav):
                    muxed.append(vpath)
        finally:
            with suppress(OSError):
                tmp_wav.unlink()
        return muxed

    def close(self) -> None:
        if self.stream is not None:
            with suppress(Exception):
                self.stream.stop()
                self.stream.close()
            self.stream = None
