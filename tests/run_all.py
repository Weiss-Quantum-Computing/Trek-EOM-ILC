#!/usr/bin/env python3
"""Every suite, in parallel lanes, one line each.

    python tests/run_all.py [<suite>/targets] [-q] [--only smoke,batch,...]

Lanes run side by side as separate processes (each suite builds its own Tk
root and scratch folder, so they do not share state); the batch-GUI suite
runs after the library batch suite in its own lane, because its "Stop
mid-campaign" block is timing-sensitive and ran into trouble when the smoke
suite shared the machine with it. The targets folder (the 28 Sep ramp
suite's) is needed by the two batch suites; without it they are skipped and
say so. Exit code 1 when anything failed.

Lanes (12 Oct 2026, this PC, Anaconda python):
  smoke      ~60-80 s with lazy canvas drawing (was ~300 s)
  gui-batch  ~150 s, after test_batch (~60 s)
  library    corrections, diagnostics, monitor, polarimetry, rin, scope files,
             protocol -- a few seconds each
"""
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
DEFAULT_TARGETS = os.path.join(os.path.expanduser("~"), "OneDrive - The Pennsylvania State University",
                               "Share Data", "Ramps", "suite-20260928", "targets")

LANES = {
    "smoke": [("smoke", "tests/smoke_test.py", [])],
    "batch": [("batch", "tests/test_batch.py", ["TARGETS"]),
              ("gui-batch", "tests/test_batch_gui.py", ["TARGETS"])],
    "library": [("corrections", "tests/test_corrections.py", []),
                ("diagnostics", "tests/test_diagnostics.py", []),
                ("monitor", "tests/test_monitor.py", []),
                ("polarimetry", "tests/test_polarimetry.py", []),
                ("rin", "tests/test_rin.py", []),
                ("scope-files", "tests/test_scope_files.py", []),
                ("protocol", "tests/test_protocol.py", []),
                ("protocol-sr760", "tests/test_protocol_sr760.py", [])],
}
PASS_MARKS = ("ALL CHECKS PASSED", "0 failure(s)", "ALL 26 CHECKS PASSED", "OK")


def run_one(name, script, args, targets, quiet, out):
    argv = [PY, os.path.join(HERE, script)] + [targets if a == "TARGETS" else a for a in args]
    if "TARGETS" in args and not (targets and os.path.isdir(targets)):
        out[name] = ("skipped", 0.0, "no targets folder given")
        return
    t0 = time.time()
    p = subprocess.run(argv, cwd=HERE, capture_output=True, text=True, errors="replace")
    dt = time.time() - t0
    text = (p.stdout or "") + (p.stderr or "")
    lines = [l for l in text.splitlines() if l.strip() and "Warning" not in l]
    marks = [l for l in lines if any(m in l for m in PASS_MARKS) or "FAIL" in l or "failure" in l]
    tail = (marks or lines or [""])[-1].strip()
    ok = p.returncode == 0 and ("FAIL" not in tail or "0 failure" in tail)
    out[name] = ("ok" if ok else "FAILED", dt, tail if quiet or ok else "\n".join(lines[-25:]))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    quiet = "-q" in argv
    only = None
    if "--only" in argv:
        i = argv.index("--only")
        only = set(argv[i + 1].split(","))
        del argv[i:i + 2]
    pos = [a for a in argv if not a.startswith("-")]
    targets = pos[0] if pos else (DEFAULT_TARGETS if os.path.isdir(DEFAULT_TARGETS) else "")
    out = {}
    threads = []

    def lane(items):
        for name, script, args in items:
            if only and name not in only:
                continue
            run_one(name, script, args, targets, quiet, out)
    t0 = time.time()
    for items in LANES.values():
        th = threading.Thread(target=lane, args=(items,), daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    bad = 0
    for items in LANES.values():
        for name, _s, _a in items:
            if name not in out:
                continue
            status, dt, tail = out[name]
            bad += status == "FAILED"
            print(f"{status:7s} {name:15s} {dt:6.0f} s   {tail if quiet or status != 'FAILED' else ''}")
            if status == "FAILED" and not quiet:
                print("   " + tail.replace("\n", "\n   "))
    print(f"\nwall {time.time() - t0:.0f} s, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
