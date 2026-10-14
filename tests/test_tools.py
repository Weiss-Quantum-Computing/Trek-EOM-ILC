#!/usr/bin/env python3
"""The spin-echo tools: tools/make_echo_target.py (two transports in one ILC
record) and tools/split_for_card.py (one card up/down pair per transport).
Needs the 2026-09-28 ramp suite's targets -- argv[1], else the OneDrive path
if it exists. Plain interpreter, numpy (and run_ilc's pandas) only.

    python tests/test_tools.py <suite>/targets
"""
import importlib.util, os, shutil, subprocess, sys, tempfile
import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
DEFAULT_TARGETS = os.path.join(os.path.expanduser("~"), "OneDrive - The Pennsylvania State University",
                               "Share Data", "Ramps", "suite-20260928", "targets")
TARGETS = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else (
    DEFAULT_TARGETS if os.path.isdir(DEFAULT_TARGETS) else None)
fails = []

# The OLD split_for_card (HEAD before 13 Oct 2026, one transport only) on the
# S47 pair with --targets, recorded before the change: flat 4770..6042 us,
# up 0..4970 us, down 5842..10812 us. The one-transport path must not move.
OLD_S47 = dict(flat=(4770.0, 6042.0), up=(0.0, 4970.0), down=(5842.0, 10812.0))
OLD_S47_LOG = ["  card: common flat stretch 4770..6042 us; hold on the atoms = card pause + 400 us",
               "  card: program up files 4.9700 ms, down files 4.9700 ms, both channels"]


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails.append(what)


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "tools", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def tgt(stem, ch):
    return os.path.join(TARGETS, f"target_{stem}{ch}.csv")


def as_drive(src, dst, scale=1 / 560):
    """A synthetic 'trained drive': the target in AWG volts, same grid."""
    t, v = SFC.read(src)
    with open(dst, "w", newline="") as f:
        f.write("# synthetic drive = target / 560\ntime_us,voltage_V\n")
        for x, y in zip(t, v * scale):
            f.write(f"{x:.6f},{y:.6f}\n")
    return dst


def card(path):
    return np.loadtxt(path, delimiter=",")[:, 1]


def main():
    global SFC
    if not TARGETS or not os.path.isdir(TARGETS):
        sys.exit("no targets folder: pass the 2026-09-28 suite's targets folder as argv[1]\n" + __doc__)
    SFC = load("split_for_card")
    MET = load("make_echo_target")
    import run_ilc
    tmp = tempfile.mkdtemp(prefix="echotools_")
    try:
        print("[1] make_echo_target: S47 at 16.667 ms, two legs")
        p = subprocess.run([sys.executable, os.path.join(HERE, "tools", "make_echo_target.py"),
                            tgt("S47", "X1"), tgt("S47", "X2"), "--spacing-ms", "16.667", "--out", tmp],
                           capture_output=True, text=True, errors="replace")
        out = p.stdout + p.stderr
        check(p.returncode == 0, f"builder exits 0 (got {p.returncode})")
        x1, x2 = (os.path.join(tmp, f"target_S47E2{c}.csv") for c in ("X1", "X2"))
        check(os.path.exists(x1) and os.path.exists(x2), "wrote target_S47E2X1.csv and target_S47E2X2.csv")
        check("check X1 (EO1): PASS" in out and "check X2 (EO2): PASS" in out,
              "the builder's own load + limit check PASS on both channels")
        check("5 characters" in out and "AWG_MAX_PTS" in out, "reminds of the stem cap and the point cap")

        (t1, v1), (t2, v2) = SFC.read(x1), SFC.read(x2)
        (ts, vs1), (_ts, vs2) = SFC.read(tgt("S47", "X1")), SFC.read(tgt("S47", "X2"))
        dt = 2.0
        i0, i1 = SFC.find_transports([vs1, vs2], dt)[0]
        lead, transport, tail = ts[i0] - ts[0], ts[i1] - ts[i0], 200.0
        check((ts[i0], ts[i1]) == (200.0, 10612.0), f"S47's transport is 200..10612 us ({ts[i0]:.0f}..{ts[i1]:.0f})")
        want = 16667.0 + transport + lead + tail
        check(abs(t1[-1] - want) <= dt, f"record length {t1[-1] / 1e3:.3f} ms = 16.667 + transport "
              f"{transport / 1e3:.3f} + lead 0.200 + tail 0.200 ms, within one 2 us step")
        check(len(t1) == 13741, f"13741 points ({len(t1)})")
        check(len(t1) == len(t2) and np.abs(t1 - t2).max() < 1e-9, "X1 and X2 share one grid")
        check(np.abs(np.diff(t1) - dt).max() < 1e-6, "on the source's 2 us grid")
        meta = SFC.read_meta(x1)
        check(meta.get("legs_ms") == [0.2, 16.868], f"legs_ms comment 0.200, 16.868 ({meta.get('legs_ms')})")
        check(meta.get("spacing_ms") == [16.668], f"spacing_ms comment 16.668, the 2 us grid's ({meta.get('spacing_ms')})")
        check(SFC.read_meta(x2).get("legs_ms") == meta.get("legs_ms"), "X2 carries the same leg starts")
        tr = meta.get("transport_us") or [0, 0, 0, 0]
        check(tr[0] == transport and abs(tr[0] - sum(tr[1:])) < 1e-6,
              f"transport_us comment: span {tr[0]:.0f} = rise {tr[1]:.0f} + hold {tr[2]:.0f} + fall {tr[3]:.0f} us")
        k2 = int(round(16868.0 / dt))
        n = i1 - i0 + 1
        check(np.array_equal(v1[i0:i0 + n], vs1[i0:i1 + 1]) and np.array_equal(v1[k2:k2 + n], vs1[i0:i1 + 1])
              and np.array_equal(v2[k2:k2 + n], vs2[i0:i1 + 1]), "both legs are the source's transport, sample for sample")
        check(np.all(v1[i0 + n:k2] == vs1[0]) and np.all(v1[k2 + n:] == vs1[0]) and np.all(v1[:i0] == vs1[:i0]),
              "lead, idle between legs and tail at the source's idle")
        for path, ch in ((x1, "X1"), (x2, "X2")):
            t, v = run_ilc.load_target(path)
            check(len(t) == 13741 and abs(np.median(np.diff(t)) - 2e-6) < 1e-12,
                  f"run_ilc.load_target reads {ch}: {len(t)} pts at {np.median(np.diff(t)) * 1e6:g} us")
            npts, _dt, rep = MET.check(path, ch)
            check(bool(rep), f"{ch}: init limit check passes ({str(rep).splitlines()[0]})")

        print("[2] make_echo_target refusals")
        try:
            MET.build([tgt("S47", "X1"), tgt("S47", "X2")], 10.0)
            check(False, "a spacing shorter than the transport is refused")
        except ValueError as e:
            check("not longer than the transport" in str(e), f"a 10 ms spacing is refused: {str(e)[:60]}")
        try:
            MET.build([tgt("S47", "X1"), tgt("S25", "X2")], 16.667)
            check(False, "sources on different grids are refused")
        except ValueError as e:
            check("grid" in str(e), f"S47X1 + S25X2 refused: {str(e)[:60]}")
        try:
            MET.build([tgt("B2S25", "X1"), tgt("B2S25", "X2")], 16.667)
            check(False, "a two-transport source is refused")
        except ValueError as e:
            check("2 transports" in str(e), f"B2S25 (already two transports) refused: {str(e)[:50]}")

        print("[2b] eomilc.echo.make: the library behind the tool, the panel's Echo... and the batch's echo key")
        from eomilc import echo
        tmp2 = os.path.join(tmp, "lib")
        quiet = []
        r = echo.make([tgt("S47", "X1")], spacing_ms=16.667, out_dir=tmp2, log=quiet.append)
        check(sorted(r["files"]) == ["X1", "X2"] and r["name"] == "S47E2" and r["ok"] and not r["notes"],
              "a lone X1 finds its X2 beside it; S47E2, both checks pass, no notes")
        check(all(r["written"].values()) and open(r["files"]["X1"], newline="").read() == open(x1, newline="").read(),
              "the library writes the file the command line wrote, byte for byte")
        r2 = echo.make([tgt("S47", "X1")], spacing_ms=16.667, out_dir=tmp2, log=quiet.append, do_check=False)
        check(not any(r2["written"].values()) and any(m.startswith("unchanged") for m in quiet),
              "an identical file on disk is left alone (the batch rebuilds at every plan load)")
        check(abs(echo.pause_ms(r["rec"]) - 5.856) < 1e-9 and any("pause between legs 5.856 ms" in m for m in quiet),
              f"card pause {echo.pause_ms(r['rec']):.3f} ms = spacing - (up + card hold + down), logged")
        r3 = echo.make([tgt("S47", "X1")], spacing_ms=16.667, out_dir=tmp2, suffix="ECHO2", log=quiet.append,
                       do_check=False)
        check(r3["name"] == "S47ECHO2" and r3["notes"] and "8 characters" in r3["notes"][0],
              f"a long suffix is written with a note: {r3['notes'][0]}")

        print("[2c] end_ms: the record padded with idle to the whole echo, the card's files unchanged")
        tmp3 = os.path.join(tmp, "pad")
        rp = echo.make([tgt("S47", "X1")], spacing_ms=16.667, out_dir=tmp3, log=quiet.append, end_ms=32.768)
        check(rp["rec"]["n"] == 16384 and abs(rp["rec"]["end_ms"] - 32.768) < 1e-9 and rp["rec"]["tail_us"] == 200.0
              and abs(rp["rec"]["pad_us"] - (32768 - 27482)) < 1e-6 and rp["ok"],
              f"S47E2 padded to 32.768 ms: 16384 pts, tail 200 us kept, pad {rp['rec']['pad_us']:.0f} us, checks pass")
        mp = SFC.read_meta(rp["files"]["X1"])
        check(mp.get("pad_us") == [5286.0] and mp.get("end_ms") == [32.768] and mp.get("tail_us") == [200.0],
              f"comments carry pad_us / end_ms / tail_us ({mp.get('pad_us')}, {mp.get('end_ms')})")
        check(abs(echo.pause_ms(rp["rec"]) - 5.856) < 1e-9, "the card pause is the design one, 5.856 ms, not the pad's")
        try:
            echo.make([tgt("S47", "X1")], spacing_ms=16.667, out_dir=tmp3, log=quiet.append, end_ms=20.0, do_check=False)
            check(False, "an end before the last transport is refused")
        except ValueError as e:
            check("before the last transport" in str(e), f"an end before the last transport is refused: {str(e)[:50]}")
        pd1 = as_drive(rp["files"]["X1"], os.path.join(tmp3, "drive_S47E2X1.csv"))
        pd2 = as_drive(rp["files"]["X2"], os.path.join(tmp3, "drive_S47E2X2.csv"))
        logp = []
        wp = SFC.split_pair([pd1, pd2], n=2500, settle_us=200.0, log=logp.append,
                            targets=[rp["files"]["X1"], rp["files"]["X2"]])
        L1p, L2p = wp["legs"]
        check(abs(wp["pauses_ms"][0] - 5.856) < 1e-6 and abs(L1p["down"][1] - (10612 + 200)) < 1e-6
              and abs(L2p["down"][1] - (16868 + 10412 + 200)) < 1e-6,
              f"the padded record's card files: pause {wp['pauses_ms'][0]:.3f} ms, leg 1 down ends {L1p['down'][1]:.0f} us, "
              f"leg 2 down ends {L2p['down'][1]:.0f} us (the design tail, not the pad)")

        print("[3] split_for_card: the E2 record, as a synthetic drive pair (target / 560)")
        d1 = as_drive(x1, os.path.join(tmp, "drive_S47E2X1.csv"))
        d2 = as_drive(x2, os.path.join(tmp, "drive_S47E2X2.csv"))
        log = []
        w = SFC.split_pair([d1, d2], n=2500, settle_us=200.0, log=log.append, targets=[x1, x2])
        names = sorted(os.path.basename(f) for f in w["files"])
        want_names = sorted(f"drive_S47E2{c}_leg{k}_{p}.csv" for c in ("X1", "X2") for k in (1, 2)
                            for p in ("upramp", "downramp"))
        check(names == want_names, f"four files per channel: {', '.join(n_ for n_ in names if 'X1' in n_)}")
        check(all(os.path.exists(f) for f in w["files"]), "all eight on disk")
        rise, hold, fall = tr[1:]
        settle, sp = 200.0, 16668.0
        L1, L2 = w["legs"]
        up_want = lead + rise + settle          # lead to settle into the hold
        down_want = settle + fall + tail        # settle before the hold's end to the tail's end
        check(abs(L1["up"][0] - 0) < 1e-9 and abs(L1["up_ms"] * 1e3 - up_want) < 1e-6,
              f"leg 1 up: 0..{L1['up'][1]:.0f} us = lead + rise + settle = {up_want:.0f} us")
        check(abs(L1["down_ms"] * 1e3 - down_want) < 1e-6 and abs(L1["down"][1] - (lead + transport + tail)) < 1e-6,
              f"leg 1 down: {L1['down'][0]:.0f}..{L1['down'][1]:.0f} us = settle + fall + tail = {down_want:.0f} us")
        check(abs(L2["up"][0] - sp) < 1e-6 and abs(L2["up_ms"] - L1["up_ms"]) < 1e-9
              and abs(L2["down_ms"] - L1["down_ms"]) < 1e-9 and abs(L2["down"][1] - t1[-1]) < 1e-9,
              f"leg 2 up {L2['up'][0]:.0f}..{L2['up'][1]:.0f} us, down {L2['down'][0]:.0f}..{L2['down'][1]:.0f} us: "
              f"the same spans, {sp / 1e3:.3f} ms later")
        check(abs(L1["card_hold_ms"] * 1e3 - (hold - 2 * settle)) < 1e-6,
              f"card hold {L1['card_hold_ms'] * 1e3:.0f} us = hold on the atoms {hold:.0f} - 2 x settle")
        pause = sp - (up_want + (hold - 2 * settle) + down_want)
        check(len(w["pauses_ms"]) == 1 and abs(w["pauses_ms"][0] * 1e3 - pause) < 1e-6,
              f"pause between legs {w['pauses_ms'][0]:.4f} ms = spacing - (up + card hold + down)")
        check(abs(pause - (L2["up"][0] - L1["down"][1])) < 1e-6,
              "... = the record's idle between leg 1's down file and leg 2's up file")
        pl = [m for m in log if "pause between leg 1 and leg 2" in m]
        check(len(pl) == 1 and f"= {pause / 1e3:.4f} ms" in pl[0] and "spacing - (up + card hold + down)" in pl[0],
              f"printed: {pl[0].strip()[:90] if pl else '(none)'}")
        f = {os.path.basename(x): x for x in w["files"]}
        for c in ("X1", "X2"):
            a, b = card(f[f"drive_S47E2{c}_leg1_upramp.csv"]), card(f[f"drive_S47E2{c}_leg2_upramp.csv"])
            a2, b2 = card(f[f"drive_S47E2{c}_leg1_downramp.csv"]), card(f[f"drive_S47E2{c}_leg2_downramp.csv"])
            check(len(a) == 2500 and np.allclose(a, b) and np.allclose(a2, b2) and abs(a[0]) < 1e-9
                  and abs(a2[-1]) < 1e-9, f"{c}: leg 2's files equal leg 1's (identical transports), 0 at the ends")

        print("[4] split_for_card: one transport (S47) unchanged")
        s1 = as_drive(tgt("S47", "X1"), os.path.join(tmp, "drive_S47X1.csv"))
        s2 = as_drive(tgt("S47", "X2"), os.path.join(tmp, "drive_S47X2.csv"))
        log = []
        w = SFC.split_pair([s1, s2], n=2500, settle_us=200.0, log=log.append,
                           targets=[tgt("S47", "X1"), tgt("S47", "X2")])
        names = sorted(os.path.basename(f) for f in w["files"])
        check(names == sorted(f"drive_S47{c}_{p}.csv" for c in ("X1", "X2") for p in ("upramp", "downramp")),
              f"the old names: {', '.join(names[:2])}, ...")
        L = w["legs"][0]
        check(L["flat_us"] == OLD_S47["flat"] and L["up"] == OLD_S47["up"] and L["down"] == OLD_S47["down"],
              f"the old script's cuts: up {L['up'][0]:.0f}..{L['up'][1]:.0f}, down "
              f"{L['down'][0]:.0f}..{L['down'][1]:.0f} us")
        check(abs(w["up_ms"] - 4.97) < 1e-9 and abs(w["down_ms"] - 4.97) < 1e-9 and w["hold_extra_us"] == 400
              and w["pauses_ms"] == [], "returned up_ms/down_ms 4.970 ms, as ilc_batch and the panel read them")
        check(log[:2] == OLD_S47_LOG, "the same two summary lines as before")
        log = []
        w = SFC.split_pair([s1, s2], n=2500, settle_us=200.0, log=log.append)
        check(len(w["files"]) == 4 and w["legs"][0]["up"] == OLD_S47["up"], "drives only: the same four files")

        print("[5] split_for_card: B2S25, the suite's two-transport record")
        b1 = as_drive(tgt("B2S25", "X1"), os.path.join(tmp, "drive_B2S25X1.csv"))
        b2 = as_drive(tgt("B2S25", "X2"), os.path.join(tmp, "drive_B2S25X2.csv"))
        w = SFC.split_pair([b1, b2], n=500, settle_us=200.0, log=lambda m: None,
                           targets=[tgt("B2S25", "X1"), tgt("B2S25", "X2")])
        check(len(w["legs"]) == 2 and len(w["files"]) == 8, "two legs, eight files")
        check(abs(w["pauses_ms"][0] - 0.102) < 1e-9,
              f"pause {w['pauses_ms'][0] * 1e3:.0f} us (100 us at 0 between the two 6.4 ms S25 records, "
              f"one 2 us step)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(fails)} failure(s)" + ("".join(f"\n  {f}" for f in fails)))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
