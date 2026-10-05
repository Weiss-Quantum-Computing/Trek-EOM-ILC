"""eomilc.scope.load reads both capture formats Scope Grab writes.

The compact NPZ (Scope Grab's Data box) has to load to exactly what the CSV
of the same capture would, give or take the CSV's seven digits, with the .txt
sidecar found either way -- the Captures glob, the native-rate spectrum and
tools/sysid_fit.py all go through load(). And a bench-kept native average,
which is also an .npz, must still be told apart from a capture.

If scope_grab.py is where ilc_bench looks for it, the NPZ is written by
scope_grab's own writer as well, so the two copies of read_npz cannot drift.

    python tests/test_scope_files.py
"""
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from eomilc import scope as scopeio          # noqa: E402

FAILS = []


def ok(label, condition, detail=""):
    print(f"  {'OK  ' if condition else 'FAIL'} {label}"
          + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILS.append(label)


tmp = tempfile.mkdtemp(prefix="ilc-scopefiles-")
rng = np.random.default_rng(3)
n = 20000
codes = ((30000 + np.linspace(-20000, 60000, n) + rng.normal(0, 40, n))
         .astype(np.int64) % 65536).astype(np.uint16)
x = (2e-7, -0.03, 0.0)
y = (1.5723e-6, 32768.0, 1.5e-4)
t = (np.arange(n) - x[2]) * x[0] + x[1]
v = (codes.astype(np.float64) - y[1]) * y[0] + y[2]
v2 = rng.normal(0, 1e-3, n)
cols = ["time_s", "CH3_Trek_Monitor_X1_V", "CH4_avg_V"]

print("hand-built NPZ, layout scope-grab-npz/1")
d = codes.copy()
d[1:] = codes[1:] - codes[:-1]
npz = os.path.join(tmp, "cap_001.npz")
np.savez_compressed(npz, format=np.array("scope-grab-npz/1"),
                    columns=np.array(cols), x=np.array(x), n=np.array(n),
                    y1=d, s1=np.array(y), y2=v2)
with open(os.path.join(tmp, "cap_001.txt"), "w") as fh:
    fh.write("CH3 V/div         : +10E-03\n")
tr = scopeio.load(npz)
ok("time exact", np.array_equal(tr.t, t))
ok("codes column exact", np.array_equal(tr["CH3"], v))
ok("volts column exact", np.array_equal(tr["CH4_avg_V"], v2))
ok("sidecar found beside an .npz", tr.volts_per_div("CH3") == 0.01)
ok("recognised as a capture", scopeio.is_capture_npz(npz))

csv = os.path.join(tmp, "cap_002.csv")
np.savetxt(csv, np.column_stack([t, v, v2]), delimiter=",",
           header=",".join(cols), comments="", fmt=["%.9e", "%.6e", "%.6e"])
tc = scopeio.load(csv)
ok("CSV of the same capture agrees to its digits",
   np.allclose(tc["CH3"], tr["CH3"], rtol=1e-6, atol=1e-15)
   and np.allclose(tc.t, tr.t, rtol=1e-9, atol=1e-18))

bench = os.path.join(tmp, "native_avg.npz")
np.savez(bench, t=t, y=v)
ok("a bench-kept average is not taken for a capture",
   not scopeio.is_capture_npz(bench))

try:
    import ilc_bench
    sg_path = ilc_bench.find_scope_grab(os.path.dirname(os.path.dirname(HERE)))
except Exception:
    sg_path = None
sg = None
if sg_path and os.path.exists(sg_path):
    try:
        sg = ilc_bench.load_module(sg_path, "scope_grab")
    except Exception as e:
        print(f"  (scope_grab at {sg_path} would not load: {e})")
if sg is not None and hasattr(sg, "write_npz"):
    print(f"written by scope_grab's own writer ({sg_path})")
    import scope_profiles
    rec = scope_profiles.Record(x, codes=codes, y=y)
    p = sg.write_npz(os.path.join(tmp, "sg_001.npz"), cols, rec.x,
                     [rec, v2])
    tr2 = scopeio.load(p)
    ok("scope_grab.write_npz -> eomilc load, exact",
       np.array_equal(tr2.t, rec.t()) and np.array_equal(tr2["CH3"], rec.v())
       and np.array_equal(tr2["CH4"], v2))
else:
    print("  (the scope_grab ilc_bench finds predates the NPZ format -- "
          "cross-check skipped)")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("scope files OK")
