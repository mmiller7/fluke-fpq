#!/usr/bin/env python3
"""Build portable, no-install zipapps for the three CLI tools.

Produces `fluke-fpq/dist/`:

    fpq-meter.pyz      meter read/configure/download
    fpq-replay.pyz     replay / self-check
    fpq-export.pyz     summary / events / export / editor
    fpq-meter, ...     Unix launchers
    fpq-meter.cmd, ... Windows launchers
    README.txt         portable usage
    LICENSE

The shared modules are copied into each zipapp; the source stays modular.
`pyserial` is bundled so the serial tools work without installing anything.
`numpy`/`matplotlib` are optional and deliberately not bundled: they are
compiled per OS/architecture.  The export zipapp works without them; only
transient/PNG export needs them.

Usage:
    python3 build_portable.py [--out DIR]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipapp
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAIN = [
    "fpq_serial_meter.py",
    "fpq_serial_replay.py",
    "fpq_export.py",
]
COMMON = [
    "fpq_wire.py",
    "fpq_gap.py",
    "fpq_dump.py",
    "fpq_writer.py",
    "fpq_editor.py",
    "fpq_progress.py",
    "fpq_env.py",
]
TOOLS = {
    "fpq-meter": "fpq_serial_meter",
    "fpq-replay": "fpq_serial_replay",
    "fpq-export": "fpq_export",
}
SERIAL_TOOLS = {"fpq-meter", "fpq-replay"}

README = """\
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
`Scripts\\python.exe`, then `python`, then `py -3`.  Do not use `py -3` when
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
"""


def _serial_package() -> Path:
    try:
        import serial  # noqa: PLC0415
    except ImportError as exc:
        raise SystemExit(
            "pyserial is required to bundle the serial tools; "
            "install it with: python3 -m pip install pyserial"
        ) from exc
    return Path(serial.__file__).resolve().parent


def _stage(modules: list[Path], serial_pkg: Path | None, entry: str,
           tmp: Path) -> Path:
    stage = tmp / entry
    stage.mkdir()
    for path in modules:
        shutil.copy2(path, stage / path.name)
    if serial_pkg is not None:
        shutil.copytree(serial_pkg, stage / "serial",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (stage / "__main__.py").write_text(
        f"from {entry} import main\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return stage


def windows_launcher(name: str) -> str:
    """Batch launcher that prefers the active venv's Python.

    `py -3` is the system launcher and ignores `VIRTUAL_ENV`; a user with
    numpy/matplotlib/pyserial installed in an activated venv then sees
    "missing dependency" even though `python` can import it.  Prefer
    `%VIRTUAL_ENV%\\Scripts\\python.exe`, then `python`, then `py -3`.
    """
    return (
        "@echo off\r\n"
        "setlocal\r\n"
        "if not defined VIRTUAL_ENV goto trypython\r\n"
        'if not exist "%VIRTUAL_ENV%\\Scripts\\python.exe" goto trypython\r\n'
        f'"%VIRTUAL_ENV%\\Scripts\\python.exe" "%~dp0{name}.pyz" %*\r\n'
        "exit /b %errorlevel%\r\n"
        ":trypython\r\n"
        "where python >nul 2>nul\r\n"
        "if errorlevel 1 goto trypy\r\n"
        f'python "%~dp0{name}.pyz" %*\r\n'
        "exit /b %errorlevel%\r\n"
        ":trypy\r\n"
        f'py -3 "%~dp0{name}.pyz" %*\r\n'
    )


def _launchers(dist: Path, name: str) -> None:
    unix = dist / name
    unix.write_text(
        "#!/bin/sh\n"
        f'exec python3 "$(dirname "$0")/{name}.pyz" "$@"\n',
        encoding="utf-8",
    )
    unix.chmod(0o755)
    win = dist / f"{name}.cmd"
    win.write_text(windows_launcher(name), encoding="ascii")


def build(out: Path) -> int:
    modules = ([ROOT / name for name in MAIN]
               + [ROOT / "src_common" / name for name in COMMON])
    missing = [str(p) for p in modules if not p.is_file()]
    if missing:
        raise SystemExit("missing source module(s):\n  " + "\n  ".join(missing))
    serial_pkg = _serial_package()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fpq-portable-") as tmp_name:
        tmp = Path(tmp_name)
        for name, entry in TOOLS.items():
            stage = _stage(modules, serial_pkg if name in SERIAL_TOOLS else None,
                           entry, tmp)
            target = out / f"{name}.pyz"
            zipapp.create_archive(
                stage, target, interpreter="/usr/bin/env python3",
                compressed=True,
            )
            target.chmod(0o755)
            _launchers(out, name)
            with zipfile.ZipFile(target) as zf:
                names = set(zf.namelist())
                if "__main__.py" not in names:
                    raise SystemExit(f"{target}: missing __main__.py")
                if name in SERIAL_TOOLS and "serial/__init__.py" not in names:
                    raise SystemExit(f"{target}: bundled pyserial missing")
            print(f"built {target} ({target.stat().st_size:,} B)")

    (out / "README.txt").write_text(README, encoding="utf-8")
    license_src = ROOT / "LICENSE"
    if not license_src.is_file():
        raise SystemExit(f"missing license: {license_src}")
    shutil.copy2(license_src, out / "LICENSE")

    # Smoke-test the zipapps exactly as a user would run them.
    for name in TOOLS:
        target = out / f"{name}.pyz"
        r = subprocess.run([sys.executable, str(target), "--help"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise SystemExit(
                f"{target} --help failed ({r.returncode}):\n"
                f"{r.stderr.strip()[-1000:]}")
        print(f"verified {target.name} --help")

    print(f"\ndist ready: {out}")
    for path in sorted(out.iterdir()):
        print(f"  {path.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "dist",
                    help="output directory (default: fluke-fpq/dist)")
    args = ap.parse_args()
    return build(args.out.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
