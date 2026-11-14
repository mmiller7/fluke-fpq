#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial>=3.5"]
# ///
"""fpq_serial_meter.py — interface a Fluke VR1710 voltage-quality recorder
over the RS-232/USB serial link: the control side (identification,
thresholds, banner) plus the memory-dump download, reproducing what Power
Log Classic 4.60 sends on this link (reverse-engineered from captured
sessions; docs/serial.md has the full byte-level map).

Subcommands:

  info       read-only snapshot: identity, counters, interval, thresholds and
             banner; --json for machine output, --transcript for the raw
             command/reply trace
  configure  change thresholds, transient recording, circular memory or the
             recording interval, with readback verification
  erase      erase the recorded data (destructive; requires --yes)
  resume     resume recording after a pause/read
  download   run Power Log's identification pass and the memory-dump
             download (wake -> ID pass -> V -> C + 0x06 ACK pump -> 0x04)
             and WRITE A .FPQ FILE (the format Power Log and fpq_export.py
             open). The item stream has no absolute time, so the newest
             record is anchored one interval before the arm instant (the
             moment the meter paused recording; saved in the sidecar) and
             rows are walked back by the interval. `--raw FILE` also keeps
             the item stream.
  build-fpq  convert an already-saved raw dump into a .fpq (same options,
             offline; opens no port).
             Records produced: G/F rows, K/H/5C 10-minute blocks, the L
             (Plt) rows computed from them, the stored D (dip), P (swell)
             and T (transient) events, and the host-derived I (interruption)
             / j (interval without measurements) gap records from the 06/09/0A
             counter timeline.  Absolute time comes from `--anchor` only;
             the row spacing, gaps and I/j are exact from the dump.
  debug send     send one raw command (no readback or verification)
  debug listen   passively read bytes for N seconds

Byte facts worth knowing before hand-experimenting (docs/serial.md):
  - the meter needs ~>=100 ms between BYTES: close-packed writes are
    dropped entirely (not even the '?' echo comes back). This tool spaces
    command bytes by `--bytegap` (default 0.15 s) and exchanges are spaced
    at least 1.45 s apart (`--exchangegap`; verified live 1.4 s = 100%
    replies).
  - echo protocol: two-char commands ('X 0', 'i 1', ...) echo the command
    char first; one-char status commands ('I', 'J', 'R', 'v', 'V') reply
    directly; '?' always echoes itself.
  - threshold triple: 'i 1' = dip, 'j 1' = swell, 'r 1' = umbral
    (transient sensitivity x100); write ops nudge one unit per iteration:
    p/P = dip -/+ 1, q/Q = swell -/+ 1, K/k = umbral +/- 1.
  - erase = 'B' (no reply, clears the record buffer only); resume =
    'E' [theory]. Both no-echo/no-reply.

Not affiliated with or endorsed by Fluke Corporation. "Fluke" and "VR1710"
are trademarks of their owner.

Run with `python3 fpq_serial_meter.py ...` (needs pyserial; see requirements.txt).
"""

import argparse
import gzip
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from datetime import datetime

# Shared modules live in src_common/ in the source tree and at the zipapp
# root in the portable build.  Add src_common when it exists.
_SRC_COMMON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "src_common")
if os.path.isdir(_SRC_COMMON) and _SRC_COMMON not in sys.path:
    sys.path.insert(0, _SRC_COMMON)

from fpq_wire import (
    umbral_raw_for_volts, umbral_volts, TRANSIENT_MIN_V, TRANSIENT_MAX_V,
    counter_clock,
    COUNTER_STEP_MINUTE as _COUNTER_STEP_MINUTE,
    COUNTER_STEP_HOUR as _COUNTER_STEP_HOUR,
    COUNTER_STEP_DAY as _COUNTER_STEP_DAY,
    PAGE_PAYLOAD, PAGE_TRAILER, PAGE_STRIDE,
    TRANSIENT_FRAMES, TRANSIENT_PAYLOAD, TRANSIENT_SAMPLES,
    crc16_xmodem as _crc16_xmodem,
    s12 as _s12,
    decode_transient_payload,
    looks_transient as _looks_transient,
)

# The counter/gap model, the dump walk and the .fpq writer now live in their
# own modules. Every moved name is re-exported here so existing callers,
# fpq_serial_replay.py and the guards need no edits.
from fpq_gap import (  # noqa: F401  (re-exported public surface)
    OLE_EPOCH, COUNTER_SCALE, DAY_SECONDS, COUNTER_DAY_TOD, COUNTER_DAY_CAP,
    CAL_SLACK_MIN, CAL_P_SLACK_MAX, CAL_PH_SLACK_MAX, GAP_ANALYSIS_ERRORS,
    COUNTER_STEP_MINUTE, COUNTER_STEP_HOUR, COUNTER_STEP_DAY,
    _counter_clock, _solve_elapsed_step, _solve_counter_step,
    _solve_counter_step_lenient, _counter_chain, _counter_score_p,
    _counter_score_ph, _counter_frac_penalty, _search_counter_phase,
    _strict_chain_ok, _calibrate_counter, _analyse_counter_gaps,
    _gap_reference, _pd_for_anchor, _ensure_gap_schedule, _gap_row_bytes,
    _anchor_pd_check, row_time_bounds, _wrap_cut, _stale_suffix_start,
)
from fpq_dump import (  # noqa: F401  (re-exported public surface)
    DUMP_PREFIX, INTERVAL_LABELS, STANDARD_INTERVALS, INTERVAL_HELP,
    INTERVAL_TYPICAL, interval_typical, interval_capacity_records,
    RING_COUNTER_UNITS, memory_use_percent, memory_used_records,
    interval_seconds, _item, _strip_page_footers, parse_dump, _median,
    derive_interval,
)
from fpq_writer import (  # noqa: F401  (re-exported public surface)
    FREQ_B, build_fpq, _header, _i_rec, _j_rec, _g_rec, _f_rec, _d_rec,
    _p_rec, _t_rec, _k_rec, _h_rec, _l_rec, _c5_rec,
)
from fpq_progress import Progress, BUILD_FPQ_STEPS
from fpq_env import interpreter_lines, module_origin


# Port ownership (METH-M03). The XP bridge takes an exclusive flock on this
# file before forwarding the link; the meter tool must not open the port while
# that lock is held (two readers would split the byte stream).  Detection is a
# non-blocking flock probe on the lock file - no bytes are written, and a
# missing/unlocked file is not an error.  `FLUKE_LOCK_DIR` overrides the search.
XP_LOCK_NAME = "xp-link.lock"


def _xp_lock_paths() -> list[str]:
    override = os.environ.get("FLUKE_LOCK_DIR")
    if override:
        return [os.path.join(override, XP_LOCK_NAME)]
    # Runtime state, deliberately independent of the repository layout: the
    # tool, bridges and forward all default to the same directory.
    return [os.path.join("/tmp/fluke-locks", XP_LOCK_NAME)]


def _bridge_owned_pty(port: str) -> bool:
    """True for the documented bridge link path.

    The bridge holds xp-link.lock for its lifetime; the tool that consumes the
    pty it created must be allowed through, or the bridge transport can never
    be used with the lock enabled.
    """
    lock_dir = os.environ.get("FLUKE_LOCK_DIR") or "/tmp/fluke-locks"
    return os.path.abspath(port) == os.path.abspath(
        os.path.join(lock_dir, "meter-pty"))


def xp_lock_holder() -> str | None:
    """Path of a held XP-link lock, or None when the port is free."""
    try:
        import fcntl
    except ImportError:                 # non-POSIX host: nothing to probe
        return None
    for path in _xp_lock_paths():
        if not os.path.exists(path):
            continue
        try:
            with open(path, "a+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return path
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            continue
    return None


def _integer_in_range(text: str, lo: int, hi: int, what: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{what} must be an integer")
    if not lo <= value <= hi:
        raise argparse.ArgumentTypeError(
            f"{what} must be {lo}-{hi} (integer)")
    return value


def threshold_volts(text: str) -> int:
    """Dip / Swell: integer volts, 0-300."""
    return _integer_in_range(text, 0, 300, "threshold (volts)")


def transient_volts(text: str) -> int:
    """Transient threshold: integer volts, 2-20."""
    return _integer_in_range(text, TRANSIENT_MIN_V, TRANSIENT_MAX_V,
                             "transient threshold (volts)")


def positive_float(text: str) -> float:
    """A finite positive number of seconds (argparse type)."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number")
    if not (value > 0 and value != float("inf")):
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


READ_GAP_MIN = 0.30      # a reply can take ~0.35 s; shorter gaps lose data
READ_GAP_MAX = 5.0


def read_gap_arg(text: str) -> float:
    """`--read-gap`: a positive fast-read gap inside the tested range."""
    value = positive_float(text)
    if not (READ_GAP_MIN <= value <= READ_GAP_MAX):
        raise argparse.ArgumentTypeError(
            f"must be between {READ_GAP_MIN:.2f} and {READ_GAP_MAX:.1f} s "
            f"(the meter's reply can take ~0.35 s)")
    return value


def positive_int(text: str) -> int:
    """A positive integer (argparse type)."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def nominal_volts(text: str) -> float:
    """Operator's system voltage -> the value Power Log writes in the header.

    The recorder itself has no nominal setting. Power Log stores 120.00 for
    120 V systems and 230.00 for its high range, which covers 208 V and 240 V
    systems in the captured corpus. Ask for the actual system voltage and map
    the high range to the observed header value.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be 120, 208 or 240")
    if value == 120:
        return 120.0
    if value in (208, 240):
        return 230.0
    raise argparse.ArgumentTypeError(
        "must be 120, 208 or 240 (the system voltage; 208/240 use Power "
        "Log's high-range header value 230.00)")


def nominal_hz(text: str) -> float:
    """Only the verified 60 Hz nominal is writable."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number")
    if abs(value - 60.0) > 0.01:
        raise argparse.ArgumentTypeError(
            "only 60 Hz is supported (the frequency codec is verified there)")
    return value


def anchor_arg(text: str) -> datetime:
    """Parse the end-of-recording anchor before any hardware is touched."""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "anchor must be 'YYYY-MM-DD HH:MM:SS'")


def decode_interval(i_raw: bytes, j_raw: bytes) -> int | None:
    """Decode the settings replies: BCD minutes in I, seconds in J."""
    m = re.search(rb"\xb0(.)(.)", i_raw, re.S)
    j = _last_int(j_raw) if j_raw else None
    if not m or j is None:
        return None
    tens = m.group(1)[0] - 0x30
    units = m.group(2)[0] & 0x0F
    if not (0 <= tens <= 9 and 0 <= units <= 9 and 0 <= j <= 999):
        return None
    return (10 * tens + units) * 60 + j


def decode_circular(v_raw: bytes) -> str | None:
    """Circular-memory state from the `v` reply's leading character.

    Captured on br1 (2026-10-31) with Power Log applying the settings:
    `v` is seven characters; ON starts with a digit (`0000000`) and OFF
    starts with `p` (`p000000`). The rest is the record counter, so the
    flag is only the first character.
    """
    text = _reply_value(v_raw, "v").decode("ascii", "replace").strip()
    if not text:
        return None
    if text[0] in "pP":
        return "off"
    if text[0].isdigit():
        return "on"
    return None


def interval_plan(current: int, target: int) -> list[tuple[str, int]]:
    """Power Log's step loop as (op, count) pairs.

    Captured on the wire (2026-10-26): every step is one single-byte
    exchange - `?T`, `?t`, `?U` or `?u` repeated count times. Digits never
    go on the wire; `?u30` is not part of the protocol.
    """
    delta = target - current
    if delta == 0:
        return []
    k = int(delta / 60 + (0.5 if delta > 0 else -0.5))
    rem = delta - 60 * k
    plan = []
    if k:
        plan.append(("T" if k > 0 else "t", abs(k)))
    if rem:
        plan.append(("U" if rem > 0 else "u", abs(rem)))
    return plan


def plan_text(plan) -> str:
    """Human form of an interval plan, e.g. 't x15, U x2'."""
    return " ".join(f"{op} x{n}" if n > 1 else op for op, n in plan)


def interval_wire(plan) -> list[str]:
    """The exact single-byte exchanges a plan puts on the wire."""
    return ["?" + op for op, n in plan for _ in range(n)]


def require_empty_memory(v, what: str = "recording interval"):
    """Fail closed: a missing counter read must never pass the erase gate."""
    if v is None:
        raise SystemExit("record counter unreadable (v); aborting instead of "
                         "assuming the memory is empty")
    if v:
        raise SystemExit(
            f"memory must be erased before changing the {what} (v={v}); "
            "run 'erase --yes' first")


def interval_sidecar_path(dump_path: str) -> str:
    return dump_path + ".interval"


def write_interval_sidecar(dump_path: str, seconds: int, source: str,
                           anchor: datetime | None = None,
                           counter: int | None = None,
                           hour_phase: float | None = None,
                           start: datetime | None = None) -> None:
    meta = {"interval_seconds": int(seconds), "source": source,
            "read_at": datetime.now().isoformat(timespec="seconds")}
    if anchor is not None:
        meta["anchor"] = anchor.replace(microsecond=0).isoformat()
    if counter is not None:
        meta["counter"] = int(counter)
    if hour_phase is not None:
        meta["hour_phase"] = float(hour_phase)
    if start is not None:
        meta["start"] = start.replace(microsecond=0).isoformat()
    with open(interval_sidecar_path(dump_path), "w") as f:
        json.dump(meta, f)
        f.write("\n")


def _raw_path_for_archive(archive_path: str) -> str:
    """Default raw path next to an archive: `NAME.tar.gz` -> `NAME.bin`."""
    base = archive_path
    low = base.lower()
    for suffix in (".tar.gz", ".tgz", ".tar"):
        if low.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base + ".bin"


def _archive_readme() -> bytes:
    return (
        "Fluke VR1710 portable raw capture\n"
        "==================================\n\n"
        "Contents: the unprocessed meter dump and its .interval sidecar.\n"
        "Build the .fpq on any machine with the same tool:\n\n"
        "    fpq-meter build-fpq THIS_ARCHIVE.tar.gz -o session.fpq\n\n"
        "The sidecar carries the recording interval and the arm-time anchor;\n"
        "keep both members together. The archive is read-only input.\n"
    ).encode("utf-8")


def write_raw_archive(archive_path: str, raw_path: str) -> str:
    """Tar-gzip the raw dump and its .interval sidecar (portable capture)."""
    sidecar = interval_sidecar_path(raw_path)
    if not os.path.isfile(raw_path):
        raise SystemExit(f"raw file not found: {raw_path}")
    if not os.path.isfile(sidecar):
        raise SystemExit(
            f"raw sidecar not found: {sidecar}\n"
            "The interval/anchor sidecar is required for a portable archive; "
            "pass --interval if the meter did not answer the settings read.")
    parent = os.path.dirname(os.path.abspath(archive_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as tf:
        tf.add(raw_path, arcname=os.path.basename(raw_path))
        tf.add(sidecar, arcname=os.path.basename(sidecar))
        info = tarfile.TarInfo("README.txt")
        payload = _archive_readme()
        info.size = len(payload)
        import io
        tf.addfile(info, io.BytesIO(payload))
    return archive_path


def extract_raw_archive(archive_path: str, dest_dir: str) -> tuple[str, str | None]:
    """Extract a portable raw archive; return (raw_path, sidecar_path)."""
    os.makedirs(dest_dir, exist_ok=True)
    names = []
    with tarfile.open(archive_path, "r:*") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = os.path.basename(m.name)
            if not name:
                continue
            with tf.extractfile(m) as src, open(os.path.join(dest_dir, name),
                                                 "wb") as dst:
                shutil.copyfileobj(src, dst)
            names.append(name)
    raws = [n for n in names
            if not n.endswith(".interval")
            and not n.lower().startswith("readme")
            and not n.lower().endswith((".txt", ".json"))]
    raws.sort(key=lambda n: (not n.lower().endswith((".bin", ".raw")), n))
    if not raws:
        raise SystemExit(f"{archive_path}: no raw dump member found")
    if len(raws) > 1:
        raise SystemExit(f"{archive_path}: more than one raw member: "
                         + ", ".join(raws))
    raw = os.path.join(dest_dir, raws[0])
    side = interval_sidecar_path(raw)
    return raw, (side if os.path.isfile(side) else None)


def _archive_out_name(archive_path: str) -> str:
    base = os.path.basename(archive_path)
    low = base.lower()
    for suffix in (".tar.gz", ".tgz", ".tar"):
        if low.endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base + ".fpq"


def read_interval_sidecar(dump_path: str):
    try:
        with open(interval_sidecar_path(dump_path)) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def sidecar_anchor_for_read(dump_path: str, arm: datetime | None,
                            counter: int | None) -> datetime | None:
    """Choose the arm time to store for a `download --raw` sidecar.

    A normal single read of a recording meter pauses it for the first time,
    so `arm` is the true end of the recording.  A repeated read of paused
    memory (for example the official Power Log read first, then the tool)
    merely re-pauses an idle meter: its arm is later than the recording's
    true end.  When the previous sidecar for the *same* raw path recorded
    both an anchor and the pre-arm record counter, and the meter's counter
    has not advanced, there are no new records, so the previous anchor is
    reused and the re-pause is not written over it.

    If the counter advanced, the previous sidecar is stale and `arm` wins.
    Missing/legacy metadata (no counter) or an unreadable counter also
    falls back to `arm`, so a single read is unchanged.
    """
    if arm is None or counter is None:
        return arm
    previous = read_interval_sidecar(dump_path)
    if not previous:
        print("note: first tool read of this raw path.  Standalone use is "
              "fine: this arm is the recording end.  If another client "
              "(Power Log) had already paused the meter, this arm is a "
              "re-pause and the absolute times carry that read-order gap; "
              "the recording data is unchanged and `fpq_export.py compare` "
              "recovers the earlier stop from a later logged pause.")
        return arm
    prev_anchor = previous.get("anchor")
    prev_counter = previous.get("counter")
    if prev_anchor is None or prev_counter is None:
        return arm
    try:
        unchanged = int(prev_counter) == int(counter)
        prev_arm = datetime.fromisoformat(prev_anchor)
    except (TypeError, ValueError):
        return arm
    if not unchanged:
        return arm
    print(f"note: no new records since the previous read (v = {counter}); "
          f"keeping the previous arm time {prev_anchor} instead of the "
          f"re-pause at {arm.replace(microsecond=0).isoformat()}")
    return prev_arm


def _resolve_interval(args, ticks, dump_path=None):
    """Interval + provenance: explicit option, download sidecar, inference."""
    if getattr(args, "interval", None) is not None:
        return (float(interval_seconds(args.interval)),
                getattr(args, "_interval_from", None) or "option")
    if dump_path:
        meta = read_interval_sidecar(dump_path)
        if meta and meta.get("interval_seconds") is not None:
            try:
                return (float(interval_seconds(meta["interval_seconds"])),
                        f"sidecar ({meta.get('source', 'settings')})")
            except ValueError:
                pass
    return derive_interval(ticks, None)


def _resolve_anchor(args):
    """Anchor + provenance: option, arm time, download sidecar, build clock.

    The newest record is the last interval completed when the meter paused
    recording (the arm), so that instant is the correct default.  A
    `build-fpq` of a saved dump reuses the arm time stored in the download
    sidecar; only when neither is available does the build clock stand in,
    and the caller warns about it.
    """
    anchor = getattr(args, "anchor", None)
    if isinstance(anchor, str):
        anchor = datetime.fromisoformat(anchor)
    if anchor is not None:
        return anchor, "option"
    arm = getattr(args, "_arm_time", None)
    if arm is not None:
        return arm, "arm time"
    dump_path = getattr(args, "dump", None)
    if dump_path:
        meta = read_interval_sidecar(dump_path)
        if meta and meta.get("anchor"):
            try:
                return (datetime.fromisoformat(meta["anchor"]),
                        "sidecar arm time")
            except ValueError:
                pass
    return datetime.now(), "build time"


# ------------------------------------------------------- host session log
# Absolute time is host-side: the meter has no clock (`R` is memory pointers),
# so Power Log anchors a download to the PC clock.  Record every pause (dump
# arm) and every resume (E) with the host time in a small JSONL log.  That is
# what lets a later read, or the `fpq_export.py compare` check, relate two
# recordings without asking the user for an anchor.  Set FPQ_SESSION_LOG to
# move it; the default is ~/.fpq-session.jsonl.
def session_log_path() -> str:
    env = os.environ.get("FPQ_SESSION_LOG")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".fpq-session.jsonl")


def record_session_event(event: str, at: datetime | None = None,
                         raw: str | None = None,
                         note: str | None = None) -> None:
    stamp = (at or datetime.now()).replace(microsecond=0)
    rec = {"at": stamp.isoformat(), "event": event}
    if raw:
        rec["raw"] = os.path.abspath(raw)
    if note:
        rec["note"] = note
    try:
        path = session_log_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass


def read_session_log(limit: int | None = None):
    path = session_log_path()
    try:
        with open(path) as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []
    return rows[-limit:] if limit else rows


def cmd_session(args):
    """Print the host pause/resume log (newest last)."""
    rows = read_session_log(getattr(args, "limit", None))
    if not rows:
        print(f"no session events in {session_log_path()}")
        return
    print(f"session log: {session_log_path()}")
    for r in rows:
        extra = f"  {r['note']}" if r.get("note") else ""
        raw = f"  {r['raw']}" if r.get("raw") else ""
        print(f"  {r['at']}  {r['event']}{raw}{extra}")


def parse_interval_arg(text: str) -> int:
    """argparse type for --interval: labels or seconds, validated."""
    try:
        return interval_seconds(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))

# Commands verified against captured/live Power Log Classic 4.60 sessions.
# Two-char commands ('X 0', 'i 1', ...) echo the command char first;
# one-char commands ('I', 'J', 'R', 'v', 'V') reply directly. '?' always
# echoes itself. (Echo-stripping happens in show()/identifier().)

WAKE = b" . . "  # sent by PowerLog right after opening the port

THRESHOLD_COMMANDS = [  # (char, internal key label in the fpq config)
    ("i", "Dip"),
    ("j", "Pico"),
    ("r", "Umbral"),
]
# The GUI names the three settings Dip / Swell / Transient.  Keep the
# internal keys (`dip`, `pico`, `umbral`) for JSON/format compatibility, but
# never print "Pico" or "Umbral" at the user-facing layer.
THRESHOLD_DISPLAY = {"Dip": "Dip", "Pico": "Swell", "Umbral": "Transient"}

# one-char read commands that may retry when their value tail is missing
READ_RETRY = {"I", "J", "R", "v"}
# blind write ops (echo only, no value); NEVER retried, a resend would
# double-step a threshold or erase twice
WRITE_CMDS = {"p", "P", "q", "Q", "K", "k", "x"}


def escape(b: bytes) -> str:
    """Bytes shown as hex pairs when not printable ASCII."""
    if all(32 <= c < 127 for c in b):
        return b.decode("ascii")
    return b.hex(" ")


VERBOSE = False


def _phase(msg: str) -> None:
    """Always-on progress line (stderr, so stdout stays parseable)."""
    print(f"* {msg}", file=sys.stderr, flush=True)


def _wire(direction: str, data: bytes) -> None:
    """Wire trace when --verbose; one line per command/reply."""
    if VERBOSE and data:
        print(f"  {direction} {escape(data)}", file=sys.stderr, flush=True)


class VR1710:
    """Serial link to the recorder, byte-paced like Power Log's own hosts.

    Real meters drop bytes written less than ~0.1 s apart (even the '?'
    echo is lost), so every single-byte write is followed by a byte-gap
    delay. Local/pty loopback devices tolerate any pacing, so the gap
    costs nothing there either.
    """

    # `auto` / `fast` = fast read-only exchanges with a sticky slow
    # fallback; `slow` keeps the historical 1.45 s gap.  The replay tool
    # keeps the slow default; the meter CLI sets these from --pacing.
    DEFAULT_PACING = "slow"
    DEFAULT_READ_GAP = 0.50

    def __init__(self, port: str, baud: int = 500000, quiet: float = 0.12,
                 bytegap: float = 0.15, exchangegap: float = 1.45,
                 pacing: str | None = None, read_gap: float | None = None):
        holder = xp_lock_holder()
        if holder is not None and not _bridge_owned_pty(port):
            raise OSError(
                f"the XP link lock is held ({holder}); another session owns "
                "the meter/port (a serial bridge is active). Stop "
                "it or wait for it to finish before opening the port.")
        if os.name != "nt" and not os.path.exists(port):
            raise SystemExit(f"serial port not found: {port}\n"
                             "Check the device name (for example "
                             "/dev/ttyUSB0) or start the meter bridge first.")
        if os.name != "nt" and os.path.isdir(port):
            raise SystemExit(f"serial port is a directory: {port}")
        try:
            import serial  # pyserial
        except ModuleNotFoundError:
            raise SystemExit(
                "pyserial is required to open the serial port.\n"
                "This interpreter:\n"
                + "\n".join(interpreter_lines())
                + "\nFix it with one of:\n"
                "  sudo apt-get install python3-serial\n"
                "  sudo yum install python3-pyserial\n"
                "  python3 -m venv .venv && . .venv/bin/activate && "
                "python3 -m pip install -r requirements.txt\n"
                "  windows: activate the venv and run `python ...`, not "
                "`py -3 ...`")

        self.ser = serial.Serial(port=port, baudrate=baud, bytesize=8,
                                 parity="N", stopbits=1, timeout=0.9,
                                 write_timeout=2.0)
        self.quiet = quiet
        self.bytegap = bytegap
        self.exchangegap = exchangegap
        pacing = self.DEFAULT_PACING if pacing is None else pacing
        if pacing not in ("auto", "fast", "slow"):
            raise ValueError(f"pacing must be auto/fast/slow, not {pacing!r}")
        self.fast_reads = pacing != "slow"
        self.fast_unreliable = False
        self.read_gap = (self.DEFAULT_READ_GAP if read_gap is None
                         else float(read_gap))
        self._last_talk = 0.0

    def write_paced(self, data: bytes):
        """Write one byte at a time with the inter-byte gap real meters need."""
        for b in data:
            n = self.ser.write(bytes([b]))
            if n != 1:
                raise TimeoutError(
                    "serial write incomplete (wrote %r of 1 byte); aborting "
                    "so a half command cannot reach the meter" % (n,))
            self.ser.flush()
            if self.bytegap:
                time.sleep(self.bytegap)

    def wake(self):
        self.write_paced(WAKE)

    def read_up_to_quiet(self, settle: float | None = None) -> bytes:
        """Read until the line stays silent for ~`settle` seconds."""
        settle = self.quiet if settle is None else settle
        buf = bytearray()
        prev = self.ser.timeout
        self.ser.timeout = settle
        try:
            while True:
                chunk = self.ser.read(1)
                if not chunk:
                    break
                buf += chunk
                self.ser.timeout = max(settle, 0.03)
                while True:
                    more = self.ser.read(self.ser.in_waiting or 1)
                    if not more:
                        break
                    buf += more
        finally:
            self.ser.timeout = prev
        return bytes(buf)

    def read_reply(self, first: float = 0.35,
                   settle: float = 0.06) -> bytes:
        """Read one reply burst, stopping shortly after its last byte.

        Official socat log (2026-11-11): the meter starts a reply in
        0.5-0.8 ms and sends the whole burst (even the multi-chunk `R`) in
        about 1 ms.  Waiting 0.4 s before reading and 0.9 s for silence was
        the settings read's main cost.  Wait up to `first` seconds for the
        first byte, then drain with a short inter-byte `settle`.  Fall back
        to the full quiet read when nothing arrives, so a genuinely slow
        meter keeps the old behaviour.
        """
        prev = self.ser.timeout
        buf = bytearray()
        try:
            self.ser.timeout = first
            chunk = self.ser.read(1)
            if not chunk:
                # `read_up_to_quiet` manages its own timeouts; the finally
                # block restores the caller's value afterwards.
                return self.read_up_to_quiet(0.9)
            buf += chunk
            self.ser.timeout = settle
            while True:
                more = self.ser.read(self.ser.in_waiting or 1)
                if not more:
                    break
                buf += more
        finally:
            self.ser.timeout = prev
        return bytes(buf)

    def talk(self, cmd: str, arg: str | None = None) -> bytes:
        """Single send path: everything goes through query() (the packed
        `?` + cmd [+ arg] stream, verified live). Kept as a named wrapper
        for `send`-style one-off commands.
        """
        return self.query(cmd, arg)

    def wait_exchange(self, gap: float | None = None):
        """Wait until `gap` seconds have elapsed since the last exchange.

        The meter answers at most one write exchange per ~1.2 s; 1.45 s was
        the verified default.  Official socat settings captures (2026-11-11)
        show read-only exchanges are safe much faster: p99 inter-request gap
        0.486 s, so read-only queries use `read_gap` (default 0.50 s) and a
        failed fast reply falls back to the full 1.45 s.  Writes always use
        `exchangegap`.
        """
        gap = self.exchangegap if gap is None else gap
        delta = time.monotonic() - self._last_talk
        if delta < gap:
            time.sleep(gap - delta)
        self._last_talk = time.monotonic()

    def query(self, cmd: str, arg: str | None = None,
              keepalive: bool = True) -> bytes:
        """One exchange exactly like Power Log's and verified live to be
        100% reliable: one paced byte stream `? <cmd> [? <arg>]` (each byte
        ~bytegap apart), then drain replies until quiet. Isolated lone '?'
        writes are answered only every second poll by the real meters, so
        the keepalive byte is always packed with the command instead.
        Returns the whole reply stream (echo included).
        """
        stream = b"?" + cmd.encode("ascii") if cmd else b"?"
        if arg is not None:
            stream += arg.encode("ascii")
        read_only = cmd not in WRITE_CMDS and cmd not in {"B", "E"}
        fast = self.fast_reads and not self.fast_unreliable and read_only
        self.wait_exchange(self.read_gap if fast else self.exchangegap)
        _wire(">", stream)
        self.write_paced(stream)
        buf = self.read_reply()
        _wire("<", buf)
        if ((arg is not None and cmd not in WRITE_CMDS)
                or cmd in READ_RETRY) and not self.has_tail(buf, cmd):
            # READS ONLY: the value part can be eaten by the meter's tick;
            # one retry. READ_RETRY = one-char read commands whose replies
            # can arrive eaten (I/J/R/v). Never retry a blind no-arg write
            # (p/P/q/Q/K/k/B/E) this way — a resent write would double-step
            # the threshold or double-erase. Writes verify by readback.
            if fast:
                # One fast retry with a longer first-byte deadline absorbs a
                # transient late reply without penalising the rest of the
                # session (observed live: one early failure, later runs
                # all at 7.25 s).
                self.wait_exchange(self.read_gap)
                _wire(">", stream)
                self.write_paced(stream)
                buf_fast = self.read_reply(first=0.60, settle=0.06)
                _wire("<", buf_fast)
                # `has_tail` only proves a value arrived, not that every
                # field did; the caller's `want`/threshold validation still
                # rejects a short reply (and the slow path below retries).
                if self.has_tail(buf_fast, cmd):
                    return buf_fast
            # The full slow path is always the next fallback, so a fast
            # read that fails still has the reliable reply available.
            time.sleep(self.exchangegap)
            self._last_talk = 0.0
            _wire(">", stream)
            self.write_paced(stream)
            buf2 = self.read_up_to_quiet(0.9)
            _wire("<", buf2)
            if self.has_tail(buf2, cmd):
                buf = buf2
                if fast:
                    self.fast_unreliable = True
                    print("note: fast settings reads were unreliable; using "
                          "the slow pacing for the rest of this session",
                          file=sys.stderr, flush=True)
        return buf

    def write_op(self, cmd: str, arg: str | None = None) -> bytes:
        """One blind write op (`B`, `E`, `p 1`, ...), never retried.

        The meter echoes the command and sends no value. A resent write
        would step a threshold twice or erase twice, so this path does
        exactly one exchange and returns whatever echoed.
        """
        stream = b"?" + cmd.encode("ascii")
        if arg is not None:
            stream += arg.encode("ascii")
        self.wait_exchange()
        _wire(">", stream)
        self.write_paced(stream)
        buf = self.read_reply(first=0.25, settle=0.06)
        _wire("<", buf)
        return buf

    def read_threshold(self, cmd: str, ch: int = 1) -> int | None:
        """Read one threshold (`i`/`j`/`r`) as an integer, or None.

        The meter sometimes eats a reply; retry a read, never a write.
        """
        for _ in range(3):
            value = _last_int(self.query(cmd, str(ch)))
            if value is not None:
                return value
            time.sleep(0.4)
        return None

    def read_counter(self) -> int | None:
        """The `v` record counter (drops to 0 after an erase).

        The reply is a zero-padded decimal whose width has varied in the
        captures (`0000060`, `0755718`, and once a stray leading byte), so
        width alone cannot tell a value from a truncated reply. Read it
        twice and accept only when both reads agree; otherwise retry. A
        quiet meter then returns None instead of a wrong counter.
        """
        for _ in range(3):
            a = _last_int(self.query("v"))
            b = _last_int(self.query("v"))
            if a is not None and a == b:
                return a
            time.sleep(0.4)
        return None

    def read_circular(self) -> str | None:
        """Circular-memory state from the `v` reply (read twice to agree)."""
        for _ in range(3):
            a = decode_circular(self.query("v"))
            b = decode_circular(self.query("v"))
            if a is not None and a == b:
                return a
            time.sleep(0.4)
        return None

    def read_interval(self) -> int | None:
        """Current recording interval from the I/J settings replies."""
        for _ in range(3):
            value = decode_interval(self.query("I"), self.query("J"))
            if value is not None:
                return value
            time.sleep(0.4)
        return None

    def read_settings(self) -> dict:
        """One Power Log settings pass: `I J v i1 j1 r1` (replies as bytes).

        This is the read part of Power Log's Apply sequence (captured
        2026-10-26): `B  I J v i1 j1 r1  <steps>  X0  I J v i1 j1 r1`.
        The erase (`B`) is deliberately left to the separate, approved
        `erase` command.
        """
        out = {}
        for key, cmd, arg in (("I", "I", None), ("J", "J", None),
                              ("v", "v", None), ("i1", "i", "1"),
                              ("j1", "j", "1"), ("r1", "r", "1")):
            out[key] = self.query(cmd, arg)
        return out

    def has_tail(self, buf: bytes, cmd: str) -> bool:
        """True if the reply carries something beyond just the echoes."""
        s = buf.lstrip(b"?").lstrip(cmd.encode())
        return any(32 <= c < 127 for c in s) and any(c for c in s)

    def identifier(self, text: bytes) -> bytes:
        # strip a leading echo of the command char (or the '?' keepalive)
        # when present
        return text[1:] if text[:1] in {b"?", b"W", b"C", b"X",
                                        b"i", b"j", b"r"} else text


def _reply_value(raw: bytes, cmd: str) -> bytes:
    """Strip the packed '?' keepalive and the optional command echo."""
    value = raw
    while value[:1] == b"?":
        value = value[1:]
    if cmd and value[:1] == cmd.encode("ascii"):
        value = value[1:]
    return value


def _last_int(raw: bytes) -> int | None:
    """Last run of decimal digits in a reply (thresholds, counters)."""
    m = re.search(rb"(\d{1,7})\s*$", raw)
    return int(m.group(1)) if m else None


def show(cmd: str, arg: str | None, raw: bytes, note: str = ""):
    # peel the packed keepalive echo ('?') and then the command echo
    value = raw
    if value[:1] in (b"?", cmd.encode()):
        value = value[1:]
    if value[:1] == cmd.encode():
        value = value[1:]
    label = f"{cmd} {arg}" if arg is not None else cmd
    tail = f"   {note}" if note else ""
    print(f"  {label:<6} reply: {escape(value)}{tail}")


# ---------------------------------------------------------------------------
# identification pass + memory-dump download (Power Log Classic 4.60 host)
# ---------------------------------------------------------------------------

# The Classic 4.60 connect-and-download sequence, byte for byte from the
# captured session: wake, then one identification pass with `?` heartbeat
# ticks between the commands, then the download.
CONNECT_PASS = [
    ("?", None), ("I", None), ("?", None), ("I", None), ("?", None),
    ("J", None), ("?", None), ("W", "Q"), ("?", None),
    ("i", "1"), ("?", None), ("j", "1"), ("?", None), ("r", "1"), ("?", None),
]

THRESHOLD_LABELS = {
    ("i", "1"): "ch1 Dip threshold", ("j", "1"): "ch1 Swell threshold",
    ("r", "1"): "ch1 Transient threshold",
}
PASS_NOTES = {
    "?": "(heartbeat echo)", "I": "(3-byte status, meaning open)",
    "J": "(decimal counter, meaning open)", "W Q": "(query, meaning open)",
    "C Q": "(session info query, meaning open)",
    "R": "(record-count info string)", "X 0": "(banner)",
}


def run_pass(vr: VR1710):
    """Send Power Log's identification pass, printing the replies.

    The `?` heartbeat entries in CONNECT_PASS are folded into the packed
    exchange stream (`query()` always sends `?` with the command, which is
    100% reliable live); each remaining entry becomes one exchange.
    Returns the raw replies keyed by "cmd arg" for the caller to reuse.
    """
    raws = {}
    for cmd, arg in CONNECT_PASS:
        if cmd == "?":
            continue
        raw = vr.query(cmd, arg)
        raws[f"{cmd} {arg}".strip() if arg else cmd] = raw
        label = f"{cmd} {arg}" if arg else cmd
        note = THRESHOLD_LABELS.get((cmd, arg), PASS_NOTES.get(label, ""))
        if (cmd, arg) == ("r", "1"):
            raw_int = _last_int(raw)
            note = "ch1 Transient threshold" + (
                f" = {umbral_volts(raw_int):.1f} V" if raw_int is not None
                else "")
        show(cmd, arg, raw, note)
    return raws


PAGE_ACK_DEFAULT = True


def add_page_ack_flags(p) -> None:
    """Add the shared page-ACK switch to a parser.

    Page-boundary ACK is the default because it is measured 3.1x faster
    and produced byte-identical dumps; `--no-page-ack` restores the older
    quiet-window path.
    """
    p.add_argument("--page-ack", dest="page_ack", action="store_true",
                   default=PAGE_ACK_DEFAULT,
                   help="acknowledge at the 1,029-byte page boundary "
                        "(default; measured 3.1x faster than the quiet "
                        "window on a full 1 s ring)")
    p.add_argument("--no-page-ack", dest="page_ack", action="store_false",
                   help="fallback: wait for line silence before each ACK "
                        "instead of acknowledging at the page boundary")


_HUNG_WARNED = False
_ARMED_DUMP_INTERRUPTED = False


def _warn_likely_hung(where: str = "") -> None:
    """One actionable stderr warning when the recorder stops answering.

    A silent meter is most often the known post-arm state: it has streamed
    a dump and is waiting for 0x06 ACKs.  The official software can hang the
    same way (owner, 2026-11-09), so the warning tells the user to stop and
    recover rather than retry blindly.
    """
    global _HUNG_WARNED
    if _HUNG_WARNED:
        return
    _HUNG_WARNED = True
    suffix = f" ({where})" if where else ""
    print(
        f"warning: the meter is not answering{suffix}; it is likely hung "
        "waiting for dump ACKs.\n"
        "warning: stop and try again; if it stays silent, send `resume` "
        "or power-cycle the recorder. The official software can hang the "
        "same way.", file=sys.stderr)


def dump(vr: VR1710, pause: float = 0.3, max_stalls: int = 20,
         force: bool = False, page_ack: bool = PAGE_ACK_DEFAULT,
         stamp: dict | None = None) -> bytes:
    """V -> preamble, C -> dump with the 0x06 ACK pump; returns ALL rx bytes.

    The meter streams continuously; by default the host ACKs each complete
    1,029-byte page at its boundary (measured 3.1x faster than the quiet
    window). With `page_ack=False` it instead starts a fresh 0x06 ACK
    whenever the line goes quiet for `pause` s (the real pump averages
    ~1 KB of dump per ACK). When a 0x06 is answered with 0x04 the dump is
    complete. Returns preamble + dump body + the closing 0x04.

    The pump cannot run forever: `max_stalls` consecutive quiet ticks with
    NO received data abort (cable pulled / meter stopped). Data itself may
    legitimately arrive late (slow reader), but a meter that answers ACK
    after ACK with silence is not a working stream.
    """
    global _ARMED_DUMP_INTERRUPTED
    _ARMED_DUMP_INTERRUPTED = False
    ACK = 0x06
    out = bytearray()
    # Bytes received since the last ACK was sent, INCLUDING the byte(s) the
    # meter pushes back while answering that ACK.  An ACK-gated meter starts
    # the next page immediately, so `send_ack()`'s reply read consumes that
    # page's first byte; not counting it leaves every later page one byte
    # short of PAGE_STRIDE and silently disables --page-ack.
    since_ack = 0
    # Check the record counter before arming anything. An erased memory
    # answers the dump arm with nothing and does not stream, and arming it
    # anyway can leave the meter unresponsive (observed live 2026-10-25).
    # `v` equals the V preamble, so a zero here means there is nothing to
    # read and the arm/C sequence is skipped entirely.
    val = vr.read_counter()
    if val == 0 and not force:
        # A single zero could be a corrupted reply (the meters occasionally
        # answer v with a stray leading byte or a truncated value), and
        # arming an empty meter can wedge it. Confirm before deciding: a
        # second zero (or a zero followed by silence) means empty; a larger
        # second reading overrides the glitch.
        time.sleep(0.4)
        val2 = vr.read_counter()
        if val2 is None or val2 == 0:
            print("< memory is empty (v = 0): dump not requested")
            return b"\x00\x00\x00\x00"
        val = val2
    if val is None:
        # An aborted dump leaves the meter streaming pages and waiting for
        # 0x06 ACKs; it then cannot answer `v`, so this is the *armed* state,
        # not an empty memory.  Clear it before raising, exactly as the
        # bad-preamble path below does.  Without this the meter stayed
        # silent and needed a power-cycle (2026-11-08 live C34 test).
        try:
            vr.write_paced(b"C")
            time.sleep(1.0)
            print("note: cleared a possibly armed dump after no v reply",
                  file=sys.stderr)
        except Exception:
            pass
        _warn_likely_hung("no v reply")
        raise TimeoutError("the meter did not answer v; not arming the dump")
    if stamp is not None:
        # The pre-arm record counter identifies whether a later read of the
        # same paused memory contains new records (used by the download
        # sidecar to keep the true arm time on repeated reads).
        stamp["counter"] = val
    if force and not val:
        # Diagnostic/trigger flow (owner 2026-10-27): erase -> set config ->
        # read memory -> continue. The empty arm answers a zero preamble and
        # is cleared with C below, which was verified live to recover.
        pass
    # Power Log's own tail, byte for byte [capture 2026-10-24]:
    #   ?C Q  ?R  ?X 0  ?V  C
    # with ~100-200 ms between bytes and no extra settle. The single C Q
    # pauses recording; there is no second pause after V.
    vr.wait_exchange()
    vr.ser.reset_input_buffer()
    # The arm pauses recording.  Its wall-clock instant is the only correct
    # anchor for the absolute timeline: the newest record is the last
    # interval completed at arm time.  Taking `datetime.now()` after the
    # dump instead shifts every row by the dump duration (minutes on a full
    # 1 s ring).
    if stamp is not None:
        stamp["arm"] = datetime.now()
    raw = b""
    for _attempt in range(2):
        vr.write_paced(b"?CQ?R?X0?V")
        time.sleep(0.6)
        raw = vr.read_up_to_quiet(0.6)
        # The tail ends with the 4-byte binary counter. A reply ending in
        # CR/LF or an ASCII digit means the V answer was eaten and the
        # buffer ends with banner/tail text; retry the arm once.
        if raw and not raw.endswith(b"\r\n") and not raw[-1:].isdigit():
            break
    pre = raw[-4:] if len(raw) >= 4 else b""
    if len(pre) != 4:
        # The arm may have been accepted and the meter may already be
        # streaming (waiting for 0x06 ACKs) even though the tail was not a
        # clean 4-byte preamble.  Never leave it armed: `C` clears the armed
        # dump, the same recovery used for a zero preamble.  Without this the
        # meter stayed silent and needed a power-cycle (2026-11-07 br2).
        try:
            vr.write_paced(b"C")
            time.sleep(1.0)
            print("note: cleared a possibly armed dump after a bad preamble",
                  file=sys.stderr)
        except Exception:
            pass
        _warn_likely_hung("bad dump preamble")
        raise TimeoutError("no 4-byte preamble in the C Q / R / X 0 / V "
                           "tail")
    out += pre
    print(f"< C Q / R / X 0 / V   preamble: {escape(pre)}")
    if pre == b"\x00\x00\x00\x00":
        # Fallback when v was non-zero but the arm still returned zero: send
        # C to clear the armed state, then stop. An erased memory streams
        # nothing (verified live 2026-10-25: 20 ACKs, no data, no EOT).
        print("< memory is empty (preamble 0): clearing the armed dump")
        vr.write_paced(b"C")
        time.sleep(1.0)
        vr.ser.reset_input_buffer()
        return bytes(out)
    vr.write_paced(b"C")
    vr.ser.timeout = 0.05
    acked = 0
    got_04 = False
    last_progress = 0
    stalls = 0
    started = last_report = time.monotonic()

    def send_ack() -> bool:
        """Acknowledge one page now; quiet fallback is handled by the caller.

        Returns True when the ACK produced any reply byte.  An empty reply
        is not always a stall: the real meter can append its unsolicited
        0x04 to the final page's read, so the page ACK then has nothing
        left to answer (handled by the caller).
        """
        nonlocal acked, got_04, stalls, since_ack
        vr.ser.write(bytes([ACK]))
        vr.ser.flush()
        acked += 1
        reply = vr.ser.read(1)
        if reply == b"\x04":
            vr.ser.timeout = pause
            follow = vr.ser.read(vr.ser.in_waiting or 1)
            if follow:
                # 0x04 was page data (a CRC can end in 0x04), not the EOT;
                # these bytes belong to the next page.
                out.extend(reply + follow)
                since_ack += len(reply) + len(follow)
                stalls = 0
                vr.ser.timeout = pause
                return True
            got_04 = True
            out.extend(reply)
            vr.ser.timeout = pause
            return True
        if reply:
            out.extend(reply)
            since_ack += len(reply)
            stalls = 0
            vr.ser.timeout = pause
            return True
        stalls += 1
        if stalls >= max_stalls:
            for _ in range(8):
                vr.ser.write(b"\x06"); vr.ser.flush()
                time.sleep(0.2)
                vr.ser.read(vr.ser.in_waiting or 1)
            _warn_likely_hung("dump stalled")
            raise TimeoutError(
                f"dump stalled: {stalls} x ACK with no data and no EOT "
                f"({len(out)} bytes received so far)")
        vr.ser.timeout = pause
        return False

    print("< dump started (the recorder is streaming; Ctrl-C to abort)",
          flush=True)
    _ARMED_DUMP_INTERRUPTED = True
    try:
        while not got_04:
            chunk = vr.ser.read(vr.ser.in_waiting or 1)
            if chunk:
                out.extend(chunk)
                since_ack += len(chunk)
                stalls = 0
                # A 0x04 at the end of a chunk is page data (a page's CRC can end
                # in 0x04) and the meter then waits for our ACK, so the line goes
                # quiet. Only a 0x04 that answers a 0x06 ACK is the closing EOT.
                now = time.monotonic()
                if (len(out) - last_progress >= 65536
                        or now - last_report >= 10.0):
                    last_progress = len(out)
                    last_report = now
                    print(f"  ... {len(out):,} bytes, {acked:,} ACKs, "
                          f"{now - started:.0f} s", flush=True)
                # ACK each full page as soon as its last byte arrives.  Keep
                # any bytes already received past the boundary (and the byte
                # `send_ack` reads back) so the next page is counted from its
                # true start instead of one byte short.
                while page_ack and since_ack >= PAGE_STRIDE and not got_04:
                    since_ack -= PAGE_STRIDE
                    if send_ack():
                        continue
                    # The real meter can send its closing 0x04 just after the
                    # final page, so the page ACK finds nothing left to answer.
                    # One remaining byte that is 0x04 past a whole page grid is
                    # that EOT, not page data: finish without another ACK.
                    if ((len(out) - 4) % PAGE_STRIDE == 1
                            and out[-1:] == b"\x04"):
                        got_04 = True
                    break
                continue
            # Partial page or quiet line: acknowledge what arrived.  If the
            # only byte past the last whole page is 0x04, the meter already sent
            # its closing EOT and is not waiting for another ACK.
            if (page_ack and len(out) >= 4 + PAGE_STRIDE
                    and (len(out) - 4) % PAGE_STRIDE == 1
                    and out[-1:] == b"\x04"):
                got_04 = True
                break
            since_ack = 0
            send_ack()
    except KeyboardInterrupt:
        # Best effort: a dump interrupted mid-stream leaves the meter
        # waiting for ACKs.  Clear the arm before letting the CLI report it.
        try:
            vr.write_paced(b"C")
            time.sleep(1.0)
            vr.ser.reset_input_buffer()
        except Exception:
            pass
        raise
    _ARMED_DUMP_INTERRUPTED = False
    print(f"{acked} x 0x06 ACKs sent; final ACK answered with 0x04 "
          f"(dump closed, {time.monotonic() - started:.1f} s)", flush=True)
    return bytes(out)

# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def _read_info_snapshot(vr, args):
    """Read identity, counters, interval and thresholds (open session)."""
    def read_text(cmd, arg=None, want=lambda b: bool(b.strip()), tries=3):
        raw = b""
        for _ in range(tries):
            raw = vr.query(cmd, arg)
            value = _reply_value(raw, cmd)
            if want(value):
                return raw
            time.sleep(0.5)
        return raw

    raw_i = read_text("I")
    if not _reply_value(raw_i, "I").strip():
        # Do not keep hammering a silent meter with J/v/R/X/threshold reads;
        # the extra commands are what leave it wedged.
        _warn_likely_hung("no I reply")
        raise TimeoutError("meter is not answering (no I reply); stopped "
                           "before sending more commands")
    _phase("info: reading J/v/R/X")
    raw_j = read_text("J", want=lambda b: _last_int(b) is not None)
    raw_v = read_text("v", want=lambda b: _last_int(b) is not None)
    raw_r = read_text("R")
    raw_x = read_text("X", "0", want=lambda b: b"FLUKE" in b)
    banner = _reply_value(raw_x, "X").strip().decode("ascii", "replace")
    channels = {}
    _phase("info: reading thresholds")
    # The `configure` subcommand has no --channels; default to 1 there.
    n_ch = max(1, int(getattr(args, "channels", 1) or 1))
    for ch in range(1, n_ch + 1):
        entry = {}
        for cmd, label in THRESHOLD_COMMANDS:
            entry[label.lower()] = vr.read_threshold(cmd, ch)
        channels[str(ch)] = entry
    data = {
        "banner": banner,
        "status_I": _reply_value(raw_i, "I").hex(" "),
        "counter_J": _last_int(raw_j),
        "record_counter_v": _last_int(raw_v),
        "circular_memory": decode_circular(raw_v),
        "record_info_R": _reply_value(raw_r, "R").decode("ascii", "replace"),
        "interval_seconds": decode_interval(raw_i, raw_j),
        "channels": channels,
    }
    pct = memory_use_percent(data["record_counter_v"],
                             data["interval_seconds"])
    cap = (interval_capacity_records(data["interval_seconds"])
           if data["interval_seconds"] is not None else None)
    data["memory_capacity_records"] = cap
    data["memory_capacity_units"] = RING_COUNTER_UNITS
    if pct is not None:
        used = data["record_counter_v"]
        data["memory_used_percent"] = round(pct, 1)
        data["memory_used_records"] = memory_used_records(used, cap and
                                                           data["interval_seconds"])
        data["memory_estimated"] = True
    else:
        data["memory_used_percent"] = None
        data["memory_used_records"] = None
    return data


def _read_info(args):
    """Open the meter, wake it, and take one info snapshot."""
    vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
                args.exchangegap)
    vr.wake()
    time.sleep(1.0)
    data = _read_info_snapshot(vr, args)
    if getattr(args, "resume", False):
        _send_resume(vr, "info")
        data["resumed"] = True
    return data


def _info_lines(data):
    """Human-readable info block (shared by `info` and `configure`)."""
    lines = [f"banner: {data['banner']!r}"]
    lines.append(f"status I: {data['status_I']}   "
                 f"counter J: {data['counter_J']}"
                 f"   record counter v: {data['record_counter_v']}")
    lines.append(f"record info R: {data['record_info_R']}")
    circ = data.get("circular_memory")
    lines.append(f"circular memory: {circ if circ is not None else 'unreadable'}")
    iv = data.get("interval_seconds")
    lines.append(f"recording interval: {iv} s" if iv is not None else
                 "recording interval: (unreadable)")
    pct = data.get("memory_used_percent")
    if pct is not None:
        cap = data.get("memory_capacity_records")
        used = data.get("memory_used_records")
        detail = (f"v {data['record_counter_v']:,} of "
                  f"{data.get('memory_capacity_units'):,} write units")
        if cap and used is not None:
            detail += f"; {used:,} of {cap:,} records at {iv} s"
        lines.append(f"memory used: {pct:.1f}% ({detail})")
    elif data.get("memory_capacity_units"):
        lines.append(f"memory used: (estimate unavailable; ring "
                     f"{data.get('memory_capacity_units'):,} write units)")
    for ch, entry in data.get("channels", {}).items():
        lines.append(f"channel {ch}: Dip={entry['dip']} V  "
                     f"Swell={entry['pico']} V"
                     f"  Transient={_transient_text(entry['umbral'])}")
    return lines


def _info_transcript(args):
    """Print the Power Log-style exchange trace (the old `probe`)."""
    vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
                args.exchangegap)
    print(f"port {args.port} @ {args.baud} 8N1 — sending wake-up + queries")
    vr.wake()
    time.sleep(1.0)
    raw = vr.query("")   # lone '?' heartbeat: echo check
    if not raw:
        _warn_likely_hung("no heartbeat echo")
        raise TimeoutError("meter is not answering (no heartbeat echo); "
                           "stopped before sending more commands")
    show("?", None, raw, "(heartbeat echo)")
    raw = vr.query("I")
    show("I", None, raw, "(3-byte status, meaning open)")
    raw = vr.query("J")
    show("J", None, raw, "(decimal counter, meaning open)")
    for c, label in THRESHOLD_COMMANDS:
        for ch in range(1, args.channels + 1):
            raw = vr.query(c, str(ch))
            show(c, str(ch), raw,
                 f"ch{ch} {THRESHOLD_DISPLAY.get(label, label)} threshold")
    raw = vr.query("R")
    show("R", None, raw, "(record-count info string)")
    raw = vr.query("X", "0")
    balanced = _reply_value(raw, "X")
    print(f"  X 0    banner: {balanced.strip()!r}")
    raw = vr.query("v")
    value = _reply_value(raw, "v").decode("ascii", "replace")
    circ = decode_circular(raw)
    print(f"  v      reply: {value.strip()!r}  (circular memory: {circ})")
    if getattr(args, "resume", False):
        _send_resume(vr, "info")
    else:
        warn_paused()


def cmd_info(args):
    """Read-only snapshot of everything the meter will report."""
    if args.transcript:
        _info_transcript(args)
        return
    _phase(f"info: opening {args.port} @ {args.baud}")
    _phase("info: waking the meter")
    data = _read_info(args)
    if args.json:
        print(json.dumps(data, indent=2))
        if not data.get("resumed"):
            warn_paused()
        return
    print("\n".join(_info_lines(data)))
    if not data.get("resumed"):
        warn_paused()


def cmd_erase(args):
    if not args.yes:
        raise SystemExit("erase is destructive: re-run with --yes")
    _phase(f"erase: opening {args.port} @ {args.baud}")
    _phase("erase: waking the meter")
    vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
                args.exchangegap)
    vr.wake()
    time.sleep(1.0)
    before = vr.read_counter()
    print(f"record counter before: {before!r}")
    if before is None:
        # Never send B to a meter that is not answering: a blind erase on a
        # wedged link is the worst case.
        _warn_likely_hung("no v reply")
        raise TimeoutError("meter is not answering (no v reply); erase NOT "
                           "sent")
    _phase("erase: sending B (destructive)")
    vr.write_op("B")
    after = None
    for attempt in range(6):          # the counter updates on a later pass
        time.sleep(0.8)
        after = vr.read_counter()
        _phase(f"erase: waiting for counter 0 (attempt {attempt + 1}/6, "
               f"v={after!r})")
        if after == 0:
            break
    print(f"record counter after:  {after!r}")
    if after != 0:
        print("warning: the counter is not 0; the erase may not have "
              "completed (the meter does not acknowledge B)",
              file=sys.stderr)
    if args.resume:
        if after == 0:
            _phase("erase: sending E (resume)")
            _send_resume(vr, "erase")
        else:
            print("resume skipped: erase not confirmed (counter "
                  f"{after!r}); recording left paused", file=sys.stderr)


def cmd_resume(args):
    _phase(f"resume: opening {args.port} @ {args.baud}")
    vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
                args.exchangegap)
    vr.wake()
    time.sleep(1.0)
    _send_resume(vr, "resume", raw=getattr(args, "raw", None))


def _transient_text(raw) -> str:
    """Raw umbral register -> the volts (or disabled) the user sees."""
    if raw is None:
        return "unknown"
    if int(raw) == 300:
        return "disabled"
    return f"{umbral_volts(raw):.1f} V"


def _threshold_text(name: str, value) -> str:
    if name == "umbral":
        return _transient_text(value)
    return f"{value} V"


def transient_target(transient):
    """Transient volts (already range-checked) -> raw register target."""
    if transient is None:
        return None
    return umbral_raw_for_volts(transient)


def _read_setting_state(vr, channel):
    """Read the settings the official software shows on entry.

    After an erase or an interval write the first I/J pair can be missing;
    re-query only that pair before calling the interval unknown.
    """
    st = vr.read_settings()
    interval = decode_interval(st.get("I", b""), st.get("J", b""))
    for _ in range(3):
        if interval is not None:
            break
        time.sleep(0.5)
        st["I"] = vr.query("I")
        st["J"] = vr.query("J")
        interval = decode_interval(st["I"], st["J"])

    def threshold(key, cmd):
        # read_settings already fetched channel 1's i/j/r; reuse those
        # replies instead of spending three more exchanges (~9 s).
        if channel == 1:
            value = _last_int(st.get(key) or b"")
            if value is not None:
                return value
        return vr.read_threshold(cmd, channel)

    return {
        "dip": threshold("i1", "i"),
        "pico": threshold("j1", "j"),
        "umbral": threshold("r1", "r"),
        "interval": interval,
        "circular": decode_circular(st.get("v", b"")),
        "counter": _last_int(st.get("v", b"")),
    }


def _print_setting_state(title, state):
    def volts(v):
        return "unreadable" if v is None else f"{v} V"
    iv = state["interval"]
    print(title)
    print(f"  Dip threshold:        {volts(state['dip'])}")
    print(f"  Swell threshold:      {volts(state['pico'])}")
    print(f"  Transient recording:  {_transient_text(state['umbral'])}")
    print(f"  recording interval:   "
          f"{'unreadable' if iv is None else f'{iv} s'} "
          f"{'' if iv is None else '(' + interval_typical(iv) + ')'}")
    circ = state.get("circular")
    print(f"  circular memory:      "
          f"{'unreadable' if circ is None else circ}")


def cmd_configure(args):
    dip = args.dip_voltage
    swell = args.swell_voltage
    sens = args.transient_sensitivity
    transient_recording = args.transient_recording
    interval = args.recording_interval
    circular = args.circular_memory
    toggle = args.toggle_circular_memory
    apply = args.apply

    if circular is not None and toggle:
        raise SystemExit("choose --circular-memory on|off or "
                         "--toggle-circular-memory, not both")
    if transient_recording == "off" and sens is not None:
        raise SystemExit("--transient-recording off and --transient-sensitivity "
                         "are mutually exclusive")
    if args.resume and interval is None:
        raise SystemExit("--resume only applies with --recording-interval")
    umbral = None
    if transient_recording == "off":
        umbral = 300                      # the meter's disabled sentinel
    elif transient_recording == "on":
        # Re-enabling the official way returns to 20 V (captured 2026-10-27).
        umbral = umbral_raw_for_volts(20 if sens is None else sens)
    elif sens is not None:
        umbral = transient_target(sens)

    targets = {"dip": dip, "pico": swell, "umbral": umbral}
    wants_thresholds = any(v is not None for v in targets.values())
    wants_any = (wants_thresholds or interval is not None
                 or circular is not None or toggle)

    if not wants_any:
        print("Settings you can change (all optional):")
        print("  --dip-voltage V               Dip threshold, 0-300 V")
        print("  --swell-voltage V             Swell threshold, 0-300 V")
        print("  --transient-sensitivity V     Transient sensitivity, 2-20 V")
        print("  --transient-recording on|off  Record transient waveforms")
        print("  --recording-interval INT      typical continuous measurement:")
        for secs in STANDARD_INTERVALS:
            print(f"      {INTERVAL_LABELS[secs]:>3}  "
                  f"{INTERVAL_TYPICAL[secs]}")
        print("  --circular-memory on|off      Keep recording when memory is "
              "full, or stop")
        print("  --toggle-circular-memory      Flip circular memory once")
        print("")
        print("Example: configure --dip-voltage 114 --swell-voltage 126 --apply")
        print("Without --apply, only the current settings and the plan are "
              "shown.")
        return

    if interval is not None:
        if wants_thresholds or circular is not None or toggle:
            raise SystemExit("configure --recording-interval on its own, not "
                             "with thresholds or circular memory")
        vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
                    args.exchangegap)
        vr.wake()
        time.sleep(1.0)
        _phase("configure: reading the current interval ...")
        state = _read_setting_state(vr, args.channel)
        _print_setting_state("Current settings:", state)
        current = state["interval"]
        if current is None:
            raise SystemExit("no interval readback; aborting")
        plan = interval_plan(current, interval)
        print(f"planned recording interval: {current} s -> {interval} s "
              f"({plan_text(plan) or 'no change'})")
        if not apply:
            print("preview only; re-run with --apply to write the change")
            return
        require_empty_memory(vr.read_counter())
        _phase(f"configure: writing interval step(s) for {plan_text(plan) or 'no change'} ...")
        for op, count in plan:
            for _ in range(count):
                vr.write_op(op)
        vr.query("X", "0")
        time.sleep(0.5)
        _phase("configure: interval readback ...")
        state = _read_setting_state(vr, args.channel)
        _print_setting_state("New settings:", state)
        if state["interval"] != interval:
            print("warning: readback differs; no retry (verify by "
                  "power-cycling and re-reading)", file=sys.stderr)
        # The info re-read must stay BEFORE the resume `E`: reads after `E`
        # stop the recording again. Meter is paused here, so it is free.
        _post_apply_info(vr, args)
        if args.resume:
            vr.write_op("E")
            print("recording resumed")
        else:
            print("configuration staged; it activates at the next recording "
                  "start (erase -> configure -> read -> resume). Recording "
                  "left paused.")
        return

    if circular is not None or toggle:
        if wants_thresholds:
            raise SystemExit("run circular-memory changes on their own, not "
                             "with thresholds")
        vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
                    args.exchangegap)
        vr.wake()
        time.sleep(1.0)
        # Power Log's circular Apply is exactly
        # `?X0 ?I ?J ?v ?i1 ?j1 ?r1 ?x0`; the leading X0 is part of it.
        _phase("configure: reading the current circular-memory state ...")
        vr.query("X", "0")
        state = _read_setting_state(vr, args.channel)
        _print_setting_state("Current settings:", state)
        current = state["circular"]
        if current is None:
            raise SystemExit("circular-memory state unreadable (v); aborting "
                             "instead of toggling blind")
        target = None if toggle else circular
        plan = (f"{current} -> {'toggle' if target is None else target}")
        print(f"planned circular memory: {plan}")
        if not apply:
            print("preview only; re-run with --apply to write the change")
            return
        if target is not None and target == current:
            print(f"circular memory is already {current}; nothing sent")
            return
        require_empty_memory(state["counter"], "circular-memory state")
        # The captured Apply is one `?x0` toggle; `v` reports the state.
        # Do not add reads between the settings pass and x0, and give the
        # meter time to commit before reading v back.
        vr.query("x", "0")
        time.sleep(2.0)
        after = vr.read_circular()
        if after is None:
            print("warning: circular-memory state unreadable after the "
                  "toggle; verify on the meter display", file=sys.stderr)
        else:
            print(f"circular memory: {current} -> {after}")
            if target is not None and after != target:
                print("warning: readback did not reach the requested state; "
                      "no retry", file=sys.stderr)
        _post_apply_info(vr, args)
        return

    vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
                args.exchangegap)
    vr.wake()
    time.sleep(1.0)
    _phase("configure: reading current settings ...")
    state = _read_setting_state(vr, args.channel)
    _print_setting_state("Current settings:", state)

    planned = []
    labels = {"dip": "Dip threshold", "pico": "Swell threshold",
              "umbral": "Transient recording"}
    for name, target in targets.items():
        if target is not None:
            shown = ("disabled" if name == "umbral" and target == 300
                     else _threshold_text(name, target))
            planned.append(f"  {labels[name]}: -> {shown}")
    if planned:
        print("Planned changes:")
        print("\n".join(planned))

    if not apply:
        print("preview only; re-run with --apply to write the changes")
        return

    spec = {"dip": ("i", "p", "P"),
            "pico": ("j", "q", "Q"),
            "umbral": ("r", "k", "K")}
    for name, target in targets.items():
        if target is None:
            continue
        read_cmd, down_cmd, up_cmd = spec[name]
        cur = state[name]
        if cur is None:
            raise SystemExit(f"{labels[name]}: no readback; aborting")
        steps = 0
        while cur != target:
            if steps >= args.max_steps:
                raise SystemExit(f"{labels[name]}: stopped at "
                                 f"{_threshold_text(name, cur)} after "
                                 f"{steps} steps (--max-steps)")
            direction = up_cmd if cur < target else down_cmd
            burst = min(abs(target - cur), args.burst,
                        args.max_steps - steps)
            for _ in range(burst):
                vr.write_op(direction, "1")
            steps += burst
            new = vr.read_threshold(read_cmd, args.channel)
            print(f"  {labels[name]}: {steps} step(s) -> "
                  f"{_threshold_text(name, new) if new is not None else None}")
            if new is None:
                raise SystemExit(f"{labels[name]}: lost readback; aborting")
            if new == cur:
                raise SystemExit(f"{labels[name]}: readback did not change; "
                                 "aborting to avoid a blind write loop")
            if (new - cur) * (target - cur) < 0:
                raise SystemExit(f"{labels[name]}: readback moved the wrong "
                                 "way; aborting")
            cur = new

    # Power Log's threshold Apply ends with X0; keep the same order.
    _phase("configure: sending X0 and reading back ...")
    vr.query("X", "0")
    time.sleep(0.5)
    state = _read_setting_state(vr, args.channel)
    _print_setting_state("New settings:", state)
    _post_apply_info(vr, args)


def _post_apply_info(vr, args):
    """Same-session full `info` re-read: what is now actually in the meter.

    Every `configure --apply` path ends with this so the user can compare
    the live state against the "Current settings" block printed before the
    change. The meter is already paused by any exchange, so the extra reads
    cost nothing; on the interval path it runs before the final `E` resume
    (reads after `E` stop the recording again).
    """
    try:
        data = _read_info_snapshot(vr, args)
    except Exception as exc:          # noqa: BLE001 - report, keep the change
        print(f"warning: post-apply info re-read failed ({exc}); the "
              "settings above are the applied values", file=sys.stderr)
        return
    print("info after configure (read back with `info`; compare with the "
          "\"Current settings\" block above):")
    print("\n".join(_info_lines(data)))


def cmd_send(args):
    if not args.command.strip():
        raise SystemExit("give a command to send")
    if args.hex:
        try:
            bytes.fromhex(args.command)
        except ValueError as e:
            raise SystemExit(f"invalid hex: {e}")
    vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
            args.exchangegap)
    vr.wake()
    time.sleep(1.0)
    if args.hex:
        data = bytes.fromhex(args.command)
        vr.write_paced(data)
        print(f"< {data.hex(' ')}")
        out = vr.read_up_to_quiet()
    else:
        parts = args.command.split()      # 'X 0', 'i 1' -> cmd + arg
        buf = bytearray()
        cmd = parts[0]
        buf += vr.talk(cmd, parts[1] if len(parts) > 1 else None)
        out = bytes(buf)
        print(f"< {args.command!r}")
    print(f"> {escape(out)}")


def cmd_sniff(args):
    vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
            args.exchangegap)
    vr.ser.timeout = args.seconds
    print(f"listening on {args.port} for {args.seconds} s (read-only)…")
    t0 = time.monotonic()
    n = 0
    while time.monotonic() - t0 < args.seconds:
        chunk = vr.ser.read(4096)
        if chunk:
            n += len(chunk)
            print("  " + chunk.hex(" "))
    print(f"{n} bytes received")


def _reply_or(raw: bytes, default: int) -> int:
    """Last integer in a settings reply, or `default` when absent.

    Threshold replies look like `b'i198'`; using `_last_int` (the same reader
    as `decode_interval`/`read_counter`) keeps the last digit run, so a reply
    that echoes the channel argument cannot turn `i 1 198` into 1198.
    """
    value = _last_int(raw)
    return default if value is None else value


def cmd_download(args):
    if args.erase and not args.yes:
        raise SystemExit("erase is destructive: re-run with --yes")
    raw_archive = getattr(args, "raw_archive", None)
    if getattr(args, "raw_only", False) and not (args.raw or raw_archive):
        raise SystemExit("--raw-only needs --raw FILE or --raw-archive FILE")
    implicit_raw = False
    if raw_archive and not args.raw:
        args.raw = _raw_path_for_archive(raw_archive)
        implicit_raw = True
    out_path = "" if getattr(args, "raw_only", False) else _fpq_out_path(args)[0]
    if (args.raw and out_path
            and os.path.abspath(args.raw) == os.path.abspath(out_path)):
        raise SystemExit("--raw and --out must be different files")
    vr = VR1710(args.port, args.baud, args.quiet, args.bytegap,
                args.exchangegap)
    print(f"port {args.port} @ {args.baud} 8N1")
    vr.wake()
    print('< " . . "   (wake-up)')
    print("identification pass (Power Log's exact sequence):")
    raws = run_pass(vr)
    # The identification pass already contains I/J, the same pair Power Log
    # uses to show the interval. Reuse it instead of spending another six
    # exchanges after the dump; fall back only if that decode failed.
    iv = decode_interval(raws.get("I", b""), raws.get("J", b""))
    iv_source = "identification pass"
    time.sleep(args.delay)
    stamp: dict = {}
    data = dump(vr, args.pause, force=getattr(args, "arm_empty", False),
                page_ack=getattr(args, "page_ack", True), stamp=stamp)
    args._arm_time = stamp.get("arm")
    if args._arm_time is not None:
        record_session_event("pause", at=args._arm_time,
                             raw=getattr(args, "raw", None),
                             note="dump arm")
    if iv is None:
        # Fallback: the pre-dump reply was unreadable. The meter is paused
        # now, so one post-dump settings pass is the safe way to recover.
        try:
            st = vr.read_settings()
        except Exception as exc:          # noqa: BLE001 - report, keep going
            st = None
            print(f"note: settings read failed after the dump ({exc})",
                  file=sys.stderr)
        iv = decode_interval(st.get("I", b""), st.get("J", b"")) if st else None
        iv_source = "settings readback"
    if iv is not None:
        print(f"interval from {iv_source}: {iv} s")
        if getattr(args, "interval", None) is None:
            args.interval = iv
            args._interval_from = iv_source
        if args.raw:
            # The sidecar records the meter's own read-back setting, not a
            # forced --interval override: it is evidence for later rebuilds.
            # Pass --interval again if you deliberately overrode it here.
            # The arm time is stored too, so an offline `build-fpq` keeps the
            # absolute timeline of this read instead of defaulting to now.
            # A repeated read of paused memory has the same pre-arm counter
            # as the previous read; in that case keep the earlier arm time
            # rather than the later re-pause of the idle meter.
            sidecar_anchor = sidecar_anchor_for_read(
                args.raw, args._arm_time, stamp.get("counter"))
            if (sidecar_anchor is not None
                    and sidecar_anchor != args._arm_time):
                args._arm_time = sidecar_anchor
            write_interval_sidecar(args.raw, iv, iv_source,
                                   anchor=sidecar_anchor,
                                   counter=stamp.get("counter"),
                                   hour_phase=getattr(args, "hour_phase", None),
                                   start=getattr(args, "start_anchor", None))
    settings_failed = False
    if iv is None:
        if getattr(args, "interval", None) is not None:
            print("warning: no post-dump settings readback; using the "
                  "explicit --interval value", file=sys.stderr)
        else:
            print("error: no post-dump settings readback; the read is "
                  "incomplete and the .fpq interval will be inferred",
                  file=sys.stderr)
            settings_failed = True
    if args.raw:
        with open(args.raw, "wb") as f:
            f.write(data)
        print(f"raw dump saved to {args.raw} ({len(data)} bytes)")
    if raw_archive:
        if not os.path.isfile(interval_sidecar_path(args.raw)):
            if getattr(args, "interval", None) is not None:
                write_interval_sidecar(args.raw, int(args.interval),
                                       "explicit", anchor=args._arm_time,
                                       hour_phase=getattr(args, "hour_phase",
                                                          None),
                                       start=getattr(args, "start_anchor",
                                                     None))
            else:
                raise SystemExit(
                    "cannot create a portable raw archive without the "
                    ".interval sidecar; pass --interval (the meter did not "
                    "answer the settings read)")
        write_raw_archive(raw_archive, args.raw)
        print(f"raw archive saved to {raw_archive} "
              f"({os.path.getsize(raw_archive):,} bytes; raw + "
              f".interval sidecar + README.txt)")
    # Finish the meter session BEFORE the (potentially slow) .fpq build:
    # the dump is already in memory, and leaving the meter paused through
    # a multi-second parse/solve/write delays recording.  When --erase is
    # used without --raw, a temporary raw file is written first so the
    # destructive op cannot lose the only copy.
    tmp_raw = _finish_meter_after_dump(vr, args, data, out_path)
    if getattr(args, "raw_only", False):
        if implicit_raw:
            for path in (args.raw, interval_sidecar_path(args.raw)):
                try:
                    os.remove(path)
                except OSError:
                    pass
            print("loose raw/sidecar removed; the portable archive is the "
                  "only output")
        if settings_failed:
            raise SystemExit(
                "read failed: the meter did not answer the post-dump settings "
                "pass; the raw archive was written, but verify the interval "
                "before building the .fpq")
        print("raw-only: no .fpq built; use `build-fpq` on the raw or archive")
        return
    try:
        progress = Progress("build-fpq",
                            enabled=not getattr(args, "no_progress", False),
                            steps=BUILD_FPQ_STEPS)
        _write_fpq_from_dump(data, args, raws, progress=progress)
    except Exception:
        if tmp_raw:
            print(f"error: .fpq build failed; raw dump kept at {tmp_raw}",
                  file=sys.stderr)
        raise
    if tmp_raw:
        try:
            os.remove(tmp_raw)
        except OSError:
            pass
        print(f"temporary raw dump removed: {tmp_raw}")
    if implicit_raw:
        for path in (args.raw, interval_sidecar_path(args.raw)):
            try:
                os.remove(path)
            except OSError:
                pass
        print("loose raw/sidecar removed; the portable archive is the "
              "only raw output")
    if settings_failed:
        raise SystemExit(
            "read failed: the meter did not answer the post-dump settings "
            "pass (interval/circular state unknown); the raw and .fpq were "
            "written, but verify the interval before using them")


def _send_resume(vr, label: str = "resume", raw: str | None = None):
    """Send E immediately; no verification poll (owner decision 2026-11-04).

    The dump/erase exchange has just completed successfully, so the link is
    at its most reliable.  Polling `v` would pause the meter again and add
    latency; the owner chose to accept the lost-E risk and keep the resume
    immediate.

    The host time of the E is logged, so the next read knows when recording
    restarted (the meter itself has no clock).
    """
    _phase(f"{label}: sending E")
    vr.write_op("E")
    record_session_event("resume", raw=raw, note=label)
    print("resume (E) sent", file=sys.stderr)


def _finish_meter_after_dump(vr, args, data: bytes, out_path: str):
    """Send erase/resume immediately after the dump, before .fpq processing.

    Returns the temporary raw path when one was needed (erase without
    --raw); the caller removes it after the .fpq is written successfully.
    """
    tmp_raw = None
    if args.erase and not args.raw:
        tmp_raw = out_path + ".raw.pending"
        with open(tmp_raw, "wb") as f:
            f.write(data)
        print(f"raw dump saved to {tmp_raw} "
              "(temporary; kept until the .fpq is written)")
    erased_ok = True
    if args.erase:
        _phase("download: erasing (B) before .fpq processing")
        before = vr.read_counter()
        print(f"record counter before erase: {before!r}")
        if before is None:
            if tmp_raw:
                print(f"error: raw dump kept at {tmp_raw}", file=sys.stderr)
            _warn_likely_hung("no v reply")
            raise TimeoutError("meter is not answering (no v reply); erase "
                               "NOT sent")
        vr.write_op("B")
        after = None
        for attempt in range(6):
            time.sleep(0.8)
            after = vr.read_counter()
            _phase(f"download: waiting for counter 0 "
                   f"(attempt {attempt + 1}/6, v={after!r})")
            if after == 0:
                break
        print(f"record counter after erase:  {after!r}")
        erased_ok = after == 0
        if not erased_ok:
            print("warning: the counter is not 0; the erase may not have "
                  "completed (the meter does not acknowledge B)",
                  file=sys.stderr)
    if args.resume:
        if not args.erase or erased_ok:
            _phase("download: resuming (E) before .fpq processing")
            _send_resume(vr, "download", raw=getattr(args, "raw", None))
        else:
            print("resume skipped: erase not confirmed (counter "
                  f"{after!r}); recording left paused", file=sys.stderr)
    else:
        print("recording is paused; run 'resume' or pass --resume "
              "to continue")
    return tmp_raw


def _print_dump_stats(data: bytes, items: dict) -> None:
    pre = int.from_bytes(data[:4], "little") if len(data) >= 4 else 0
    print(f"stats: {len(data)} bytes, preamble {pre}, "
          f"{items.get('pages', 0)} page(s), "
          f"{items.get('crc_bad', 0)} bad CRC, "
          f"{len(items['rows'])} row(s), {len(items['ticks'])} block(s), "
          f"{len(items.get('counters', []))} counter item(s), "
          f"{len(items.get('transients', []))} transient capture(s), "
          f"{len(items.get('dips', []))} dip event(s), "
          f"{len(items.get('swells', []))} swell event(s), "
          f"{items.get('skipped', 0)} skipped byte(s), "
          f"{items.get('wrapped_rows', 0)} wrapped row(s), "
          f"{items.get('zero_pad', 0)} zero-pad byte(s)")
    if items.get("trim"):
        trim = items["trim"]
        print(f"trim: {trim['rule']} cut at row {trim['cut']} "
              f"({trim['rows']} row(s) dropped)")
    if items.get("gap_error"):
        print(f"gap: none ({items['gap_error']})")


def _infer_nominal(items: dict):
    """Power Log's header nominal from the measured voltage.

    The recorder has no nominal setting. Power Log stores 120.00 for 120 V
    systems and 230.00 for its high range, which covers the 208 V and 240 V
    systems in the corpus. Use the median L1-N average; a value above 160 V
    is treated as the high range.
    """
    avgs = sorted(row[0][1] / 8.0 for row in items["rows"])
    median = avgs[len(avgs) // 2]
    return (230.0 if median > 160.0 else 120.0), median


def _fpq_out_path(args) -> tuple[str, bool]:
    """Resolve the .fpq output path and whether to gzip it.

    `--gzip` appends `.gz` when the requested name does not already end in
    it; a name ending in `.gz` is compressed even without the flag.  The
    file content is the exact gzip stream of the plain `.fpq` blob, which
    the reader/exporter already accept.
    """
    out = str(args.out)
    compress = bool(getattr(args, "gzip_output", False))
    if out.lower().endswith(".gz"):
        compress = True
    elif compress:
        out += ".gz"
    return out, compress


def _write_fpq_from_dump(data: bytes, args, raws: dict | None = None,
                         progress=None):
    """Parse a downloaded dump and write the .fpq (the deliverable format)."""
    if len(data) < 5:
        if getattr(args, "stats", False):
            print(f"stats: {len(data)} byte(s), preamble "
                  f"{int.from_bytes(data[:4], 'little')}, no dump body")
        print("memory is empty: no voltage/frequency records, "
              "no .fpq written")
        return
    _phase(f"build-fpq: parsing {len(data):,} byte(s) ...")
    _phase("build-fpq: the counter analysis can take several minutes on a "
           "slow machine ...")
    items = parse_dump(data,
                       keep_after_reset=bool(
                           getattr(args, "keep_after_reset", False)),
                       progress=progress)
    _phase(f"build-fpq: parsed {len(items['rows'])} row(s), "
           f"{len(items.get('counter_items', []))} counter item(s), "
           f"{len(items.get('dips', []))} dip(s), "
           f"{len(items.get('transients', []))} capture(s)")
    if getattr(args, "stats", False):
        _print_dump_stats(data, items)
    if not items["rows"]:
        print("memory is empty: no voltage/frequency records, "
              "no .fpq written")
        return
    interval, how = _resolve_interval(args, items["ticks"],
                                      dump_path=getattr(args, "dump", None))
    if how.startswith("default"):
        print("warning: interval defaulted to 5 s; pass --interval or keep "
              "the download sidecar", file=sys.stderr)
    anchor, anchor_src = _resolve_anchor(args)
    if anchor_src == "build time":
        print("warning: --anchor not given and the dump sidecar has no arm "
              "time; absolute dates use the build clock, not the read "
              "clock (relative times are unaffected)", file=sys.stderr)
    dip = args.dip if args.dip is not None else _reply_or(
        (raws or {}).get("i 1", b""), 198)
    pico = args.pico if args.pico is not None else _reply_or(
        (raws or {}).get("j 1", b""), 218)
    umbral = (transient_target(args.transient) if args.transient is not None
              else _reply_or((raws or {}).get("r 1", b""), 100))
    vnom = args.vnom
    if vnom is None:
        vnom, median = _infer_nominal(items)
        label = "120 V system" if vnom == 120.0 else (
            "high range (208/240 V system)")
        print(f"nominal: {label}, inferred from median L1-N {median:.1f} V "
              "(override with --nominal)")
    build_stats: dict = {}
    if progress is not None:
        progress.phase("assembling .fpq records")
    _phase("build-fpq: solving the counter timeline and assembling records ...")
    hour_phase = getattr(args, "hour_phase", None)
    start_anchor = getattr(args, "start_anchor", None)
    if getattr(args, "dump", None):
        meta = read_interval_sidecar(args.dump)
        if meta:
            if hour_phase is None and meta.get("hour_phase") is not None:
                hour_phase = float(meta["hour_phase"])
            if start_anchor is None and meta.get("start"):
                try:
                    start_anchor = datetime.fromisoformat(meta["start"])
                except ValueError:
                    pass
    blob = build_fpq(items, anchor, interval, dip, pico, umbral,
                     vnom, args.fnom, stats=build_stats,
                     hour_phase=hour_phase,
                     start_anchor=start_anchor)
    if progress is not None:
        progress.done()
    out_path, compress = _fpq_out_path(args)
    _phase(f"build-fpq: writing {out_path} ...")
    if compress:
        with gzip.open(out_path, "wb", compresslevel=9) as f:
            f.write(blob)
    else:
        with open(out_path, "wb") as f:
            f.write(blob)
    rows = items["rows"]
    first, last = row_time_bounds(items, anchor, interval)
    print(f"rows: {len(rows)}  blocks: {len(items['ticks'])}  "
          f"interval: {interval:g} s ({how})")
    print(f"time span: {first:%Y-%m-%d %H:%M:%S} .. {last:%Y-%m-%d %H:%M:%S} "
          f"(anchor {anchor:%Y-%m-%d %H:%M:%S})")
    gap = items.get("gap")
    if gap is None and items.get("gap_error") and items.get("counter_items"):
        print(f"warning: no I/j gap records: {items['gap_error']}; the row "
              "timeline is uniform and the outages/read pauses it proves "
              "are lost", file=sys.stderr)
    if gap and gap.get("gaps"):
        ni = build_stats.get("i_records", 0)
        nj = build_stats.get("j_records", 0)
        ph = gap.get("_marker_phase", gap.get("ph"))
        print(f"gaps: {len(gap['gaps'])} ({ni} I + {nj} j) from the "
              f"06/09/0A counter (p={gap['p']:g} s, ph={ph:g} s); "
              f"{ni + nj} written")
        if gap.get("_day_branch_ambiguous"):
            print("warning: a gap's 32,768 s day-step branch is ambiguous "
                  "(no start anchor and the daily phase does not "
                  "discriminate); its I duration may be short.  Use a "
                  "tool-armed read (download) so the pause time is known.",
                  file=sys.stderr)
        tail = [g for g in gap["gaps"] if g["resume"] >= len(rows)]
        if tail:
            print(f"note: {len(tail)} gap(s) resume past the kept rows (ring "
                  "wrap trim) and cannot be written", file=sys.stderr)
        if gap.get("uniform_rows"):
            print(f"note: {gap['uniform_rows']} row(s) have counter evidence "
                  "the gap solver could not place; their I/j records cannot "
                  "be written", file=sys.stderr)
        if gap.get("anchor_ok") is False:
            print("warning: the anchor's local time-of-day does not place "
                  "the daily counter step in a detected bracket; relative "
                  "times are unaffected, absolute wall time may be off",
                  file=sys.stderr)
    if build_stats.get("dips_suppressed"):
        print(f"note: {build_stats['dips_suppressed']} sub-interval dip(s) on "
              "interruption rows not written (Power Log's rule)",
              file=sys.stderr)
    if compress:
        print(f"wrote {os.path.getsize(out_path):,} gzip bytes to {out_path} "
              f"({len(blob):,} B .fpq, {rows and 'records' or 'empty'})")
    else:
        print(f"wrote {len(blob)} bytes to {out_path} "
              f"({len(blob):,} B, .fpq format, {rows and 'records' or 'empty'})")
    if items.get("pages"):
        print(f"paging: {items['pages']} page(s), "
              f"{items.get('crc_bad', 0)} bad CRC(s)")
    trim = items.get("trim")
    if trim and trim.get("rule") == "power-cycle reset":
        print(f"warning: dropped {trim['rows']} row(s) after a power-up "
              f"counter reset (cut at row {trim['cut']}); pass "
              "--keep-after-reset to keep them", file=sys.stderr)
    elif items.get("wrapped_rows"):
        print(f"ring wrap: dropped {items['wrapped_rows']} row(s) of stale "
              "older-segment content at the end of the dump")
    if items["skipped"]:
        print(f"note: {items['skipped']} unparsed byte(s) skipped while "
              "resynchronising the item stream", file=sys.stderr)
    if items.get("zero_pad"):
        print(f"note: {items['zero_pad']} trailing zero-pad byte(s) "
              "(not item data)", file=sys.stderr)


def cmd_build_fpq(args):
    if not os.path.isfile(args.dump):
        raise SystemExit(f"raw dump not found: {args.dump}")
    tmpdir = None
    try:
        if tarfile.is_tarfile(args.dump):
            original = args.dump
            tmpdir = tempfile.mkdtemp(prefix="fpq-raw-")
            raw, side = extract_raw_archive(args.dump, tmpdir)
            args.dump = raw
            if args.out == "vr1710_dump.fpq":
                args.out = _archive_out_name(original)
            _phase(f"build-fpq: extracted {os.path.basename(raw)} from "
                   f"{os.path.basename(original)} ...")
            if side is None:
                print("warning: archive has no .interval sidecar; the "
                      "interval/anchor may be inferred or need --interval/"
                      "--anchor", file=sys.stderr)
        out_path, _ = _fpq_out_path(args)
        if os.path.abspath(out_path) == os.path.abspath(args.dump):
            raise SystemExit("--out must differ from the input dump")
        _phase(f"build-fpq: reading {args.dump} "
               f"({os.path.getsize(args.dump):,} B) ...")
        with open(args.dump, "rb") as f:
            data = f.read()
        progress = Progress("build-fpq",
                            enabled=not getattr(args, "no_progress", False),
                            steps=BUILD_FPQ_STEPS)
        _write_fpq_from_dump(data, args, progress=progress)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _writer_args(p):
    """Options shared by download and build-fpq."""
    p.add_argument("--anchor", type=anchor_arg, default=None,
                   help="wall-clock end of the recording 'YYYY-MM-DD HH:MM:SS'; "
                        "the newest row is placed one interval before it "
                        "(default: the arm time captured by a download, or the "
                        "sidecar's arm time for build-fpq; only if neither "
                        "exists does the build clock stand in)")
    p.add_argument("--keep-after-reset", action="store_true",
                   help="do not drop rows after a power-up counter reset "
                        "(default: match Power Log and cut the new segment; "
                        "use when a genuine pause exceeded the ring capacity, "
                        "about 25 h at 1 s)")
    p.add_argument("--hour-phase", type=float, default=None, metavar="SECONDS",
                   help="counter hour-step phase (0-3600 s) for the I-duration "
                        "correction; overrides the automatic/anchored phase. "
                        "Use it when the first read followed an official pause "
                        "and the default anchor is late (C27/O5)")
    p.add_argument("--start-anchor", type=anchor_arg, default=None,
                   metavar="TIME",
                   help="recording-start wall time 'YYYY-MM-DD HH:MM:SS'; with "
                        "--anchor it resolves the hour phase automatically "
                        "(total duration + long-segment consensus). Store it "
                        "in the sidecar for later build-fpq use")
    p.add_argument("--interval", type=parse_interval_arg, default=None,
                   metavar="INT",
                   help="recording interval written into the .fpq: one of " +
                        INTERVAL_HELP + " (numeric values are seconds; "
                        "default: inferred from the 10-minute blocks, else "
                        "5 s). This does NOT change the meter; use "
                        "`set --interval` for that.")
    p.add_argument("--nominal", dest="vnom", type=nominal_volts,
                   default=None, metavar="{120,208,240}",
                   help="optional system voltage: 120, 208 or 240 V; normally "
                        "inferred from the measured voltage (Power Log writes "
                        "120.00 or its high-range 230.00)")
    p.add_argument("--fnom", type=nominal_hz, default=60.0,
                   help="nominal frequency written to the .fpq config "
                        "(default 60.0; only 120/60 are accepted)")
    p.add_argument("--gzip", dest="gzip_output", action="store_true",
                   help="gzip-compress the .fpq output (also implied by an "
                        "-o/--out name ending .gz)")
    p.add_argument("--no-progress", action="store_true",
                   help="do not print phase/heartbeat progress on stderr")
    p.add_argument("--dip", type=threshold_volts, default=None,
                   help="dip threshold 0-300 V written to the .fpq config "
                        "(default: read from the meter)")
    p.add_argument("--swell", "--pico", dest="pico", metavar="V",
                   type=threshold_volts, default=None,
                   help="Swell threshold 0-300 V written to the .fpq config "
                        "(default: read from the meter; --pico is accepted as "
                        "the old alias)")
    p.add_argument("--transient", type=transient_volts, metavar="V",
                   default=None,
                   help="transient threshold 2-20 V written to the .fpq "
                        "config (default: read from the meter)")


def _serial_status_text() -> str:
    """Short capability block appended to every --help."""
    path = module_origin("serial")
    lines = ["Machine status (this interpreter):"]
    lines.extend(interpreter_lines())
    if path is not None:
        lines.append(f"  pyserial available: serial access enabled ({path})")
        return "\n".join(lines)
    lines.append("  pyserial  missing: all serial operations disabled")
    lines.append("  fix (one of):")
    lines.append("    sudo apt-get install python3-serial")
    lines.append("    sudo yum install python3-pyserial")
    lines.append("    python3 -m venv .venv && . .venv/bin/activate && "
                 "python3 -m pip install -r requirements.txt")
    lines.append("  windows: activate the venv and run `python ...`, not "
                 "`py -3 ...`")
    return "\n".join(lines)


class _HelpParser(argparse.ArgumentParser):
    """Append the machine-status block to the main and subcommand helps."""

    def __init__(self, *args, **kwargs):
        # Keep the line breaks in descriptions/epilogs (the purpose table).
        kwargs.setdefault("formatter_class",
                          argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)

    def format_help(self) -> str:
        text = super().format_help().rstrip("\n")
        return f"{text}\n\n{_serial_status_text()}\n"


def warn_paused():
    """The recorder records OR talks serial, never both (owner 2026-10-27).

    Any exchange - even a read - stops the recording until `E` resumes it.
    Never auto-resume: the caller decides (the read commands are used in
    flows where the meter must stay paused).
    """
    print("note: this serial exchange paused the recording; send 'resume' "
          "(E) to continue", file=sys.stderr)


def warn_disclaimer():
    # status/support text belongs on stderr; stdout stays for data output
    print("* " + "-" * 72, file=sys.stderr)
    print("* fpq_serial_meter is an independent, reverse-engineered client.",
          file=sys.stderr)
    print("* It only talks the protocol Power Log Classic 4.60 was seen and",
          file=sys.stderr)
    print("* verified speaking; it commands nothing beyond those commands.",
          file=sys.stderr)
    print("* Not affiliated with or endorsed by Fluke Corporation.",
          file=sys.stderr)
    print("* 'Fluke', 'VR1710' and 'Power Log' are trademarks of their owner.",
          file=sys.stderr)
    print("*", file=sys.stderr)
    print("* Provided \"as is\", without warranty of any kind, express or",
          file=sys.stderr)
    print("* implied, including accuracy, completeness, fitness for a",
          file=sys.stderr)
    print("* particular purpose or non-infringement. No guarantee is made",
          file=sys.stderr)
    print("* that readings, dumps or decoded values match the instrument or",
          file=sys.stderr)
    print("* the vendor's software. You use this software and rely on its",
          file=sys.stderr)
    print("* output entirely at your own risk; the authors accept no liability",
          file=sys.stderr)
    print("* for any loss, damage or decision made using it. For measurement,",
          file=sys.stderr)
    print("* compliance or safety decisions use a calibrated instrument and",
          file=sys.stderr)
    print("* the vendor's official software.", file=sys.stderr)
    print("* " + "-" * 72, file=sys.stderr)


def main():
    warn_disclaimer()
    ap = _HelpParser(
        prog="fpq_serial_meter.py",
        description="Fluke VR1710 serial interface: control commands plus the "
                    "memory-dump download (Power Log Classic 4.60 command "
                    "set); see docs/serial.md.",
        epilog="Commands by purpose:\n"
               "  read-only   info (identity, settings, counters, banner)\n"
               "  change      set (thresholds/interval, with readback)\n"
               "  recording   erase, resume\n"
               "  transfer    download (from the meter), build-fpq (from a "
               "saved dump, offline)\n"
               "  diagnostic  debug send (one command, no verification), "
               "debug listen (passive)\n\n"
               "All commands take --port, --baud, --bytegap and --exchangegap.\n"
               "Threshold changes are single-step nudges on the meter; set "
               "verifies every burst by readback and never retries a write.")
    ap.add_argument("-p", "--port", default="/dev/ttyUSB0",
                    help="serial port (default /dev/ttyUSB0; e.g. COM3 on "
                         "Windows)")
    ap.add_argument("--baud", type=int, default=500000,
                    help="baud rate (default 500000, as observed through the "
                         "CP2102 bridge)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="show progress and the exact wire bytes on stderr "
                         "(use --debug-quiet for the timing window, which is "
                         "not a verbosity control)")
    dbg = ap.add_argument_group(
        "debug timing (normally leave at the verified defaults)")
    dbg.add_argument("--debug-quiet", dest="quiet", type=positive_float, default=0.12,
                     help="seconds of silence that mark the end of a reply "
                          "(default 0.12)")
    dbg.add_argument("--debug-byte-gap", dest="bytegap", type=positive_float,
                     default=0.15,
                     help="seconds between bytes inside one command "
                          "(default 0.15; real meters silently drop bytes "
                          "written closer than ~0.1 s)")
    dbg.add_argument("--debug-exchange-gap", dest="exchangegap", type=positive_float,
                     default=1.45,
                     help="minimum seconds between commands (default 1.45; "
                          "the meter answers at most about one per second)")
    pac = ap.add_argument_group("read pacing")
    pac.add_argument("--pacing", choices=("auto", "fast", "slow"),
                     default="auto",
                     help="read-only exchange pacing: auto/fast = 0.50 s "
                          "reads with a sticky 1.45 s fallback; slow = the "
                          "historical 1.45 s always (default: auto)")
    pac.add_argument("--read-gap", dest="read_gap", type=read_gap_arg,
                     default=0.50,
                     help="fast read-only gap in seconds, 0.30-5.0 "
                          "(default 0.50; official settings p99 was 0.486 s)")
    common = _HelpParser(add_help=False)
    common.add_argument("-p", "--port", default=argparse.SUPPRESS,
                        help="serial port (default /dev/ttyUSB0)")
    common.add_argument("--baud", type=int, default=argparse.SUPPRESS,
                        help="baud rate (default 500000)")
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS,
                        help="show progress and the exact wire bytes on stderr")
    cdbg = common.add_argument_group("debug timing")
    cdbg.add_argument("--debug-quiet", dest="quiet",
                      type=positive_float, default=argparse.SUPPRESS)
    cdbg.add_argument("--debug-byte-gap", dest="bytegap",
                      type=positive_float, default=argparse.SUPPRESS)
    cdbg.add_argument("--debug-exchange-gap", dest="exchangegap",
                      type=positive_float, default=argparse.SUPPRESS)
    cdbg.add_argument("--pacing", choices=("auto", "fast", "slow"),
                      default=argparse.SUPPRESS)
    cdbg.add_argument("--read-gap", dest="read_gap",
                      type=read_gap_arg, default=argparse.SUPPRESS)

    sub = ap.add_subparsers(dest="cmd", required=True,
                            parser_class=_HelpParser)

    p = sub.add_parser("info", parents=[common], help="read identity, counters, interval, thresholds and "
                     "banner (read-only)")
    p.add_argument("--channels", type=int, default=1, choices=range(1, 4),
                   help="how many channels to read thresholds for "
                        "(only ch 1 verified; default 1)")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output")
    p.add_argument("--resume", action="store_true",
                   help="send E after the read to resume recording")
    p.add_argument("--transcript", action="store_true",
                   help="print the raw command/reply trace instead of the "
                        "summary (the old `probe` output)")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("erase", parents=[common], help="erase the recorded data (destructive)")
    p.add_argument("--yes", action="store_true",
                   help="confirm the destructive erase")
    p.add_argument("--resume", action="store_true",
                   help="resume recording afterwards")
    p.set_defaults(func=cmd_erase)

    p = sub.add_parser("resume", parents=[common], help="resume recording after a read or pause")
    p.add_argument("--raw", help="raw dump this resume belongs to (recorded "
                                 "in the host session log)")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("session", parents=[common],
                       help="show the host pause/resume log "
                            "(~/.fpq-session.jsonl)")
    p.add_argument("--limit", type=positive_int, default=20,
                   help="show only the newest N events (default 20)")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("configure", parents=[common],
        description="Change recorder settings with readback verification. "
                    "Values use the official software's names and units.",
        help="change thresholds, transient recording, circular memory or "
             "the recording interval")
    p.add_argument("--dip-voltage", type=threshold_volts, metavar="V",
                   help="Dip threshold, 0-300 V (integer)")
    p.add_argument("--swell-voltage", type=threshold_volts, metavar="V",
                   help="Swell threshold, 0-300 V (integer)")
    p.add_argument("--transient-sensitivity", type=transient_volts,
                   metavar="V",
                   help="Transient sensitivity, 2-20 V (integer)")
    p.add_argument("--transient-recording", choices=("on", "off"),
                   metavar="{on,off}",
                   help="record transient waveforms; 'on' enables (at "
                        "--transient-sensitivity or 20 V), 'off' disables")
    p.add_argument("--recording-interval", type=parse_interval_arg,
                   metavar="INT",
                   help="recording interval: " + INTERVAL_HELP + "; needs "
                        "empty memory; activates at the next recording start "
                        "(erase -> configure -> read -> resume; a power cycle "
                        "may revert it); use this option ALONE (not with "
                        "thresholds or circular memory)")
    p.add_argument("--circular-memory", choices=("on", "off"),
                   help="keep recording when memory is full (on) or stop "
                        "when full (off); readback-verified from v")
    p.add_argument("--resume", action="store_true",
                   help="resume recording after an interval change (default: "
                        "leave it paused)")
    p.add_argument("--apply", action="store_true",
                   help="write the changes; without it the command only "
                        "previews")
    adv = p.add_argument_group("advanced (rarely needed)")
    adv.add_argument("--channel", type=int, default=1, choices=range(1, 4),
                     help="channel to read/set; only channel 1 is verified")
    adv.add_argument("--burst", type=positive_int, default=20,
                     help="writes to send before re-reading the value "
                          "(default 20)")
    adv.add_argument("--max-steps", type=positive_int, default=400,
                     help="stop after this many single-step writes "
                          "(default 400)")
    adv.add_argument("--toggle-circular-memory", action="store_true",
                     help="flip circular memory once; readback-verified from "
                          "the v reply")
    p.epilog = (
        "Typical continuous measurement (Power Log's VR1710 Settings "
        "dialog):\n" + '  1s  1 day 0 h 50 min\n  2s  2 days 1 h 34 min\n  5s  5 days 3 h 6 min\n  10s  10 days 3 h 32 min\n  30s  29 days 4 h 10 min\n  1m  54 days 21 h 58 min\n  5m  186 days 17 h 5 min\n  10m  266 days 17 h 30 min\n  15m  311 days 4 h 15 min\n  20m  339 days 11 h 20 min')
    p.set_defaults(func=cmd_configure)

    p = sub.add_parser("debug", parents=[common],
        description="Protocol diagnostics. These commands bypass the normal "
                    "verified paths; they can change settings or erase.",
        help="protocol diagnostics (not for normal use)")
    dbg = p.add_subparsers(dest="debug_cmd", required=True,
                           parser_class=_HelpParser)
    q = dbg.add_parser("send", parents=[common], help="send one raw command (no readback or verification)")
    q.add_argument("command",
                   help="raw command, e.g. 'X 0' (split into "
                        "command/argument) or hex pairs with --hex")
    q.add_argument("--hex", action="store_true",
                   help="interpret command as hex pairs instead of ASCII")
    q.set_defaults(func=cmd_send)
    q = dbg.add_parser("listen", parents=[common], help="passively read bytes for N seconds")
    q.add_argument("--seconds", type=positive_float, default=10.0,
                   help="how long to read the port (default 10 s)")
    q.set_defaults(func=cmd_sniff)

    p = sub.add_parser("download", parents=[common],
        description="Read the meter over the serial port and write a .fpq. "
                    "For an existing raw dump use build-fpq (offline). "
                    "Use --raw-only to keep the raw capture and skip the "
                    "parse; --raw-archive writes a portable tar.gz.",
        epilog="Examples:\n"
               "  fpq-meter download -p COM3 --raw br1.bin --raw-only\n"
               "  fpq-meter download -p COM3 --raw-archive br1.tar.gz "
               "--raw-only\n"
               "  fpq-meter build-fpq br1.tar.gz -o br1.fpq",
        help="read the meter: identification pass, memory dump, .fpq file")
    p.add_argument("--delay", type=positive_float, default=1.0,
                   help="seconds to wait between the identification pass and "
                        "the dump (default 1.0)")
    p.add_argument("--pause", type=positive_float, default=0.3,
                   help="seconds of line silence before requesting the next "
                        "block (default 0.3)")
    add_page_ack_flags(p)
    p.add_argument("-o", "--out", default="vr1710_dump.fpq",
                   help="output .fpq file")
    p.add_argument("--raw", metavar="FILE", default=None,
                   help="also save the unprocessed meter data to FILE")
    p.add_argument("--raw-only", action="store_true",
                   help="write the raw dump and .interval sidecar, then exit "
                        "without building the .fpq; requires --raw or "
                        "--raw-archive")
    p.add_argument("--raw-archive", metavar="FILE", default=None,
                   help="also write a portable .tar.gz containing the raw "
                        "dump, its .interval sidecar and a README; with "
                        "--raw-only and no --raw it is the only output")
    p.add_argument("--resume", action="store_true",
                   help="resume recording immediately after the dump, before "
                        "the .fpq is built (default: leave it paused)")
    p.add_argument("--erase", action="store_true",
                   help="erase the recorded data after the raw dump is saved "
                        "and before the .fpq is built (requires --yes; a "
                        "temporary raw is written when --raw is not given; "
                        "the meter stays paused unless --resume is given)")
    p.add_argument("--yes", action="store_true",
                   help="confirm the destructive erase")
    p.add_argument("--stats", action="store_true",
                   help="print parsed counts (rows, events, pages) after "
                        "writing")
    p.add_argument("--arm-empty", action="store_true",
                   help="arm even when the record counter is zero (used "
                        "after an erase, before the first recording)")
    _writer_args(p)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("build-fpq", parents=[common],
        description="Offline conversion of a saved raw dump; no port is "
                    "opened. The input may be a raw .bin or a portable "
                    "raw .tar.gz/.tgz (raw + .interval sidecar).",
        epilog="Examples:\n"
               "  fpq-meter build-fpq br1.bin -o br1.fpq\n"
               "  fpq-meter build-fpq br1.tar.gz -o br1.fpq",
        help="convert a saved raw dump/archive into a .fpq file (offline)")
    p.add_argument("dump", help="saved raw dump file")
    p.add_argument("-o", "--out", default="vr1710_dump.fpq",
                   help="output .fpq file")
    _writer_args(p)
    p.set_defaults(func=cmd_build_fpq)

    args = ap.parse_args()
    VR1710.DEFAULT_PACING = getattr(args, "pacing", "auto")
    VR1710.DEFAULT_READ_GAP = getattr(args, "read_gap", 0.50)
    global VERBOSE
    VERBOSE = bool(getattr(args, "verbose", False))
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        if _ARMED_DUMP_INTERRUPTED:
            print("warning: the dump was interrupted after the arm; the "
                  "recorder may still be waiting for ACKs. Try `info` or "
                  "`resume`; power-cycle it if it stays silent.",
                  file=sys.stderr)
        return 130
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        # data/decode refusals (50 Hz, no records, malformed dump): not a
        # serial-link failure, so do not label them "serial error".
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # serial errors: port busy/absent etc.
        print(f"serial error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
