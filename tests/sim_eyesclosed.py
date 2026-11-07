"""Eyes-closed (mental math vs rest) test on fake EEG.
Run: python -m tests.sim_eyesclosed   (about 6 minutes)"""
import asyncio, tempfile
from pathlib import Path
from aiohttp import web
import tests.sim_awareness as T
from blindspot.server import make_app, Ledger


async def main():
    app = make_app("sim", min_gap_h=0)
    app["ledger"] = Ledger(Path(tempfile.mkdtemp()) / "l.json", 0)
    r = web.AppRunner(app); await r.setup(); await web.TCPSite(r, "localhost", T.PORT).start()
    ok = True
    for sim_mode, want in [("blind", "not_interpretable"), ("normal", "pass1"), ("nofollow", "no_conclusion")]:
        m = await T.one_run(app, sim_mode, "eyesclosed", patient=f"EC-{sim_mode}")
        got = m.get("outcome") or m["mode"]
        ok &= got == want
        print(f"{'OK ' if got == want else 'BAD'} {sim_mode:9s} -> {got:17s} {T.show(m)}")
    await r.cleanup()
    print("ALL PASS" if ok else "SOME CHECKS FAILED")

if __name__ == "__main__":
    asyncio.run(main())
