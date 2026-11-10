# Manual and real-video validation

Status on 1 November 2026: synthetic and local runtime checks executed; **real dancer video, webcam, and teacher validation are pending**. No suitable rights-cleared personal video was supplied, and this implementation did not activate the user's webcam unattended. The available runtime identifies Apple M2 Pro / macOS 26.6.2 arm64. This is not evidence of successful camera access.

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
  synthetic and GUI checks; a graphical display is needed for Tk tests.
- Run `uv run --frozen mypy src` and
  `uv run --frozen mypy --platform win32 src` to check both platform-specific
  type paths locally. The latter is not a Windows runtime test; the CI Windows
  job remains necessary.
- Pure synthetic geometry/timing, simulated lifecycle and storage tests: see implementation report.
- Verified model loaded with CPU delegate and processed blank generated frames and a short locally encoded blank video.
- Blank-video run used blocked Python socket/URL calls. It produced UNKNOWN observations, completed exports, and released capture/detector resources.
- No human pose, false-alert rate, missed-deviation rate, webcam latency, or teacher suitability result is available. `docs/runtime_smoke.json` reports a blank-video runtime check only; it must not be advertised as dancer performance.
