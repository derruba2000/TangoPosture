import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .calibration import load_calibration_file
from .config import load
from .display import PracticeDisplay
from .models import fetch
from .storage import Storage


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Local side-view movement observation relative to your reference"
    )
    p.add_argument("--config", type=Path, help="Validated TOML configuration")
    p.add_argument("--data-dir", help="Local SQLite and exports directory")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("gui", help="Open the graphical practice setup and controls").add_argument(
        "--ignore-env",
        action="store_true",
        help="Open with defaults/TOML to migrate an old .env through Settings → Import",
    )
    models = sub.add_parser("models")
    models.add_subparsers(dest="action", required=True).add_parser("fetch").add_argument(
        "--output", type=Path, help="Model destination; explicit setup-only network operation"
    )
    run = sub.add_parser("run")
    source = run.add_mutually_exclusive_group()
    source.add_argument("--camera", type=int)
    source.add_argument("--video")
    run.add_argument("--side", choices=["left", "right"])
    run.add_argument("--facing", choices=["left", "right"])
    run.add_argument("--model-path")
    run.add_argument("--capture-profile")
    run.add_argument("--mirror", action="store_true", default=None)
    run.add_argument("--calibration", help="Saved reference UUID")
    run.add_argument(
        "--confirm-camera-unchanged",
        action="store_true",
        help="Explicitly confirm physical placement unchanged when reusing a reference",
    )
    run.add_argument("--headless", action="store_true")
    run.add_argument(
        "--reference-interval",
        nargs=2,
        type=float,
        metavar=("START", "END"),
        help="Explicitly accept a stable reference interval; must match configured 12s duration",
    )
    run.add_argument("--reference-name", default="My reference")
    run.add_argument("--practice-name", help="Practice archive name; existing names append recordings")
    run.add_argument(
        "--teacher-reviewed", metavar="NOTE", help="Explicit user-recorded teacher review note"
    )
    run.add_argument(
        "--record-video",
        nargs="?",
        const=Path("videos/practice.mp4"),
        type=Path,
        metavar="PATH",
        help="Record raw and overlay video into a .tp practice archive; PATH is a legacy option",
    )
    run.add_argument(
        "--record-overlay",
        nargs="?",
        const=Path("videos/overlay.mp4"),
        type=Path,
        metavar="PATH",
        help="Record raw and overlay video into a .tp practice archive; PATH is a legacy option",
    )
    sessions = sub.add_parser("sessions").add_subparsers(dest="action", required=True)
    sessions.add_parser("list")
    export = sessions.add_parser("export")
    export.add_argument("session_id")
    export.add_argument("--output", required=True, type=Path)
    sub.add_parser("calibrations").add_subparsers(dest="action", required=True).add_parser("list")
    return p


def main(argv: list[str] | None = None, *, display: PracticeDisplay | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(arguments or ["gui"])
    storage = None
    try:
        overrides = {
            k: getattr(args, k, None)
            for k in (
                "data_dir",
                "camera",
                "side",
                "facing",
                "model_path",
                "capture_profile",
                "mirror",
            )
        }
        cfg = load(
            args.config,
            overrides,
            use_env=display is None and not (args.command == "gui" and args.ignore_env),
        )
        if args.command == "gui":
            from .gui import launch

            return launch(cfg)
        if args.command == "models":
            destination = args.output or Path(cfg.model_path)
            fetch(destination)
            print(f"Verified model installed: {destination}")
            return 0
        storage = Storage(Path(cfg.data_dir))
        if args.command in ("sessions", "calibrations"):
            if args.action == "list":
                for row in storage.list(args.command):
                    print(
                        json.dumps(
                            {
                                k: v
                                for k, v in row.items()
                                if k
                                in (
                                    "id",
                                    "name",
                                    "created_at_utc",
                                    "start_utc",
                                    "status",
                                    "active_duration",
                                )
                            }
                        )
                    )
            else:
                print(storage.export(args.session_id, args.output))
            return 0
        reference = None
        if args.calibration:
            cal_dir = (
                Path("calibrations")
                if cfg.data_dir == "data"
                else Path(cfg.data_dir) / "calibrations"
            )
            reference = load_calibration_file(args.calibration, cal_dir)
            if reference is not None:
                storage.save_calibration(reference)
            else:
                reference = storage.calibration(args.calibration)
        if reference:
            if args.reference_interval:
                raise ValueError("Choose either an existing reference or a reference interval")
            for key in ("side", "facing", "capture_profile"):
                if getattr(args, key) is None and key not in cfg.model_fields_set:
                    setattr(cfg, key, reference.profile[key])
            if not args.confirm_camera_unchanged:
                if args.headless or not sys.stdin.isatty():
                    raise ValueError(
                        "Reusing a reference requires --confirm-camera-unchanged; recalibrate if placement changed"
                    )
                answer = input("Confirm unchanged physical camera placement [yes/no]: ")
                if answer.strip().lower() != "yes":
                    raise ValueError("Camera placement not confirmed; recalibrate")
        elif any(key not in cfg.model_fields_set for key in ("side", "facing")):
            raise ValueError("Explicitly select --side and --facing (or provide both in TOML)")
        if args.headless and not args.video:
            raise ValueError("Headless mode requires --video")
        if args.headless and not reference and not args.reference_interval:
            raise ValueError("Headless mode needs --calibration or --reference-interval")
        interval = tuple(args.reference_interval) if args.reference_interval else None
        if interval:
            import math

            if not args.video or not all(math.isfinite(v) for v in interval) or interval[0] < 0:
                raise ValueError(
                    "Reference interval needs a video and finite nonnegative timestamps"
                )
            if abs(interval[1] - interval[0] - cfg.calibration_duration) > 1e-6:
                raise ValueError(f"Reference interval must be {cfg.calibration_duration} seconds")
        from .app import Application

        review = (
            {"label": "teacher-reviewed", "note": args.teacher_reviewed}
            if args.teacher_reviewed
            else None
        )
        Application(
            cfg,
            storage,
            args.video,
            args.headless,
            reference,
            interval,
            args.reference_name,
            review,
            record_video=args.record_video,
            record_overlay=args.record_overlay,
            display=display,
            practice_name=args.practice_name,
        ).run()
        return 0
    except (ValueError, OSError, sqlite3.Error, RuntimeError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    finally:
        if storage:
            storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
