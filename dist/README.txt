Fluke VR1710 / Power Log portable tools
=======================================

> **Disclaimer:** Provided as is, without warranty of any kind, express or
> implied; no guarantee of accuracy, completeness or fitness for any purpose.
> Use at your own risk; the authors accept no liability. Not affiliated with
> or endorsed by Fluke Corporation. This is not a calibrated instrument or a
> safety device.
>
> **License:** MIT License (see LICENSE in this folder).
>
> **Limitations:** 50 Hz operation is unsupported (60 Hz only); the official
> supported minimum is 70 V (below is best-effort); high-range/208 V
> behaviour is derived or interpolated, not raw-captured; byte-exact parity
> holds only where the guards/evidence reach; hardware operations are at your
> own risk.

Three self-contained Python zipapps.  No installation is needed; unpack this
folder and run the launcher for your OS.  Python 3.11 or newer is required.

  fpq-meter.pyz    read, configure, erase and download the meter
  fpq-replay.pyz   replay a .fpq over a serial port; self-check files
  fpq-export.pyz   summary, events, CSV/JSON export, editor

Run directly
------------

  Linux / macOS:
    ./fpq-meter.pyz info -p /dev/ttyUSB0

  Windows (activated venv recommended):
    python fpq-meter.pyz info -p COM3

The `fpq-meter`, `fpq-replay`, `fpq-export` shell launchers and the
`fpq-meter.cmd`, `fpq-replay.cmd`, `fpq-export.cmd` launchers are provided
for convenience.  The `.cmd` launchers prefer the active virtualenv's
`Scripts\python.exe`, then `python`, then `py -3`.  Do not use `py -3` when
you installed optional packages in a venv: the system launcher ignores
`VIRTUAL_ENV`, so numpy/matplotlib look missing.

Common tasks
------------

  fpq-meter.pyz info -p /dev/ttyUSB0
  fpq-meter.pyz download -p /dev/ttyUSB0 -o session.fpq
  fpq-meter.pyz download -p /dev/ttyUSB0 -o session.fpq.gz
  fpq-meter.pyz download -p /dev/ttyUSB0 --raw br1.bin --raw-only --resume
  fpq-meter.pyz download -p /dev/ttyUSB0 --raw-archive br1.tar.gz --raw-only --resume
  fpq-meter.pyz build-fpq br1.tar.gz -o br1.fpq
  fpq-meter.pyz resume -p /dev/ttyUSB0
  fpq-replay.pyz serve session.fpq -p /dev/ttyUSB0
  fpq-replay.pyz self-check --counters session.fpq
  fpq-export.pyz summary session.fpq.gz
  fpq-export.pyz events session.fpq

Notes
-----

- `pyserial` is bundled.  Serial port names: Linux `/dev/ttyUSB0`,
  macOS `/dev/cu.usbserial-*`, Windows `COM3`.
- A read pauses the meter; `resume` (or `download --resume`) sends `E`.
  `E` must be the last command.
- Page-boundary ACK is the default; `--no-page-ack` is the quiet-window
  fallback.
- `--gzip` writes `.fpq.gz`; `.gz` output names are also accepted.
- Transient waveform/PNG export needs `numpy` and `matplotlib` installed on
  the target machine.  They are not bundled because they contain
  OS/architecture-specific compiled code.  Normal summary/CSV/JSON/editor
  use only the standard library and the bundled pyserial.
- The tools are provided "as is"; see LICENSE and the disclaimer printed by
  each command.

Rebuild from the source tree:

  python3 build_portable.py
