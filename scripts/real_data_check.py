"""Run our analysis on real EEG recordings (not the simulator).

1) g.tec's BR41N.IO SSVEP dataset (2 people, 8 channels over the back of the
   head, LEDs flickering at 9, 10, 12 and 15 Hz, people looking straight at
   them). We only use 4 of the channels, like our Core-4.
   Get it with:  git clone https://github.com/bkowshik/br41n-ssvep
   Run:          python scripts/real_data_check.py --br41n br41n-ssvep/data

2) Optional: PhysioNet "EEG During Mental Arithmetic Tasks" (36 people, rest
   with eyes closed vs mental math), to see how often alpha at the back of the
   head drops during math. Download the EDF files from
   https://physionet.org/content/eegmat/1.0.0/ into a folder, then:
               python scripts/real_data_check.py --eegmat path/to/eegmat
   Needs pyedflib (comes with gpype[all], or: pip install pyedflib).
   Note: that study recorded rest and math as two separate blocks, and it
   doesn't say the eyes stayed closed during math, so treat it as a rough check.
"""

import argparse
import glob
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from blindspot.analysis import alpha_metrics, median_split, score_segment  # noqa: E402

BR41N_CH = ["PO7", "PO3", "POz", "PO4", "PO8", "O1", "Oz", "O2"]
USE4 = ["PO7", "POz", "PO8", "Oz"]
FREQS = [9, 10, 12, 15]


def br41n(folder):
    import scipy.io
    files = sorted(glob.glob(str(Path(folder) / "*.mat")))
    if not files:
        print(f"No .mat files in {folder}")
        return
    print("\nReal SSVEP recordings (g.tec BR41N.IO dataset), 4 of 8 channels\n")
    for path in files:
        m = scipy.io.loadmat(path)
        fs, y = float(m["fs"][0, 0]), m["y"]
        eeg, trig = y[1:9], y[9]
        idx = [BR41N_CH.index(c) for c in USE4]
        on = trig > 0
        edges = np.flatnonzero(np.diff(on.astype(int))) + 1
        starts = [e for e in edges if on[e]]
        ends = [e for e in edges if not on[e]]
        rows, on_snr, off_snr = [], [], []
        for s in starts:
            e = next((e for e in ends if e > s), None)
            if e is None:
                continue
            f = int(round(np.median(trig[s:e])))
            sc = score_segment(eeg[idx][:, s + int(0.5 * fs):e], fs, USE4, {str(k): k for k in FREQS})["snr"]
            rows.append((f, sc))
            on_snr.append(sc[str(f)])
            nxt = next((s2 for s2 in starts if s2 > e), None)
            if nxt and (nxt - e) > 2 * fs:  # gap with no flicker: same frequency, should be low
                off_snr.append(score_segment(eeg[idx][:, e + int(0.25 * fs):nxt], fs, USE4, {"a": f})["snr"]["a"])
        acc = sum(max(FREQS, key=lambda k: sc[str(k)]) == f for f, sc in rows)
        pair = [(f, np.log(sc["9"] / sc["12"])) for f, sc in rows if f in (9, 12)]
        calls = median_split([r for _, r in pair])
        k = sum(c == ("left" if f == 9 else "right") for c, (f, _) in zip(calls, pair))
        print(f"{Path(path).name}: picked the right flicker {acc}/{len(rows)} | "
              f"9 vs 12 Hz with our median split {k}/{len(pair)} | "
              f"step-1 SNR flicker {np.median(on_snr):.2f} vs no flicker {np.median(off_snr):.2f}")


def eegmat(folder):
    try:
        import pyedflib
    except ImportError:
        print("pyedflib missing: pip install pyedflib")
        return
    back = ["O1", "O2", "P3", "P4"]

    def load(path):
        f = pyedflib.EdfReader(path)
        labels = [l.replace("EEG ", "").replace("-A1", "").replace("-A2", "").strip() for l in f.getSignalLabels()]
        fs = f.getSampleFrequency(0)
        x = np.array([f.readSignal(i) for i in range(f.signals_in_file)])
        f.close()
        return x, labels, fs

    def mean_alpha(x, labels, fs):
        chans = [c for c in back if c in labels]
        win = int(10 * fs)
        vals = [alpha_metrics(x[:, s:s + win], fs, labels, channels=chans, mains_hz=50)["power"]
                for s in range(0, x.shape[1] - win + 1, win)]
        return float(np.mean(vals))

    rest = sorted(glob.glob(str(Path(folder) / "**" / "*_1.edf"), recursive=True)) or \
        sorted(glob.glob(str(Path(folder) / "**" / "*task-rest_eeg.edf"), recursive=True))
    if not rest:
        print(f"No EDF files found in {folder}")
        return
    drops = 0
    print("\nRest (eyes closed) vs mental math, alpha at O1/O2/P3/P4\n")
    for r in rest:
        m = r.replace("_1.edf", "_2.edf") if r.endswith("_1.edf") else r.replace("task-rest", "task-mentalArithmetic")
        if not Path(m).exists():
            continue
        a_rest, a_math = mean_alpha(*load(r)), mean_alpha(*load(m))
        drops += a_math < a_rest
        print(f"{Path(r).name}: alpha rest {a_rest:.2f}, math {a_math:.2f} -> {'drops' if a_math < a_rest else 'goes up'}")
    print(f"\nAlpha dropped during math in {drops} of {len(rest)} people.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--br41n", help="folder with the BR41N.IO .mat files")
    ap.add_argument("--eegmat", help="folder with the PhysioNet mental arithmetic EDF files")
    a = ap.parse_args()
    if not (a.br41n or a.eegmat):
        ap.print_help()
    if a.br41n:
        br41n(a.br41n)
    if a.eegmat:
        eegmat(a.eegmat)
