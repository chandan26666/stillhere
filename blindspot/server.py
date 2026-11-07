"""StillHere server. Reads the EEG, runs the flicker trials in the browser
and scores them. (The package is still called blindspot from our first version.)

Run:
    python -m blindspot.server --source sim --min-gap-hours 0   # no headset, fake EEG
    python -m blindspot.server --source lsl                     # Core-4 via scripts/stream_core.py
Then open http://localhost:8765 in Chrome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import time
from pathlib import Path

import numpy as np
from aiohttp import web, WSMsgType

from .analysis import (alpha_metrics, candidate_channels, classify, log_ratio, median_split,
                       permutation_p, score_segment, split_bar,
                       thresholds_from_calibration)
from .sources import BrainFlowSource, LSLSource, SimSource

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
REC_DIR = Path(__file__).resolve().parent.parent / "recordings"


class Recorder:
    """Pulls EEG from the source in the background and keeps all of it."""

    def __init__(self, source):
        self.source = source
        self.chunks, self.ts_chunks = [], []
        self.running = False

    async def run(self):
        self.running = True
        while self.running:
            x, ts = self.source.read_new()
            if ts.size:
                self.chunks.append(x)
                self.ts_chunks.append(ts)
            await asyncio.sleep(0.05)

    def pull(self):
        x, ts = self.source.read_new()
        if ts.size:
            self.chunks.append(x)
            self.ts_chunks.append(ts)

    def all(self):
        if not self.ts_chunks:
            return np.zeros((len(self.source.channel_names), 0)), np.zeros(0)
        return np.concatenate(self.chunks, axis=1), np.concatenate(self.ts_chunks)

    def tail(self, seconds: float, fs: float):
        """Last `seconds` of EEG, walking chunks from the end (cheap)."""
        need = int(seconds * fs)
        xs, got = [], 0
        for c in reversed(self.chunks):
            xs.append(c)
            got += c.shape[1]
            if got >= need:
                break
        if not xs:
            return np.zeros((len(self.source.channel_names), 0))
        x = np.concatenate(xs[::-1], axis=1)
        return x[:, -need:]

    def window(self, t_start: float, t_end: float):
        x, ts = self.all()
        keep = (ts >= t_start) & (ts <= t_end)
        return x[:, keep]


N_SCORED = 12  # trials in every scored run, fixed on the server


class Ledger:
    """Keeps track of every run per patient (saved to recordings/ledger.json).

    We enforce the rules here so the browser can't get around them:
    - every scored run is saved, pass or fail, and shows up on the report
    - only one scored run per patient every 12 h (min_gap_h)
    - "refer" only if two scored runs in a row pass, a fail in between resets it
    - runs that stop at step 1 get logged but have no score
    """

    PASS1, REFER, NONE = "pass1", "refer", "no_conclusion"

    def __init__(self, path: Path, min_gap_h: float = 12.0):
        self.path, self.min_gap_s = path, min_gap_h * 3600
        try:
            self.data = json.loads(path.read_text())
        except Exception:
            self.data = {}

    def _entry(self, patient):
        e = self.data.setdefault(patient or "UNKNOWN", {"runs": []})
        e.setdefault("runs", [])
        return e

    def _save(self):
        self.path.parent.mkdir(exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    def scored(self, patient):
        return [r for r in self._entry(patient)["runs"] if r["outcome"] != "not_interpretable"]

    def check(self, patient):
        """None if a scored run may start now, else the reason it may not."""
        runs = self.scored(patient)
        if runs and self.min_gap_s > 0:
            wait = runs[-1]["time"] + self.min_gap_s - time.time()
            if wait > 0:
                when = time.strftime("%a %H:%M", time.localtime(runs[-1]["time"] + self.min_gap_s))
                return (f"One scored run per {self.min_gap_s / 3600:g} h. The next scored run "
                        f"for {patient} can start after {when}.")
        return None

    def log_abort(self, patient, mode, reason):
        self._entry(patient)["runs"].append({"time": time.time(), "mode": mode,
                                             "outcome": "not_interpretable", "reason": reason})
        self._save()

    def record(self, patient, passed, mode, n, bar, k, p):
        runs = self.scored(patient)
        prev = runs[-1] if runs else None
        if passed and prev and prev["outcome"] == self.PASS1:
            outcome, note = self.REFER, "Passed two runs in a row."
        elif passed:
            outcome, note = self.PASS1, "First pass. Needs one more pass in the next run."
        else:
            outcome = self.NONE
            note = ("Didn't confirm the earlier pass, so the count starts over."
                    if prev and prev["outcome"] == self.PASS1 else "")
        self._entry(patient)["runs"].append({"time": time.time(), "mode": mode, "n": n, "bar": bar,
                                             "correct": k, "p": p, "outcome": outcome})
        self._save()
        return outcome, note

    def log_stopped(self, patient, mode):
        """Stopping after scoring began still counts: it uses up the gap and
        resets a pending pass, so a bad run cannot be thrown away and retried."""
        self._entry(patient)["runs"].append({"time": time.time(), "mode": mode, "outcome": "stopped"})
        self._save()

    def history(self, patient):
        return [{k: r.get(k) for k in ("time", "mode", "correct", "n", "outcome", "reason")}
                for r in self._entry(patient)["runs"]]


class Stopped(Exception):
    """The user pressed Esc."""


class Session:
    def __init__(self, app, ws):
        self.app, self.ws = app, ws
        self.queue: asyncio.Queue = asyncio.Queue()
        self.freqs = {"left": 60 / 7, "right": 12.0}
        self.log = []
        self.meta = {}
        self.locked = None
        self.patient, self.sitting = "", ""
        self.running = False

    @property
    def src(self):
        return self.app["source"]

    @property
    def rec(self) -> Recorder:
        return self.app["recorder"]

    async def send(self, msg: dict):
        await self.ws.send_str(json.dumps(msg))

    async def expect(self, *types):
        while True:
            msg = await self.queue.get()
            if msg.get("type") in types:
                if msg["type"] == "stop":
                    raise Stopped()
                return msg

    async def run_trial(self, i, n, mode, left, right, duration, reveal, command=None,
                        question=None, prompt=None):
        """One trial. `left`/`right` are contrasts (0 = blank gray half)."""
        await self.send({"type": "trial", "i": i, "n": n, "mode": mode,
                         "left": left, "right": right, "duration": duration,
                         "command": command if question is None else None,
                         "question": question, "prompt": prompt})
        await self.expect("trial_started", "stop")
        t0 = time.time()
        self.src.set_stimulus(left, right, attend=command)
        live = asyncio.create_task(self.live_loop(t0))
        try:
            await self.expect("trial_ended")
        finally:
            live.cancel()
        t1 = time.time()
        self.src.set_stimulus(0.0, 0.0)
        await asyncio.sleep(0.6)  # let Bluetooth catch up
        self.rec.pull()
        seg = self.rec.window(t0 + 0.5, t1)  # skip the first half second
        fs = self.src.fs
        if seg.shape[1] < fs * 2:
            score = {"snr": {"left": 0.0, "right": 0.0}, "per_channel": {},
                     "warning": f"only {seg.shape[1]} samples, check headset"}
        else:
            score = score_segment(seg, fs, list(self.src.channel_names),
                                  self.freqs, mains_hz=self.app["mains"],
                                  channels=self.locked)
            if mode.startswith("eyesclosed"):
                score["alpha"] = alpha_metrics(seg, fs, list(self.src.channel_names),
                                               channels=self.locked or None,
                                               mains_hz=self.app["mains"])
        row = {"i": i, "mode": mode, "left": left, "right": right,
               "t0": t0, "t1": t1, "n_samples": int(seg.shape[1]), **score}
        self.log.append(row)
        await self.send({"type": "trial_done", "i": i,
                         "score": score if reveal else None})
        await self.expect("ready", "stop")
        return row

    async def trace_loop(self):
        """While no test is running, stream the last 4 s of filtered EEG
        (downsampled to 100 Hz) plus per-sensor contact quality, 4x a second,
        for the live monitor on the home screen."""
        from .analysis import preprocess, channel_quality
        fs = self.src.fs
        names = list(self.src.channel_names)
        step = max(1, int(round(fs / 100)))
        while True:
            await asyncio.sleep(0.25)
            if self.running or self.ws.closed:
                continue
            self.rec.pull()
            x = self.rec.tail(4.0, fs)
            if x.shape[1] < fs:
                continue
            try:
                y = preprocess(x, fs, self.app["mains"])
                q = channel_quality(x, fs, names, self.app["mains"])
            except Exception:
                continue
            await self.send({"type": "trace", "names": names, "fs": fs / step,
                             "data": np.round(y[:, ::step], 2).tolist(), "quality": q})

    async def live_loop(self, t0):
        """During a trial, send a live left/right score once a second (for the
        on-screen meter). Uses the last 4 s of this trial only."""
        fs = self.src.fs
        try:
            while True:
                await asyncio.sleep(1.0)
                self.rec.pull()
                now = time.time()
                seg = self.rec.window(max(t0 + 0.5, now - 4.0), now)
                if seg.shape[1] >= fs * 2:
                    s = score_segment(seg, fs, list(self.src.channel_names),
                                      self.freqs, mains_hz=self.app["mains"])
                    await self.send({"type": "live", "score": {"snr": s["snr"]}})
        except asyncio.CancelledError:
            pass

    async def run_calibrate(self, duration):
        plan = [(1.0, 1.0), (0.0, 0.0), (1.0, 1.0), (0.0, 0.0)]
        rows = []
        for i, (l, r) in enumerate(plan):
            rows.append(await self.run_trial(i, len(plan), "calibrate", l, r,
                                             duration, reveal=True))
        on = [r for r in rows if r["left"] > 0]
        off = [r for r in rows if r["left"] == 0]
        th = thresholds_from_calibration(on, off)
        self.app["state"]["thresholds"] = th
        await self.send({"type": "summary", "mode": "calibrate", "thresholds": th,
                         "on": [r["snr"] for r in on], "off": [r["snr"] for r in off]})

    async def run_test(self, n_trials, duration):
        """Blinded test. Hidden side is drawn at random; the classifier only
        sees EEG plus the thresholds fixed during calibration."""
        th = dict(self.app["state"]["thresholds"])
        conds = (["none_hidden", "left_hidden", "right_hidden"] * n_trials)[:n_trials]
        secrets.SystemRandom().shuffle(conds)
        stim = {"none_hidden": (1.0, 1.0), "left_hidden": (0.0, 1.0),
                "right_hidden": (1.0, 0.0)}
        results = []
        for i, cond in enumerate(conds):
            l, r = stim[cond]
            row = await self.run_trial(i, len(conds), "test", l, r, duration,
                                       reveal=False)
            predicted = classify(row["snr"], th)  # EEG only, no label
            results.append({"i": i, "truth": cond, "predicted": predicted,
                            "snr": row["snr"]})
        correct = sum(r["truth"] == r["predicted"] for r in results)
        await self.send({"type": "summary", "mode": "test", "thresholds": th,
                         "results": results, "correct": correct,
                         "n": len(results), "chance": 1 / 3})

    async def step1_visual(self, mode, duration):
        """Step 1: flicker on, blank, flicker on. Each side must follow its own
        flicker, and at least 2 sensors must have good contact in every step-1
        trial. Those sensors are then locked for the rest of the run.
        Returns None if the run may continue, else the reason to stop."""
        plan = [(1.0, 1.0), (0.0, 0.0), (1.0, 1.0)]
        rows = []
        for i, (l, r) in enumerate(plan):
            rows.append(await self.run_trial(i, len(plan), f"{mode}-step1", l, r,
                                             duration, reveal=True))
        names = list(self.src.channel_names)
        cand = [names[i] for i in candidate_channels(names)]
        good = [c for c in cand if all(r.get("quality", {}).get(c) == "ok" for r in rows)]
        on = [r for r in rows if r["left"] > 0]
        off = [r for r in rows if r["left"] == 0]
        side_ok = {}
        for side in ("left", "right"):
            on_s = float(np.mean([r["snr"][side] for r in on]))
            off_s = float(np.mean([r["snr"][side] for r in off]))
            side_ok[side] = bool(on_s > max(1.5, 1.3 * off_s))
            self.meta[f"step1_{side}"] = {"on": on_s, "off": off_s, "ok": side_ok[side]}
        self.meta["channels"] = good
        if len(good) < 2:
            reason = "Not interpretable: fewer than 2 sensors with good contact."
        elif not all(side_ok.values()):
            reason = "Not interpretable: visual pathway not confirmed."
        else:
            reason = None
        await self.send({"type": "step1_done", "visual_on": reason is None})
        if reason is None:
            self.locked = good
        return reason

    async def abort(self, mode, reason):
        """No score of any kind leaves the server."""
        self.meta["outcome"] = "not_interpretable"
        self.app["ledger"].log_abort(self.patient, mode, reason)
        await self.send({"type": "summary", "mode": mode, "outcome": "not_interpretable",
                         "reason": reason, "history": self.app["ledger"].history(self.patient)})

    async def finish(self, mode, results, calls):
        n = len(results)
        for r, c in zip(results, calls):
            r["brain"] = c
        k = sum(r["target"] == r["brain"] for r in results)
        bar = split_bar(n)
        p = permutation_p(k, n)
        outcome, note = self.app["ledger"].record(self.patient, k >= bar, mode, n, bar, k, p)
        self.meta.update({"outcome": outcome, "correct": k, "n": n, "bar": bar, "p": p})
        await self.send({"type": "summary", "mode": mode, "outcome": outcome, "note": note,
                         "results": results, "correct": k, "n": n, "bar": bar, "p_chance": p,
                         "history": self.app["ledger"].history(self.patient)})

    async def scored_trials(self, mode, targets, duration, questions=None):
        """Run the locked, balanced, shuffled trials. Returns the results with
        their left/right ratios, or None if the signal dropped (run stops)."""
        self.meta["scoring_started"] = True
        results = []
        for i, side in enumerate(targets):
            q = questions[i] if questions else None
            row = await self.run_trial(i, len(targets), f"{mode}-step2", 1.0, 1.0, duration,
                                       reveal=False, command=side,
                                       question=q["text"] if q else None)
            if "warning" in row:
                return None
            res = {"i": i, "target": side, "ratio": log_ratio(row["snr"]), "snr": row["snr"]}
            if q:
                res["question"] = q["text"]
            results.append(res)
        return results

    async def run_awareness(self, duration):
        """Step 1 visual check, then 12 commands ("focus on the LEFT side"),
        exactly 6 left and 6 right in a shuffled order. The decision is a
        median split of the left/right ratios: EEG only."""
        n, bar = N_SCORED, split_bar(N_SCORED)
        await self.send({"type": "plan", "mode": "awareness", "n": n, "bar": bar})
        reason = await self.step1_visual("awareness", duration)
        if reason:
            return await self.abort("awareness", reason)
        targets = ["left", "right"] * (n // 2)
        secrets.SystemRandom().shuffle(targets)
        results = await self.scored_trials("awareness", targets, duration)
        if results is None:
            return await self.abort("awareness", "Not interpretable: signal dropped during the run.")
        await self.finish("awareness", results, median_split([r["ratio"] for r in results]))

    async def run_answers(self, questions, duration):
        """Step 1 visual check, then 12 known-answer questions: 6 YES and 6 NO,
        drawn at random from the list and shuffled. Left = YES, right = NO."""
        rng = secrets.SystemRandom()
        yes = [q for q in questions if q["answer"] == "yes"]
        no = [q for q in questions if q["answer"] == "no"]
        qs = rng.sample(yes, N_SCORED // 2) + rng.sample(no, N_SCORED // 2)
        rng.shuffle(qs)
        n, bar = N_SCORED, split_bar(N_SCORED)
        await self.send({"type": "plan", "mode": "answers", "n": n, "bar": bar})
        reason = await self.step1_visual("answers", duration)
        if reason:
            return await self.abort("answers", reason)
        targets = ["left" if q["answer"] == "yes" else "right" for q in qs]
        results = await self.scored_trials("answers", targets, duration, qs)
        if results is None:
            return await self.abort("answers", "Not interpretable: signal dropped during the run.")
        await self.finish("answers", results, median_split([r["ratio"] for r in results]))

    async def step1_alpha(self, duration):
        """Eyes-closed step 1: two resting trials with eyes closed. We need a
        clear alpha rhythm (8-13 Hz) on at least 2 good sensors, otherwise
        there's nothing to measure and the test stops."""
        rows = []
        for i in range(2):
            rows.append(await self.run_trial(
                i, 2, "eyesclosed-step1", 0.0, 0.0, duration, reveal=False, command="rest",
                prompt="Close your eyes and relax." if i == 0 else "Keep your eyes closed and relax."))
        names = list(self.src.channel_names)
        cand = [names[i] for i in candidate_channels(names)]
        good = [c for c in cand if all(r.get("quality", {}).get(c) == "ok" for r in rows)]
        self.meta["channels"] = good
        if len(good) < 2:
            reason = "Not interpretable: fewer than 2 sensors with good contact."
        else:
            prom = float(np.mean([r["alpha"]["prom"] for r in rows if "alpha" in r] or [0]))
            self.meta["step1_alpha_prom"] = prom
            reason = None if prom > 1.4 else "Not interpretable: no eyes-closed alpha rhythm found."
        await self.send({"type": "step1_done", "visual_on": reason is None})
        if reason is None:
            self.locked = good
        return reason

    async def run_eyesclosed(self, duration):
        """Eyes-closed version (no flicker). 12 spoken commands, 6 "count
        backwards by sevens" and 6 "just relax", in random order. Alpha usually
        drops during mental math, so the 6 trials with the lowest alpha are
        called "math". Same pass mark and rules as the other tests.
        Note: in some people alpha goes the other way during mental math, so
        they'd get "No conclusion"."""
        n, bar = N_SCORED, split_bar(N_SCORED)
        await self.send({"type": "plan", "mode": "eyesclosed", "n": n, "bar": bar})
        reason = await self.step1_alpha(duration)
        if reason:
            return await self.abort("eyesclosed", reason)
        rng = secrets.SystemRandom()
        targets = ["math", "rest"] * (n // 2)
        rng.shuffle(targets)
        self.meta["scoring_started"] = True
        results = []
        for i, t in enumerate(targets):
            prompt = (f"Count backwards from {rng.randint(300, 999)} by sevens." if t == "math"
                      else "Just relax. Don't count anything.")
            row = await self.run_trial(i, n, "eyesclosed-step2", 0.0, 0.0, duration,
                                       reveal=False, command=t, prompt=prompt)
            if "warning" in row or "alpha" not in row:
                return await self.abort("eyesclosed", "Not interpretable: signal dropped during the run.")
            results.append({"i": i, "target": t, "alpha": row["alpha"]["power"]})
        order = np.argsort([r["alpha"] for r in results])
        calls = ["rest"] * n
        for j in order[: n // 2]:
            calls[j] = "math"
        await self.finish("eyesclosed", results, calls)

    async def run_contrast(self, duration):
        steps = [1.0, 0.5, 0.2, 0.0]
        rows = []
        for i, c in enumerate(steps):
            rows.append(await self.run_trial(i, len(steps), "contrast", 1.0, c,
                                             duration, reveal=True))
        await self.send({"type": "summary", "mode": "contrast",
                         "steps": [{"contrast": r["right"], "snr_right": r["snr"]["right"],
                                    "snr_left": r["snr"]["left"]} for r in rows],
                         "thresholds": self.app["state"]["thresholds"]})

    async def run_check(self):
        """Headset check: both halves flicker, live SNR every second."""
        await self.send({"type": "trial", "i": 0, "n": 1, "mode": "check",
                         "left": 1.0, "right": 1.0, "duration": 0})
        self.src.set_stimulus(1.0, 1.0)
        fs = self.src.fs
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                    if msg.get("type") in ("stop", "trial_ended"):
                        break
                except asyncio.TimeoutError:
                    pass
                self.rec.pull()
                now = time.time()
                seg = self.rec.window(now - 6.0, now)
                if seg.shape[1] >= fs * 3:
                    s = score_segment(seg, fs, list(self.src.channel_names),
                                      self.freqs, mains_hz=self.app["mains"])
                    await self.send({"type": "live", "score": s,
                                     "n_samples": int(seg.shape[1])})
                else:
                    await self.send({"type": "live", "score": None,
                                     "n_samples": int(seg.shape[1])})
        finally:
            self.src.set_stimulus(0.0, 0.0)
            await self.send({"type": "summary", "mode": "check"})

    def save(self, mode):
        REC_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        x, ts = self.rec.all()
        np.savez_compressed(REC_DIR / f"{stamp}-{mode}-raw.npz", eeg=x, ts=ts,
                            channels=np.array(self.src.channel_names), fs=self.src.fs)
        (REC_DIR / f"{stamp}-{mode}-log.json").write_text(json.dumps(
            {"freqs": self.freqs, "thresholds": self.app["state"]["thresholds"],
             "patient": self.patient, "sitting": self.sitting, "result": self.meta,
             "trials": self.log}, indent=2))

    async def runner(self):
        while True:
            msg = await self.expect("hello", "run")
            if msg["type"] == "hello":
                self.freqs = {k: float(v) for k, v in msg["freqs"].items()}
                if hasattr(self.src, "set_freqs"):
                    self.src.set_freqs(self.freqs)
                await self.send({"type": "hello_ack", "source": self.app["source_name"],
                                 "fs": self.src.fs, "channels": list(self.src.channel_names),
                                 "freqs": self.freqs, "thresholds": self.app["state"]["thresholds"],
                                 "min_gap_h": self.app["ledger"].min_gap_s / 3600,
                                 "n_scored": N_SCORED, "bar": split_bar(N_SCORED)})
                continue
            mode = msg.get("mode", "test")
            duration = min(15.0, max(6.0, float(msg.get("duration", 10))))
            self.log = []
            self.meta = {}
            self.locked = None
            self.patient = str(msg.get("patient", "")).strip()[:64]
            self.sitting = str(msg.get("sitting", ""))[:64]
            qs = []
            if mode in ("awareness", "answers", "eyesclosed"):
                problem = None if self.patient else "Enter a patient ID first."
                if mode == "answers" and not problem:
                    qs = [{"text": str(q.get("text", "")).strip()[:200],
                           "answer": "yes" if str(q.get("answer", "")).lower().startswith("y") else "no"}
                          for q in msg.get("questions", []) if str(q.get("text", "")).strip()]
                    if min(sum(q["answer"] == "yes" for q in qs), sum(q["answer"] == "no" for q in qs)) < N_SCORED // 2:
                        problem = f"The Yes/No test needs at least {N_SCORED // 2} YES and {N_SCORED // 2} NO questions."
                problem = problem or self.app["ledger"].check(self.patient)
                if problem:
                    await self.send({"type": "summary", "mode": "invalid", "reason": problem})
                    continue
            self.running = True
            try:
                if mode == "calibrate":
                    await self.run_calibrate(duration)
                elif mode == "test":
                    await self.run_test(int(msg.get("trials", 9)), duration)
                elif mode == "contrast":
                    await self.run_contrast(duration)
                elif mode == "answers":
                    await self.run_answers(qs, duration)
                elif mode == "awareness":
                    await self.run_awareness(duration)
                elif mode == "eyesclosed":
                    await self.run_eyesclosed(duration)
                elif mode == "check":
                    await self.run_check()
            except Stopped:
                if self.meta.get("scoring_started") and "outcome" not in self.meta:
                    self.app["ledger"].log_stopped(self.patient, mode)
                    self.meta["outcome"] = "stopped"
                await self.send({"type": "summary", "mode": "stopped",
                                 "counted": self.meta.get("outcome") == "stopped"})
            finally:
                self.running = False
                self.src.set_stimulus(0.0, 0.0)
                if self.log:
                    self.save(mode)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    session = Session(request.app, ws)
    task = asyncio.create_task(session.runner())
    trace = asyncio.create_task(session.trace_loop())
    try:
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                await session.queue.put(json.loads(m.data))
    finally:
        task.cancel()
        trace.cancel()
    return ws


async def index(request):
    return web.FileResponse(WEB_DIR / "index.html")


async def on_startup(app):
    app["source"].start()
    app["rec_task"] = asyncio.create_task(app["recorder"].run())


async def on_cleanup(app):
    app["recorder"].running = False
    app["source"].stop()


def make_app(source_name="sim", serial="", mains=60.0, lsl_name="", channels=None, use=None,
             sim_mode="normal", min_gap_h=12.0):
    app = web.Application()
    if source_name == "sim":
        source = SimSource()
        source.mode = sim_mode
    elif source_name == "lsl":
        source = LSLSource(lsl_name, channels, use)
    else:
        source = BrainFlowSource(source_name, serial)
    app["source"], app["source_name"] = source, source_name
    app["recorder"] = Recorder(source)
    app["state"] = {"thresholds": {"left": 2.0, "right": 2.0}}
    app["ledger"] = Ledger(REC_DIR / "ledger.json", min_gap_h)
    app["mains"] = mains
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", WEB_DIR)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    p = argparse.ArgumentParser(description="BlindSpot SSVEP visual field screen")
    p.add_argument("--source", default="sim", choices=["sim", "lsl", "unicorn", "synthetic"],
                   help="sim = fake EEG, lsl = BCI Core-4/8 via g.Pype, unicorn = Hybrid Black via BrainFlow")
    p.add_argument("--serial", default="", help="Unicorn serial, e.g. UN-2021.05.36")
    p.add_argument("--lsl-name", default="", help="LSL stream name (default: first EEG stream)")
    p.add_argument("--channels", default="", help="optional names for the channels you keep, e.g. PO7,O1,O2,PO8")
    p.add_argument("--use", default="", help="LSL only: socket numbers with electrodes, e.g. 1,2,3,4 (default: all)")
    p.add_argument("--sim-mode", default="normal", choices=["normal", "blind", "nofollow", "stuck"],
                   help="sim only: normal = follows commands, blind = no visual response "
                        "(shows the Step 1 stop), nofollow = sees flicker, ignores commands, "
                        "stuck = always favors the left side")
    p.add_argument("--min-gap-hours", type=float, default=12.0,
                   help="hours between scored runs for one patient (0 only for development)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--mains", type=float, default=60.0, help="power line Hz (60 in the US)")
    a = p.parse_args()
    chans = [c.strip() for c in a.channels.split(",") if c.strip()] or None
    use = [int(u) for u in a.use.split(",") if u.strip()] or None
    web.run_app(make_app(a.source, a.serial, a.mains, a.lsl_name, chans, use, a.sim_mode,
                         a.min_gap_hours), port=a.port)


if __name__ == "__main__":
    main()
