# Manual and real-video validation

Status on 12 November 2026: synthetic and local runtime checks executed; **real dancer video, webcam, and teacher validation are pending**. No suitable rights-cleared personal video was supplied, and this implementation did not activate the user's webcam unattended. The available runtime identifies Apple M2 Pro / macOS 26.6.2 arm64. This is not evidence of successful camera access.

## Reproducible user-supplied fixture

Record a video you own or have explicit permission to analyse. Do not redistribute or commit it. Use 640×480 initially, fixed level camera, whole body, adequate light, one person. Note source SHA-256 privately to identify repeated runs. Record a 12-second stable reference followed by at least 20 seconds of side-on standing and walking. Maintain the selected facing direction.

```bash
uv sync --frozen
uv run tango-posture models fetch
# Disconnect the network now. These commands use only the installed model.
uv run python scripts/smoke_video.py ./practice.mp4 --side left --facing right \
  --output ./data/validation/real-video-smoke.json
uv run tango-posture --data-dir ./data/validation run --video ./practice.mp4 \
  --side left --facing right --reference-interval 0 12 --headless
```

The smoke report contains valid frame coverage, abstention reasons, model/runtime versions, resolution, FPS, and median/p95 decode-to-measurement latency. File throughput is not capture-to-overlay latency. The session export reports duration-based coverage and events. Run twice with the same fixture/config; compare numeric observations/events at absolute tolerance `1e-9` for the pure pipeline, and `1e-6` for same-host real-model repeated inference. UUIDs, UTC times and runtime timing are expected to differ. Cross-platform model output reproducibility is not asserted.

If the calibration fails due to insufficient reliable far-shoulder/hip evidence, record that failure and its coverage. Do not lower gates just to make the demo pass. Side-on confidence coverage is a key unresolved empirical limitation.

## Webcam protocol

Run `uv run tango-posture run --camera 0 --side left --facing right`, grant OS permission if required, calibrate and explicitly accept, then press S. Record hardware/CPU, OS, camera, requested and actual resolution, model hash, active duration, processed FPS, median/p95 capture-to-overlay submission latency, and dropped frames from the export. Compare against targets ≥15 FPS and median <200 ms; do not treat these as guarantees. Check Q under slow inference, E, pause/resume, window close, Ctrl+C, device unplug/disconnect and permission denial. Confirm the camera indicator turns off and the final session status/exports agree.

## Scenarios and annotation worksheet

Repeat for both visible anatomical sides and both screen-facing directions. Test mirror display and verify unchanged measurements. Include fitted and loose clothing where possible.

| Scenario | Expected observation | Coverage / false alerts / misses / notes |
| --- | --- | --- |
| Stable reference stance | Reference-relative values, no sustained alerts | Pending |
| Side-on solo walking | Supported when view and confidence remain adequate | Pending |
| Deliberate forward head displacement | Persistent positive head-forward delta may trigger | Pending |
| Changed ear–shoulder segment angle | Persistent head-inclination change may trigger | Pending |
| Changed trunk angle | Persistent trunk-inclination change may trigger | Pending |
| Excursion shorter than 1.5 seconds | No confirmed event | Pending |
| Turn toward camera | Abstain if width heuristic detects change | Pending |
| Leave frame / obscure selected ear | UNKNOWN for affected metrics; event closes | Pending |
| Poor light / loose clothing | Record confidence coverage and abstentions | Pending |
| Second person enters | Suspend when two poses found; log misses | Pending |
| Camera moved | Recalibrate; metadata cannot detect physical movement | Pending |
| Complete turn | Unsupported; cannot reliably detect from skeleton | Pending |

Ask a tango teacher to annotate the intended reference's suitability and visible deviations independently, before inspecting app feedback. Record clip time ranges, whether the change is observable from the selected camera, false alerts, missed observable deviations, abstentions, and agreement/disagreement. The teacher-review flag records only explicit review metadata. No teacher review or usefulness for technique has been established.

## Executed evidence

- Desktop CI checks include recording start/stop and song-delay transitions, recovery
  files after archive-save failures, headless recording archives, calibration retry
  archives with matching video frame counts, and replay overlay controls with the
  import form open on a small window. Run `uv run --frozen pytest -q` for the
  synthetic and GUI checks. Qt widget tests use the offscreen platform; Tk is not required.
- Run `uv run --frozen mypy src` and
  `uv run --frozen mypy --platform win32 src` to check both platform-specific
  type paths locally. The latter is not a Windows runtime test; the CI Windows
  job remains necessary.
- Windows portability regressions cover zero-duration timing samples (reported
  as unavailable FPS, not a division error), non-cascade menu entries, explicit
  UTF-8 manual decoding, and serialized replay extraction/archive replacement.
  macOS chart copying is exercised with a mocked executable rather than assuming
  a macOS system tool exists on every test runner.
- Archive writes retry Windows access/sharing errors for at most 0.775 seconds
  before surfacing a save failure. Regression tests inject transient and
  persistent failures, check that both segments retain their analysis, and verify
  that a failed replacement preserves the previous archive.
- Qt Ollama regressions use a local protocol fixture and bounded event pumping
  to verify streaming, cancellation, and saved results. Thread exceptions must
  not be treated as harmless warnings.
- Pure synthetic geometry/timing, simulated lifecycle and storage tests: see implementation report.
- Verified model loaded with CPU delegate and processed blank generated frames and a short locally encoded blank video.
- Blank-video run used blocked Python socket/URL calls. It produced UNKNOWN observations, completed exports, and released capture/detector resources.
- No human pose, false-alert rate, missed-deviation rate, webcam latency, or teacher suitability result is available. `docs/runtime_smoke.json` reports a blank-video runtime check only; it must not be advertised as dancer performance.

## Qt desktop acceptance checklist

Run `uv run tango-posture qt` from the repository root. This checklist is
**pending manual execution**; offscreen widget tests do not establish native
window-manager, accessibility, camera, media, or teacher-acceptance behavior.

| Area | Manual check | Evidence to record |
| --- | --- | --- |
| Layout | Complete the Home → Practice → References → Library → Review → Analytics → AI Coach → Settings → Help flows at 1280×800 and 1024×700; repeat at 125% and 150% display scaling. | OS, display scale, screenshots, clipped/overlapping controls |
| Keyboard and focus | Navigate without a mouse, verify visible focus and shortcut discoverability, and confirm navigation shortcuts do not interrupt text editing. | Steps attempted, focus/contrast issues, shortcut results |
| Practice lifecycle | On a real camera, calibrate and explicitly accept a reference; verify scoring and recording are independent, pause affects scoring only, and stop/save, abort, and quit preserve the intended segments. | Camera/model versions, session/export status, saved segment count |
| Failure recovery | Deny camera permission, disconnect the device, and close/cancel during capture; confirm actionable errors, released camera indicator, and preserved completed data. | Failure step, displayed message, device/resource state |
| Library and imports | Rename `.tp`/`.tpc` entries and restart to verify persistence; cancel deletion, then confirm Trash deletion on a disposable fixture. Import local and permitted YouTube/Facebook Reel clips only through Library, cancel during download/analysis/packaging, and check invalid URLs, missing yt-dlp and network failures. Migrate legacy fixtures twice and verify originals remain and failures are reported. | Names after restart, Trash recovery, source hashes, indexed archive members, cancellation/shutdown recovery, migration counts/errors |
| References and profiles | Import a permitted video as `.tpr`, promote a selected `.tp` recording and excerpt, save dancer selections, and create two independently named `.tpc` profiles from different passages. Include tracking loss and couple crossings; verify ambiguous sections remain unknown until an explicit same-dancer restart. Edit example passages independently of target limits. | Source hashes unchanged, retained originals, cached-inference reuse, histogram/timeline coverage and selected dancer |
| Guided range calibration | Preview before recording, complete or retry/skip neutral/change/return/repeat holds, and finish a walking drill. Confirm both video and camera reach the same approval screen. Check a video profile with the camera; enable only responsive, explicitly approved metrics. Remove the source video and verify the checked profile still scores offline; change camera/model settings and verify rejection or unknowns. | Spoken/visible prompt timing, camera release, repeat failures, per-metric gates, source independence, view-change abstentions; all pending real-camera/teacher validation |
| Review and analytics | Verify both archive pickers offer only Library `.tp`/`.tpc` entries and no comparison-stream combobox exists. Analyze a raw recording, reopen its `.tp`, and verify restored skeleton, metrics, alerts, pivot rings and steps; compare floor-lines against the selected Library archive. Check two recordings retain separate `analytics_00.json`/`analytics_01.json`. Copy the archive to an environment without its database and verify Analytics charts/events still load; missing samples remain gaps and uncalibrated measurements have no reference coverage. Seek reports/events and confirm the inspector does not reduce the video area. | Archive-only selection, portable analytics, independent recording indices, frame/time offsets, comparison synchronization, cancellation results, video-stage dimensions |
| Local AI | With Ollama absent, confirm status is explicit; with a local model, explicitly start, observe streaming, cancel, and retry analysis. Change the report on disk during generation and verify the stale result is rejected. | Endpoint locality, model, cancellation result, source report/provenance hash |
| Installation | Install from clean macOS ARM and Windows x64 environments; check model checksum, first launch, permissions, offline operation after setup, and uninstall data retention. | OS/build/checksum, first-launch result, camera/mic result |

Do not mark an area passed from a simulated worker, blank-video smoke test, or
offscreen Qt test. Attach reproducible environment details and distinguish
automated evidence from physical-device observations.
