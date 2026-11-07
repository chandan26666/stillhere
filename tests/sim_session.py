"""End-to-end check: start the server on fake EEG and act like the browser."""
import asyncio
import json
import aiohttp
from aiohttp import web
from blindspot.server import make_app

PORT = 8799


async def browser(mode, **kw):
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"http://localhost:{PORT}/ws") as ws:
            await ws.send_str(json.dumps({"type": "hello", "refresh": 60, "freqs": {"left": 60 / 7, "right": 12.0}}))
            await ws.receive_json()
            await ws.send_str(json.dumps({"type": "run", "mode": mode, **kw}))
            while True:
                m = await ws.receive_json()
                if m["type"] == "trial":
                    await ws.send_str(json.dumps({"type": "trial_started", "i": m["i"]}))
                    await asyncio.sleep(m["duration"])
                    await ws.send_str(json.dumps({"type": "trial_ended", "i": m["i"]}))
                elif m["type"] == "trial_done":
                    await ws.send_str(json.dumps({"type": "ready"}))
                elif m["type"] == "summary":
                    return m


async def main():
    runner = web.AppRunner(make_app("sim"))
    await runner.setup()
    await web.TCPSite(runner, "localhost", PORT).start()
    cal = await browser("calibrate", duration=8)
    print("calibrate thresholds:", {k: round(v, 2) for k, v in cal["thresholds"].items()})
    test = await browser("test", duration=8, trials=6)
    print(f"blinded test: {test['correct']}/{test['n']} correct")
    for r in test["results"]:
        print("  ", r["truth"], "->", r["predicted"], {k: round(v, 2) for k, v in r["snr"].items()})
    con = await browser("contrast", duration=8)
    for s in con["steps"]:
        print(f"   contrast {s['contrast']:.1f}: right SNR {s['snr_right']:.2f}")
    await runner.cleanup()

asyncio.run(main())
