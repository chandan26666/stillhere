"""Offline check: does the analyzer tell left / right / none apart on fake EEG?"""
import time
import numpy as np
from blindspot.analysis import score_segment, classify, thresholds_from_calibration
from blindspot.sources import SimSource

FREQS = {"left": 60 / 7, "right": 12.0}


def fake_trial(left, right, seconds=10, ssvep_uv=1.2, seed=None):
    s = SimSource(ssvep_uv=ssvep_uv, seed=seed)
    s.set_freqs(FREQS)
    s.set_stimulus(left, right)
    s._t0 = time.time() - seconds
    x, _ = s.read_new()
    return score_segment(x, s.fs, s.channel_names, FREQS)


def run(ssvep_uv, seconds, n=60, seed=0):
    rng = np.random.default_rng(seed)
    on = [fake_trial(1, 1, seconds, ssvep_uv, rng.integers(1e9)) for _ in range(2)]
    off = [fake_trial(0, 0, seconds, ssvep_uv, rng.integers(1e9)) for _ in range(2)]
    th = thresholds_from_calibration(on, off)
    stim = {"none_hidden": (1, 1), "left_hidden": (0, 1), "right_hidden": (1, 0)}
    conds = list(stim) * (n // 3)
    correct = 0
    for c in conds:
        sc = fake_trial(*stim[c], seconds, ssvep_uv, rng.integers(1e9))
        correct += classify(sc["snr"], th) == c
    return correct / len(conds), th


if __name__ == "__main__":
    for uv in (2.0, 1.2, 0.8, 0.5):
        for sec in (6, 10):
            acc, th = run(uv, sec)
            print(f"ssvep {uv:.1f} uV, {sec:>2}s trials: accuracy {acc:.0%}  thresholds L {th['left']:.2f} R {th['right']:.2f}")
