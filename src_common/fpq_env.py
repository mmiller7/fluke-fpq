"""Interpreter / virtual-environment diagnostics.

The Windows launchers used to call ``py -3``, which selects the system Python
launcher and **ignores an activated venv**.  A user who installed numpy,
matplotlib or pyserial in ``.venv`` then saw "missing dependency" even though
``python`` in the same shell could import it.

This module reports the interpreter that is actually running the tool, its
venv/system status and where a module was found, so the next report names the
real cause instead of guessing.  It is used by the `--help` machine-status
block and by the missing-dependency error.

Disclaimer: provided "as is", without warranty of any kind, express or
implied. Use at your own risk. Not affiliated with or endorsed by Fluke
Corporation.
"""
from __future__ import annotations

import os
import sys


def in_venv() -> bool:
    """True when the running interpreter is inside a virtual environment."""
    return bool(getattr(sys, "prefix", None)
                and getattr(sys, "base_prefix", None)
                and sys.prefix != sys.base_prefix)


def module_origin(name: str) -> str | None:
    """Path of an importable module, or None when it is missing."""
    try:
        from importlib.util import find_spec
        spec = find_spec(name)
    except (ImportError, ValueError):
        return None
    return getattr(spec, "origin", None) if spec is not None else None


def interpreter_lines() -> list[str]:
    """Human-readable executable/venv block (no imports of the target)."""
    lines = [f"  interpreter: {sys.executable}"]
    if in_venv():
        lines.append(f"  venv: {sys.prefix} (base {sys.base_prefix})")
    else:
        lines.append("  venv: none (system interpreter)")
    env = os.environ.get("VIRTUAL_ENV")
    if env:
        lines.append(f"  VIRTUAL_ENV: {env}")
    return lines


def missing_hint(name: str) -> str:
    """One-line hint naming the interpreter that should get the package."""
    origin = module_origin(name)
    if origin is not None:
        return f"  {name}: found at {origin}"
    lines = [f"  {name}: NOT found by this interpreter"]
    lines.extend(interpreter_lines())
    lines.append("  windows: activate the venv and run `python ...`, not "
                 "`py -3 ...` (py ignores VIRTUAL_ENV)")
    lines.append("  any OS: run the .pyz with the venv's python, or install "
                 "into it: python -m pip install -r requirements.txt")
    return "\n".join(lines)
