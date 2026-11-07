"""Step 1: check the BCI Core-4 headset with g.tec's own live scope.

Based on g.Pype's BCI Core training example
(https://gpype.gtec.at/content/2_gpype_training/season3_gearing_up/s3e1_bci_core8.html),
opened as a Core-4 (4 channels) with our serial, U4-2025.10.50.

Run:   python scripts/check_headset.py
What to look for:
  - You should see 4 lines that look like brain waves. A flat or very noisy line
    means that electrode is not touching the scalp: part the hair under it.
  - Close your eyes for 10 seconds.       The lines should get bigger and more rhythmic
                                          (alpha waves, about 10 per second).
  - Open your eyes.                       They should calm down again.
If that happens, the sensors are on the visual part of your brain. Close the
window to stop.

Install first:  pip install "gpype[all]"
macOS: give VS Code / Terminal Bluetooth permission
(System Settings > Privacy & Security > Bluetooth), or the scan finds nothing.
If macOS says libgtecble.dylib is blocked, open System Settings > Privacy & Security and click Allow Anyway (g.tec's own fix), then run again.
"""

import gpype as gp

CHANNELS = 4             # BCI Core-4: g.Pype cannot detect this, so it must be set
SERIAL = "U4-2025.10.50"  # our headset; other teams' Unicorns will be in range

if __name__ == "__main__":
    app = gp.MainApp()
    p = gp.Pipeline()

    # Our headset by serial. No montage: the channels are named by socket.
    source = gp.BCICore(serial=SERIAL, channel_count=CHANNELS)

    # 1-30 Hz keeps brain rhythms, drops slow drift and muscle noise.
    bandpass = gp.Bandpass(f_lo=1, f_hi=30)
    notch60 = gp.Bandstop(f_lo=58, f_hi=62)  # US power-line hum

    scope = gp.TimeSeriesScope(amplitude_limit=50, time_window=10)

    p.connect(source, bandpass)
    p.connect(bandpass, notch60)
    p.connect(notch60, scope)
    app.add_widget(scope)

    p.start()
    app.run()
    p.stop()
    p.close()
