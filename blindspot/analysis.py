"""SSVEP scoring (started as BlindSpot, now used by StillHere).

Plain version: each half of the screen flickers at its own speed. If the eye
sees that half, the visual cortex pulses at that speed. We measure how strong
each speed is in the EEG compared with the speeds right next to it (the SNR).
A strong SNR means "seen". A weak SNR means "possibly missing".

Important: classify() only ever receives EEG and thresholds. It never sees which
side was hidden, so the blinded test cannot cheat.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, filtfilt, iirnotch, detrend

# Channels over the visual cortex on the g.tec Unicorn Hybrid Black.
VISUAL_CHANNELS = ("PO7", "Oz", "PO8")


def preprocess(x: np.ndarray, fs: float, mains_hz: float = 60.0) -> np.ndarray:
    """Remove drift, power-line hum, and very slow or very fast noise.

    x has shape (channels, samples).
    """
    x = detrend(np.asarray(x, dtype=float), axis=-1)
    b, a = iirnotch(mains_hz, 30.0, fs)
    x = filtfilt(b, a, x, axis=-1)
    b, a = butter(4, [3.0, 40.0], btype="bandpass", fs=fs)
    return filtfilt(b, a, x, axis=-1)


def amplitude_spectrum(x: np.ndarray, fs: float, pad_to_s: float = 20.0):
    """Hann-windowed FFT amplitude per channel, zero padded for fine bins."""
    n = x.shape[-1]
    n_fft = max(n, int(pad_to_s * fs))
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(x * win, n=n_fft, axis=-1))
    freqs = np.fft.rfftfreq(n_fft, 1.0 / fs)
    return freqs, spec


def snr_at(freqs: np.ndarray, spec: np.ndarray, f: float,
           exclude_hz: float = 0.25, band_hz: float = 1.0) -> np.ndarray:
    """Amplitude at f divided by the mean amplitude of nearby frequencies."""
    target = np.argmin(np.abs(freqs - f))
    dist = np.abs(freqs - f)
    neighbors = (dist > exclude_hz) & (dist <= band_hz)
    peak = spec[..., max(target - 1, 0): target + 2].max(axis=-1)
    noise = spec[..., neighbors].mean(axis=-1)
    return peak / np.maximum(noise, 1e-12)


def score_segment(data: np.ndarray, fs: float, channel_names: list[str],
                  freqs_by_side: dict[str, float], use_harmonic: bool = True,
                  mains_hz: float = 60.0, channels: list[str] | None = None) -> dict:
    """Score one trial.

    data: (channels, samples) for ALL EEG channels, in channel_names order.
    freqs_by_side: e.g. {"left": 8.571, "right": 12.0}
    Returns per-side SNR (averaged over the visual channels, fundamental plus
    second harmonic) and the per-channel detail for the slides.
    """
    quality = channel_quality(data, fs, channel_names, mains_hz)
    if channels:
        # Channel set locked at step 1: the same sensors for every scored trial.
        idx = [channel_names.index(c) for c in channels]
    else:
        idx = candidate_channels(channel_names)
        good = [i for i in idx if quality[channel_names[i]] == "ok"]
        if good:  # drop sensors that are flat or very noisy, if any are left
            idx = good
    x = preprocess(data[idx], fs, mains_hz)
    freqs, spec = amplitude_spectrum(x, fs)
    out = {"per_channel": {}, "snr": {}, "quality": quality,
           "used": [channel_names[i] for i in idx]}
    for side, f in freqs_by_side.items():
        s = snr_at(freqs, spec, f)
        if use_harmonic and 2 * f < 40:
            s = (s + snr_at(freqs, spec, 2 * f)) / 2.0
        out["snr"][side] = float(np.mean(s))
        out["per_channel"][side] = {
            channel_names[i]: float(v) for i, v in zip(idx, s)
        }
    return out


def candidate_channels(channel_names: list[str]) -> list[int]:
    """Visual-cortex channels if the headset labels them, else every channel
    (e.g. a 4-channel band worn on the back of the head)."""
    idx = [channel_names.index(c) for c in VISUAL_CHANNELS if c in channel_names]
    return idx if len(idx) >= 2 else list(range(len(channel_names)))


def channel_quality(data: np.ndarray, fs: float, channel_names: list[str],
                    mains_hz: float = 60.0) -> dict[str, str]:
    """Is each sensor touching the scalp?

    "flat"  = almost no signal (sensor not touching, or disconnected)
    "noisy" = huge signal (loose sensor, movement, or bad contact)
    "ok"    = normal EEG range
    Thresholds are rough starting points for dry sensors, in microvolts.
    """
    x = preprocess(data, fs, mains_hz)
    sd = x.std(axis=-1)
    out = {}
    for name, s in zip(channel_names, sd):
        if s < 0.5:
            out[name] = "flat"
        elif s > 100:
            out[name] = "noisy"
        else:
            out[name] = "ok"
    return out


def classify(snr: dict[str, float], thresholds: dict[str, float]) -> str:
    """Turn per-side SNR into a call: which side looks missing.

    Returns one of: "none_hidden", "left_hidden", "right_hidden", "both_hidden".
    """
    left_seen = snr["left"] >= thresholds["left"]
    right_seen = snr["right"] >= thresholds["right"]
    if left_seen and right_seen:
        return "none_hidden"
    if not left_seen and right_seen:
        return "left_hidden"
    if left_seen and not right_seen:
        return "right_hidden"
    return "both_hidden"


def attention_side(snr: dict[str, float], baseline_ratio: float) -> str:
    """Which side did the brain favor on this trial?

    Compares log(left SNR / right SNR) with the person's own baseline ratio,
    measured while both sides flickered and no instruction was given.
    The call is made from EEG only; it never sees the instruction.
    """
    r = np.log(max(snr["left"], 1e-6) / max(snr["right"], 1e-6))
    return "left" if r > baseline_ratio else "right"


def binomial_p(k: int, n: int, p: float = 0.5) -> float:
    """Chance of getting k or more right out of n by guessing (one-sided)."""
    from math import comb
    return float(sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def preset_bar(n: int, alpha: float = 0.05) -> int | None:
    """Smallest number correct out of n that a guesser reaches with probability
    <= alpha (one-sided). Fixed before the run starts. None if no score out of
    n can clear the bar (n too small)."""
    for k in range(n + 1):
        if binomial_p(k, n) <= alpha:
            return k
    return None


def thresholds_from_calibration(on_scores: list[dict], off_scores: list[dict],
                                default: float = 2.0) -> dict[str, float]:
    """Per-person thresholds, fixed BEFORE the blinded test starts.

    on_scores: trials where both halves flickered.
    off_scores: trials where both halves were blank gray.
    Threshold = midpoint between the person's "seen" and "not seen" SNR.
    """
    out = {}
    for side in ("left", "right"):
        on = [s["snr"][side] for s in on_scores]
        off = [s["snr"][side] for s in off_scores]
        if on and off:
            out[side] = float((np.mean(on) + np.mean(off)) / 2.0)
        elif on:
            out[side] = float(max(default, np.mean(on) / 2.0))
        else:
            out[side] = default
    return out


def log_ratio(snr: dict[str, float]) -> float:
    """log(left SNR / right SNR): positive = the left flicker won."""
    return float(np.log(max(snr["left"], 1e-6) / max(snr["right"], 1e-6)))


def median_split(ratios: list[float]) -> list[str]:
    """Commands are always half left / half right, so we just call the half of
    the trials with the highest left/right ratio "left". No baseline needed, and
    if everything drifts to one side it doesn't help anyone pass."""
    n = len(ratios)
    order = np.argsort(ratios)[::-1]
    calls = ["right"] * n
    for i in order[: n // 2]:
        calls[i] = "left"
    return calls


def permutation_p(k: int, n: int) -> float:
    """Exact one-sided p for k matches out of n under a balanced median split
    (n/2 left, n/2 right, order shuffled). Matches come in pairs: j correct
    "left" calls means k = 2j. Hypergeometric tail, no independence assumed."""
    from math import comb
    h = n // 2
    j = k // 2
    total = comb(n, h)
    return float(sum(comb(h, i) * comb(h, h - i) for i in range(j, h + 1)) / total)


def split_bar(n: int, alpha: float = 0.05) -> int | None:
    """Smallest k (matches out of n) with permutation_p <= alpha."""
    for k in range(0, n + 1, 2):
        if permutation_p(k, n) <= alpha:
            return k
    return None


def alpha_metrics(data, fs, channel_names, channels=None, mains_hz=60.0):
    """For the eyes-closed test. Looks at the alpha band (8-13 Hz), which is
    big at the back of the head when the eyes are closed and usually drops when
    someone does mental math.
    Returns {"power": log of mean 8-13 Hz amplitude, "prom": how much the alpha
    band sticks out above 4.5-7 Hz and 14-20 Hz}, averaged over the channels."""
    if channels:
        idx = [channel_names.index(c) for c in channels]
    else:
        idx = candidate_channels(channel_names)
    x = preprocess(data[idx], fs, mains_hz)
    freqs, spec = amplitude_spectrum(x, fs)
    band = (freqs >= 8) & (freqs <= 13)
    flank = ((freqs >= 4.5) & (freqs <= 7)) | ((freqs >= 14) & (freqs <= 20))
    a = spec[:, band].mean(axis=1)
    f = spec[:, flank].mean(axis=1)
    return {"power": float(np.mean(np.log(np.maximum(a, 1e-9)))),
            "prom": float(np.mean(a / np.maximum(f, 1e-9)))}
