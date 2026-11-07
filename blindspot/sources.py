"""Where the EEG comes from.

BrainFlowSource reads a real headset (g.tec Unicorn by default).
SimSource makes fake EEG with a flicker response for whichever half of the
screen is visible, so the whole app can be built and tested with no headset.
"""

from __future__ import annotations

import threading
import time

import numpy as np


class BrainFlowSource:
    def __init__(self, board: str = "unicorn", serial_number: str = ""):
        from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds

        ids = {
            "unicorn": BoardIds.UNICORN_BOARD,
            "synthetic": BoardIds.SYNTHETIC_BOARD,
        }
        self.board_id = ids[board]
        params = BrainFlowInputParams()
        if serial_number:
            params.serial_number = serial_number
        self.board = BoardShim(self.board_id, params)
        self.fs = BoardShim.get_sampling_rate(self.board_id)
        self.eeg_rows = BoardShim.get_eeg_channels(self.board_id)
        self.ts_row = BoardShim.get_timestamp_channel(self.board_id)
        try:
            self.channel_names = BoardShim.get_eeg_names(self.board_id)
        except Exception:
            self.channel_names = [f"ch{i}" for i in range(len(self.eeg_rows))]

    def start(self):
        self.board.prepare_session()
        self.board.start_stream(45000)

    def read_new(self):
        data = self.board.get_board_data()
        return data[self.eeg_rows], data[self.ts_row]

    def set_stimulus(self, left: float, right: float, attend=None):
        pass  # real brains do this part

    def stop(self):
        try:
            self.board.stop_stream()
        finally:
            self.board.release_session()


class LSLSource:
    """Reads any EEG stream on Lab Streaming Layer.

    Used for the g.tec Unicorn BCI Core-4 headband: g.Pype (g.tec's SDK) reads
    the headset and publishes it as an LSL stream (see scripts/stream_core.py),
    and BlindSpot reads that stream here. Works on macOS and Windows.
    """

    def __init__(self, stream_name: str = "", channel_names: list[str] | None = None,
                 use: list[int] | None = None):
        """use: 1-based socket numbers to keep, e.g. [1, 2, 3, 4] when only
        those sockets of a Core-8 base have electrodes. None keeps all."""
        from pylsl import StreamInlet, resolve_byprop, local_clock

        self._local_clock = local_clock
        if stream_name:
            streams = resolve_byprop("name", stream_name, timeout=10)
        else:
            streams = resolve_byprop("type", "EEG", timeout=10)
        if not streams:
            raise RuntimeError(
                "No LSL EEG stream found. Start scripts/stream_core.py first "
                "and check the headset is on.")
        self.inlet = StreamInlet(streams[0], max_buflen=60)
        info = self.inlet.info()
        self.fs = int(round(info.nominal_srate())) or 250
        n = info.channel_count()
        # We ignore the stream's own labels on purpose: with 8 channels and no
        # montage, g.Pype labels them with the standard Core-8 cap positions
        # (Fz ... PO8), which is wrong for electrodes moved to the back of the head.
        self.rows = [u - 1 for u in use] if use else list(range(n))
        if any(r < 0 or r >= n for r in self.rows):
            raise ValueError(f"--use must be socket numbers from 1 to {n}")
        if channel_names and len(channel_names) == len(self.rows):
            names = list(channel_names)
        else:
            names = [f"socket{r + 1}" for r in self.rows]
        self.channel_names = names
        self.n = len(self.rows)

    def start(self):
        self.inlet.open_stream(timeout=10)
        # LSL clock -> wall clock, so trial times line up with time.time()
        self._offset = time.time() - self._local_clock()
        self._remap = None  # set if the sender stamps on its own clock

    def read_new(self):
        samples, stamps = self.inlet.pull_chunk(timeout=0.0, max_samples=4096)
        if not stamps:
            return np.zeros((self.n, 0)), np.zeros(0)
        x = np.asarray(samples, dtype=float).T[self.rows]
        corr = self.inlet.time_correction(timeout=1.0)
        ts = np.asarray(stamps) + corr
        # g.Pype 4.x's LSLSender stamps samples with its own clock instead of
        # LSL's local_clock() (seen on macOS: ~12 days apart). Then no sample
        # falls inside any trial window and every run stops at step 1 with
        # "fewer than 2 sensors". If the newest stamp is far from "now", map
        # the sender's clock onto ours, keeping its (exact) sample spacing.
        # Bluetooth delivers in bursts up to ~0.7 s late, so the offset is the
        # SMALLEST lag seen (the most prompt chunk), allowed to creep up only
        # very slowly for real clock drift. A jump of more than 3 s (e.g. the
        # sender's clock restarting after a reconnect) starts it over.
        lag = self._local_clock() - ts[-1]
        if self._remap is None and abs(lag) > 5.0:
            self._remap = lag
        if self._remap is not None:
            if abs(lag - self._remap) > 3.0 or lag < self._remap:
                self._remap = lag
            else:
                self._remap += 0.001 * (lag - self._remap)
            ts = ts + self._remap
        return x, ts + self._offset

    def set_stimulus(self, left: float, right: float, attend=None):
        pass

    def stop(self):
        self.inlet.close_stream()


class SimSource:
    """Fake Unicorn: 8 channels at 250 Hz with background noise and alpha.

    When a half of the screen flickers, the opposite-side visual channel gets a
    small sine wave at that half's frequency (left field shows up on PO8, right
    field on PO7, both on Oz). The response scales with the contrast.
    """

    channel_names = ["Fz", "C3", "Cz", "C4", "Pz", "PO7", "Oz", "PO8"]

    def __init__(self, fs: int = 250, ssvep_uv: float = 1.2, seed: int | None = None):
        self.fs = fs
        self.ssvep_uv = ssvep_uv
        self.rng = np.random.default_rng(seed)
        self.freqs = {"left": 60 / 7, "right": 12.0}
        self.contrast = {"left": 0.0, "right": 0.0}
        self._lock = threading.Lock()
        self._t0 = None
        self._n_sent = 0
        # Demo knobs: "normal" follows commands, "blind" = no visual response
        # (eyes closed / visual pathway down), "nofollow" = sees the flicker but
        # ignores commands, "stuck" = always favors the left side.
        self.mode = "normal"

    def set_freqs(self, freqs: dict[str, float]):
        with self._lock:
            self.freqs = dict(freqs)

    def set_stimulus(self, left: float, right: float, attend=None):
        """attend: "left"/"right" simulates a person following the command
        (that side's response is boosted, as when you look at or focus on it)."""
        with self._lock:
            self.contrast = {"left": float(left), "right": float(right)}
            self.attend = attend

    def start(self):
        self._t0 = time.time()
        self._n_sent = 0

    def read_new(self):
        now = time.time()
        n_total = int((now - self._t0) * self.fs)
        n = n_total - self._n_sent
        if n <= 0:
            return np.zeros((8, 0)), np.zeros(0)
        idx = np.arange(self._n_sent, n_total)
        t = idx / self.fs
        with self._lock:
            freqs, contrast = dict(self.freqs), dict(self.contrast)
            attend = getattr(self, "attend", None)
        gain = {"left": 1.0, "right": 1.0}
        if self.mode == "blind":
            contrast = {"left": 0.0, "right": 0.0}
        if self.mode == "stuck" and attend in gain:
            gain["left"] = 1.8          # always favors the left, whatever was asked
        elif attend in gain and self.mode == "normal":
            gain[attend] = 1.8
        x = self.rng.normal(0, 6.0, (8, n))
        # alpha rhythm: bigger with eyes closed (attend = "rest"/"math"),
        # and in "normal" mode it drops while doing mental math
        alpha = 4.0
        if attend in ("rest", "math"):
            alpha = 10.0
            if self.mode == "blind":
                alpha = 1.0
            elif self.mode == "stuck" or (self.mode == "normal" and attend == "math"):
                alpha = 5.0
        x += alpha * np.sin(2 * np.pi * 10.0 * t + 0.3)
        w = {"PO7": {"left": 0.4, "right": 1.0},
             "Oz": {"left": 0.8, "right": 0.8},
             "PO8": {"left": 1.0, "right": 0.4}}
        for name, weights in w.items():
            ch = self.channel_names.index(name)
            for side in ("left", "right"):
                a = self.ssvep_uv * contrast[side] * weights[side] * gain[side]
                f = freqs[side]
                x[ch] += a * np.sin(2 * np.pi * f * t)
                x[ch] += 0.4 * a * np.sin(2 * np.pi * 2 * f * t)
        self._n_sent = n_total
        ts = self._t0 + t
        return x, ts

    def stop(self):
        pass
