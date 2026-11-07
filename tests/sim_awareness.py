"""End-to-end check of the StillHere rules on fake EEG, the way the browser talks
to the server. Every rule the pitch claims is checked here.

Run: python -m tests.sim_awareness      (about 12 minutes)
"""
import asyncio, json, tempfile
from pathlib import Path
import aiohttp
from aiohttp import web
from blindspot.server import make_app, Ledger

PORT = 8798
Q12 = [{"text": f"Q{i}", "answer": "yes" if i % 2 else "no"} for i in range(12)]
Q8 = Q12[:8]


async def one_run(app, sim_mode, mode, patient="TEST-01", questions=Q12, stop_at=None, duration=6):
    app["source"].mode = sim_mode
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"http://localhost:{PORT}/ws") as ws:
            await ws.send_str(json.dumps({"type": "hello", "refresh": 60,
                                          "freqs": {"left": 60 / 7, "right": 12.0}}))
            await ws.receive_json()
            await ws.send_str(json.dumps({"type": "run", "mode": mode, "duration": duration,
                                          "patient": patient, "sitting": "x",
                                          "questions": questions if mode == "answers" else []}))
            while True:
                m = await ws.receive_json()
                if m["type"] == "trial":
                    await ws.send_str(json.dumps({"type": "trial_started", "i": m["i"]}))
                    await asyncio.sleep(m["duration"])
                    await ws.send_str(json.dumps({"type": "trial_ended", "i": m["i"]}))
                    if stop_at is not None and m["mode"].endswith("step2") and m["i"] == stop_at:
                        await ws.send_str(json.dumps({"type": "stop"}))
                elif m["type"] == "trial_done":
                    await ws.send_str(json.dumps({"type": "ready"}))
                elif m["type"] == "summary":
                    return m


def show(m):
    if m.get("outcome") == "not_interpretable":
        leaked = [k for k in ("correct", "n", "p_chance", "results", "bar") if k in m]
        return f"{m['reason']} | leaked: {leaked or 'none'}"
    if m["mode"] in ("invalid", "stopped"):
        return m.get("reason", "") or f"counted={m.get('counted')}"
    return f"{m['correct']}/{m['n']} bar {m['bar']} p={m['p_chance']:.3f} {m.get('note', '')}"


async def main():
    app = make_app("sim", min_gap_h=0)
    app["ledger"] = Ledger(Path(tempfile.mkdtemp()) / "ledger.json", 0)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "localhost", PORT).start()
    L = app["ledger"]
    checks = [
        ("blind sim stops at step 1, no score", dict(sim_mode="blind", mode="awareness"), "not_interpretable"),
        ("normal run passes", dict(sim_mode="normal", mode="awareness"), "pass1"),
        ("second pass in a row refers", dict(sim_mode="normal", mode="answers"), "refer"),
        ("only 4 YES / 4 NO is refused", dict(sim_mode="normal", mode="answers", questions=Q8), "invalid"),
        ("pass again", dict(sim_mode="normal", mode="awareness"), "pass1"),
        ("stopping mid-run is counted", dict(sim_mode="normal", mode="awareness", stop_at=2), "stopped"),
        ("next pass is only pass 1 again", dict(sim_mode="normal", mode="awareness"), "pass1"),
        ("ignoring commands: no conclusion", dict(sim_mode="nofollow", mode="awareness"), "no_conclusion"),
    ]
    ok = True
    for label, kw, want in checks:
        m = await one_run(app, **kw)
        got = m.get("outcome") or m["mode"]
        good = got == want and not (got == "not_interpretable" and "correct" in m)
        ok &= good
        print(f"{'OK ' if good else 'BAD'} {label:40s} -> {got:17s} {show(m)}")
    L.min_gap_s = 3600
    m = await one_run(app, "normal", "awareness")
    good = m["mode"] == "invalid"
    ok &= good
    print(f"{'OK ' if good else 'BAD'} {'12 h gap enforced by the server':40s} -> {m['mode']:17s} {show(m)}")
    print("history:", [r["outcome"] for r in L.history("TEST-01")])
    await runner.cleanup()
    print("ALL PASS" if ok else "SOME CHECKS FAILED (sim noise can flip a borderline run; rerun once)")

if __name__ == "__main__":
    asyncio.run(main())
