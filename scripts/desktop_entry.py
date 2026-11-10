import os
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout


def run() -> int:
    if sys.argv[1:] == ["--smoke-test"] and "TANGOPOSTURE_SMOKE_LOG" in os.environ:
        with (
            open(os.environ["TANGOPOSTURE_SMOKE_LOG"], "w", encoding="utf-8", buffering=1) as log,
            redirect_stdout(log),
            redirect_stderr(log),
        ):
            try:
                print("Starting packaged desktop smoke test", flush=True)
                from tango_posture.desktop import main

                return main()
            except Exception:  # noqa: BLE001 - Report smoke failures without a blocking Windows dialog.
                traceback.print_exc(file=log)
                return 1
    from tango_posture.desktop import main

    return main()


if __name__ == "__main__":
    raise SystemExit(run())
