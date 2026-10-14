"""The batch window's progress and time left, from the batch's events.

Plain Python so tests/test_batch.py drives it with a fake clock; ilc_gui feeds
it from App._batch_event and reads fraction() for the bar and text() for the
status lines.
"""

class BatchProgress:
    """What a running batch has done and what is left, from its events, for
    the progress bar and the status lines (14 Oct 2026). Plain Python, no
    Tk: fed by App._batch_event on the main thread, tested without a window.

    Time left is the plan's own estimates (the "batch" event's plan_est per
    campaign) corrected by what this run has measured: the iteration time
    from the gaps between iteration events, the scan time from the gaps
    between optical events, and a finished campaign's real minutes against
    its estimate for the campaigns still queued. The electrical part is
    counted to the iteration cap, so "at the cap" is what the text says: an
    early stop shortens it.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.t0 = None
        self.todo, self.n = [], 0
        self.est = {}
        self.index, self.stem = None, None
        self.t_camp = None
        self.it = {}                 # key -> last iteration number
        self.rms = {}                # key -> last rms
        self.stopped = set()
        self.t_it = []               # measured seconds per iteration (campaign)
        self._t_last_it = None
        self.optical = None          # None | dict(step, pass, scan, scans_done, t_scan)
        self.finished = []           # (stem, actual_s, est_s)
        self.ended = False

    # -- events -----------------------------------------------------------
    def event(self, kind, info, now):
        if kind == "batch":
            self.reset()
            self.t0 = float(info.get("started") or now)
            self.todo, self.n = list(info.get("todo") or []), int(info.get("n") or 0)
            self.est = dict(info.get("plan_est") or {})
        elif kind == "campaign":
            self.index, self.stem, self.t_camp = int(info["index"]), info["stem"], now
            self.it, self.rms, self.stopped, self.t_it = {}, {}, set(), []
            self._t_last_it, self.optical = None, None
        elif kind == "iteration":
            key, it = info.get("key"), int(info.get("it", 0))
            self.it[key] = it
            self.rms[key] = float(info.get("rms_v", float("nan")))
            if info.get("stop"):
                self.stopped.add(key)
            # one wall-clock gap per iteration number, whichever channel reports first
            if self._t_last_it is not None and it != getattr(self, "_last_it_no", None):
                self.t_it.append(now - self._t_last_it)
            if it != getattr(self, "_last_it_no", None):
                self._t_last_it, self._last_it_no = now, it
        elif kind == "optical":
            o = self.optical or dict(scans_done=0, t_scan=[], step=None, scan=None, t_step=None,
                                     learn_it=0)
            step = info.get("step")
            if o["step"] == "scan" and o["t_step"] is not None and step != "scan":
                o["t_scan"].append(now - o["t_step"])
                o["scans_done"] += 1
            elif o["step"] == "scan" and step == "scan" and o["t_step"] is not None:
                o["t_scan"].append(now - o["t_step"])
                o["scans_done"] += 1
            o.update(step=step, scan=info.get("scan"), t_step=now, **{"pass": info.get("pass")})
            if step == "learn":
                o["learn_pass"] = info.get("pass")
            self.optical = o
        elif kind == "campaign_end":
            e = info.get("entry") or {}
            est = self.est.get(info.get("stem"), {})
            self.finished.append((info.get("stem"), float(e.get("minutes", 0.0)) * 60.0,
                                  float(est.get("electrical_s", 0.0)) + float(est.get("optical_s", 0.0))))
        elif kind == "batch_end":
            self.ended = True

    # -- numbers ----------------------------------------------------------
    def _per_it(self):
        est = self.est.get(self.stem, {})
        if len(self.t_it) >= 2:
            return sum(self.t_it[-5:]) / len(self.t_it[-5:])
        return float(est.get("per_it_s", 30.0))

    def _t_scan(self):
        o = self.optical or {}
        meas = o.get("t_scan") or []
        if meas:
            return sum(meas) / len(meas)
        est = self.est.get(self.stem, {})
        scans = max(1, int(est.get("scans", 1)))
        learn = int(est.get("learn_it", 0))
        return max(0.0, (float(est.get("optical_s", 0.0)) - learn * float(est.get("per_it_s", 30.0))) / scans)

    def campaign_left_s(self):
        """Seconds left in the current campaign, at the iteration cap."""
        if self.stem is None:
            return 0.0
        est = self.est.get(self.stem, {})
        max_it = int(est.get("max_it", 20))
        per_it = self._per_it()
        if self.optical is None:
            it = max(self.it.values()) if self.it else -1
            both_stopped = bool(self.it) and all(k in self.stopped for k in self.it)
            left = 0.0 if both_stopped else max(0, max_it - it) * per_it
            return left + float(est.get("optical_s", 0.0))
        o = self.optical
        scans = int(est.get("scans", 0))
        learn = int(est.get("learn_it", 0))
        left = max(0, scans - o["scans_done"] - (1 if o["step"] == "scan" else 0)) * self._t_scan()
        if o["step"] == "scan":
            left += max(0.0, self._t_scan() - (0.0 if o["t_step"] is None else 0.0))
        if o["step"] != "end":
            left += learn * per_it * (0.5 if o["step"] == "learn" else 1.0)
        return left

    def batch_left_s(self):
        """Seconds left in the whole batch: this campaign plus the queued ones,
        the plan's estimates scaled by what finished campaigns really took."""
        left = self.campaign_left_s()
        scale = 1.0
        done = [(a, e) for _s, a, e in self.finished if e > 0 and a > 0]
        if done:
            scale = sum(a for a, _e in done) / sum(e for _a, e in done)
        if self.index is not None:
            for stem in self.todo[self.index + 1:]:
                est = self.est.get(stem, {})
                left += scale * (float(est.get("electrical_s", 0.0)) + float(est.get("optical_s", 0.0)))
        return left

    def fraction(self):
        """0..n: finished campaigns plus the current one's share (electrical
        to the cap weighted against the optical stage, scans counted)."""
        if self.index is None:
            return 0.0
        if self.ended:
            return float(self.n)
        est = self.est.get(self.stem, {})
        e_s, o_s = float(est.get("electrical_s", 1.0)), float(est.get("optical_s", 0.0))
        w_e = e_s / max(e_s + o_s, 1e-9)
        max_it = max(1, int(est.get("max_it", 20)))
        it = max(self.it.values()) if self.it else -1
        f_e = min(1.0, (it + 1) / (max_it + 1))
        if self.optical is None:
            both_stopped = bool(self.it) and all(k in self.stopped for k in self.it)
            f = (1.0 if both_stopped else f_e) * w_e
        else:
            o = self.optical
            scans = max(1, int(est.get("scans", 1)))
            f_o = min(1.0, o["scans_done"] / scans)
            f = w_e + (1.0 - w_e) * (1.0 if o["step"] == "end" else f_o)
        return float(self.index) + f

    # -- text -------------------------------------------------------------
    @staticmethod
    def hms(s):
        s = max(0, int(round(s)))
        h, m = divmod(s // 60, 60)
        return f"{h}:{m:02d} h" if h else f"{m} min" if m else f"{s} s"

    def text(self, now, short=False):
        if self.t0 is None or self.index is None:
            return ""
        if self.ended:
            return f"batch ended after {self.hms(now - self.t0)} -- see the log"
        est = self.est.get(self.stem, {})
        pos = f"{self.stem} {self.index + 1}/{self.n}"
        if self.optical is None:
            it = max(self.it.values()) if self.it else None
            where = (f"i{it:02d} of <={int(est.get('max_it', 20))}" if it is not None else "starting")
            if self.rms:
                where += " (" + ", ".join(f"{k} {v:.2f} V" for k, v in sorted(self.rms.items())) + ")"
        else:
            o = self.optical
            scans = int(est.get("scans", 0))
            step = o["step"] or "?"
            where = f"optical pass {o.get('pass') or '?'}: {step}"
            if step == "scan" and o.get("scan"):
                where += f" {o['scan']} ({o['scans_done'] + 1}/{scans})"
            elif scans:
                where += f" (scans {o['scans_done']}/{scans})"
        el = self.hms(now - self.t0)
        c_left, b_left = self.campaign_left_s(), self.batch_left_s()
        if short:
            return f"batch {pos} | {where} | {el} elapsed, ~{self.hms(b_left)} left"
        return (f"{pos} | {where} | elapsed {el} | this campaign ~{self.hms(c_left)} left"
                + (" (at the iteration cap)" if self.optical is None else "")
                + f" | batch ~{self.hms(b_left)} left")
