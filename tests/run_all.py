"""Run every test in this folder and report a single verdict."""
import os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SUITES = [
    ("Parser unit tests",          "test_parser.py"),
    ("Controller state machine",   "test_controller.py"),
    ("Headless GUI smoke",         "test_gui_smoke.py"),
    ("End-to-end (TCP simulator)", "test_end_to_end.py"),
]

failures = []
for name, fn in SUITES:
    print(f"\n{'='*60}\n  {name}  ({fn})\n{'='*60}")
    rc = subprocess.call([sys.executable, os.path.join(HERE, fn)], cwd=ROOT)
    if rc != 0:
        failures.append(name)

print(f"\n{'#'*60}")
if failures:
    print(f"  FAILED: {failures}")
    sys.exit(1)
print(f"  ALL {len(SUITES)} SUITES PASSED")
print(f"{'#'*60}")
