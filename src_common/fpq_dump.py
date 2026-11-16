"""Dump framing and item walk for the VR1710 serial capture.

This module turns the raw memory dump (4-byte preamble, paged payload with
3-byte footers and CRC-16/XMODEM per page, closing 0x04) into row/event
dictionaries, and derives the recording interval from the 10-minute blocks.
It was split out of fpq_serial_meter.py without behaviour changes; the meter
re-exports every name below.

Page framing constants and the transient codec come from fpq_wire; the
counter/gap model comes from fpq_gap.

Disclaimer: provided "as is", without warranty of any kind, express or
implied, including accuracy or fitness for a particular purpose. Use at your
own risk; the authors accept no liability for any loss or damage. Not
affiliated with or endorsed by Fluke Corporation.
"""
import re
import struct
import sys

from fpq_wire import (
    PAGE_PAYLOAD, PAGE_TRAILER, PAGE_STRIDE,
    TRANSIENT_PAYLOAD,
    TRANSIENT_STORED,
    crc16_xmodem as _crc16_xmodem,
    s12 as _s12,
    decode_transient_payload,
    looks_transient as _looks_transient,
)
from fpq_gap import (GAP_ANALYSIS_ERRORS, _analyse_counter_gaps,
                     _analyse_counter_gaps_robust, _wrap_cut,
                     _stale_suffix_start, _reset_suffix_start)

DUMP_PREFIX = b"\x02\x01\xfe"
# Real meter dumps are paged: after the 4-byte preamble the stream is a
# 3-byte footer (`02 k ~k`), then 1,024 payload bytes, then the
# CRC-16/XMODEM of those bytes (big-endian), repeated on a 1,029-byte
# stride [verified on the clean meter2/live dumps: 686/261 consecutive
# footers, k increasing by one, every page CRC valid].
# Items SPLIT across the footer, so footers AND the CRC must be removed
# before item walking; deleting only the footer leaves the CRC bytes
# in-stream, corrupts the split item and can invent a spurious 0x0b
# block.
# Page framing constants come from fpq_wire (shared with the replay).
# Recording intervals supported by the recorder / Power Log Classic 4.60
# (owner-confirmed from the official software). Do not add values here
# without that confirmation: any future interval-setting code must send only
# these, and derive_interval() snaps only to these.
INTERVAL_LABELS = {1: "1s", 2: "2s", 5: "5s", 10: "10s", 30: "30s",
                   60: "1m", 300: "5m", 600: "10m", 900: "15m",
                   1200: "20m"}
STANDARD_INTERVALS = tuple(INTERVAL_LABELS)
INTERVAL_HELP = ", ".join(INTERVAL_LABELS[s] for s in STANDARD_INTERVALS)

# Power Log's Settings dialog, "Typical Continuous Measurement", captured
# 2026-10-27 (5 s-20 m) and 2026-10-30 (1 s and 2 s).
INTERVAL_TYPICAL = {
    1: "1 day 0 h 50 min",
    2: "2 days 1 h 34 min",
    5: "5 days 3 h 6 min",
    10: "10 days 3 h 32 min",
    30: "29 days 4 h 10 min",
    60: "54 days 21 h 58 min",
    300: "186 days 17 h 5 min",
    600: "266 days 17 h 30 min",
    900: "311 days 4 h 15 min",
    1200: "339 days 11 h 20 min",
}


def interval_typical(seconds) -> str:
    """The official dialog's typical continuous measurement for an interval."""
    return INTERVAL_TYPICAL.get(int(seconds), "unknown")


def _typical_seconds(text: str) -> int | None:
    """'1 day 0 h 50 min' -> seconds (None when the text does not parse)."""
    try:
        parts = text.split()
        total = 0
        i = 0
        while i + 1 < len(parts):
            if not parts[i].isdigit():
                return None
            value = int(parts[i])
            unit = parts[i + 1].lower()
            mult = {"d": 86400, "day": 86400, "days": 86400,
                    "h": 3600, "hour": 3600, "hours": 3600,
                    "m": 60, "mi": 60, "min": 60, "minute": 60,
                    "s": 1, "sec": 1}.get(unit, None)
            if mult is None:
                return None
            total += value * mult
            i += 2
        return total or None
    except Exception:
        return None


# Ring capacity in rows, derived from Power Log's own "Typical Continuous
# Measurement" dialog (that is the official software's display, so the
# values carry that provenance, not ours): the dialog's total time is the
# capacity in seconds; rows = seconds / interval (1 s -> 89,400 rows,
# 2 s -> 89,220 rows, 5 s -> 87,792 rows, ...).
INTERVAL_CAPACITY_RECORDS = {}
for _seconds, _typical in INTERVAL_TYPICAL.items():
    _cap_seconds = _typical_seconds(_typical)
    if _cap_seconds is not None and _seconds > 0:
        INTERVAL_CAPACITY_RECORDS[_seconds] = _cap_seconds // _seconds


def interval_capacity_records(seconds: int) -> int | None:
    """Ring capacity in rows for `seconds`, from Power Log's dialog."""
    return INTERVAL_CAPACITY_RECORDS.get(int(seconds))


# The `v` counter (also the 4-byte V preamble) counts memory-write units,
# not records: the ring is 2**21 units.  A record consumes
# 2**21 / capacity_records(interval) units, so the fill is
# min(v, 2**21) / 2**21.  Calibrated live 2026-11-10: br2 reported
# v = 1,092,192 and Power Log showed 52.07% (1,092,192 / 2,097,152 =
# 52.08%); the old code compared v directly with the record count and
# showed 100%.
RING_COUNTER_UNITS = 1 << 21


def memory_used_records(counter: int | None, interval: int | None) -> int | None:
    """Record-count equivalent of the write counter, capped at the ring."""
    cap = interval_capacity_records(int(interval)) if interval is not None else None
    if counter is None or not cap:
        return None
    units = min(int(counter), RING_COUNTER_UNITS)
    return min(cap, int(round(units / float(RING_COUNTER_UNITS) * cap)))


def memory_use_percent(counter: int | None, interval: int | None) -> float | None:
    """Recorded memory fill in percent (0-100), or None when unknowable.

    `counter` is the `v` memory-write counter.  The ring capacity is
    `RING_COUNTER_UNITS` write units; the per-interval record capacity is
    only a display conversion (records = v / units_per_record).
    """
    if counter is None or interval is None:
        return None
    if interval_capacity_records(int(interval)) is None:
        return None
    return max(0.0, min(int(counter), RING_COUNTER_UNITS)
               / float(RING_COUNTER_UNITS) * 100.0)


def expected_dump_bytes(counter: int | None) -> int | None:
    """Total bytes a dump of `counter` write units will stream.

    The `v` counter (also the 4-byte V preamble) counts item-stream bytes,
    so the reply is: 4-byte preamble + one 1,029-byte page per 1,024
    payload bytes (the last page is padded) + the closing 0x04.
    Reproduces the exact size of every fixture with a usable counter (30_,
    35_, 43_, 44_, 46_, 50_, 67_, 77_, 85_, 87_, 89_-96_, 99_, 100_), which
    makes it usable for a download percentage and ETA.  82_ is the one
    stream with an interior duplicate page run, so its file is larger than
    this estimate.  Returns None for a missing/zero counter.
    """
    if counter is None or int(counter) <= 0:
        return None
    pages = -(-int(counter) // PAGE_PAYLOAD)
    return 4 + pages * PAGE_STRIDE + 1


def interval_seconds(value) -> int:
    """Parse and validate a recording interval ('2', '2s', '1m', ...).

    Returns the interval in whole seconds. Raises ValueError for anything
    the recorder cannot be set to. This is the single gate future
    interval-setting code must use before sending a value.
    """
    text = str(value).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|m|min)?", text)
    if not m:
        raise ValueError(f"invalid interval {value!r}; supported: "
                         f"{INTERVAL_HELP}")
    secs = float(m.group(1)) * (60 if (m.group(2) or "s").startswith("m")
                                else 1)
    if secs not in STANDARD_INTERVALS:
        raise ValueError(f"unsupported interval {value!r}; the recorder "
                         f"supports only: {INTERVAL_HELP}")
    return int(secs)


def _item(buf: bytes, i: int):
    """Decode one 6-byte item at `i` -> (v1, v2, v3) as signed 12-bit."""
    b = buf
    v1 = ((b[i + 1] & 0x0F) << 8) | b[i + 2]
    v2 = (b[i + 3] << 4) | (b[i + 4] >> 4)
    v3 = ((b[i + 4] & 0x0F) << 8) | b[i + 5]
    return _s12(v1), _s12(v2), _s12(v3)


# Transient constants and the codec come from fpq_wire.


def _strip_page_footers(body: bytes, stats: dict | None = None,
                        progress=None) -> bytes:
    """Remove the meter's paging and return the continuous item stream.

    Real meter layout [fact, verified byte-for-byte against 686 pages]:
    at offset 0 there is the 3-byte footer `02 01 fe` (the dump prefix),
    then each page is 1,024 payload bytes followed by the CRC-16/XMODEM of
    those bytes stored big-endian, then the next footer `02 k ~k`; stride
    is exactly 1,029 bytes. The payload is a cut through the continuous
    item track, so items split across pages are rejoined here.

    The older replay-tool layout (prefix + 1,021 then 1,024-byte payloads
    separated by 3-byte footers, 1,027 stride, no checksum) is still
    accepted for old captures.

    Detection is strict: the footer grid AND the page CRCs must agree, so
    a footer-free flat stream is returned untouched. `stats` (if given)
    receives `pages` and `crc_bad`.
    """
    if stats is not None:
        stats["pages"] = 0
        stats["crc_bad"] = 0
        stats.pop("seq", None)

    # Real-meter layout: footers at 0, 1029, 2058, ...  A single complete
    # page is enough to establish the grid; the CRC check then decides
    # whether the de-framing is lossless. Short sessions (1-3 pages) used to
    # fall through to the flat path, leaving each page CRC in the item
    # stream and corrupting items that straddle a page boundary.
    if len(body) >= PAGE_STRIDE:
        pages = []
        seq = []
        pos = 0
        page_no = 0
        while pos + PAGE_STRIDE <= len(body):
            if not (body[pos] == 0x02
                    and (body[pos + 1] + body[pos + 2]) & 0xFF == 0xFF):
                break
            page_no += 1
            if progress is not None and page_no % 128 == 0:
                progress.tick()
            payload = body[pos + 3:pos + 3 + PAGE_PAYLOAD]
            crc = struct.unpack(
                ">H", body[pos + 3 + PAGE_PAYLOAD:
                           pos + 3 + PAGE_PAYLOAD + PAGE_TRAILER])[0]
            pages.append((payload, crc))
            seq.append(body[pos + 1])
            pos += PAGE_STRIDE
        if pages:
            crc_ok = sum(1 for payload, crc in pages
                         if _crc16_xmodem(payload) == crc)
            # A real grid numbers its footers k, k+1, ... and has no
            # complete page left after the last one we consumed.
            seq_ok = all((seq[k + 1] - seq[k]) & 0xFF == 1
                         for k in range(len(seq) - 1))
            grid_complete = seq_ok and pos + PAGE_STRIDE > len(body)
            if crc_ok == len(pages):
                out = bytearray()
                for payload, _crc in pages:
                    out += payload
                out += body[pos:]          # final partial page/tail
                if stats is not None:
                    stats["pages"] = len(pages)
                    stats["crc_bad"] = 0
                    stats["seq"] = list(seq)
                return bytes(out)
            # A verified page CRC is proof the footer grid is real; with
            # >=4 framing-consistent pages the grid itself is proof enough.
            # Only then de-page a lossy capture (a bad page means data loss,
            # so warn loudly instead of silently leaving CRCs in-stream).
            if grid_complete and (crc_ok >= 1 or len(pages) >= 4):
                print(f"warning: paged dump: {len(pages) - crc_ok} of "
                      f"{len(pages)} page CRC(s) failed; de-framing the "
                      "grid anyway (data on those pages is suspect)",
                      file=sys.stderr)
                out = bytearray()
                for payload, _crc in pages:
                    out += payload
                out += body[pos:]
                if stats is not None:
                    stats["pages"] = len(pages)
                    stats["crc_bad"] = len(pages) - crc_ok
                    stats["seq"] = list(seq)
                return bytes(out)
            if grid_complete and len(pages) >= 2:
                print("warning: paged dump detected but its page grid could "
                      "not be verified (bad CRC); keeping the legacy "
                      "flat-stream fallback, page CRCs remain in-stream",
                      file=sys.stderr)

    # Replay paged layout (legacy): prefix at 0, footer k=2 at 1,024, then
    # 1,027-byte stride.
    if body.startswith(DUMP_PREFIX) and len(body) >= 1024 + 3 * 1027:
        ok = all(body[p] == 0x02
                 and (body[p + 1] + body[p + 2]) & 0xFF == 0xFF
                 for p in (1024, 2051, 3078))
        if ok:
            out = bytearray(body[3:1024])          # prefix + 1,021 payload
            pos = 1024
            while pos < len(body):
                if (pos + 3 <= len(body) and body[pos] == 0x02
                        and (body[pos + 1] + body[pos + 2]) & 0xFF == 0xFF):
                    pos += 3
                out += body[pos:pos + PAGE_PAYLOAD]
                pos += PAGE_PAYLOAD
            return bytes(out)

    return body


def _stale_page_runs(seq):
    """Interior runs of non-sequential page numbers (ring-page replay).

    A wrapped ring can duplicate whole pages in the middle of the stream:
    the footer `k` stops incrementing and repeats one or more values.  The
    duplicated pages are stale copies; Power Log keeps the last page of the
    run (the one that resumes the sequence) and drops the rest.  Return
    `(first_page, last_page)` 0-based indices; the caller drops
    `first_page..last_page-1` and keeps `last_page`.  Only interior runs
    qualify; a run that reaches the end is the suffix case handled by the
    reset/stale-suffix rules.
    """
    runs = []
    i = 1
    while i < len(seq):
        if (seq[i] - seq[i - 1]) & 0xFF == 1:
            i += 1
            continue
        j = i
        while (j + 1 < len(seq)
               and (seq[j + 1] - seq[j]) & 0xFF != 1):
            j += 1
        if j > i and j < len(seq) - 1:
            runs.append((i, j))
        i = j + 1
    # A duplicated page group can contain one page whose k happens to be the
    # expected successor (e.g. k=186 repeated, then k=187 repeated): that one
    # page sits between two non-sequential runs but is part of the same stale
    # block.  Merge runs separated by at most one page.
    merged = []
    for first, last in runs:
        if merged and first - merged[-1][1] <= 2:
            merged[-1] = (merged[-1][0], last)
        else:
            merged.append((first, last))
    return merged


def parse_dump(raw: bytes, interval: float | None = None,
               keep_after_reset: bool = False, progress=None) -> dict:
    """Tolerant item-stream parser: preamble + body + closing 0x04 -> rows.

    Returns {"rows": [(l1n, ng, freq)], "ticks": [(row_index, pp, lanes)],
             "skipped": n, "pages": n, "crc_bad": n,
             "wrapped_rows": n, "gap": {...}}.  Values stay in WIRE order
    (max, avg, min) as signed 12-bit codes; conversion to volts/Hz happens
    in build_fpq().
    Every D (`dips`) and P (`swells`) item carries `offset` (its byte
    position in the de-framed stream) and `order` (its sequence among events
    and counter markers); `counter_items_meta` exposes the same fields for
    the `06`/`09`/`0A` markers, so intra-row order survives the parse.
    When the `06` counter goes backwards (ring wrap), rows after the last
    record of the current segment are dropped, which is what Power Log
    keeps from the same wire stream.  A wrapped ring can also end in a
    partially overwritten page whose counter drop is under the 60 s wrap
    threshold; the repeated counter value and the duplicated row run trim
    that stale suffix too.
    Paged dumps are de-framed first (footers and page CRCs removed), so
    items split across a page boundary are rejoined. Unknown/fragment bytes
    are still skipped one at a time so a dropped byte cannot abort the dump.

    `gap` is the counter-derived relative timeline (see
    `_analyse_counter_gaps`): per-row elapsed seconds, the 09/0A pairs and
    one gap record per row gap (`I` when the resume row has a 09+0A pair,
    else `j`).  It uses no absolute time; build_fpq() shifts it under its
    anchor.  `interval` defaults to the 10-minute-block estimate.

    `trim` reports which suffix rule cut the rows (`ring wrap`,
    `stale ring copy` or `power-cycle reset`), so the caller can warn when
    a reset-sized counter jump removed data.  Set `keep_after_reset=True`
    to skip that rule (a genuine pause longer than the ring capacity).
    `gap_error` holds the solver's reason when counter evidence exists but
    no I/j schedule could be built; it must not be dropped silently.
    """
    if len(raw) < 5:
        raise ValueError("dump too short")
    body = raw[4:]                      # drop the 4-byte preamble
    paging: dict = {}
    if progress is not None:
        progress.phase("de-framing pages")
    body = _strip_page_footers(body, paging, progress=progress)   # 1,024 + CRC-16 / 1,029 grid
    # The closing EOT is only safe to remove when paging fixed the payload
    # length: the byte after the last page belongs to no item.  On a flat
    # stream the last byte can be real data (`04` dip or a duration), so it is
    # left to the walker, which skips an orphan byte but never eats data.
    if paging.get("pages", 0) and body.endswith(b"\x04"):
        body = body[:-1]
    if body.startswith(DUMP_PREFIX):    # also a valid k=1 footer
        body = body[3:]
    rows = []
    ticks = []
    counters = []                   # (rows completed so far, counter value)
    counter_items = []              # (row, type 0x06/0x09/0x0A, payload)
    # Same counter markers as `counter_items`, but with the item-stream byte
    # offset and a sequential order.  D/P events carry the same two fields,
    # so a writer can reproduce Power Log's intra-row order (an `04` before
    # the `09`/`0A` pair is the interruption start edge; after it is the
    # post-interruption residual).  `counter_items` stays a tuple list for
    # every existing caller.
    counter_items_meta = []
    marker_seq = 0
    page_rows = [(0, 0)] if paging.get("seq") else []
    next_page = PAGE_PAYLOAD
    transients = []                 # decoded 0x15 captures (see below)
    dips = []                       # 0x04 dip events (see below)
    swells = []                     # 0x10 swell events (see below)
    suspect_transients = 0          # length-valid but frame-lead suspect
    skipped = 0
    zero_pad = 0
    pv = pn = None
    i = 0
    n = len(body)
    next_tick = 65536
    if progress is not None:
        progress.phase("scanning item stream")
    while i < n:
        while page_rows and i >= next_page:
            page_rows.append((next_page, len(rows)))
            next_page += PAGE_PAYLOAD
        if progress is not None and i >= next_tick:
            progress.tick()
            next_tick = i + 65536
        b = body[i]
        if (b == 0x02 and i + 3 <= n
                and (body[i + 1] + body[i + 2]) & 0xFF == 0xFF):
            i += 3                      # page footer 02 k ~k
            continue
        if b == 0x12 and i + 6 <= n and (body[i + 1] >> 4) <= 1:
            vals = _item(body, i)
            if body[i + 1] >> 4 == 0:
                pv = vals
            else:
                pn = vals
            i += 6
            continue
        if b == 0x17 and i + 6 <= n and (body[i + 1] >> 4) == 1:
            pf = _item(body, i)
            if pv is None or pn is None:
                skipped += 6
            else:
                rows.append((pv, pn, pf))
            pv = pn = None
            i += 6
            continue
        # Counter family. Subtype 0x02 is the first unit's form (payload a
        # multiple of 128); the second unit writes subtype 0x00 with the
        # same 6-byte shape and ~128 units/s (2026-10-27). Accept both so
        # the stream stays in step.
        if b in (0x06, 0x09, 0x0A) and i + 6 <= n and body[i + 1] in (0x00,
                                                                    0x02):
            value = int.from_bytes(body[i + 2:i + 6], "big")
            counters.append((len(rows), value))
            counter_items.append((len(rows), b, value))
            counter_items_meta.append({"row": len(rows), "type": b,
                                       "value": value, "offset": i,
                                       "order": marker_seq})
            marker_seq += 1
            i += 6                      # counter/status item (meaning open)
            continue
        # Dip event: `04 <u16 lo> <u8 code> <u16 dur BE>` (6-byte item).
        # Voltage = code/8 + the event row's 32 V window offset (96 V on
        # 120 V, 224 V on the br1 244.5 V row; see
        # fpq_wire.dip_volt_offset) and
        # duration = dur/240 s. `lo` is a 1/4 s counter used to place
        # several events inside one row. The code is a single byte and the
        # duration is a big-endian u16: when the duration's high byte is 0
        # this is identical to the older `04 <u16 lo> <u16 code LE> <u8 dur>`
        # reading that fitted the 2026-10-27 capture (all 128 D items there
        # have a zero code high byte), but the 2026-10-28 variac run has
        # durations of 3.5/36/20 s whose high byte is nonzero (0x03/0x21/
        # 0x12) - the naive reading then overlaps duration and voltage.
        # P is the separate 0x10 item; I/j are gap-derived. This item
        # type is the dip family.
        if b == 0x04 and i + 6 <= n:
            dips.append({
                "row": len(rows),
                "lo": int.from_bytes(body[i + 1:i + 3], "little"),
                "code": body[i + 3],
                "dur_raw": int.from_bytes(body[i + 4:i + 6], "big"),
                "offset": i,
                "order": marker_seq,
            })
            marker_seq += 1
            i += 6
            continue
        # Swell event: `10 <u8 flags> <u16 code> <u16 dur>` (6-byte item).
        # Matched to Power Log's 4 swells on the 2026-10-28 variac capture:
        # voltage = (code & 0xFFF)/8, duration = dur/240 s. `row` is the
        # stream position (the event end, which is what Power Log stamps).
        if b == 0x10 and i + 6 <= n:
            swells.append({
                "row": len(rows),
                "code": int.from_bytes(body[i + 2:i + 4], "big") & 0xFFF,
                "dur_raw": int.from_bytes(body[i + 4:i + 6], "big"),
                "offset": i,
                "order": marker_seq,
            })
            marker_seq += 1
            i += 6
            continue
        # Transient capture, second-unit variant: `15 00 <u32 BE ticks>` +
        # a VARIABLE number of 6-byte frames, terminated by the next normal
        # item (09/0A/06 counter or 12/17 row).  The br1 2026-11-03 timer
        # session writes this form; the `15 02` form below is the fixed
        # 427-frame block.  The byte after the last frame is real stream
        # data, so consuming a fixed 2,562-byte payload would swallow the
        # counters and then rescan waveform bytes as D/P (the observed
        # failure: 7 false D, 16 false P, 2,247 skipped bytes, 0 T).
        if (b == 0x15 and i + 12 <= n and body[i + 1] == 0x00
                and body[i + 6] in (0x14, 0xD4)):
            k = 0
            while (i + 6 + 6 * (k + 1) <= n
                   and body[i + 6 + 6 * k] in (0x14, 0xD4)):
                k += 1
            if k >= 8:
                # The stored .fpq keeps all 3k samples, zero-padded to the
                # 1,280-sample slot.  (A mistaken "3 pre-trigger samples"
                # reading came from decoding one frame late; the official 2026-11-03 save
                # matches the full run sample-for-sample.)
                frames = body[i + 6:i + 6 + 6 * k]
                codes = decode_transient_payload(
                    frames + b"\x00" * (TRANSIENT_PAYLOAD - len(frames))
                )[:3 * k]
                codes = (codes + [0] * TRANSIENT_STORED)[:TRANSIENT_STORED]
                transients.append({
                    "row": len(rows),
                    "ticks": int.from_bytes(body[i + 2:i + 6], "big"),
                    "codes": codes,
                    "offset": i,
                })
                i += 6 + 6 * k
                continue
        # Transient capture: 15 02 <u32 BE ticks> + 427 x 6-byte frames.
        # Payloads are opaque to the normal item grammar, so consume the
        # whole block in one step. `row` is the stream position (rows
        # completed at the capture) and is what the .fpq writer uses to
        # place the T record; `ticks` is the meter's 1/128 s timebase.
        if (b == 0x15 and i + 6 + TRANSIENT_PAYLOAD <= n
                and body[i + 1] == 0x02):
            payload = body[i + 6:i + 6 + TRANSIENT_PAYLOAD]
            if _looks_transient(payload):
                transients.append({
                    "row": len(rows),
                    "ticks": int.from_bytes(body[i + 2:i + 6], "big"),
                    "codes": decode_transient_payload(payload),
                    "offset": i,
                })
            else:
                # A firmware variant may use different frame lead bits. The
                # block length is known, so NEVER rescan its payload as
                # items (waveform bytes would turn into spurious dips,
                # swells, counters and rows): consume it and record a
                # suspect capture instead.
                suspect_transients += 1
                print("warning: transient capture at offset "
                      f"{i} does not match the verified frame-lead pattern; "
                      "consumed as a suspect capture (samples not written)",
                      file=sys.stderr)
            i += 6 + TRANSIENT_PAYLOAD
            continue
        # Status/10-min block: `0b <u16 PP BE> 00 00 00` followed by 6-byte
        # `01 <II> <h0..h3>` groups. PP = round(Pst*256). The two high
        # bytes are the big-endian PP; they read as 0 in low-Pst blocks and
        # as 0/1 in the 2026-10-28 power run, so the header test is on the
        # three trailing zero bytes.  Accepting the block here keeps the
        # harmonic group bytes (which can contain 0x04) out of the top-level
        # item scan. Any other first payload byte is left to the resync path.
        if (b == 0x0B and i + 6 <= n
                and body[i + 3:i + 6] == b"\x00\x00\x00"):
            pp = int.from_bytes(body[i + 1:i + 3], "big")
            i += 6
            lanes = [0] * 49
            while i + 6 <= n and body[i] == 0x01:
                ii = body[i + 1]
                base = 8 * (ii // 2) + (1 if ii % 2 == 0 else 0)
                for k in range(4):
                    ln = base + 2 * k
                    if ln < 49:
                        lanes[ln] = body[i + 2 + k]
                i += 6
            if rows:
                ticks.append((len(rows) - 1, pp, lanes))
            continue
        if b == 0 and not any(body[i:]):
            # Final-page zero padding is not a parse failure; account for it
            # separately so a clean dump does not report hundreds of
            # "unparsed" bytes.
            zero_pad = n - i
            break
        skipped += 1
        i += 1
    if progress is not None:
        progress.phase("trimming ring suffix")
    # Ring wrap: the dump ends in an OLDER segment once the counter goes
    # backwards [fact: meter2's counter drops at row 37,631 after a 10-min
    # session; Power Log keeps 37,631 rows and discards the 45 that follow].
    # Only a fully CRC-verified page grid may be trimmed: on a lossy or
    # flat capture (pages 0 / bad CRCs) a counter jump is as likely to be
    # a dropped byte as a real wrap, and trimming would silently discard
    # good rows. A single stray value is also not a wrap: the jump must be
    # at least a minute and every later counter must stay below the peak.
    wrapped_rows = 0
    gap_interval = (float(interval) if interval is not None
                    else derive_interval(ticks)[0])
    trusted_grid = paging.get("pages", 0) >= 4 and not paging.get("crc_bad")
    trim = None
    interior_pages = []
    if trusted_grid and page_rows and paging.get("seq"):
        # A wrapped ring can replay whole pages in the middle of the dump.
        # The footer sequence stops incrementing there; drop the duplicate
        # pages but keep the page that resumes the sequence, then continue
        # with the newer data (the old suffix-only rule cut everything
        # after the first duplicate).
        page_row = {off // PAGE_PAYLOAD: row for off, row in page_rows}
        remove = set()
        for first, last in _stale_page_runs(paging["seq"]):
            a = page_row.get(first)
            b = page_row.get(last)
            if a is None or b is None or b <= a:
                continue
            remove.update(range(a, b))
            interior_pages.append((first, last))
        if remove:
            keep = [r for r in range(len(rows)) if r not in remove]
            remap = {old: new for new, old in enumerate(keep)}
            rows = [rows[r] for r in keep]
            ticks = [(remap[r], pp, lanes) for r, pp, lanes in ticks
                     if r in remap]
            counters = [(remap[r], v) for r, v in counters if r in remap]
            counter_items = [(remap[r], t, v)
                             for r, t, v in counter_items if r in remap]
            counter_items_meta = [dict(m, row=remap[m["row"]])
                                  for m in counter_items_meta
                                  if m["row"] in remap]
            transients = [dict(x, row=remap[x["row"]])
                          for x in transients if x["row"] in remap]
            dips = [dict(x, row=remap[x["row"]])
                    for x in dips if x["row"] in remap]
            swells = [dict(x, row=remap[x["row"]])
                      for x in swells if x["row"] in remap]
            page_rows = [(off, remap.get(row, len(keep)))
                         for off, row in page_rows]
            wrapped_rows += len(remove)
            trim = {"rule": "stale ring pages",
                    "cut": None, "rows": len(remove),
                    "interior_pages": interior_pages}
    if trusted_grid:
        cut = None
        trim_rule = None
        for k in range(1, len(counters)):
            prev = counters[k - 1][1]
            rest = counters[k:]
            if (prev - rest[0][1] >= 60 * 128 and len(rest) >= 2
                    and all(v < prev for _r, v in rest)):
                # The boundary is the row count carried by the *first* item
                # of the older segment: rows 0..cut-1 are the new segment.
                # The old `counters[k-1]` cut also dropped the rows between
                # that counter and the wrap point (7 extra rows on the 5 s
                # power dump, hiding its final 1,742 s j).
                cut = min(_wrap_cut(counters, rows, k), len(rows))
                trim_rule = "ring wrap"
                break
        # A wrapped ring can also end in a partially overwritten page whose
        # counter drop is too small for the >= 60 s rule above: a stale copy
        # from a few dozen rows earlier, then the start of the ring.  The
        # repeated counter value and the duplicated row run pin that start,
        # which can lie before the first stale counter item.
        stale = _stale_suffix_start(counter_items, rows)
        if stale is not None and (cut is None or stale < cut):
            cut = stale
            trim_rule = "stale ring copy"
        # A power-up base reset is a forward counter jump far beyond the
        # ring's capacity, not elapsed time.  Power Log keeps the pre-reset
        # segment and drops the new segment's 28-row ring-start block.
        # `keep_after_reset` is the escape hatch for the case this rule
        # cannot distinguish: a genuine pause longer than the ring capacity
        # (25 h at 1 s).  The caller warns when the trim fires.
        reset = (None if keep_after_reset else
                 _reset_suffix_start(counter_items, len(rows), gap_interval,
                                     rows=rows))
        if reset is not None and (cut is None or reset < cut):
            cut = reset
            trim_rule = "power-cycle reset"
        if cut is not None:
            suffix_rows = len(rows) - cut
            wrapped_rows += suffix_rows
            if trim is None:
                trim = {"rule": trim_rule, "cut": cut, "rows": suffix_rows}
            else:
                trim.update({"rule": trim_rule, "cut": cut,
                             "suffix_rows": suffix_rows,
                             "rows": wrapped_rows})
            rows = rows[:cut]
            # An item recorded at row `cut` sits after the last kept row,
            # so every filter is strict `<` (also matches ticks).
            ticks = [t for t in ticks if t[0] < cut]
            counters = [c for c in counters if c[0] < cut]
            counter_items = [c for c in counter_items if c[0] < cut]
            counter_items_meta = [m for m in counter_items_meta
                                  if m["row"] < cut]
            transients = [t for t in transients if t["row"] < cut]
            dips = [d for d in dips if d["row"] < cut]
            swells = [w for w in swells if w["row"] < cut]
    gap = None
    gap_error = None
    has_counter = any(t == 0x06 for _r, t, _v in counter_items)
    if len(counter_items) >= 2 and rows and has_counter:
        try:
            if progress is not None:
                progress.phase("solving the counter timeline")
            gap = _analyse_counter_gaps_robust(counter_items, len(rows),
                                                gap_interval,
                                                progress=progress)
        except GAP_ANALYSIS_ERRORS as exc:
            gap = None
            gap_error = str(exc)
        if gap is None and gap_error is None:
            # The robust fallback swallowed the error: recover a reason so a
            # dump with counter evidence never loses its I/j records
            # silently (the 2026-11-02 br2 failure produced 0 I / 0 j with
            # no message at all).
            try:
                _analyse_counter_gaps(counter_items, len(rows), gap_interval)
            except GAP_ANALYSIS_ERRORS as exc:
                gap_error = str(exc)
            else:
                gap_error = "no consistent counter gap schedule"
    if progress is not None:
        progress.done()
    return {"rows": rows, "ticks": ticks, "skipped": skipped,
            "zero_pad": zero_pad,
            "counters": counters,
            "counter_items": counter_items,
            "counter_items_meta": counter_items_meta,
            "transients": transients,
            "dips": dips,
            "swells": swells,
            "suspect_transients": suspect_transients,
            "gap": gap,
            "gap_error": gap_error,
            "trim": trim,
            "pages": paging.get("pages", 0),
            "crc_bad": paging.get("crc_bad", 0),
            "wrapped_rows": wrapped_rows}


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def derive_interval(ticks, override=None) -> tuple[float, str]:
    """Recording interval: 10-min blocks are 600 s apart, so the median
    row spacing between ticks gives the interval. Snaps to a standard
    setting when within 2 %."""
    if override is not None:
        return float(interval_seconds(override)), "option"
    if len(ticks) >= 2:
        rows = [t[0] for t in ticks]
        med = _median([rows[k + 1] - rows[k] for k in range(len(rows) - 1)])
        if med > 0:
            est = 600.0 / med
            for s in STANDARD_INTERVALS:
                if abs(est - s) <= 0.02 * s:
                    return float(s), f"inferred from {len(ticks)} blocks"
            return round(est, 3), f"inferred (non-standard) from {len(ticks)} blocks"
    return 5.0, "default (no 10-min blocks in the dump)"
