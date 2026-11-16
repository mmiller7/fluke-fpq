#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "numpy",
#   "matplotlib",
# ]
# ///
"""fluke-fpq: a single self-contained CLI for decoding Fluke VR1710 .fpq files.

The `.fpq` session format ("Fichero FPQ V 4.2") written by Fluke Power Log
Classic 4.60 for VR1710 power quality loggers (firmware 1.15), reverse
engineered and verified against PowerLog CSV exports and screenshots. See
README.md and docs/ for the full format documentation and confidence levels.

Run as an executable, or via `python3 fpq_export.py …` (uv run also works):
it pins Python >= 3.11 and auto-installs numpy/matplotlib on first
use (cached afterwards). Stdlib-only subcommands (`config`, `walk`,
`walk-records`, `rms`, `events`, `summary`) need no downloaded packages at
all when run with a normal Python; `transients` (npz/CSV, `--png` also
matplotlib) and `all` need numpy.

Subcommands:
  config      cleartext config/threshold header -> JSON
              (incl. recording interval + record count)
  walk        loose record inventory / counts
  walk-records  strict per-record listing
  rms         RMS/frequency/THD/harmonics/Pst/Plt -> PowerLog-
              identical 164-column CSV
  events      dips/swells w/ thresholds+voltages (L1N channel),
              interruptions (0x49), gaps (0x6A),
              transient-capture timestamps (T)
  transients  T-record waveforms -> npz (float32 + 12-bit ADC codes)
              + per-record CSV, --png renders a graph per capture
              volts = raw/(32*sqrt(2)), 38.4 kHz
  summary     high-level overview: nominal settings, recording start/
              stop/duration/rows, event counts (Power Log Summary tabs)
  all         one-shot export: summary, rms CSV, events CSV, transients
              (npz/CSV), whole-session graph (PNG via --png), config JSON

Input files may be plain `.fpq`, gzip-compressed `.fpq.gz` (detected by
magic bytes; the extension does not matter) or `-` for the file piped in
on stdin. The disclaimer banner goes to stderr, so stdout stays clean for
piping (e.g. `config` JSON straight into `jq`).

Timestamps everywhere are OLE automation dates (float64 LE days since
1899-12-30). The record stream starts 43 bytes after the END of the
b"678901234" marker (offset 0x387 in all observed files) and runs to EOF
(optionally followed by zero padding).
"""

import argparse
import csv
import gzip
import json
import os
import re
import struct
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_COMMON = os.path.join(_HERE, "src_common")
if os.path.isdir(_SRC_COMMON):
    sys.path.insert(0, _SRC_COMMON)
sys.path.insert(0, _HERE)
from fpq_wire import umbral_volts
from fpq_progress import Progress
from decimal import Decimal, ROUND_HALF_UP

MARKER = b"678901234"
OLE_EPOCH = datetime(1899, 12, 30)
RECORD_BASE_FALLBACK = 0x387
INTERVAL_OFFSET = 0x373        # f64 OLE days, recording interval
RECORD_COUNT_OFFSET = 0x383    # u32 LE, total records in the stream
MARKER_RECORD_SLACK = 43     # record base = marker end + 43 in observed files
START_SEARCH_SPAN = 64       # firmware variants may shift the base by 0..63 B
_STDIN_CACHE = {}

TYPE_SIZES = {
    0x44: 22,    # 'D' dip event
    0x46: 30,    # 'F' frequency
    0x47: 77,    # 'G' voltage RMS
    0x48: 65,    # 'H' THD
    0x49: 18,    # 'I' interruption (float64 duration in days)
    0x4B: 22,    # 'K' Pst
    0x4C: 22,    # 'L' Plt
    0x50: 22,    # 'P' swell event
    0x54: 5153,  # 'T' transient waveform
    0x5C: 1262,  # harmonic lanes
    0x6A: 18,    # 'j' interval without measurements (duration layout)
}
RECORD_NAMES = {
    0x44: "D dip-event", 0x46: "F frequency", 0x47: "G voltage-rms",
    0x48: "H thd", 0x49: "I interruption", 0x4B: "K pst",
    0x4C: "L plt", 0x50: "P swell-event", 0x54: "T transient",
    0x5C: "\\l harmonics", 0x6A: "j measurement-gap",
}


def ole(v):
    """OLE automation days -> datetime."""
    return OLE_EPOCH + timedelta(days=v)


def norm(t):
    """Snap to the nearest whole second (fractional part ~ .999999/.000001)."""
    return (t + timedelta(microseconds=500000)).replace(microsecond=0)


def load_fpq(path):
    """Read a .fpq's bytes.

    Accepts a plain file path, a gzip-compressed `file.fpq.gz` (gzip is
    detected by its magic bytes, so the extension does not matter), or `-`
    for the .fpq piped in on stdin. A stdin read is memoized: several
    subcommands call this twice (e.g. `events` parses header+records), and
    a pipe has only one readable copy.
    """
    if path == "-":
        if "-" in _STDIN_CACHE:
            return _STDIN_CACHE["-"]
        data = sys.stdin.buffer.read()
    else:
        with open(path, "rb") as f:
            data = f.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if path == "-":
        _STDIN_CACHE["-"] = data
    return data


def outstem(path):
    """Input basename without extension(s), for derived output names.

    `file.fpq` -> `file`, `file.fpq.gz` -> `file`, `-` (stdin) -> `stdin`.
    """
    name = "stdin" if path == "-" else os.path.basename(path)
    if name.endswith(".gz"):
        name = name[:-3]
    return name.rsplit(".", 1)[0] if "." in name else name


def display_name(path):
    """Name for messages: the input as typed, with `<stdin>` for `-` and a
    gzip input shown by its .fpq name."""
    if path == "-":
        return "<stdin>"
    n = os.path.basename(path)
    return n[:-3] if n.endswith(".gz") else n


def ole_from_bytes(b):
    try:
        t = ole(struct.unpack("<d", b[:8])[0])
        return t if datetime(2010, 1, 1) < t < datetime(2035, 1, 1) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# record-stream walkers
# ---------------------------------------------------------------------------

def _try_walk(data, start):
    """Walk complete records from `start`.

    Returns (records, None) on success, else (None, reason). Success means
    the whole stream frames exactly (known type bytes, no truncation, zero
    padding only at EOF).
    """
    pos, recs, n = start, [], len(data)
    while pos < n:
        t = data[pos]
        if t == 0:
            break
        size = TYPE_SIZES.get(t)
        if size is None:
            return None, f"unknown type {t:#04x} at offset {pos:#x}"
        if pos + size > n:
            return None, f"truncated {t:#04x} record at {pos:#x}"
        ts = struct.unpack_from("<d", data, pos + 1)[0]
        recs.append((pos, t, ts))
        pos += size
    if any(data[pos:]):
        return None, f"nonzero slack after terminator at {pos:#x}"
    return recs, None


def find_records(data):
    """The one strict record walker: returns [(pos, type, ts_raw)].

    Both `walk_records()` (used by summary/events/transients/editor) and
    `rms` come through here, so the same bytes either pass every subcommand
    or fail them all. A candidate stream start is accepted only when the
    whole file frames exactly (known type bytes, no truncation, zero padding
    only at EOF) *and* the walked count equals the header count at 0x383
    (when the header is present).

    The record base is normally marker end + 43 (0x387), but firmware
    variants could shift it, so every start from marker end to marker end +
    63 is tried, then 0x387 as a marker-less fallback.
    """
    mi = data.find(MARKER)
    if mi != -1:
        canonical = mi + len(MARKER) + MARKER_RECORD_SLACK
        candidates = [canonical + k for k in range(START_SEARCH_SPAN)]
    else:
        canonical = RECORD_BASE_FALLBACK
        candidates = []
    candidates.append(RECORD_BASE_FALLBACK)

    header_count = (struct.unpack_from("<I", data, RECORD_COUNT_OFFSET)[0]
                    if len(data) >= RECORD_BASE_FALLBACK else None)
    count_mismatch = canonical_reason = None
    for start in dict.fromkeys(candidates):      # dedupe, keep order
        if not 0 <= start <= len(data):
            continue
        recs, reason = _try_walk(data, start)
        if start == canonical:
            canonical_reason = reason
        if recs is None:
            continue
        if not recs:
            # A file that ends exactly at the canonical record base is a
            # valid empty stream (header count 0); any other empty walk is
            # just a candidate that landed on zero padding.
            if start == canonical and header_count in (None, 0):
                return []
            continue
        if header_count is not None and header_count != len(recs):
            count_mismatch = (
                f"{len(recs)} records walked but header says {header_count} "
                f"at 0x383 (file truncated or misdetected stream start)")
            continue
        return recs
    if count_mismatch is not None:
        raise ValueError(count_mismatch)
    if canonical_reason is not None:
        raise ValueError(canonical_reason)
    raise ValueError(
        "record stream not found (no marker / 0x387 fallback failed)")


def walk_records(path):
    """Strict-framing record iterator: yields (offset, type, ts_raw, bytes).

    Delegates the start search and the header-count check to
    `find_records()`; each subcommand therefore accepts exactly the same
    files. All record types carry an OLE timestamp at +1. Raises ValueError
    on any byte that is not a known record type, a nonzero-slack terminator,
    or a truncated record; also raises if the walked record count differs
    from the header's record count (u32 at 0x383), so a truncated or
    misdetected file never yields silently partial data.
    """
    data = load_fpq(path)
    for pos, t, ts_raw in find_records(data):
        yield pos, t, ts_raw, data[pos:pos + TYPE_SIZES[t]]


def inventory(path):
    """Loose/tolerant record classifier; a quick inventory view, not a
    decode path (that is walk_records()).

    Walks from the record-stream base (0x387 area), NOT from byte 0, so the
    config header's text bytes are not miscounted as records. Unknown bytes
    inside the stream are skipped and counted by hex value. Returns
    (counts{type_byte: n}, t_offsets) plus per-type names via RECORD_NAMES.
    """
    data = load_fpq(path)
    mi = data.find(MARKER)
    base = mi + 9 + 43 if mi != -1 else RECORD_BASE_FALLBACK
    counts = {}
    t_recs = []
    i, n = base, len(data)
    while i < n and data[i]:
        b = data[i]
        if b in TYPE_SIZES:
            counts[b] = counts.get(b, 0) + 1
            if b == 0x54:
                t_recs.append(i)
            i += TYPE_SIZES[b]
        else:
            counts[("unk", hex(b))] = counts.get(("unk", hex(b)), 0) + 1
            i += 1
    return counts, t_recs


# ---------------------------------------------------------------------------
# shared config-header parsing
# ---------------------------------------------------------------------------

def parse_config(path):
    d = load_fpq(path)
    marker = d.find(MARKER)
    head = d[:marker if marker > 0 else 900].decode("latin-1")
    cfg = {"banner": d[:17].decode("latin-1").rstrip("\x00")}
    cfg["raw_fields_0x15"] = list(d[0x15:0x21])
    m = re.search(r"Tipo=(\d+)", head)
    cfg["tipo"] = m.group(1) if m else None
    cfg["channels"] = []
    for m in re.finditer(
            r"Canal=\s*(\d+)\s+Dip=\s*(\d+)\s+Pico=\s*(\d+)\s+Umbral=\s*(\d+)"
            r"\s+Interrupciones(\d+)", head):
        cfg["channels"].append(dict(canal=int(m.group(1)), dip=int(m.group(2)),
                                    pico=int(m.group(3)), umbral=int(m.group(4)),
                                    interrupciones=int(m.group(5))))
    for k, pat in [
            ("me", r"Me=\s*(-?\d+)\s+PT=\s*(\d+)\s+ST=\s*(\d+)\s+PC=\s*(\d+)\s+SC=\s*(\d+)"),
            ("vnom", r"VNom=\s*([\d.]+)\s+CNom=\s*([\d.]+)\s+FNom=\s*([\d.]+)"
                     r"\s+Topol=\s*(\d+)\s+Idiom=\s*(\d+)\s+NomFa=(\S)(\d*)"),
            ("armon", r"Armon=(\d+)\s+PLVer=(\d+)")]:
        m = re.search(pat, head)
        if m:
            if k == "me":
                cfg.update(me=int(m.group(1)), pt=int(m.group(2)),
                           st=int(m.group(3)), pc=int(m.group(4)), sc=int(m.group(5)))
            elif k == "vnom":
                cfg.update(vnom=float(m.group(1)), cnom=float(m.group(2)),
                           fnom=float(m.group(3)), topol=int(m.group(4)),
                           idiom=int(m.group(5)), nomfa=m.group(6))
            else:
                cfg.update(armon=int(m.group(1)), plver=int(m.group(2)))
    x = re.search(r"<confMode>.*?</cm>", head, re.S)
    cfg["xml"] = x.group() if x else None
    cfg["config_end_offset"] = marker + 9
    if marker != -1 and len(d) >= RECORD_BASE_FALLBACK:
        # verified on all 7 sample files: 0x373 = recording interval as OLE
        # days (1/5/60 s -> *86400), 0x383 = total record count
        cfg["interval_seconds"] = struct.unpack_from(
            "<d", d, INTERVAL_OFFSET)[0] * 86400
        cfg["record_count"] = struct.unpack_from(
            "<I", d, RECORD_COUNT_OFFSET)[0]
        # ST/SC text fields are always 1000 observed; NOT the interval
    return cfg


# ---------------------------------------------------------------------------
# subcommand: config
# ---------------------------------------------------------------------------

def cmd_config(args):
    out = {}
    for p in args.fpq:
        out[display_name(p)] = parse_config(p)
    print(json.dumps(out, indent=1))


# ---------------------------------------------------------------------------
# subcommand: walk  (loose inventory)
# ---------------------------------------------------------------------------

def cmd_walk(args):
    for path in args.fpq:
        counts, t_recs = inventory(path)
        per_type = {RECORD_NAMES.get(k, hex(k)): v
                    for k, v in sorted(counts.items()) if not isinstance(k, tuple)}
        unk = {k: v for k, v in counts.items() if isinstance(k, tuple)}
        print(f"== {display_name(path)}")
        print(f"   records by type: {per_type}")
        if unk:
            print(f"   stray bytes: {unk}")
        print(f"   T records: {len(t_recs)} at offsets "
              f"{t_recs[:12]}{' ...' if len(t_recs) > 12 else ''}")


def cmd_walk_records(args):
    """Strict walkthrough, one line per record (offset, type, length, ts)."""
    for path in args.fpq:
        print("== " + display_name(path))
        for off, t, ts_raw, rec in walk_records(path):
            ts = norm(ole(ts_raw))
            print(f"  {off:#011x}  {RECORD_NAMES.get(t, hex(t)):22s}  "
                  f"len={len(rec):4d}  {ts}")


# ---------------------------------------------------------------------------
# subcommand: rms  (verified byte-identical to PowerLog)
# ---------------------------------------------------------------------------

HARMONICS = range(2, 51)      # 49 lanes harmonics 2..50
LANE_START = 71               # abs offset of lane 0's Min float
LANE_GAP = 24                 # Min@0, Max@+4, Avg@+8 within a lane
N_NO_HARM_EMPTY = 151         # THD(3) + harmonics(147) + Pst(1) as empty


def fmt_num(v, keep_zero=True):
    """3 decimals; for |v|<1 suppress the leading '0.' unless keep_zero.

    Half-up rounding (PowerLog prints 1.5625 as 1.563, not banker-rounded).
    """
    v = float(v)
    s = str(Decimal(v).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
    if s.endswith("-0.000") or s == "-0.000":
        s = s.replace("-0.000", "0.000")
    if not keep_zero and abs(v) < 1 and s.startswith("0."):
        s = s[1:]
    return s


def rms_header(extra=True):
    """PowerLog CSV header. extra=True: the 164-column layout (Date/Time +
    L1N + NG + frequency + THD + harmonics + Pst + Plt + trailing empty).
    extra=False: PowerLog's 12-field layout for recordings without any
    THD/harmonic/flicker records."""
    cols = ["Date", "Time"]
    for name in ("L1N", "NG"):
        cols += [f"Half Cycle Voltage V RMS {name} {m}" for m in ("Min", "Avg", "Max")]
    cols += [f"Frequency {m}" for m in ("Min", "Avg", "Max")]
    if not extra:
        cols += [""]
        return cols
    cols += [f"THD V L1N {m}" for m in ("Min", "Avg", "Max")]
    for h in HARMONICS:
        cols += [f"Volts Harmonics{h} L1N {m}" for m in ("Min", "Avg", "Max")]
    cols += ["Pst  L1N", "Plt  L1N", ""]
    return cols


def rms_extract(path, all_columns=False, progress=None):
    """Returns (counts_hex, first_ts, last_ts, rows, harm_rows, extra).

    extra: True if the file carries any THD/harmonic/Pst/Plt record and (or
    all_columns=True was given). rows follow the matching layout.
    """
    data = load_fpq(path)
    recs = find_records(data)
    counts, gpos, fpos = {}, [], []
    hmap, kmap, cmap, lmap = {}, {}, {}, {}
    first_ts = last_ts = None
    if progress is not None:
        progress.phase("reading RMS records")
    for _n, (pos, t, ts) in enumerate(recs, start=1):
        if progress is not None and _n % 5000 == 0:
            progress.tick()
        counts[t] = counts.get(t, 0) + 1
        tt = norm(ole(ts))
        if t == 0x47:
            gpos.append(pos)
            if first_ts is None:
                first_ts = tt
            last_ts = tt
        elif t == 0x46:
            fpos.append(pos)
        elif t == 0x48:
            hmap[tt] = pos
        elif t == 0x4B:
            kmap[tt] = pos
        elif t == 0x5C:
            cmap[tt] = pos
        elif t == 0x4C:
            lmap[tt] = pos

    def f32(pos, off):
        return struct.unpack_from("<f", data, pos + off)[0]

    # PowerLog merges faulty seconds that carry TWO G/F pairs: one export row,
    # L1N/NG values of both pairs, frequency of the last pair. Group
    # consecutive G records sharing a normalized second.
    groups = []
    idx = 0
    if progress is not None:
        progress.phase("grouping RMS rows")
    while idx < len(gpos):
        ts = norm(ole(struct.unpack_from("<d", data, gpos[idx] + 1)[0]))
        grp = [gpos[idx]]
        idx += 1
        while idx < len(gpos):
            t2 = norm(ole(struct.unpack_from("<d", data, gpos[idx] + 1)[0]))
            if t2 != ts:
                break
            grp.append(gpos[idx])
            idx += 1
        groups.append((ts, grp))
        if progress is not None and len(groups) % 5000 == 0:
            progress.tick()

    # positional pairing only valid if each G's F-partner shares its second.
    # Tolerance: a group of G records whose second carries NO F records at all
    # is exported with empty Frequency cells (observed in the wild once: a
    # lone G logged in the first second of a ~6 s measurement gap (0x6A),
    # home 3-dec-2025). Anything else -- F without G, out-of-order pairs --
    # still fails loudly rather than silently corrupting columns.
    fidx = 0
    no_f_groups = []
    paired = []
    for ts, grp in groups:
        fgrp = None
        if (fidx < len(fpos)
                and norm(ole(struct.unpack_from("<d", data, fpos[fidx] + 1)[0])) == ts):
            fgrp = fpos[fidx:fidx + len(grp)]
            fidx += len(fgrp)
            for f_ in fgrp:
                ft = norm(ole(struct.unpack_from("<d", data, f_ + 1)[0]))
                if ft != ts:
                    raise ValueError(f"G/F record-order drift at {ts} vs {ft}")
        elif not any(norm(ole(struct.unpack_from("<d", data, f_ + 1)[0])) == ts
                     for f_ in fpos[fidx:fidx + len(grp)]):
            fgrp = None
            no_f_groups.append(ts)
        else:
            # F records for this second exist but not at the expected slot
            raise ValueError(
                f"G/F records out of order at {ts}: frequency record(s) for "
                f"this second exist but do not follow the G group positionally")
        paired.append((ts, grp, fgrp))
    if fidx != len(fpos):
        raise ValueError(f"unpaired F records beyond the last G group: "
                         f"{len(fpos) - fidx} leftover")

    rows = []
    harm_rows = 0
    if progress is not None:
        progress.phase("building spreadsheet rows")
    extra = bool(all_columns or counts.get(0x48) or counts.get(0x4B)
                 or counts.get(0x5C) or counts.get(0x4C))
    for _n, (ts, grp, fgrp) in enumerate(paired, start=1):
        if progress is not None and _n % 5000 == 0:
            progress.tick()
        row = [ts.strftime("%d/%m/%Y"), ts.strftime("%H:%M:%S")]
        for g in grp:
            for off in (18, 26, 22, 45, 53, 49):    # L1N & NG  Min Avg Max
                row.append(fmt_num(f32(g, off), keep_zero=False))
        if fgrp:
            for off in (18, 26, 22):                # Freq Min Avg Max (last pair)
                row.append(fmt_num(f32(fgrp[-1], off), keep_zero=False))
        else:                                       # lone-gap G: no F partner
            row += ["", "", ""]
        if extra:
            if ts in hmap:
                harm_rows += 1
                hp = hmap[ts]
                kpos = kmap.get(ts)
                cpos = cmap.get(ts)
                # every H row needs its K (Pst) and 5C (harmonics) partners; fail
                # loudly instead of TypeError'ing on None if one is ever missing
                if kpos is None or cpos is None:
                    raise ValueError(f"harmonic cluster incomplete at {ts}: "
                                     f"K={'missing' if kpos is None else 'ok'}, "
                                     f"5C={'missing' if cpos is None else 'ok'}")
                row.append(fmt_num(f32(hp, 22), keep_zero=False))   # THD Min (slot)
                row.append(fmt_num(f32(hp, 26), keep_zero=False))   # THD Avg
                row.append(fmt_num(f32(hp, 31), keep_zero=False))   # THD Max (slot)
                for h in HARMONICS:
                    base = cpos + LANE_START + LANE_GAP * (h - 2)
                    row.append(fmt_num(f32(base, 0), keep_zero=False))
                    row.append(fmt_num(f32(base, 8), keep_zero=False))
                    row.append(fmt_num(f32(base, 4), keep_zero=False))
                row.append(fmt_num(f32(kpos, 18), keep_zero=True))
            else:
                row += [""] * N_NO_HARM_EMPTY
            lpos = lmap.get(ts)
            row.append(fmt_num(f32(lpos, 18), keep_zero=True) if lpos else "")
        row.append("")
        rows.append(row)
    counts_hex = {"0x%02X" % t: c for t, c in sorted(counts.items())}
    if no_f_groups:
        print(f"note: {len(no_f_groups)} voltage row(s) carried no frequency "
              f"record at that second; exported with empty Frequency cells "
              f"(each was the first second of a 0x6A measurement gap)",
              file=sys.stderr)
    return counts_hex, first_ts, last_ts, rows, harm_rows, extra


def cmd_rms(args):
    progress = Progress("export", enabled=not getattr(args, "no_progress", False))
    try:
        counts, first_ts, last_ts, rows, harm_rows, extra = \
            rms_extract(args.fpq, all_columns=args.all_columns,
                        progress=progress)
    except ValueError as e:
        sys.exit(f"{args.fpq}: {e}")   # clean one-line error, exit code 1
    out = args.out or os.path.join(os.path.dirname(args.fpq),
                                   outstem(args.fpq) + "_rms.csv")
    # PowerLog writes CRLF line endings and, like its exports, no newline
    # after the last row; write bytes to reproduce that exactly.
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(rms_header(extra=extra))
    w.writerows(rows)
    payload = buf.getvalue().encode("latin-1")
    while payload.endswith(b"\r\n"):        # strip the ONE final CRLF
        payload = payload[:-2]
    if progress is not None:
        progress.phase("writing CSV")
    with open(out, "wb") as f:
        f.write(payload)
    if progress is not None:
        progress.done()
    lay = ("164-column (all THD/harmonic/flicker columns present)"
           if extra else
           "12-column layout (file has no THD/harmonic/flicker records)")
    print(f"{args.fpq}: {len(rows)} RMS rows ({harm_rows} with THD/Pst) "
          f"[{lay}]")
    if rows:
        print(f"  first ts: {first_ts}   last ts: {last_ts}")
        print(f"  record counts: {counts}")
    print(f"  wrote {out}")


# ---------------------------------------------------------------------------
# subcommand: summary
# ---------------------------------------------------------------------------

EVENT_TYPE_NAME = {0x49: "interruption", 0x6A: "interval_without_measurements",
                   0x44: "dip", 0x50: "swell", 0x54: "transient_capture"}


def summary_data(path, progress=None):
    """High-level overview comparable to Power Log's Summary tabs.
    Returns (config, recording-dict, events-dict)."""
    cfg = parse_config(path)
    rec = {"interval_seconds": cfg.get("interval_seconds"),
           "first_row": None, "last_row": None, "duration_s": None,
           "rms_rows": 0, "freq_rows": 0, "transient_captures": 0}
    ev = {"dips": 0, "swells": 0, "interruptions": 0,
          "intervals_without_measurements": 0,
          "min_dip_V": None, "max_swell_V": None,
          "max_interruption_s": None, "max_gap_s": None}
    firstg = lastg = None
    if progress is not None:
        progress.phase("reading recording summary")
    for _n, (off, t, _ts, r) in enumerate(walk_records(path), start=1):
        if progress is not None and _n % 5000 == 0:
            progress.tick()
        ts = norm(ole(struct.unpack_from("<d", r, 1)[0]))
        if t == 0x47:
            rec["rms_rows"] += 1
            if firstg is None:
                firstg = ts
            lastg = ts
        elif t == 0x46:
            rec["freq_rows"] += 1
        elif t == 0x54:
            rec["transient_captures"] += 1
        elif t == 0x44:
            ev["dips"] += 1
            v = struct.unpack_from("<f", r, 17)[0]
            if ev["min_dip_V"] is None or v < ev["min_dip_V"]:
                ev["min_dip_V"] = v
        elif t == 0x50:
            ev["swells"] += 1
            v = struct.unpack_from("<f", r, 17)[0]
            if ev["max_swell_V"] is None or v > ev["max_swell_V"]:
                ev["max_swell_V"] = v
        elif t in (0x49, 0x6A):
            s = struct.unpack_from("<d", r, 9)[0] * 86400
            if t == 0x49:
                ev["interruptions"] += 1
                if ev["max_interruption_s"] is None or s > ev["max_interruption_s"]:
                    ev["max_interruption_s"] = s
            else:
                ev["intervals_without_measurements"] += 1
                if ev["max_gap_s"] is None or s > ev["max_gap_s"]:
                    ev["max_gap_s"] = s
    if firstg is not None:
        rec["first_row"] = firstg.isoformat()
        rec["last_row"] = lastg.isoformat()
        rec["duration_s"] = (lastg - firstg).total_seconds()
    return cfg, rec, ev


def format_summary_text(stem, cfg, rec, ev):
    L = []
    A = L.append
    A(f"== {stem}")
    A("Measurement summary")
    A(f"  Nominal: V={cfg.get('vnom')} V  F={cfg.get('fnom')} Hz  "
      f"topology={cfg.get('topol')}")
    dips = [c["dip"] for c in cfg["channels"] if "dip" in c]
    picos = [c["pico"] for c in cfg["channels"] if "pico" in c]
    transients = sorted({umbral_volts(c['umbral']) for c in cfg['channels']
                         if c.get('umbral') is not None})
    A(f"  Thresholds: Dip={dips} V  Swell={picos} V  "
      f"Transient={[f'{v:.1f} V' for v in transients]}")
    A("Recording summary")
    if rec["first_row"] is None:
        A("  (no RMS rows)")
    else:
        A(f"  Start: {rec['first_row']}   Stop: {rec['last_row']}")
        d = rec["duration_s"] or 0
        h, rem = divmod(int(d), 3600); m, s = divmod(rem, 60)
        A(f"  Duration: {h:d}:{m:02d}:{s:02d} ({d:.0f} s)")
        A(f"  Interval: {rec['interval_seconds']} s   "
          f"RMS rows: {rec['rms_rows']}   Frequency rows: {rec['freq_rows']}")
    A("Events summary")
    A(f"  Dips: {ev['dips']}" + (f"   (min residual {ev['min_dip_V']:.1f} V)"
       if ev["min_dip_V"] is not None else ""))
    A(f"  Swells: {ev['swells']}" + (f"   (max excursion {ev['max_swell_V']:.1f} V)"
       if ev["max_swell_V"] is not None else ""))
    A(f"  Interruptions: {ev['interruptions']}" +
      (f"   (longest {ev['max_interruption_s']:.1f} s)"
       if ev["max_interruption_s"] is not None else ""))
    A(f"  Intervals without measurements: "
      f"{ev['intervals_without_measurements']}" +
      (f"   (longest {ev['max_gap_s']:.1f} s)"
       if ev["max_gap_s"] is not None else ""))
    A(f"  Transient captures: {rec['transient_captures']}")
    return "\n".join(L) + "\n"


def cmd_summary(args):
    progress = Progress("export", enabled=not getattr(args, "no_progress", False))
    for p in args.fpq:
        cfg, rec, ev = summary_data(p, progress=progress)
        print(format_summary_text(display_name(p), cfg, rec, ev), end="")
    if progress is not None:
        progress.done()


# ---------------------------------------------------------------------------
# subcommand: compare  (two recordings of the same meter memory)
# ---------------------------------------------------------------------------

def _g_rows(path):
    """[(ole_days, (L1, L2, L3))] for every RMS (`G`) row, in file order."""
    data = load_fpq(path)
    out = []
    for pos, typ, ts in find_records(data):
        if typ == 0x47:
            out.append((ts, struct.unpack_from("<3f", data, pos + 18)))
    return out


def cmd_compare(args):
    """Align two recordings by RMS row values and report the clock offset.

    This is the official-first / resume / tool-read flow: the first client
    pauses the meter, `resume` restarts it, and the tool reads later.  The
    tool's own dump has newer rows, so the two files are not byte-identical;
    the shared RMS rows still align exactly.  The measured offset between the
    aligned timestamps is the difference between the two pauses, so a stop
    time that was not logged can be recovered from the one that was.
    """
    import difflib
    import statistics
    a = _g_rows(args.first)
    b = _g_rows(args.second)
    print(f"{display_name(args.first)}: {len(a)} RMS rows")
    print(f"{display_name(args.second)}: {len(b)} RMS rows")
    if not a or not b:
        print("no RMS rows to compare")
        return 2
    av = [v for _t, v in a]
    bv = [v for _t, v in b]
    sm = difflib.SequenceMatcher(None, av, bv, autojunk=False)
    blocks = [m for m in sm.get_matching_blocks() if m.size]
    if not blocks:
        print("no overlapping RMS rows (different readings or a different "
              "meter memory)")
        return 2
    best = max(blocks, key=lambda m: m.size)
    total = sum(m.size for m in blocks)
    offs = [(b[best.b + k][0] - a[best.a + k][0]) * 86400.0
            for k in range(best.size)]
    offset = statistics.median(offs)
    spread = max(offs) - min(offs) if len(offs) > 1 else 0.0
    a0 = norm(ole(a[best.a][0]))
    a1 = norm(ole(a[best.a + best.size - 1][0]))
    b0 = norm(ole(b[best.b][0]))
    b1 = norm(ole(b[best.b + best.size - 1][0]))
    print(f"overlap: {best.size} row(s) ({total} across {len(blocks)} "
          f"block(s)); values identical")
    print(f"  first : rows {best.a}..{best.a + best.size - 1}  "
          f"{a0:%Y-%m-%d %H:%M:%S} .. {a1:%Y-%m-%d %H:%M:%S}")
    print(f"  second: rows {best.b}..{best.b + best.size - 1}  "
          f"{b0:%Y-%m-%d %H:%M:%S} .. {b1:%Y-%m-%d %H:%M:%S}")
    print(f"  clock offset (second - first): {offset:+.1f} s "
          f"(spread {spread:.3f} s)")
    print(f"  second has {len(b) - best.b - best.size} newer row(s) after "
          f"the overlap, {best.b} older row(s) before it")
    # The same physical pause: recover the unlogged stop from the logged one.
    last_a = norm(ole(a[-1][0]))
    last_b = norm(ole(b[-1][0]))
    print(f"stop times: first {last_a:%Y-%m-%d %H:%M:%S}  "
          f"second {last_b:%Y-%m-%d %H:%M:%S}")
    print(f"if the second stop was logged at the tool's pause, the first "
          f"stop is {last_b - timedelta(seconds=offset):%Y-%m-%d %H:%M:%S}")
    if spread > 1.0:
        print("warning: the offset is not constant across the overlap; the "
              "two files disagree on the internal timeline", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# subcommand: all  (one-shot full export)
# ---------------------------------------------------------------------------

def cmd_all(args):
    """One-shot export of everything: summary text, rms CSV, events CSV,
    transients (npz + csv, + png with --png), the whole-session graph
    (with --png) and config JSON -> outdir."""
    progress = Progress("export", enabled=not getattr(args, "no_progress", False))
    path = args.fpq
    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    stem = outstem(path)
    made = []
    # config JSON
    cfgj = os.path.join(outdir, stem + "_config.json")
    with open(cfgj, "w") as f:
        json.dump({stem: parse_config(path)}, f, indent=1)
    made.append(cfgj)
    # summary text
    progress.phase("summary")
    cfg, rec, ev = summary_data(path, progress=progress)
    stx = format_summary_text(stem, cfg, rec, ev)
    stx_path = os.path.join(outdir, stem + "_summary.txt")
    with open(stx_path, "w") as f:
        f.write(stx)
    print(stx, end="" if stx.endswith("\n") else "\n")
    made.append(stx_path)
    # rms CSV (same rules as `rms`, incl. layout + no final newline)
    progress.phase("spreadsheet")
    counts, first_ts, last_ts, rows, harm_rows, extra = \
        rms_extract(path, all_columns=args.all_columns, progress=progress)
    out_rms = os.path.join(outdir, stem + "_rms.csv")
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(rms_header(extra=extra))
    w.writerows(rows)
    payload = buf.getvalue().encode("latin-1")
    while payload.endswith(b"\r\n"):
        payload = payload[:-2]
    with open(out_rms, "wb") as f:
        f.write(payload)
    made.append(out_rms)
    print(f"rms: {len(rows)} rows [{extra and '164' or '12'}-column layout] -> {out_rms}")
    # events CSV
    progress.phase("events")
    out_ev = os.path.join(outdir, stem + "_events.csv")
    erows = event_rows(path, progress=progress)
    with open(out_ev, "w", newline="") as fh:
        cw = csv.writer(fh)
        cw.writerow(["type", "timestamp_iso", "duration_seconds", "voltage",
                     "threshold", "channel", "t_index", "trailer_byte",
                     "record_offset"])
        cw.writerows(erows)
    made.append(out_ev)
    print(f"events: {len(erows)} rows -> {out_ev}")
    # transients
    progress.phase("transients")
    transient_extract(path, outdir, write_csv=not args.no_csv,
                      write_png=args.png, progress=progress)
    made.append(outdir)
    # whole-session graph, the same output as `graph`; --png asks for every
    # PNG this export can produce, not just the per-capture ones
    if args.png:
        out_graph = os.path.join(outdir, stem + "_graph.png")
        graph_session(path, out_graph, progress=progress)
        made.append(out_graph)
    if progress is not None:
        progress.done()
    print(f"wrote {len(made)} outputs for {stem}")


def event_rows(path, progress=None):
    """One tuple per event row, in file order:
    (type_name, ts_iso_snapped, duration_s|None, voltage|None,
     threshold|None, channel|None, t_index|None, trailer_byte|None,
     record_offset). Dips/swells carry voltage (L1N channel) and the
     Dip/Swell threshold from the file config; transient rows carry t_index
     (T0..Tn) instead of a threshold; trailing bytes: +21 for D/P, +17 for
     I/j, unused for T."""
    cfg = parse_config(path)
    dips = [c["dip"] for c in cfg["channels"] if "dip" in c]
    picos = [c["pico"] for c in cfg["channels"] if "pico" in c]
    dip_thr = min(dips) if dips else None
    pico_thr = max(picos) if picos else None
    rows = []
    t_idx = 0
    if progress is not None:
        progress.phase("reading event records")
    for _n, (off, t, _ts, rec) in enumerate(walk_records(path), start=1):
        if progress is not None and _n % 5000 == 0:
            progress.tick()
        if t not in EVENT_TYPE_NAME:
            continue
        ts = norm(ole(struct.unpack("<d", rec[1:9])[0]))
        secs = voltage = thr = trailer = t_ix = None
        if t in (0x49, 0x6A, 0x44, 0x50):
            secs = struct.unpack("<d", rec[9:17])[0] * 86400
        if t in (0x44, 0x50):
            voltage = struct.unpack_from("<f", rec, 17)[0]
            thr = dip_thr if t == 0x44 else pico_thr
            trailer = rec[21]
            chan = "L1N"
        elif t in (0x49, 0x6A):
            trailer = rec[17]
            chan = None
        else:                       # 0x54 transient capture
            t_ix = t_idx
            t_idx += 1
            chan = None
        rows.append((EVENT_TYPE_NAME[t], ts.isoformat(), secs, voltage,
                     thr, chan, t_ix, trailer, off))
    return rows


def _event_line(name, ts, secs, volt, thr, chan, t_ix, trailer):
    """One `events` stdout line, byte-compatible with the historical layout.

    `val` is already 7 chars wide (`{volt:7.1f}` or 6 spaces + '-'), so it is
    interpolated without a second width spec. The channel field is padded
    only on the trailer line, which keeps the transient line's trailing
    space (its channel is empty and there is no trailer).
    """
    dur = f"{secs:12.4f} s" if secs is not None else "           -"
    val = f"{volt:7.1f}" if volt is not None else "     -"
    lim = (f"{thr}" if thr is not None else
           f"T{t_ix}" if t_ix is not None else "-")
    chan = chan or ""
    if trailer is not None:
        return (f"{ts} {name:30s} {dur}  V={val} ({lim}) "
                f"{chan:4s} trailer={trailer:#04x}")
    return f"{ts} {name:30s} {dur}  V={val} ({lim}) {chan}"


def cmd_events(args):
    progress = Progress("export", enabled=not getattr(args, "no_progress", False))
    rows = event_rows(args.fpq, progress=progress)
    for name, ts, secs, volt, thr, chan, t_ix, trailer, _off in rows:
        print(_event_line(name, ts, secs, volt, thr, chan, t_ix, trailer))
    if args.out:
        with open(args.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["type", "timestamp_iso", "duration_seconds", "voltage",
                        "threshold", "channel", "t_index", "trailer_byte",
                        "record_offset"])
            w.writerows(rows)
        print(f"wrote {args.out}")
    if progress is not None:
        progress.done()


# ---------------------------------------------------------------------------
# subcommand: transients
# ---------------------------------------------------------------------------

VOLT_SCALE = 1.0 / (32 * 2 ** 0.5)
NOMINAL_SAMPLES = 1280
SAMPLE_HZ = 38400  # 38.4 samples/ms; t_ms = arange(1280) * 1000 / 38400


def transient_extract(path, outdir, write_csv=True, write_png=False,
                      progress=None):
    try:
        import numpy as np
    except ModuleNotFoundError:
        raise SystemExit(
            "numpy is required for transient export.\n"
            "Fix it with one of:\n"
            + "\n".join(_fix_lines("python3-numpy", "python3-numpy")))
    os.makedirs(outdir, exist_ok=True)
    kit = outstem(path)
    tss, offs, raws, metas, ns = [], [], [], [], []
    if write_png:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ModuleNotFoundError:
            raise SystemExit(
                "matplotlib is required for --png graphs.\n"
                "Fix it with one of:\n"
                + "\n".join(_fix_lines("python3-matplotlib",
                                       "python3-matplotlib")))
    t_n = 0            # T-record counter for T0..Tn naming (matches events)
    if progress is not None:
        progress.phase("reading transient captures")
    for _n, (off, t, _, rec) in enumerate(walk_records(path), start=1):
        if progress is not None and _n % 5000 == 0:
            progress.tick()
        if t != 0x54:
            continue
        ts = ole(struct.unpack("<d", rec[1:9])[0]).isoformat()
        n = struct.unpack_from("<I", rec, 22)[0]
        if n != NOMINAL_SAMPLES or len(rec) != 26 + 4 * n + 7:
            raise ValueError(f"T record {t_n}: unexpected n={n}, len={len(rec)}")
        raw = np.frombuffer(rec, dtype="<f4", count=n, offset=26).copy()
        tss.append(ts)
        offs.append(off)
        raws.append(raw)
        metas.append(np.frombuffer(rec[9:17], dtype=np.uint8))
        ns.append(n)
        if write_csv or write_png:
            name = ts.replace(":", "").replace("-", "").replace("T", "_") if ts else f"unk{t_n}"
            stem = f"{kit}_T{t_n}_{name}"
            volts = raw[::-1] * VOLT_SCALE          # display order
            t_ms = np.arange(n) * 1000 / SAMPLE_HZ
            if write_csv:
                cs = os.path.join(outdir, stem + ".csv")
                with open(cs, "w") as f:
                    f.write("idx,volts,t_ms\n")
                    f.writelines(f"{j},{volts[j]:.4f},{t_ms[j]:.4f}\n"
                                 for j in range(n))
            if write_png:
                fig, ax = plt.subplots(figsize=(10, 4))
                ax.plot(t_ms, volts, lw=0.8, color="#1f4e79")
                ax.axhline(0, color="0.3", lw=0.6)
                ax.set_title(f"{display_name(path)}  T{t_n}  @ {ts}")
                ax.set_xlabel("waveform time (ms)")
                ax.set_ylabel("volts")
                ax.grid(alpha=0.3)
                fig.tight_layout()
                fig.savefig(os.path.join(outdir, stem + ".png"), dpi=110)
                plt.close(fig)
        t_n += 1
        if progress is not None and t_n % 10 == 0:
            progress.tick()
    if not tss:
        print(f"{kit}: no T records")
        if progress is not None:
            progress.done()
        return
    if progress is not None:
        progress.phase("writing transient outputs")
    raws = np.array(raws, dtype=np.float32)
    volts = raws[..., ::-1].astype(np.float32) * np.float32(VOLT_SCALE)
    np.savez(os.path.join(outdir, f"{kit}_T.npz"),
             ts=np.array(tss), off=np.array(offs),
             raw=raws, volts=volts,
             code=(raws // 10).astype(np.int16),
             t_ms=np.arange(NOMINAL_SAMPLES) * 1000.0 / SAMPLE_HZ,
             meta=np.array(metas), n=np.array(ns))
    print(f"{kit}: {len(offs)} T records -> {outdir}")


def cmd_transients(args):
    progress = Progress("export", enabled=not getattr(args, "no_progress", False))
    for p in args.fpq:
        transient_extract(p, args.outdir, write_csv=not args.no_csv,
                          write_png=args.png, progress=progress)
    if progress is not None:
        progress.done()



def _fix_lines(debian: str, redhat: str, extras: str = "") -> list[str]:
    """Distribution and virtualenv install lines (no bare system pip)."""
    venv = ("python3 -m venv .venv && . .venv/bin/activate && "
            "python3 -m pip install -r requirements.txt")
    return [f"  sudo apt-get install {debian}",
            f"  sudo yum install {redhat}",
            f"  {venv}{extras}"]


def _machine_status_text() -> str:
    """Short capability block appended to every --help."""
    from importlib.util import find_spec
    from fpq_env import interpreter_lines
    missing = [name for name in ("numpy", "matplotlib")
               if find_spec(name) is None]
    lines = ["Machine status (this interpreter):"]
    lines.extend(interpreter_lines())
    if not missing:
        lines.append("  numpy, matplotlib available: all features enabled")
        return "\n".join(lines)
    why = {"numpy": "transients/all disabled",
           "matplotlib": "--png graphs disabled"}
    for name in missing:
        lines.append(f"  {name:<11} missing: {why[name]}")
    deb = " ".join(f"python3-{name}" for name in missing)
    lines.append("  fix (one of):")
    lines.extend(_fix_lines(deb, deb))
    lines.append("  windows: activate the venv and run `python ...`, not "
                 "`py -3 ...`")
    return "\n".join(lines)


class _HelpParser(argparse.ArgumentParser):
    """Append the machine-status block to the main and subcommand helps."""

    def format_help(self) -> str:
        text = super().format_help().rstrip("\n")
        return f"{text}\n\n{_machine_status_text()}\n"


def _warn_optional_deps() -> None:
    """Non-fatal startup note when an optional package is missing.

    numpy and matplotlib are needed only by `transients`/`all` (and
    `--png`), so the rest of the tool keeps working. Printing the note
    early makes the missing package obvious before a command fails.
    """
    from importlib.util import find_spec
    missing = [name for name in ("numpy", "matplotlib")
               if find_spec(name) is None]
    if not missing:
        return
    deb = " ".join(f"python3-{name}" for name in missing)
    print(f"note: optional package(s) missing: {', '.join(missing)}",
          file=sys.stderr)
    print("      'transients'/'all' need numpy; '--png' also needs "
          "matplotlib.", file=sys.stderr)
    print("      fix (one of):", file=sys.stderr)
    for line in _fix_lines(deb, deb):
        print("  " + line.strip(), file=sys.stderr)


def print_disclaimer(prog="fluke-fpq"):
    """Print the legal/independence notice to stderr.

    stdout is reserved for data (config JSON, tables, CSV), so every notice
    goes to stderr and cannot contaminate pipes or redirects.
    """
    print("* " + "-" * 72, file=sys.stderr)
    print(f"* {prog} is an independent, reverse-engineered tool.",
          file=sys.stderr)
    print("* Not affiliated with or endorsed by Fluke Corporation.",
          file=sys.stderr)
    print("* 'Fluke', 'VR1710' and 'Power Log' are trademarks of their owner;",
          file=sys.stderr)
    print("* outputs matching Power Log's export do not imply any measurement",
          file=sys.stderr)
    print("* accuracy or calibration.", file=sys.stderr)
    print("*", file=sys.stderr)
    print("* Provided \"as is\", without warranty of any kind, express or",
          file=sys.stderr)
    print("* implied, including accuracy, completeness, fitness for a",
          file=sys.stderr)
    print("* particular purpose or non-infringement. No guarantee is made",
          file=sys.stderr)
    print("* that decoded or exported data matches the instrument or the",
          file=sys.stderr)
    print("* vendor's software. You use this software and rely on its output",
          file=sys.stderr)
    print("* entirely at your own risk; the authors accept no liability for",
          file=sys.stderr)
    print("* any loss, damage or decision made using it. For measurement,",
          file=sys.stderr)
    print("* compliance or safety decisions use a calibrated instrument and",
          file=sys.stderr)
    print("* the vendor's official software.", file=sys.stderr)
    print("* " + "-" * 72, file=sys.stderr)


def _break_traces(t, series, bands):
    """Break voltage traces across bands with no recorded data.

    `t` is a sorted list of datetimes, `series` a sequence of equal-length
    float sequences, and `bands` a list of (start, end) datetime pairs (the
    `I` outage and `j` measurement-gap spans).  Rows whose timestamp falls
    inside a band are masked, and a NaN sample is inserted at every band
    edge, so matplotlib cannot draw a line across a period the recorder did
    not measure.  Pure Python (no numpy) so it is unit-testable.
    """
    spans = []
    for start, end in bands:
        if end < start:
            start, end = end, start
        spans.append((start, end))
    if not spans:
        return list(t), [list(s) for s in series]
    inside = [any(start < ti < end for start, end in spans) for ti in t]
    order = [(ti, i) for i, ti in enumerate(t)]
    order += [(edge, None) for span in spans for edge in span]
    order.sort(key=lambda point: point[0])
    t_out = [point[0] for point in order]
    out = []
    for values in series:
        out.append([float("nan") if i is None or inside[i] else values[i]
                    for _ti, i in order])
    return t_out, out


def graph_session(path, out, progress=None):
    """Render the whole session as a Power-Log-style PNG.

    Two panels (L1N, N-G) with min/avg/max traces; grey `I` outage bands,
    hatched `j` measurement gaps, red dip markers/labels and transient
    markers.  Large files are downsampled for the trace only; the bands and
    markers are complete.
    """
    try:
        import numpy as np
    except ModuleNotFoundError:
        raise SystemExit(
            "numpy is required for --png graphs.\n"
            "Fix it with one of:\n"
            + "\n".join(_fix_lines("python3-numpy", "python3-numpy")))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ModuleNotFoundError:
        raise SystemExit(
            "matplotlib is required for --png graphs.\n"
            "Fix it with one of:\n"
            + "\n".join(_fix_lines("python3-matplotlib",
                                   "python3-matplotlib")))

    if progress is not None:
        progress.phase("reading records")
    rows, dips, swells, transients, outages, gaps = [], [], [], [], [], []
    for _n, (_off, t, _ts, rec) in enumerate(walk_records(path), start=1):
        if progress is not None and _n % 5000 == 0:
            progress.tick()
        if t == 0x47:
            rows.append((
                ole(struct.unpack_from("<d", rec, 1)[0]),
                struct.unpack_from("<f", rec, 18)[0],
                struct.unpack_from("<f", rec, 26)[0],
                struct.unpack_from("<f", rec, 22)[0],
                struct.unpack_from("<f", rec, 45)[0],
                struct.unpack_from("<f", rec, 53)[0],
                struct.unpack_from("<f", rec, 49)[0],
            ))
        elif t in (0x44, 0x50):
            dt = ole(struct.unpack_from("<d", rec, 1)[0])
            dur = struct.unpack_from("<d", rec, 9)[0] * 86400.0
            volts = struct.unpack_from("<f", rec, 17)[0]
            (dips if t == 0x44 else swells).append((dt, dur, volts))
        elif t == 0x54:
            transients.append(ole(struct.unpack_from("<d", rec, 1)[0]))
        elif t in (0x49, 0x6A):
            dt = ole(struct.unpack_from("<d", rec, 1)[0])
            dur = struct.unpack_from("<d", rec, 9)[0] * 86400.0
            (outages if t == 0x49 else gaps).append((dt, dur))

    if not rows:
        raise ValueError(f"{display_name(path)}: no G (voltage-RMS) rows")
    rows.sort(key=lambda r: r[0])
    n_all = len(rows)
    if n_all > 20000:
        idx = np.linspace(0, n_all - 1, 20000).astype(int)
        rows = [rows[i] for i in idx]

    if progress is not None:
        progress.phase("rendering voltage traces")
    t = [r[0] for r in rows]
    l1n_min = np.array([r[1] for r in rows], dtype=float)
    l1n_avg = np.array([r[2] for r in rows], dtype=float)
    l1n_max = np.array([r[3] for r in rows], dtype=float)
    ng_min = np.array([r[4] for r in rows], dtype=float)
    ng_avg = np.array([r[5] for r in rows], dtype=float)
    ng_max = np.array([r[6] for r in rows], dtype=float)
    # No data was recorded inside an outage/gap band, so the trace must not
    # bridge it: mask the rows and insert NaN edges (matplotlib leaves a gap).
    band_spans = [(dt, dt + timedelta(seconds=max(0.0, dur)))
                  for dt, dur in outages + gaps]
    t_plot, (l1n_min, l1n_avg, l1n_max,
             ng_min, ng_avg, ng_max) = _break_traces(
        t, (l1n_min, l1n_avg, l1n_max, ng_min, ng_avg, ng_max), band_spans)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, sharex=True, figsize=(16, 9),
        gridspec_kw={"height_ratios": [2.1, 1.0]})
    for ax, lo, avg, hi, colour, label in (
            (ax1, l1n_min, l1n_avg, l1n_max, "#1f4e79", "L1-N"),
            (ax2, ng_min, ng_avg, ng_max, "#2e7d32", "N-G")):
        ax.fill_between(t_plot, lo, hi, color=colour, alpha=0.16, linewidth=0,
                        label=f"{label} min/max")
        ax.plot(t_plot, avg, color=colour, lw=0.8, label=f"{label} avg")
        ax.plot(t_plot, lo, color=colour, lw=0.25, alpha=0.55)
        ax.plot(t_plot, hi, color=colour, lw=0.25, alpha=0.55)
        ax.grid(alpha=0.25)
        ax.set_ylabel(f"{label} (V)")

    # Bands are drawn on both panels so the two traces stay aligned.
    for ax in (ax1, ax2):
        first_i = True
        for dt, dur in outages:
            end = dt + timedelta(seconds=max(0.0, dur))
            ax.axvspan(dt, end, color="0.72", alpha=0.55, zorder=0,
                       label="outage (I)" if first_i else None)
            first_i = False
        first_j = True
        for dt, dur in gaps:
            end = dt + timedelta(seconds=max(0.0, dur))
            ax.axvspan(dt, end, facecolor="white", edgecolor="0.45",
                       hatch="///", alpha=0.35, zorder=0,
                       label="gap (j)" if first_j else None)
            first_j = False

    # Dip / swell markers on the L1N panel.
    if dips:
        ax1.scatter([d[0] for d in dips], [d[2] for d in dips],
                    marker="v", s=34, color="#c62828", zorder=5,
                    label="dip (D)")
        if len(dips) <= 20:
            for dt, _dur, volts in dips:
                ax1.annotate(f"{volts:.1f} V", (dt, volts),
                             xytext=(0, 8), textcoords="offset points",
                             ha="center", fontsize=7, color="#8e0000",
                             bbox=dict(boxstyle="round,pad=0.15",
                                       fc="white", ec="none", alpha=0.75))
        else:
            dt, _dur, volts = min(dips, key=lambda d: d[2])
            ax1.annotate(f"min {volts:.1f} V", (dt, volts),
                         xytext=(0, 8), textcoords="offset points",
                         ha="center", fontsize=7, color="#8e0000")
    if swells:
        ax1.scatter([d[0] for d in swells], [d[2] for d in swells],
                    marker="^", s=34, color="#6a1b9a", zorder=5,
                    label="swell (P)")

    # Transient captures: vertical markers on the L1N panel.
    first_t = True
    for dt in transients:
        ax1.axvline(dt, color="#ef6c00", lw=0.8, ls=":", alpha=0.85,
                    zorder=4, label="transient (T)" if first_t else None)
        first_t = False

    try:
        vnom = parse_config(path).get("vnom")
    except Exception:
        vnom = None
    if vnom:
        ax1.axhline(float(vnom), color="0.35", lw=0.6, ls="--",
                    label=f"nominal {float(vnom):g} V")

    ax1.set_title(
        f"{display_name(path)}  |  {t[0]:%Y-%m-%d %H:%M:%S} -> "
        f"{t[-1]:%Y-%m-%d %H:%M:%S}  |  {n_all:,} rows"
        + (f"  |  {len(outages)} I / {len(gaps)} j / "
           f"{len(dips)} D / {len(swells)} P / {len(transients)} T"
           if (outages or gaps or dips or swells or transients) else ""))
    ax1.legend(loc="upper right", fontsize=8, ncol=3, framealpha=0.9)
    ax2.grid(alpha=0.25)
    ax2.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(
        ax2.xaxis.get_major_locator()))
    fig.autofmt_xdate()
    fig.tight_layout()

    if progress is not None:
        progress.phase("writing PNG")
    fig.savefig(out, dpi=120, bbox_inches="tight",
                metadata={"Software": "fluke-fpq"})
    plt.close(fig)
    if progress is not None:
        progress.done()
    print(f"wrote {out}: {n_all} rows, {len(dips)} D, {len(swells)} P, "
          f"{len(transients)} T, {len(outages)} I, {len(gaps)} j")
    return out


def cmd_graph(args):
    # Default to the current directory (like `transients`/`all`), not the
    # input file's directory: callers often batch inputs from elsewhere.
    out = args.out or (outstem(args.fpq) + "_graph.png")
    if not out.lower().endswith(".png"):
        out += ".png"
    progress = Progress("export", enabled=not getattr(args, "no_progress", False))
    graph_session(args.fpq, out, progress=progress)


def _add_progress_flag(p):
    p.add_argument("--no-progress", action="store_true",
                   help="do not print phase/heartbeat progress on stderr")


def main():
    # --help carries the machine status itself; do not repeat the note
    if not any(a in ("-h", "--help") for a in sys.argv[1:]):
        _warn_optional_deps()
    print_disclaimer("fluke-fpq")
    ap = _HelpParser(
        prog="fpq_export.py",
        description="Fluke VR1710 .fpq file decoder toolkit "
                    "(Power Log Classic 4.60 format; run everything with "
                    "'python3 fpq_export.py ...' or ./fpq_export.py ...)",
        epilog="Format details and confidence levels: README.md, docs/.")
    sub = ap.add_subparsers(dest="cmd", required=True,
                            parser_class=_HelpParser)

    p = sub.add_parser(
        "settings",
        help="saved recorder settings from the .fpq header -> JSON")
    p.add_argument("fpq", nargs="+")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser(
        "spreadsheet",
        help="Power Log Spreadsheet CSV (RMS/frequency/THD/flicker)")
    p.add_argument("fpq")
    p.add_argument("-o", "--out", help="output CSV (default <fpq>_rms.csv, "
                                       "written next to the input file)")
    p.add_argument("--all-columns", action="store_true",
                   help="always write the 164-column layout (default: "
                        "PowerLog's 12-field layout for recordings with no "
                        "THD/harmonic/flicker records)")
    _add_progress_flag(p)
    p.set_defaults(fn=cmd_rms)

    p = sub.add_parser(
        "debug",
        description="Format diagnostics for damaged or unusual files.")
    dbg = p.add_subparsers(dest="debug_cmd", required=True,
                           parser_class=_HelpParser)
    q = dbg.add_parser("inventory",
                       help="record inventory / counts (tolerant of damage)")
    q.add_argument("fpq", nargs="+")
    q.set_defaults(fn=cmd_walk)
    q = dbg.add_parser("records",
                       help="strict record listing, one line per record "
                            "(fails on a damaged stream)")
    q.add_argument("fpq", nargs="+")
    q.set_defaults(fn=cmd_walk_records)

    p = sub.add_parser("events",
                       help="event timeline: dips/swells (with threshold "
                            "extremes from the file config), interruptions, "
                            "measurement gaps, transient-capture timestamps")
    p.add_argument("fpq")
    p.add_argument("-o", "--out", help="write CSV of events")
    _add_progress_flag(p)
    p.set_defaults(fn=cmd_events)

    p = sub.add_parser("transients",
                       help="T records -> npz (+ optional CSV and PNG graphs)")
    p.add_argument("-o", "--outdir", default=".",
                   help="output directory (default: current directory)")
    p.add_argument("--no-csv", action="store_true",
                   help="skip per-record CSVs, write only the .npz")
    p.add_argument("--png", action="store_true",
                   help="also render one graph per transient: volts vs "
                        "waveform time (ms), <stem>.png in the output dir")
    _add_progress_flag(p)
    p.set_defaults(fn=cmd_transients)
    p = sub.add_parser("summary",
                       help="high-level overview: nominal settings, "
                            "recording start/stop/duration/rows, event "
                            "counts (dip/swell/interruption/gap/captures)")
    p.add_argument("fpq", nargs="+")
    _add_progress_flag(p)
    p.set_defaults(fn=cmd_summary)

    p = sub.add_parser(
        "compare",
        help="align two recordings by RMS row values and report the clock "
             "offset (official-first / resume / tool-read flow)")
    p.add_argument("first", help="first recording (e.g. the official save)")
    p.add_argument("second", help="second recording (e.g. the tool build)")
    p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("all",
                       help="one-shot full export: summary (stdout + txt), "
                            "spreadsheet CSV, events CSV, transients npz/CSV, "
                            "whole-session graph, settings JSON "
                            "(PNGs with --png)")
    p.add_argument("fpq")
    p.add_argument("-o", "--outdir", default=".",
                   help="output directory (default: current directory)")
    p.add_argument("--no-csv", action="store_true",
                   help="skip per-capture CSVs (npz/PNG only)")
    p.add_argument("--png", action="store_true",
                   help="render the whole-session graph and one graph per "
                        "transient capture")
    p.add_argument("--all-columns", action="store_true",
                   help="force the 164-column rms layout")
    _add_progress_flag(p)
    p.set_defaults(fn=cmd_all)

    p = sub.add_parser(
        "graph",
        help="render the whole session as a PNG: L1N/N-G traces, outage and "
             "gap bands, dip/swell/transient markers")
    p.add_argument("fpq")
    p.add_argument("-o", "--out", help="output PNG (default "
                                       "<fpq-stem>_graph.png in the current "
                                       "directory)")
    _add_progress_flag(p)
    p.set_defaults(fn=cmd_graph)

    args = ap.parse_args()
    paths = getattr(args, "fpq", None)
    if paths is not None:
        if not isinstance(paths, (list, tuple)):
            paths = [paths]
        for path in paths:
            if path != "-" and not os.path.isfile(path):
                sys.exit(f"{path}: file not found")
    out = getattr(args, "out", None)
    if out:
        parent = os.path.dirname(os.path.abspath(out)) or "."
        if not os.path.isdir(parent):
            sys.exit(f"{out}: output directory not found: {parent}")
    outdir = getattr(args, "outdir", None)
    if outdir:
        parent = os.path.dirname(os.path.abspath(outdir)) or "."
        if not os.path.isdir(parent):
            sys.exit(f"{outdir}: output directory not found: {parent}")
    try:
        args.fn(args)
    except (ValueError,) as e:
        sys.exit(str(e))
    except BrokenPipeError:
        # A pipe consumer (head, less) closed early; not an export error.
        try:
            sys.stdout.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
