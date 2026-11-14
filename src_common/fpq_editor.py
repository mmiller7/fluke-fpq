#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""fpq_editor.py: cut a Fluke VR1710 .fpq session to a date-time window.

This is a companion to fpq_export.py.  It reuses that module's strict record
walker and timestamp helpers, copies the config header unchanged except for
the record count at 0x383, and writes a new .fpq that Power Log Classic 4.60
accepts.

Subcommand:
  trim        keep records whose timestamp is inside --start..--end
              (both ends optional / open-ended)

Boundary rules are summarised authoritatively in cmd_trim(); docs/editor.md
is the user-facing copy.
Run as an executable or via `python3 fpq_editor.py trim ...`.

Legal: this tool is provided "as is", without warranty of any kind, express
or implied, including accuracy, completeness or fitness for any purpose. It
is an independent reverse-engineering effort, not affiliated with or
endorsed by Fluke Corporation; 'Fluke', 'VR1710' and 'Power Log' are
trademarks of their owner. You use it and rely on its output entirely at
your own risk; the authors accept no liability for any loss, damage or
decision made using it. For measurement, compliance or safety decisions use
a calibrated instrument and the vendor's official software.
"""

import argparse
import os
import struct
import sys
import tempfile
from datetime import datetime

# fpq_editor lives in src_common/; fpq_export is one level up.  Make both
# importable when this file is run directly or imported from a zipapp.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
for _p in (_HERE, _PARENT):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import fpq_export as fx  # noqa: E402  (main tool, stdlib-only at import)
from fpq_progress import Progress  # noqa: E402

RECORD_COUNT_OFFSET = 0x383   # u32 LE, total records in the stream [verified]
DT_FORMAT = "%Y-%m-%d %H:%M:%S"

# Record classes by timestamp semantics (verified against docs/format.md and
# the 2 s/5 s Power Log fixtures):
#   MEASUREMENT_TYPES  G/F rows: one row per averaging interval; the timestamp
#                      is the END of that interval.  A G/F pair shares its
#                      second.
#   INTERVAL_TYPES     K/H/L/5C: 10-min and 2-h derived records.  K/H/5C of
#                      one reporting tick share one timestamp and are kept or
#                      dropped together (the filter is per record timestamp,
#                      so this is automatic); L shares the 12th Pst tick's
#                      second.
#   EVENT_TYPES        D/P: timestamp = event END (the covering row second).
#                      I: interruption START (last good row second).
#                      j: the last good G row's second before the gap.
#                      T: capture timestamp.
# The trim rule is always the record's own (snapped) timestamp; there is no
# per-type fudging.  See cmd_trim() and docs/editor.md.
MEASUREMENT_TYPES = {0x47, 0x46}
INTERVAL_TYPES = {0x4B, 0x48, 0x4C, 0x5C}
EVENT_TYPES = {0x44, 0x50, 0x49, 0x6A, 0x54}
ALL_TYPES = MEASUREMENT_TYPES | INTERVAL_TYPES | EVENT_TYPES


def parse_dt(text):
    """Parse the CLI's naive local 'YYYY-MM-DD HH:MM:SS' (recorder clock)."""
    try:
        return datetime.strptime(text, DT_FORMAT)
    except ValueError:
        raise ValueError(
            f"invalid date-time {text!r}: expected 'YYYY-MM-DD HH:MM:SS'")


def record_time(rec):
    """Timestamp of a walked record, snapped to the whole second."""
    return fx.norm(fx.ole(struct.unpack_from("<d", rec, 1)[0]))


def type_name(t):
    return fx.RECORD_NAMES.get(t, f"0x{t:02x}")


def _same_file(a, b):
    """True if paths a and b are the same file, including via symlinks or
    hard links.  `samefile` needs both to exist; the realpath fallback covers
    a not-yet-created output (a dangling symlink to the input resolves)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def _check_output_path(src, out):
    """Refuse to write over the source, before any expensive work."""
    if _same_file(src, out):
        raise ValueError("output path is the input file; refusing to overwrite")


def _write_atomic(path, payload):
    """Write payload to path via a same-directory temp file + os.replace, so
    a failure never truncates an existing output and readers never see a
    partially written .fpq."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        raise ValueError(f"output directory does not exist: {directory}")
    fd, tmp = tempfile.mkstemp(prefix=".fpq_editor.", suffix=".tmp",
                               dir=directory)
    try:
        try:                      # keep the mode of an existing output ...
            mode = os.stat(path).st_mode & 0o777
        except OSError:           # ... else the usual umask-derived mode
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cmd_trim(args):
    """Keep records inside [start, end] and write a trimmed .fpq.

    Boundary rules (authoritative summary; docs/editor.md mirrors this):
      * A record is kept iff its own timestamp is in the closed interval
        [--start, --end].  Whole-second timestamps are compared, matching the
        snapped timestamps Power Log stores.
      * G/F rows are kept independently by timestamp; a G/F pair shares its
        second, so both survive or both are dropped.  A lone G or F (which
        the recorder can emit at a gap edge) is kept on its own timestamp.
      * K/H/L/5C interval records are atomic by timestamp: every record of a
        reporting tick carries the same second, so the whole tick (including
        L, whose timestamp is the 12th Pst tick) is kept iff that second is
        inside the window.  A block straddling the window edge therefore
        cannot be split; it is included when its timestamp is inside.
      * Event records are kept by the timestamp stored in the record, which
        is not the same convention for every type: D/P carry the event END
        (the covering row second), I the interruption START, j the last good
        G row second before the gap, and T the capture time.  So a D/P whose
        end second is inside --start..--end is kept even though the event
        began before --start; an I/j/gap whose marker second is inside is
        kept even if the resuming G row falls after --end (the record's own
        duration is explicit, so the output then ends during the event).
        Durations are NOT clipped, so an event that starts just before
        --end may extend past it.
      * If no G (voltage-RMS) row survives, no file is written: Power Log
        would have nothing to graph.  The command exits non-zero.
      * Header/config bytes 0x000..(record base) are copied byte-for-byte,
        except the u32 record count at 0x383 (updated to the kept count).
        The header contains no start/stop fields (docs/format.md); the
        0x373 interval float and all threshold text stay untouched.
      * Original record order and record bytes are preserved exactly.
    """
    start = parse_dt(args.start) if args.start else None
    end = parse_dt(args.end) if args.end else None
    if start is not None and end is not None and start > end:
        raise ValueError("--start is after --end")

    if not args.dry_run:
        _check_output_path(args.input, args.out)

    progress = Progress("editor", enabled=not getattr(args, "no_progress", False))
    if progress is not None:
        progress.phase("reading records")
    data = fx.load_fpq(args.input)
    walked = []
    for _n, rec in enumerate(fx.walk_records(args.input), start=1):
        if progress is not None and _n % 5000 == 0:
            progress.tick()
        walked.append(rec)
    if not walked:
        raise ValueError(f"{fx.display_name(args.input)}: no records")
    base = walked[0][0]
    if base < fx.RECORD_BASE_FALLBACK:
        raise ValueError(
            f"unexpected record base {base:#x} "
            f"(expected >= {fx.RECORD_BASE_FALLBACK:#x})")

    kept = []
    src_counts = {}
    kept_counts = {}
    first_ts = last_ts = None
    first_g = last_g = None
    if progress is not None:
        progress.phase("filtering records")
    for _n, (off, t, _ts_raw, rec) in enumerate(walked, start=1):
        if progress is not None and _n % 5000 == 0:
            progress.tick()
        src_counts[t] = src_counts.get(t, 0) + 1
        ts = record_time(rec)
        if (start is None or ts >= start) and (end is None or ts <= end):
            kept.append((off, t, rec))
            kept_counts[t] = kept_counts.get(t, 0) + 1
            if first_ts is None or ts < first_ts:
                first_ts = ts
            if last_ts is None or ts > last_ts:
                last_ts = ts
            if t == 0x47:
                if first_g is None or ts < first_g:
                    first_g = ts
                if last_g is None or ts > last_g:
                    last_g = ts

    unknown = sorted(set(src_counts) - ALL_TYPES)
    if unknown:
        # A new/unknown record type would be filtered by a timestamp rule
        # nobody has verified; refuse rather than silently trim it by guess.
        raise ValueError(
            "unhandled record type(s) in input: "
            + ", ".join(f"0x{t:02x}" for t in unknown))

    win = f"{args.start or '(open)'} .. {args.end or '(open)'}"
    print(f"input:  {fx.display_name(args.input)}")
    print(f"window: {win}  (local recorder time, inclusive)")
    print(f"records kept: {len(kept)} of {len(walked)}")
    print(f"{'type':22s} {'kept':>8s} {'source':>8s}")
    for t in sorted(src_counts):
        print(f"{type_name(t):22s} {kept_counts.get(t, 0):8d} "
              f"{src_counts[t]:8d}")
    if first_ts is not None:
        print(f"kept first timestamp: {first_ts.isoformat(sep=' ')}")
        print(f"kept last timestamp:  {last_ts.isoformat(sep=' ')}")
    if first_g is not None:
        print(f"kept G rows: {kept_counts.get(0x47, 0)}  "
              f"first {first_g.isoformat(sep=' ')}  "
              f"last {last_g.isoformat(sep=' ')}")

    if not kept_counts.get(0x47):
        raise ValueError(
            "the requested window contains no G (voltage-RMS) row; "
            "refusing to write a file Power Log cannot graph")

    if args.dry_run:
        print("dry run: no file written")
        return

    header = bytearray(data[:base])
    old_count = struct.unpack_from("<I", header, RECORD_COUNT_OFFSET)[0]
    if old_count != len(walked):
        # walk_records() already enforces this; keep the check local and loud.
        raise ValueError(
            f"header record count {old_count} != walked {len(walked)}")
    struct.pack_into("<I", header, RECORD_COUNT_OFFSET, len(kept))
    _check_output_path(args.input, args.out)   # re-check right before writing
    if progress is not None:
        progress.phase("writing trimmed file")
    payload = bytes(header) + b"".join(rec for _off, _t, rec in kept)
    _write_atomic(args.out, payload)
    if progress is not None:
        progress.done()
    print(f"wrote {args.out}: {len(payload)} bytes, header count "
          f"{old_count} -> {len(kept)} at {RECORD_COUNT_OFFSET:#x}")


def main():
    fx.print_disclaimer("fpq_editor")
    ap = argparse.ArgumentParser(
        prog="fpq_editor.py",
        description="Trim a Fluke VR1710 .fpq session to a start/end "
                    "date-time window (Power Log Classic 4.60 format).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser(
        "trim", help="keep records whose timestamp is inside the window")
    p.add_argument("input", help="source .fpq (or .fpq.gz)")
    p.add_argument("-o", "--out", required=True, help="output .fpq path")
    p.add_argument("--start", help="inclusive start, 'YYYY-MM-DD HH:MM:SS' "
                                   "(default: open)")
    p.add_argument("--end", help="inclusive end, 'YYYY-MM-DD HH:MM:SS' "
                                 "(default: open)")
    p.add_argument("--no-progress", action="store_true",
                   help="do not print phase/heartbeat progress on stderr")
    p.add_argument("--dry-run", action="store_true",
                   help="print the kept record counts/first/last timestamp "
                        "without writing")
    p.set_defaults(fn=cmd_trim)

    args = ap.parse_args()
    if not os.path.isfile(args.input):
        sys.exit(f"fpq_editor.py: error: input not found: {args.input}")
    if os.path.abspath(args.out) == os.path.abspath(args.input):
        sys.exit("fpq_editor.py: error: --out must differ from the input")
    parent = os.path.dirname(os.path.abspath(args.out)) or "."
    if not os.path.isdir(parent):
        sys.exit(f"fpq_editor.py: error: output directory not found: {parent}")
    try:
        args.fn(args)
    except (ValueError, OSError) as e:
        sys.exit(f"fpq_editor.py: error: {e}")


if __name__ == "__main__":
    main()
