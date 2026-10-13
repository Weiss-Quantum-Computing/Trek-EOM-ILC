#!/usr/bin/env python3
"""Run ONE block of the smoke suite against a freshly loaded session, for
iterating on a new block without the whole suite.

    python tests/smoke_block.py 55            # block [55]
    python tests/smoke_block.py 54 55         # several, in order

The panel is built in a scratch folder with the S92PX1B campaign copied in
(the fixture every late block loads), the state is loaded, and the block's
source -- from its "# [NN]" comment to the next "# [" comment -- is executed
with the same names the suite has (app, root, np, os, glob, SCRATCH,
ilc_gui, run_ilc). Only blocks written to stand on their own run this way;
the suite itself remains the record. Blocks from [54] on were written for it.
"""
import glob
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def main(argv):
    if not argv:
        sys.exit(__doc__)
    import numpy as np
    import tkinter as tk
    import ilc_gui
    import run_ilc
    SCRATCH = os.path.join(tempfile.gettempdir(), "eom-ilc-smoke-block")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(os.path.join(SCRATCH, "run"))
    for f in glob.glob(os.path.join(HERE, "run", "*S92PX1B*")):
        shutil.copy(f, os.path.join(SCRATCH, "run"))
    ilc_gui.RUN_DIR = os.path.join(SCRATCH, "run")
    ilc_gui.LOG_PATH = os.path.join(SCRATCH, "run", "ilc_gui.log")
    ilc_gui.CONFIG_PATH = os.path.join(SCRATCH, "config.json")
    root = tk.Tk()
    root.withdraw()
    app = ilc_gui.App(root)
    app.state_var.set(os.path.join(SCRATCH, "run", "drive_S92PX1B.state.npz"))
    app.do_load()
    root.update()
    src = open(os.path.join(HERE, "tests", "smoke_test.py"), encoding="utf-8").read().replace("\r\n", "\n")
    ns = dict(app=app, root=root, np=np, os=os, glob=glob, SCRATCH=SCRATCH, REPO=HERE,
              ilc_gui=ilc_gui, run_ilc=run_ilc, re=re, shutil=shutil, tempfile=tempfile)
    rc = 0
    for blk in argv:
        m = re.search(rf"^# \[{re.escape(blk)}\].*?(?=^# \[|^# Figure PNGs)", src, re.S | re.M)
        if m is None:
            print(f"block [{blk}] not found")
            rc = 1
            continue
        try:
            exec(compile(m.group(0), f"block[{blk}]", "exec"), ns)
            print(f"block [{blk}] ok")
        except Exception:
            import traceback
            traceback.print_exc()
            rc = 1
    root.destroy()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
