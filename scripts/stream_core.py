"""Stream a g.tec Unicorn BCI Core-4 (or Core-8) to Lab Streaming Layer.

Our device: Unicorn BCI Core-4, serial U4-2025.10.50 (4 dry electrodes on the
back of the head). g.Pype cannot discover the channel count, so a Core-4 must be
opened with channel_count=4. The serial is set by default so we never connect to
another team's headset in a room full of Unicorns.

g.Pype (g.tec's official Python SDK) reads the headset over Bluetooth and
publishes it as an LSL stream called "blindspot_eeg". BlindSpot then reads it:

    Terminal 1:  python scripts/stream_core.py
    Terminal 2:  python -m blindspot.server --source lsl

Install:  pip install "gpype[all]"
macOS:    give your terminal (or VS Code) Bluetooth permission in
          System Settings > Privacy & Security > Bluetooth. Without it the scan
          finds nothing and gives no error. No pairing needed.
          If macOS says libgtecble.dylib is blocked, open System Settings > Privacy & Security and click Allow Anyway (g.tec's own fix), then run again.
"""

import argparse

import gpype as gp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", type=int, default=4, help="4 for the BCI Core-4 (our device), 8 for a BCI Core-8")
    ap.add_argument("--serial", default="U4-2025.10.50", help="headset serial; '' = first one found (risky in a busy room)")
    ap.add_argument("--name", default="blindspot_eeg", help="LSL stream name")
    a = ap.parse_args()

    kwargs = {"channel_count": a.channels}
    if a.serial:
        kwargs["serial"] = a.serial

    p = gp.Pipeline()
    source = gp.BCICore(**kwargs)
    sender = gp.LSLSender(stream_name=a.name)
    p.connect(source, sender)

    p.start()
    input(f"Streaming {a.channels} channels as LSL '{a.name}'. Press Enter to stop.\n")
    p.stop()
    p.close()


if __name__ == "__main__":
    main()
