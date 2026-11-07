"""Check that this laptop is ready to run StillHere.

Run from the stillhere folder, with the virtual environment active:
    python scripts/selfcheck.py

It checks Python, the packages, the web files and the signal analysis, then
says exactly what to fix. Nothing here needs the headset.
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, BAD, WARN = "  OK  ", "  FIX ", "  NOTE"
problems = 0


def line(tag, msg):
    print(f"[{tag}] {msg}")


def need(cond, ok_msg, fix_msg, required=True):
    global problems
    if cond:
        line(OK, ok_msg)
    else:
        line(BAD if required else WARN, fix_msg)
        if required:
            problems += 1


print("\nStillHere self-check\n")

# 1. Python
v = sys.version_info
need(v >= (3, 10), f"Python {v.major}.{v.minor}",
     f"Python {v.major}.{v.minor} is too old. Install Python 3.12 and make the venv again (see README, step 1).")
need(sys.prefix != getattr(sys, "base_prefix", sys.prefix), "Running inside a virtual environment",
     "Not inside a virtual environment. Run: source .venv/bin/activate (Mac) or .venv\\Scripts\\activate (Windows)",
     required=False)

# 2. Core packages
for mod in ("numpy", "scipy", "aiohttp"):
    try:
        importlib.import_module(mod)
        line(OK, f"{mod} installed")
    except Exception:
        line(BAD, f"{mod} missing. Run: pip install -r requirements.txt")
        problems += 1

# 3. Headset packages (needed only with the headset)
try:
    import pylsl  # noqa: F401
    line(OK, "pylsl (Lab Streaming Layer) works")
except Exception as e:
    line(WARN, f"pylsl not working ({type(e).__name__}). Needed for the headset. "
               "Run: pip install pylsl. On a Mac, if it says the LSL library was not found: "
               "brew install labstreaminglayer/tap/lsl")
try:
    import gpype  # noqa: F401
    line(OK, "g.Pype installed (headset driver)")
except Exception:
    line(WARN, 'g.Pype not installed. Needed for the headset. Run: pip install "gpype[all]"')

# 4. App files
web = ROOT / "web"
need((web / "index.html").exists(), "web/index.html found", "web/index.html missing: unzip the full folder again.")
fonts = list((web / "fonts").glob("*.woff2")) if (web / "fonts").exists() else []
need(len(fonts) >= 9, f"{len(fonts)} font files bundled (works offline)", "web/fonts is missing font files: unzip the full folder again.",
     required=False)

# 5. The analysis really finds a flicker and a left/right preference
try:
    import numpy as np
    from blindspot.analysis import score_segment, median_split, permutation_p, split_bar
    fs, t = 250, np.arange(0, 10, 1 / 250)
    rng = np.random.default_rng(0)
    names = ["socket1", "socket2", "socket3", "socket4"]

    def trial(left_gain):
        x = rng.normal(0, 6, (4, t.size))
        x += 1.2 * left_gain * np.sin(2 * np.pi * (60 / 7) * t) + 1.2 * (2.6 - left_gain) * np.sin(2 * np.pi * 12 * t)
        return score_segment(x, fs, names, {"left": 60 / 7, "right": 12.0})["snr"]

    ratios = [np.log(s["left"] / s["right"]) for s in [trial(1.8), trial(0.8)] * 6]
    calls = median_split(ratios)
    k = sum(c == want for c, want in zip(calls, ["left", "right"] * 6))
    need(k == 12 and split_bar(12) == 10 and abs(permutation_p(10, 12) - 0.040) < 0.001,
         "Signal analysis: finds the attended side on test data, bar 10 of 12, p = 0.040",
         f"Signal analysis gave {k}/12 on clean test data. Something is wrong with the install.")
except Exception as e:
    line(BAD, f"Signal analysis failed to run: {e}")
    problems += 1

print()
if problems:
    print(f"{problems} thing(s) to fix above. Fix them and run this again.\n")
    sys.exit(1)
print("All good. Next:")
print("  No headset:   python -m blindspot.server --source sim --min-gap-hours 0")
print("  With headset: see README, 'Connect the headset'")
print("  Then open http://localhost:8765 in Chrome.\n")
