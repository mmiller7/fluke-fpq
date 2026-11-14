#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial>=3.5"]
# ///
"""fpq_serial_replay.py — make a .fpq file act as the VR1710 recorder itself.

`serve` turns an existing .fpq session into the *device* side of the
Power Log Classic 4.60 download: it answers the recorder's ASCII command
set and, after `V` + `C`, streams the compact binary dump (the 6-byte item
stream of docs/serial.md) that the recorded values belong to. Power Log, or
any client speaking the protocol (fpq_serial_meter.py), token-streams
that back into the same half-cycle RMS values, the 10-min Pst/harmonics
block, and the host-computed THD/Plt — so a replayed session downloads with
byte-identical measurement data.

    fpq_serial_replay.py serve --fpq SESSION.fpq --port /dev/ttyUSB0
    fpq_serial_replay.py self-check SESSION.fpq        # offline round-trip

Idle-mode answers come from the .fpq where they are stored (thresholds) and
otherwise from [open]-meaning observed defaults, overridable with options.
The dump carries every G/F/K/H/L/5C value plus the stored events that are
really on the wire: the `04` dips, `10` swells and `15` transient captures.
The only things NOT on the wire are the absolute timestamps and the
gap-derived I/j records (Power Log computes those from the row timeline).
Absolute time
is anchored by Power Log to the PC clock at download (verified: a capture
made with the PC clock ~4 days ahead moved every timestamp by exactly the
same +349,195 s while all values and internal gaps stayed identical), so a
Power Log save of a replay is re-timestamped from the downloading PC's
clock. See docs/serial.md.

`self-check` rebuilds the token stream from a .fpq, decodes it back as Power
Log would and verifies every reconstructable value byte-for-byte:

    voltages   L1N / N-G = float32(code / 8)             [verified codec]
    frequency  stored    = float32(60 + code * B)        [verified codec]
    Pst        = PP / 256 from the `0b` header           [verified, byte-exact]
    harmonics  lane codes / 1024 from `0b`               [verified, byte-exact]
    THD        = sqrt(sum (code_j/1024)^2), 49 lanes     [verified, byte-exact]
    Plt        = (mean of the 12 preceding Pst^3)^(1/3)  [verified, 24/26 exact,
                                                           2 within 1 ULP]
    dips       = `04` items: row, code/8 + row 32 V window, dur/240 s
    swells     = `10` items: row, code/8 V, dur/240 s      [4/4 on br2]
    captures   = `15` items: 1,280 samples each            [43/43 exact]
    I/j        = host-derived from row gaps (never fabricated)

Not affiliated with or endorsed by Fluke Corporation. "Fluke" and "VR1710"
are trademarks of their owner.

Run with `python3 fpq_serial_replay.py ...` (needs pyserial; see requirements.txt).
"""

import argparse
import bisect
import math
import os
import re
import struct
import sys
import time
from datetime import datetime, timedelta

_SRC_COMMON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "src_common")
if os.path.isdir(_SRC_COMMON) and _SRC_COMMON not in sys.path:
    sys.path.insert(0, _SRC_COMMON)

from fpq_progress import Progress
from fpq_serial_meter import VR1710, escape, _HelpParser
from fpq_wire import (
    PAGE_PAYLOAD, PAGE_CRC, PAGE_STRIDE,
    TRANSIENT_FRAMES, TRANSIENT_PAYLOAD, TRANSIENT_STORED,
    crc16_xmodem,
    encode_transient,
    encode_transient_variable,
    decode_transient_payload as decode_capture,
    DIP_VOLT_OFFSET, dip_volt_offset, event_code, quarter_cycles,
)

# Item-stream constants (docs/serial.md; verified byte-exact)
FREQ_B = 1.1106768e-3  # Hz per count: stored = float32(60 + code * B)
TYPE_VOLTAGE = 0x12
TYPE_FREQ = 0x17
TYPE_BLOCK = 0x0B   # the 10-minute Pst/harmonics block
TYPE_COUNTER = 0x06
TYPE_HARM = 0x01    # byte tag of one `0b` harmonic group
TYPE_DIP = 0x04     # stored dip event (6 B)
TYPE_SWELL = 0x10   # stored swell event (6 B)
LANES = 49          # harmonic lanes per block (lane j = harmonic j+2)
LANES_PER_OCTAVE = 8
DUMP_PREFIX = b"\x02\x01\xfe"   # verified stream prefix [meaning open]

RECORD_SIZE = {  # .fpq record-stream type bytes -> record sizes
    0x44: 22, 0x46: 30, 0x47: 77, 0x48: 65, 0x49: 18, 0x4B: 22, 0x4C: 22,
    0x50: 22, 0x54: 5153, 0x5C: 1262, 0x6A: 18,
}
MARKER = b"678901234"  # end-of-header marker; record stream = marker_end + 43


def f32_bytes(x: float) -> bytes:
    """The little-endian byte representation of a float32."""
    return struct.pack("<f", x)


def ole(v: float) -> datetime:
    return datetime(1899, 12, 30) + timedelta(days=v)


# ---------------------------------------------------------------------------
# .fpq reading (self-contained; no imports from fpq_export)
# ---------------------------------------------------------------------------

class Fpq:
    """Everything the replay needs from one .fpq: config + record stream.

    Loading is gzip-transparent (a `.fpq.gz` is detected by its magic
    bytes), matching fpq_export.load_fpq.
    """

    def __init__(self, path: str):
        with open(path, "rb") as fh:
            d = fh.read()
        if d[:2] == b"\x1f\x8b":       # gzip magic: decompress
            import gzip
            d = gzip.decompress(d)
        if d[:16].rstrip(b"\x00")[:5] != b"Fiche":
            raise ValueError(f"{path}: not a .fpq (banner missing)")
        self.path = path
        head = d[: d.find(MARKER) if MARKER in d else 900].decode("latin-1")
        self.dip = self.pico = self.umbral = None
        m = re.search(r"Canal=\s*\d+\s+Dip=\s*(\d+)\s+Pico=\s*(\d+)"
                      r"\s+Umbral=\s*(\d+)", head)
        if m:
            self.dip, self.pico, self.umbral = (int(m.group(1)),
                                                int(m.group(2)),
                                                int(m.group(3)))
        # Nominal voltage/frequency. The D voltage offset is the event
        # row's 32 V window (96 V on 120 V, 224 V on the br1 244.5 V row);
        # the header VNom=230.00 is fixed metadata, not the input voltage.
        # The
        # frequency grid (60 + code*B) is verified only for 60 Hz. Loading
        # stays permissive (exporters/editors legitimately read other
        # sessions); build_stream() refuses a session the codec cannot
        # encode.
        self.vnom = self.fnom = None
        mn = re.search(r"VNom=\s*([0-9.]+).*?FNom=\s*([0-9.]+)", head)
        if mn:
            self.vnom, self.fnom = float(mn.group(1)), float(mn.group(2))
        self.interval = (struct.unpack_from("<d", d, 0x373)[0] * 86400
                         if len(d) >= 0x387 else None)
        self.n_records = (struct.unpack_from("<I", d, 0x383)[0]
                          if len(d) >= 0x387 else None)

        # strict record walk (runs clean on every sample file)
        base = d.find(MARKER) + len(MARKER) + 43
        pos = base
        self.G = []    # (ts_f64, l1n[min,max,avg], ng[min,max,avg]) f32
        self.F = []    # (ts_f64, [min,max,avg]) f32
        self.K = []    # (ts_f64, Pst f32)
        self.H = []    # (ts_f64, THD f32)
        self.L = []    # (ts_f64, Plt f32)
        self.C5 = []   # (ts_f64, [49 harmonic f32])
        self.T = []    # (ts_f64, [1280 int codes])
        self.D = []    # (ts_f64, duration s, volts) dip records
        self.P = []    # (ts_f64, duration s, volts) swell records
        self.I = []    # (ts_f64, duration s) interruptions (0x49)
        self.j = []    # (ts_f64, duration s) measurement gaps (0x6A)
        self.events = {"I": 0, "D": 0, "P": 0, "j": 0, "T": 0}
        while pos < len(d) and d[pos] != 0:
            t = d[pos]
            size = RECORD_SIZE.get(t)
            if size is None or pos + size > len(d):
                raise ValueError(f"{path}: bad record stream at {pos:#x} "
                                 f"(type {t:#x}); see fpq_export.py debug records")
            r = d[pos:pos + size]
            if t == 0x47:
                self.G.append((struct.unpack_from("<d", r, 1)[0],
                               struct.unpack_from("<3f", r, 18),
                               struct.unpack_from("<3f", r, 45)))
            elif t == 0x46:
                self.F.append((struct.unpack_from("<d", r, 1)[0],
                               struct.unpack_from("<3f", r, 18)))
            elif t == 0x4B:
                self.K.append((struct.unpack_from("<d", r, 1)[0],
                               struct.unpack_from("<f", r, 18)[0]))
            elif t == 0x48:
                self.H.append((struct.unpack_from("<d", r, 1)[0],
                               struct.unpack_from("<f", r, 26)[0]))
            elif t == 0x4C:
                self.L.append((struct.unpack_from("<d", r, 1)[0],
                               struct.unpack_from("<f", r, 18)[0]))
            elif t == 0x5C:
                self.C5.append((struct.unpack_from("<d", r, 1)[0],
                                [struct.unpack_from("<f", r, 71 + 24 * j + 8)[0]
                                 for j in range(LANES)]))
            elif t == 0x54:
                # stored T values are 10 x the meter's 12-bit codes; keep
                # codes so the wire encoder is the exact inverse
                self.T.append((struct.unpack_from("<d", r, 1)[0],
                               [int(v) // 10
                                for v in struct.unpack_from("<1280f", r, 26)]))
                self.events["T"] += 1
            elif t in (0x44, 0x50):
                # D/P record: 22 B, ts + duration days + f32 volts. These
                # are exactly what the 0x04/0x10 dump items carry, so the
                # replay emits items back at the record's row.
                rec = (struct.unpack_from("<d", r, 1)[0],
                       struct.unpack_from("<d", r, 9)[0] * 86400.0,
                       struct.unpack_from("<f", r, 17)[0])
                if t == 0x44:
                    self.D.append(rec)
                    self.events["D"] += 1
                else:
                    self.P.append(rec)
                    self.events["P"] += 1
            elif t in (0x49, 0x6A):
                # I/j: timestamp + duration (days). The replay needs the I
                # list to place the meter's 09/0A pause/resume markers at
                # the same gaps; without them Power Log classifies every
                # gap as `j` and deadlocks after the download (A5b).
                rec = (struct.unpack_from("<d", r, 1)[0],
                       struct.unpack_from("<d", r, 9)[0] * 86400.0)
                if t == 0x49:
                    self.I.append(rec)
                    self.events["I"] += 1
                else:
                    self.j.append(rec)
                    self.events["j"] += 1
            pos += size
        pad = len(d) - pos
        if pad and any(d[pos:]):
            raise ValueError(f"{path}: {pad} non-zero bytes after the last "
                             "record")
        # chronological order for replay/self-check; stable for equal times
        self.D.sort(key=lambda r: r[0])
        self.P.sort(key=lambda r: r[0])

    def describe(self) -> str:
        n = len(self.G)
        span = ("" )
        if self.G:
            t0, t1 = ole(self.G[0][0]), ole(self.G[-1][0])
            span = f", {t0:%Y-%m-%d %H:%M:%S} … {t1:%Y-%m-%d %H:%M:%S}"
        ev = "".join(f"{k}" for k, v in self.events.items() if v)
        return (f"{self.path}: {n} G rows, {len(self.K)} 10-min blocks, "
                f"{len(self.L)} Plt, interval {self.interval:g} s{span}"
                + (f", events: {ev}" if ev else ", events: none"))


# ---------------------------------------------------------------------------
# encoding: .fpq values -> wire codes (inverse of the verified decode)
# ---------------------------------------------------------------------------

def volt_code(v: float) -> int:
    """V RMS -> 12-bit code on the verified 1/8 V grid."""
    c = round(v * 8)
    if f32_bytes(c / 8.0) != f32_bytes(v):
        for dc in (-2, -1, 1, 2):
            if f32_bytes((c + dc) / 8.0) == f32_bytes(v):
                return c + dc
        raise ValueError(f"voltage {v!r} is not on the 1/8 V grid")
    return c

def freq_code(hz: float) -> int:
    """Hz -> 12-bit signed code on the verified 60 + code * B grid."""
    c = round((hz - 60.0) / FREQ_B)
    if c < -2048 or c > 2047:
        raise ValueError(f"frequency {hz!r} outside the 12-bit range")
    for dc in (-2, -1, 0, 1, 2):
        if f32_bytes(60.0 + (c + dc) * FREQ_B) == f32_bytes(hz):
            return c + dc
    raise ValueError(f"frequency {hz!r} does not round-trip")


def encode_item(t: int, group: int, mx: int, avg: int, mn: int) -> bytes:
    """One 6-byte item: (v1,v2,v3) = (max, avg, min) as signed 12-bit
    two's-complement; `group` goes to the high nibble of byte 1."""
    v1, v2, v3 = mx & 0xFFF, avg & 0xFFF, mn & 0xFFF
    return bytes((t,
                  (group << 4) | (v1 >> 8),
                  v1 & 0xFF,
                  v2 >> 4,
                  ((v2 & 0x0F) << 4) | (v3 >> 8),
                  v3 & 0xFF))


def encode_thd_header(pst: float) -> bytes:
    """Header of the 10-min block: `0b <u16 PP BE> 00 00 00`.

    PP = round(Pst*256). The 2026-10-28 variac capture has six blocks with
    Pst > 1 (PP 267..297), stored as `0b 01 29` etc. — so the field is a
    big-endian u16, not a flags byte + one PP byte. Blocks with Pst < 1
    (PP < 256) are byte-identical to the older `0b 00 PP 00 00 00` reading.
    """
    pp = round(pst * 256)
    if not 0 <= pp <= 0xFFFF:
        raise ValueError(f"Pst {pst!r} outside the u16 PP range")
    return bytes((TYPE_BLOCK, (pp >> 8) & 0xFF, pp & 0xFF, 0, 0, 0))


def harm_code(v: float) -> int:
    """5C lane value -> the `0b` byte (code = round(v*1024) = h*100)."""
    c = round(v * 1024)
    if c % 100:
        raise ValueError(f"harmonic {v!r} is not on the code*100 grid")
    h = c // 100
    if not 0 <= h <= 255:
        raise ValueError(f"harmonic code {h} out of byte range")
    return h


def encode_dip(lo: int, volts: float, dur_s: float,
               offset: float | None = None) -> bytes:
    """One stored dip: `04 <u16 lo LE> <u8 code> <u16 dur BE>`.

    The 32 V window is bits 8-11 of `lo`; the 12-bit event code is
    ``(window << 8) | code`` and the stored voltage is
    ``code/8 + 32*window``.  The window is derived from the `.fpq` voltage
    (not the event row's minimum), so an event inside an outage (67_) or a
    deep/high-range event still encodes.  Bits 0-7 and 12-15 remain the
    1/4 s counter.  `offset` is accepted for backward compatibility and
    ignored.
    """
    if not 0 <= lo <= 0xFFFF:
        raise ValueError(f"dip lo {lo!r} outside u16")
    window = int(volts // 32.0)
    if window > 7:
        raise ValueError(f"dip voltage {volts!r} outside the 32 V windows")
    code = event_code(volts, 32.0 * window, 0xFF, "dip voltage")
    lo = (lo & 0xF0FF) | ((window & 0xF) << 8)
    dur = quarter_cycles(dur_s)
    return bytes((TYPE_DIP, lo & 0xFF, (lo >> 8) & 0xFF, code,
                  (dur >> 8) & 0xFF, dur & 0xFF))


def encode_swell(flags: int, volts: float, dur_s: float) -> bytes:
    """One stored swell: `10 <u8 flags> <u16 code BE> <u16 dur BE>`.

    voltage = (code & 0xFFF)/8; duration = dur/240 s. Verified against all
    4 official P records of the 2026-10-28 variac capture. The flags byte's
    meaning is [open] (the four observed items share no set bit), so the
    replay writes 0.
    """
    if not 0 <= flags <= 0xFF:
        raise ValueError(f"swell flags {flags!r} outside u8")
    code = event_code(volts, 0.0, 0xFFF, "swell voltage")
    dur = quarter_cycles(dur_s)
    return bytes((TYPE_SWELL, flags, (code >> 8) & 0xFF, code & 0xFF,
                  (dur >> 8) & 0xFF, dur & 0xFF))


def encode_thd_block(pst: float, lanes) -> bytes:
    """Complete 10-min block: header + one 6-byte harmonic group per octave
    that has a non-zero lane, exactly as the observed meter emits them.
    Lane map [verified]: even II -> the octave's odd lanes, odd II -> the
    octave's even lanes; lanes base, base+2, base+4, base+6."""
    hs = [0] * LANES
    for j, v in enumerate(lanes):
        hs[j] = harm_code(v)
    out = bytearray(encode_thd_header(pst))
    for k in range((LANES + LANES_PER_OCTAVE - 1) // LANES_PER_OCTAVE):
        odd = [hs[i] for i in range(8 * k + 1, 8 * k + 8, 2)
               if i < LANES]
        even = [hs[i] for i in range(8 * k, 8 * k + 7, 2) if i < LANES]
        if any(odd):
            out += bytes((TYPE_HARM, 2 * k, *odd))
        if any(even):
            out += bytes((TYPE_HARM, 2 * k + 1, *even))
    return bytes(out)


# ---------------------------------------------------------------------------
# token stream: what the meter sends after `V` + `C`
# ---------------------------------------------------------------------------

def tick_g_index(fpq: Fpq, kts: float) -> int:
    """Index of the G record whose OLE timestamp equals a 10-min tick
    (verified exact: K/H/5C share the timestamp of their tick's G record).

    The nearest preceding G row is used when no row matches exactly.
    """
    g_ts = [row[0] for row in fpq.G]
    i = bisect.bisect_left(g_ts, kts)
    if i < len(g_ts) and g_ts[i] == kts:
        return i
    if i == 0:
        raise ValueError("no G record at or before a 10-min tick")
    return i - 1


def marker_payload(item_type: int, payload: int) -> bytes:
    """`<type> 02 <BE32>` for the counter family (06, 09, 0A)."""
    return bytes((item_type, 2, (payload >> 24) & 0xFF,
                  (payload >> 16) & 0xFF, (payload >> 8) & 0xFF,
                  payload & 0xFF))


def counter_payload(payload: int) -> bytes:
    """`06 02 <BE32>` built from the raw payload value."""
    return marker_payload(TYPE_COUNTER, payload)


def counter_item(n: int) -> bytes:
    """`06 02 <BE32>`: observed payload is always a multiple of 128."""
    return counter_payload(n << 7)


# Page and transient constants come from fpq_wire (shared with the meter).
# Power Log sizes the dump from `V`/`v`; the observed relation on the two
# matched captures is ~18.63 dump bytes per 5 s/2 s row (a whole page is
# 1,029 B, so this is only used to answer the `v` query).
EST_BYTES_PER_ROW = 18.63


# ---------------------------------------------------------------------------
# counter clock: the meter's `06` value, exactly as fpq_serial_meter.py
# decodes it (see that module's `_counter_clock`).  The value is in 1/128 s
# units but is NOT a plain clock: the meter adds fixed bookkeeping steps
# that depend on elapsed recording time.
#
#   counter/128 = t + 4*floor((t-p)/60) + 256*floor((t-ph)/3600)
#                 + 32768*floor((t-pd)/86400) + C
#
# `p`/`ph` place the minute/hour steps relative to the first counter item.
# They are elapsed-zero here (the decoder's calibration recovers that pair
# exactly; see counter_phases).  `pd` places the daily step at 17:43:15
# local (the observed meter phase).  The replay emits this model verbatim,
# so the production decoder inverts it to the source session's own timeline.
# (The old flat 135.3 units/s ramp was fitted to one 5 s capture; it made
# every replayed timeline ~5.7 % fast and is retired.)
METER_COUNTER_START = 0x6E8E5900   # the meter's own start value, raw payload
COUNTER_BASE = METER_COUNTER_START >> 7   # its /128 form (14,490,802)
COUNTER_SCALE = 128
from fpq_wire import (counter_clock,  # noqa: E402  (shared with the decoder)
                     COUNTER_STEP_MINUTE, COUNTER_STEP_HOUR, COUNTER_STEP_DAY)
COUNTER_DAY_TOD = 17 * 3600 + 43 * 60 + 15   # local time-of-day of the step
# Kept for callers that still pass the old name; the canonical clock is
# 128 units/s plus the steps above, so this is no longer a tunable rate.
COUNTER_UNITS_PER_SECOND = 128.0


def counter_phases(start_ole: float) -> tuple[float, float, float]:
    """(p, ph, pd) for a session whose row 0 is at OLE `start_ole`.

    The minute/hour phases are deliberately elapsed-zero: the counter items
    only expose the step *offsets*, not the meter's absolute wall phase, and
    the production calibration (`_calibrate_counter`) is biased towards low
    phases.  A wall-derived phase (e.g. 50/3530) leaves the exact synthetic
    chain ambiguous: several wrong (p, ph) pairs fit the regular 14-row
    cadence equally well, and `parse_dump` then returns gap=None (RT drift of
    thousands of seconds).  With p=ph=0 the decoder recovers (0, 0)
    exactly, which is the one phase pair this replay can guarantee.

    NOTE (A5b): this synthetic clock is valid for the tool's own decoder but
    Power Log Classic's post-download display decoder rejects it (0 % CPU
    deadlock after "Drawing Graphs...").  `serve` therefore leaves the
    timeline items out by default; see the `--counters` help.

    The daily phase stays wall-derived: the production writer (`build_fpq`)
    computes its own `pd` from the anchor and *always* applies the day term,
    so the replay must put the +32768 step at the same wall time-of-day
    (17:43:15).  `pd` is relative to the first counter item (row 0).
    """
    a = round(start_ole * 86400.0)     # whole local seconds since OLE epoch
    p = 0.0
    ph = 0.0
    pd = float((COUNTER_DAY_TOD - a % 86400) % 86400)
    return p, ph, pd



def _require_replay_nominal(fpq: Fpq) -> None:
    """Refuse a session the verified replay codecs cannot encode."""
    if fpq.vnom is None or fpq.fnom is None:
        return
    if (abs(fpq.vnom - 120.0) > 0.01
            and abs(fpq.vnom - 230.0) > 0.01):
        raise ValueError(
            f"{fpq.path}: session declares VNom={fpq.vnom:g}; the replay's "
            "D voltage offset is selected from the event row's 32 V "
            "window; VNom is fixed metadata (see docs/serial.md)")
    if abs(fpq.fnom - 60.0) > 0.01:
        raise ValueError(
            f"{fpq.path}: session declares FNom={fpq.fnom:g}; the replay's "
            "frequency grid is verified only for 60 Hz recordings")


def build_stream(fpq: Fpq, lead: int = COUNTER_BASE,
                 counter_period_s: float | None = None,
                 counter_rows: int | None = 14,
                 paged: bool = False, pad_seconds: float | None = None,
                 counter_rate: float | None = None,
                 counter_track: (list[tuple[int, int]]
                                 | list[tuple[int, int, int]] | None) = None,
                 pad_kind: str = "09a06",
                 counter_steps: bool = False,
                 day_step: bool = True,
                 transients: bool = True,
                 transient_ticks: list[int] | None = None) -> bytes:
    """The meter-side dump.

    flat: prefix, a leading counter item (observed), one (V, NG, F)
    triple per G/F row with the 10-min `0b` block riding its tick's row,
    the stored `04` dip / `10` swell items at their own rows, the `15`
    transient captures, and `06` counter items roughly every 14 rows.
    Every source I record is also emitted as its own `09`+`0A` pair, even
    when the record creates no row gap (a zero-duration interruption):
    the writer emits one I per pair, so omitting those pairs loses I
    records on a rebuild.  `counter_track` accepts the native
    `(row, value)` pairs or `(row, type, value)` triples from
    `fpq_dump.parse_dump`'s `counter_items`; a triple track reproduces the
    native counter positions and the 09/0A markers exactly.
    Counter values follow the meter's own step model (counter_clock): the
    elapsed time comes from the .fpq's record timestamps and the phases
    from the session start (counter_phases).  That is exactly what
    fpq_serial_meter decodes, so a rebuilt .fpq keeps the source session's
    row times; real gaps and irregular ticks are preserved, which is what
    the host uses to rebuild event/pause structure. `pad_seconds` appends
    one final counter jump representing "no data" since the recording
    ended, so the replayed timeline can be walked back from the host's
    clock.
    paged: the meter's REAL layout [fact, verified on all 686 pages of
    the 2026-10-24 dump]: the DUMP_PREFIX `02 01 fe` acts as the k=1
    footer, then each page is 1,024 payload bytes + the CRC-16/XMODEM of
    those bytes (big-endian), followed by the next `02 k ~k` footer
    (k +1 cadence). Items may SPLIT across page boundaries — that is
    exactly what the live dump does (212+ split joins).

    The canonical clock includes the once-a-day +32768 step at 17:43:15
    local.  It is emitted by default (`day_step=True`): the production
    writer always re-solves its counter timeline with the day term once an
    anchor is supplied, so a replay spanning 17:43:15 must encode the step
    or the rebuilt timeline cannot be inverted.  `--no-counter-day-step`
    keeps the old non-day stream for comparison only.
    """
    _require_replay_nominal(fpq)
    if counter_rate is not None and counter_rate != COUNTER_UNITS_PER_SECOND:
        print(f"note: counter_rate={counter_rate:g} ignored — counters now "
              "follow the meter's step model (128 units/s plus minute/hour/"
              "day steps); --counter-rate is deprecated", file=sys.stderr)
    for i, (t, _v) in enumerate(fpq.K):
        if t != fpq.H[i][0] or t != fpq.C5[i][0]:
            raise ValueError(f"tick {i}: K/H/5C timestamps differ; the "
                             "0b block needs them equal")
    freq_base = freq_base_of(fpq)      # rejects 50 Hz cleanly [fact]
    # pair F rows to G rows by timestamp: real files can carry a G row
    # whose F record differs or is absent (observed: home 3-dec-2025,
    # G 116,647 vs F 116,646 rows, divergence at row 116,627). F rows
    # that pair no G row are skipped; a G row with no F row emits V/N
    # only — cadence break is counted, not fabricated [fact].
    track = None
    if counter_track:
        first_item = counter_track[0]
        if len(first_item) == 3:
            # (row, item type, raw payload): native counter_items
            entries = [(int(r), int(t), int(v)) for r, t, v in counter_track]
        elif len(first_item) == 2:
            # legacy (row, raw payload): a plain `06`
            entries = [(int(r), TYPE_COUNTER, int(v))
                       for r, v in counter_track]
        else:
            raise ValueError("counter_track items must be (row, value) or "
                             "(row, type, value)")
        track = {}
        for r, typ, val in entries:
            track.setdefault(r, []).append((typ, val))
        if 0 in track:
            lead = track[0][0][1] >> 7   # the leading item IS the row-0 counter
    it = iter(fpq.F)
    cur = next(it, None)
    def this_freq_code(hz: float) -> int:
        """freq_code against this session's base; not-exact rows quantize
        to the closest code (the decoded value then differs from the .fpq
        float by <B/2 — cadence is never broken)."""
        c = round((hz - freq_base) / FREQ_B)
        if c < -2048 or c > 2047:
            raise ValueError(f"frequency {hz!r} outside the 12-bit range")
        for dc in (-2, -1, 0, 1, 2):
            if f32_bytes(freq_base + (c + dc) * FREQ_B) == f32_bytes(hz):
                return c + dc
        return c
    # tick_i rides the G row whose timestamp it shares
    g_ts = [row[0] for row in fpq.G]

    def g_index(ts: float) -> int:
        i = bisect.bisect_left(g_ts, ts)
        if i < len(g_ts) and g_ts[i] == ts:
            return i
        if i == 0:
            raise ValueError("no G record at or before a 10-min tick")
        return i - 1

    tick_of_row = {g_index(t): i for i, (t, _v) in enumerate(fpq.K)}
    # A counter only anchors the row it is charged to (the item after row i
    # is row i+1 in the decoder).  When the source has a row gap, emit that
    # anchor immediately before the gap so `build_fpq` places the pause at
    # the real resume row instead of at the next periodic counter, which can
    # be up to 13 rows later.  This makes the rebuilt row timeline exact.
    gap_anchor_rows = set()
    i_gap_by_prev: dict[int, tuple[int, float, float]] = {}
    covered_i: set[int] = set()
    for i in range(1, len(fpq.G)):
        dt = (fpq.G[i][0] - fpq.G[i - 1][0]) * 86400.0
        # Any row gap above the interval needs a 06 anchor at the resume
        # row, not only gaps wider than 1.5 s: a 1 s pause (2 s row delta)
        # otherwise lets the next periodic 06 swallow the jump and moves
        # the I/j one row late.  The 2026-11-08 O6 check found this on 65_:
        # source I at row 114097 (dur 1) came back as an I plus a spurious
        # 1 s j.  The real meter emits 09,0A,06 together at the resume row.
        if dt > fpq.interval + 0.01:
            gap_anchor_rows.add(i - 1)
            # An interruption (I record) is a 09/0A pause/resume pair on
            # the wire: 09 at the last measured row, 0A one I-duration
            # later, then a 06 at the resuming row.  A gap with no I record
            # is a bare counter jump (j).  This keeps the tool-side I/j
            # semantics exact when counters are enabled; Power Log's
            # post-download display still rejects the synthetic counter
            # timeline itself (A5b), so `serve` keeps it off by default.
            lo, hi = fpq.G[i - 1][0], fpq.G[i][0]
            for k, (ets, edur) in enumerate(fpq.I):
                if lo - 1e-9 <= ets < hi:
                    i_gap_by_prev[i - 1] = (
                        i, edur, (ets - fpq.G[0][0]) * 86400.0)
                    covered_i.add(k)
                    break
    # The writer writes one I record per 09+0A pair (`fpq_gap.i_pairs`),
    # including pairs on rows that have no row gap: a zero-duration
    # interruption, or several I records inside one row.  Those are real
    # native items (61_: 3 of 5 I; 65_: 3 of 8 I) and are otherwise lost
    # when the source .fpq is rebuilt from the replayed stream.  Emit the
    # pair at the interruption's row with the counter clock at its own
    # start/end, so the pair survives the decoder's sequential pairing.
    extra_i_by_row: dict[int, list[tuple[float, float]]] = {}
    # Stored D/P events: the item sits between the preceding row's F item
    # and the next row's V item, so its stream row is the first G row whose
    # timestamp is >= the event time (verified 36/36 D and 4/4 P against the
    # 2026-10-28 memory dump of the same session). Power Log stamps the
    # record from that position (and from the item's sub-row counter).

    # Counter clock phases for this session; the decoder's t=0 is the
    # leading counter item (row 0), so elapsed seconds are measured there.
    ctr_p, ctr_ph, ctr_pd = (counter_phases(fpq.G[0][0]) if fpq.G
                             else (0.0, 0.0, 0.0))
    if not day_step:
        ctr_pd = None

    def counter_value(elapsed: float) -> int:
        """`06` counter in /128 units at `elapsed` s (canonical step model).

        Power Log timestamps are whole seconds, but an OLE double converts
        back to e.g. 349.999999 s.  The step clock is discontinuous at a
        minute/hour boundary, so evaluating it just below 350 s would omit
        the +4 step that the decoder (which works in whole seconds) must
        see.  Snap to the whole second first; the self-check's 1 s row
        tolerance covers any source file with a genuinely fractional time.
        """
        return lead + round(counter_clock(round(elapsed), ctr_p, ctr_ph,
                                         ctr_pd))

    def event_row(ets: float) -> int:
        return bisect.bisect_left(g_ts, ets)

    if fpq.G:
        for k, (ets, edur) in enumerate(fpq.I):
            if k in covered_i:
                continue
            row = min(max(0, event_row(ets)), len(fpq.G) - 1)
            extra_i_by_row.setdefault(row, []).append((ets, edur))
    if track is not None and any(
            typ in (0x09, 0x0A)
            for items in track.values() for typ, _val in items):
        # A native track already carries every 09/0A pair; adding the
        # synthetic ones would duplicate the I records.
        extra_i_by_row = {}
    # Row -> chronological (start elapsed, duration) pairs.  The row-gap I
    # pairs and the no-gap I pairs share one ordered list so several I
    # records on the same row are emitted in time order, and a pair never
    # moves the counter backwards.
    i_pairs_by_row: dict[int, list[tuple[float, float]]] = {}
    counter_mode = (track is not None or counter_rows is not None
                    or counter_period_s is not None)
    if counter_mode and (track is None or not any(
            typ in (0x09, 0x0A)
            for items in track.values() for typ, _val in items)):
        for _prev_row, (resume_row, idur, secs) in i_gap_by_prev.items():
            i_pairs_by_row.setdefault(resume_row, []).append((secs, idur))
        for row, pairs in extra_i_by_row.items():
            i_pairs_by_row.setdefault(row, []).extend(
                ((ets - fpq.G[0][0]) * 86400.0, edur)
                for ets, edur in pairs)
        for pairs in i_pairs_by_row.values():
            pairs.sort()

    ev_by_row: dict[int, list[tuple]] = {}
    for kind, recs in (("D", fpq.D), ("P", fpq.P)):
        for ets, edur, evolts in recs:
            ev_by_row.setdefault(event_row(ets), []).append(
                (ets, kind, edur, evolts))
    for _evs in ev_by_row.values():
        _evs.sort(key=lambda e: e[0])

    def emit_events(row: int) -> None:
        """Append this row's stored events in chronological order."""
        prev_ts = (fpq.G[row - 1][0] if 0 < row <= len(fpq.G)
                   else (fpq.G[0][0] if fpq.G else 0.0))
        for ets, kind, edur, evolts in ev_by_row.get(row, ()):
            if kind == "D":
                # `lo` is a 1/4 s counter. The verified multi-dip burst
                # steps it by 4 per second and anchors the group at the
                # preceding row, so encode the .fpq event's own offset.
                off = max(0, min(0xFFFF // 4,
                                 int(round((ets - prev_ts) * 86400.0))))
                flat.extend(encode_dip(1 + 4 * off, evolts, edur))
            else:
                flat.extend(encode_swell(0, evolts, edur))

    first = (track[0][0][1] if (track and 0 in track)
             else counter_value(0.0))
    if track and 0 in track:
        # Preserve the native row-0 item exactly (observed as a `06`).
        typ0, val0 = track[0][0]
        flat = bytearray(DUMP_PREFIX + marker_payload(typ0, val0))
        last_ctr_payload = val0
    else:
        flat = bytearray(DUMP_PREFIX + counter_item(first))
        last_ctr_payload = (first << 7) & 0xFFFFFFFF
    for secs, edur in i_pairs_by_row.get(0, ()):
        span = (counter_value(secs + edur) - counter_value(secs)) << 7
        v09 = (counter_value(secs) << 7) & 0xFFFFFFFF
        if v09 < last_ctr_payload:
            if last_ctr_payload - v09 > (fpq.interval or 1.0) * 128:
                continue
            v09 = last_ctr_payload
        flat += marker_payload(0x09, v09)
        flat += marker_payload(0x0A, v09 + span)
        last_ctr_payload = v09 + span
    last_counter_ts = fpq.G[0][0] if fpq.G else None
    next_tick = 0
    no_f_rows = 0

    def emit_transient(t_ts: float, codes, idx: int) -> None:
        """Append one stored capture at its chronological stream position."""
        secs = (t_ts - fpq.G[0][0]) * 86400.0 if fpq.G else 0.0
        # With a native counter track, use the raw capture's own tick: the
        # synthetic step clock can differ by a daily step (32,768 s) and
        # Power Log then hangs even though the samples round-trip.  Match by
        # capture order; the raw row can be one off the .fpq T row.
        if transient_ticks and idx < len(transient_ticks):
            ticks = transient_ticks[idx] & 0xFFFFFFFF
        else:
            ticks = (counter_value(secs) << 7) & 0xFFFFFFFF
        # A `15 00` capture keeps only 3k real samples (zero-padded to the
        # 1,280 slot); a `15 02` capture fills the slot.  The stored trailing
        # zeros therefore select the native framing (67_ 2026-11-03).
        last = max((i for i, c in enumerate(codes) if c), default=-1)
        if (0 <= last < TRANSIENT_STORED - 1 and (last + 1) % 3 == 0
                and last // 3 + 1 >= 8):
            flat.extend(encode_transient_variable(codes, ticks))
        else:
            flat.extend(encode_transient(codes, ticks))

    t_idx = 0
    for i in range(len(fpq.G)):
        ts = fpq.G[i][0]
        # Tracked native counter markers sit before the row's own items, so
        # the decoder charges them to this row (the position parse_dump
        # reports).  Preserve the item type (06 / 09 / pause-resume).
        if track is not None and i > 0 and i in track:
            for typ, val in track[i]:
                flat += marker_payload(typ, val)
                last_ctr_payload = val
        emit_events(i)
        if transients:
            while t_idx < len(fpq.T) and fpq.T[t_idx][0] <= ts:
                emit_transient(fpq.T[t_idx][0], fpq.T[t_idx][1], t_idx)
                t_idx += 1
        while cur is not None and cur[0] < ts:      # F row without a G row
            cur = next(it, None)
        has_f = cur is not None and abs(cur[0] - ts) < 1e-5
        if has_f:
            fr = cur[1]
            cur = next(it, None)
        else:
            fr = None
            no_f_rows += 1
        l1n, ng = fpq.G[i][1], fpq.G[i][2]
        flat += encode_item(TYPE_VOLTAGE, 0, *mirror(*l1n, volt_code))
        flat += encode_item(TYPE_VOLTAGE, 1, *mirror(*ng, volt_code))
        if fr is not None:
            # F rows that are not exact code-grid floats (boot-gap values
            # like 60.89298629760742) are emitted on the CLOSEST code —
            # sub-ULP error, cadence preserved
            flat += encode_item(TYPE_FREQ, 1, *mirror(*fr, this_freq_code))
        t = tick_of_row.get(i)
        if t is not None and t == next_tick:
            flat += encode_thd_block(fpq.K[next_tick][1],
                                     fpq.C5[next_tick][1])
            next_tick += 1
        # Every source I record becomes one 09+0A pair on the wire (the
        # writer emits one I per pair, including pairs on rows with no row
        # gap).  A pair belongs to the row it resumes at, so it is emitted
        # at the END of the preceding row: that is after the previous
        # counter and before the counter carrying this row's clock, so the
        # pair stays in counter order.  Overlapping source I records (a
        # start that precedes the previous pair's end) cannot be placed on
        # a monotone counter track; a small rounding difference is clamped,
        # a real overlap is skipped rather than corrupting the timeline.
        for secs, edur in i_pairs_by_row.get(i + 1, ()):
            span = ((counter_value(secs + edur) - counter_value(secs)) << 7)
            v09 = (counter_value(secs) << 7) & 0xFFFFFFFF
            if v09 < last_ctr_payload:
                if last_ctr_payload - v09 > (fpq.interval or 1.0) * 128:
                    continue
                v09 = last_ctr_payload
            v0a = v09 + span
            flat += marker_payload(0x09, v09)
            flat += marker_payload(0x0A, v0a)
            last_ctr_payload = v0a
        if track is None:
            # Row-based cadence: the meter emits a 06 about every 14-15
            # rows at BOTH 5 s and 2 s (measured 2026-10-27 on the 2 s
            # overnight session: 300 rows per 10-min block and counters
            # ~15 rows apart, i.e. ~30 s, not 70 s).  The item sits after
            # row i, and the decoder charges it to row i+1, so its value is
            # the clock at the NEXT row (the last row's own time at the end).
            # A gap anchor is emitted just before a paused row for the same
            # reason: it is the only way the anchor-free decoder can tell
            # where inside a 14-row block the source timeline jumped.
            emit_row = ((counter_rows is not None or counter_period_s)
                        and i in gap_anchor_rows)
            if counter_rows and (i + 1) % counter_rows == 0:
                emit_row = True
            if (counter_period_s and last_counter_ts is not None
                    and (ts - last_counter_ts) * 86400.0 >= counter_period_s):
                emit_row = True
            if emit_row:
                secs = ((fpq.G[i + 1][0] if i + 1 < len(fpq.G) else ts)
                        - fpq.G[0][0]) * 86400.0
                v06 = (counter_value(secs) << 7) & 0xFFFFFFFF
                flat += counter_item(counter_value(secs))
                last_ctr_payload = v06
                last_counter_ts = ts
    if transients:
        while t_idx < len(fpq.T):
            emit_transient(fpq.T[t_idx][0], fpq.T[t_idx][1], t_idx)
            t_idx += 1
    emit_events(len(fpq.G))      # events after the newest row
    if next_tick != len(tick_of_row):
        raise ValueError("some 10-min ticks found no G row to ride on")
    if pad_seconds and fpq.G:
        span = (fpq.G[-1][0] - fpq.G[0][0]) * 86400.0
        last_n = counter_value(span)
        adv_n = counter_value(span + pad_seconds)
        # one resume tick's worth of units at the padded reading
        # (observed +640/+768 after 0A on 5 s sessions)
        tick = round(counter_clock(span + pad_seconds + fpq.interval, ctr_p,
                                   ctr_ph, ctr_pd)
                     - counter_clock(span + pad_seconds, ctr_p, ctr_ph,
                                     ctr_pd))
        if pad_kind == "06":
            flat += counter_item(adv_n)
        elif pad_kind == "0a":
            flat += marker_payload(0x0A, adv_n << 7)
        elif pad_kind == "0a06":
            flat += marker_payload(0x0A, adv_n << 7)
            flat += counter_item(adv_n + tick)
        elif pad_kind == "09a":
            flat += marker_payload(0x09, last_n << 7)
            flat += marker_payload(0x0A, adv_n << 7)
        elif pad_kind == "09a06":
            flat += marker_payload(0x09, last_n << 7)
            flat += marker_payload(0x0A, adv_n << 7)
            flat += counter_item(adv_n + tick)
        elif pad_kind == "09":
            flat += marker_payload(0x09, adv_n << 7)
        elif pad_kind == "0609":
            flat += counter_item(adv_n)
            flat += marker_payload(0x09, adv_n << 7)
        elif pad_kind == "09a09":
            flat += marker_payload(0x09, last_n << 7)
            flat += marker_payload(0x0A, adv_n << 7)
            flat += marker_payload(0x09, adv_n << 7)
        else:
            raise ValueError(f"unknown pad kind {pad_kind!r}")
    if no_f_rows:
        print(f"note: {no_f_rows} G row(s) have no F record in the source "
              ".fpq — emitted V/N without F (wire has no fabrication)")
    if not paged:
        return bytes(flat)
    # Page the flat stream exactly like the meter does [fact: every page of
    # the 2026-10-24 meter2 dump, 686/686 CRC-verified]: footer `02 k ~k`,
    # then exactly 1,024 item-stream bytes, then CRC-16/XMODEM of those
    # bytes stored big-endian. Items SPLIT across page boundaries — the
    # payload is just a cut through the continuous item track, not an
    # item-aligned unit. The final page is padded to a full payload with
    # zeros (the meter's memory is page-aligned, so it never sends a short
    # page) and its CRC covers the padded payload.
    body = flat[3:]
    out = bytearray(DUMP_PREFIX)       # footer k=1
    pos, k = 0, 2
    while pos < len(body):
        chunk = bytes(body[pos:pos + PAGE_PAYLOAD])
        pos += len(chunk)
        if len(chunk) < PAGE_PAYLOAD:  # meter always ends on a full page
            chunk += b"\x00" * (PAGE_PAYLOAD - len(chunk))
        out += chunk
        out += struct.pack(">H", crc16_xmodem(chunk))
        if pos < len(body):
            out += bytes((0x02, k & 0xFF, (~k) & 0xFF))
            k += 1
    return bytes(out)


def mirror(mn: float, mx: float, avg: float, code) -> tuple[int, int, int]:
    """A .fpq (min, max, avg) triple -> wire (max, avg, min) codes."""
    return code(mx), code(avg), code(mn)


def freq_base_of(fpq: Fpq) -> float:
    """The frequency codec's base (60 + code*B is verified for 60 Hz
    sessions only [verified codec on the 60 Hz kits]).

    A meter on a 50 Hz grid stores ~50 Hz in the F rows, which is
    IMPOSSIBLE with the 60+B codec, and the 50 Hz wire codec value is
    [open] (one sample: 49.99629592895508 f32 = 50 - 3.3 x B, not an
    integral code on B). Detections here amount to rejecting 50 Hz
    sessions instead of producing wrong bytes — or hanging a host.
    """
    if not fpq.F:
        return 60.0
    mids = sorted(row[1][1] for row in fpq.F)
    mid = mids[len(mids) // 2]
    if 55 <= mid <= 65:
        return 60.0
    if 45 <= mid < 55:
        raise ValueError(
            f"{fpq.path}: 50 Hz session ({mid:.2f} Hz) — the wire frequency "
            "codec is only verified for 60 Hz; replay of 50 Hz recordings "
            "is [open] (needs its own codec derivation)")
    raise ValueError(f"{fpq.path}: frequency median {mid} Hz outside any "
                     "known grid")


# ---------------------------------------------------------------------------
# self-check: decode the built stream the way Power Log does
# ---------------------------------------------------------------------------

class Stream:
    """Strict walk of a (clean, self-built) dump stream."""

    def __init__(self, data: bytes):
        if data[:3] != DUMP_PREFIX:
            raise ValueError("stream must start with the dump prefix")
        self.V = []         # (max, avg, min) L1N codes
        self.N = []         # same, N-G
        self.F = []         # same, frequency
        self.blocks = []    # (PP, [49 lane bytes])
        self.counters = []  # (row, value/128) from the `06` payloads
        self.marks = []     # (row, 0x09/0x0A, value/128) pause/resume
        self.T = []         # (ticks, 1280 codes) from 0x15 captures
        self.D = []         # dicts: row/lo/code/dur from 0x04 items
        self.P = []         # dicts: row/flags/code/dur from 0x10 items
        self.types = set()  # every item type seen (I/j must be absent)
        pos = 3
        while pos < len(data):
            t = data[pos]
            if len(data) - pos < 6:
                raise ValueError(f"truncated item at {pos:#x}")
            self.types.add(t)
            if t in (TYPE_COUNTER, 0x09, 0x0A):
                payload = struct.unpack(">I", data[pos + 2:pos + 6])[0]
                if t == TYPE_COUNTER:
                    self.counters.append((len(self.V), payload >> 7))
                else:
                    self.marks.append((len(self.V), t, payload >> 7))
                pos += 6
            elif t == TYPE_DIP:
                self.D.append({
                    "row": len(self.V),
                    "lo": data[pos + 1] | (data[pos + 2] << 8),
                    "code": data[pos + 3],
                    "dur": (data[pos + 4] << 8) | data[pos + 5],
                })
                pos += 6
            elif t == TYPE_SWELL:
                self.P.append({
                    "row": len(self.V),
                    "flags": data[pos + 1],
                    "code": ((data[pos + 2] << 8) | data[pos + 3]) & 0xFFF,
                    "dur": (data[pos + 4] << 8) | data[pos + 5],
                })
                pos += 6
            elif t == 0x15:
                if data[pos + 1] == 0x02:
                    if len(data) - pos < 6 + TRANSIENT_PAYLOAD:
                        raise ValueError(f"truncated capture at {pos:#x}")
                    self.T.append((
                        struct.unpack(">I", data[pos + 2:pos + 6])[0],
                        decode_capture(
                            data[pos + 6:pos + 6 + TRANSIENT_PAYLOAD])))
                    pos += 6 + TRANSIENT_PAYLOAD
                elif data[pos + 1] == 0x00:
                    # Variable `15 00` form (67_): k six-byte frames,
                    # terminated by the next normal item.
                    k = 0
                    while (pos + 6 + 6 * (k + 1) <= len(data)
                           and data[pos + 6 + 6 * k] in (0x14, 0xD4)):
                        k += 1
                    if k < 8:
                        raise ValueError(f"truncated capture at {pos:#x}")
                    frames = data[pos + 6:pos + 6 + 6 * k]
                    codes = decode_capture(
                        frames + b"\x00" * (TRANSIENT_PAYLOAD - len(frames))
                    )[:3 * k]
                    codes = (codes + [0] * TRANSIENT_STORED)[:TRANSIENT_STORED]
                    self.T.append((
                        struct.unpack(">I", data[pos + 2:pos + 6])[0], codes))
                    pos += 6 + 6 * k
                else:
                    raise ValueError(f"truncated capture at {pos:#x}")
            elif t == TYPE_BLOCK:
                pp, lanes = ((data[pos + 1] << 8) | data[pos + 2], [0] * LANES)
                pos += 6
                while pos < len(data) and data[pos] == TYPE_HARM:
                    if len(data) - pos < 6:
                        raise ValueError(f"truncated block at {pos:#x}")
                    base = block_lane_base(data[pos + 1])
                    for k in range(4):
                        ln = base + 2 * k
                        if ln < LANES:
                            lanes[ln] = data[pos + 2 + k]
                    pos += 6
                self.blocks.append((pp, lanes))
            elif t in (TYPE_VOLTAGE, TYPE_FREQ):
                b0, b1, b2, b3, b4 = data[pos + 1:pos + 6]
                raw = (((b0 & 0x0F) << 8) | b1, (b2 << 4) | (b3 >> 4),
                       ((b3 & 0x0F) << 8) | b4)
                vals = tuple(v - 4096 if v >= 2048 else v for v in raw)
                if t == TYPE_FREQ:
                    self.F.append(vals)
                elif b0 >> 4 == 0:
                    self.V.append(vals)
                else:
                    self.N.append(vals)
                pos += 6
            else:
                raise ValueError(f"unparsable item {t:#x} at {pos:#x}")
        if not len(self.V) == len(self.N):
            raise ValueError("V/N item counts differ")
        if len(self.V) != len(self.F):
            print(f"note: stream has V/N x{len(self.V)} and F x"
                  f"{len(self.F)} (source file's own G-without-F rows)")


def block_lane_base(ii: int) -> int:
    """Inverse of the [verified] `0b` lane map: even II -> octave*8+1,
    odd II -> octave*8."""
    return LANES_PER_OCTAVE * (ii // 2) + (1 if ii % 2 == 0 else 0)


def _timeline_roundtrip(fpq: Fpq, stream: bytes) -> tuple[bool, str]:
    """Build -> parse_dump -> rebuild .fpq -> compare every row.

    The encoder must be the exact inverse of the production decoder: the
    rebuilt .fpq keeps the source session's row count, row timestamps
    (whole-second; the <=1 s bound is the spec) and measured values.
    """
    if not fpq.G or not fpq.interval:
        return True, "skipped (no rows or interval)"
    import tempfile
    import fpq_serial_meter as fm
    raw = (struct.pack("<I", round(EST_BYTES_PER_ROW * len(fpq.G)))
           + stream + b"\x04")
    items = fm.parse_dump(raw, interval=fpq.interval)
    if len(items["rows"]) != len(fpq.G):
        return False, (f"row count {len(items['rows'])} != source "
                       f"{len(fpq.G)}")
    anchor_dt = ole(fpq.G[-1][0]).replace(microsecond=0)
    # OLE doubles turn a whole second into ...58.999999; round to the
    # nearest second before adding the interval, otherwise build_fpq's
    # `_pd_for_anchor` is one second late and the daily step lands in the
    # wrong bracket.
    if ole(fpq.G[-1][0]).microsecond >= 500000:
        anchor_dt += timedelta(seconds=1)
    anchor = anchor_dt + timedelta(seconds=fpq.interval)
    blob = fm.build_fpq(items, anchor, fpq.interval,
                        fpq.dip if fpq.dip is not None else 198,
                        fpq.pico if fpq.pico is not None else 218,
                        fpq.umbral if fpq.umbral is not None else 100)
    with tempfile.NamedTemporaryFile(suffix=".fpq", delete=False) as fh:
        fh.write(blob)
        path = fh.name
    try:
        rebuilt = Fpq(path)
    finally:
        os.unlink(path)
    if len(rebuilt.G) != len(fpq.G):
        return False, (f"rebuilt row count {len(rebuilt.G)} != source "
                       f"{len(fpq.G)}")
    bad_ts = bad_v = near_f = 0
    max_dt = 0.0
    for (sts, sl, sn), (rts, rl, rn) in zip(fpq.G, rebuilt.G):
        dt = abs((rts - sts) * 86400.0)
        max_dt = max(max_dt, dt)
        # 1 s is the contract; allow 1 ms for the OLE-double round-trip
        # (...58.999999 <-> ...59.000000) so a whole-second timestamp is
        # never reported as a one-second error.
        if dt > 1.0 + 1e-3:
            bad_ts += 1
        for a, b in ((sl, rl), (sn, rn)):
            for x, y in zip(a, b):
                if f32_bytes(x) != f32_bytes(y):
                    bad_v += 1
    for (sts, sf), (rts, rf) in zip(fpq.F, rebuilt.F):
        for x, y in zip(sf, rf):
            if f32_bytes(x) != f32_bytes(y):
                if abs(x - y) <= FREQ_B + 1e-9:
                    near_f += 1
                else:
                    bad_v += 1
    ok = not bad_ts and not bad_v
    return ok, (f"{len(rebuilt.G)}/{len(fpq.G)} rows, max |dt| "
                f"{max_dt:.3f} s, {bad_v} value mismatch(es)"
                + (f", {near_f} frequency field(s) within 1 code"
                   if near_f else ""))


def self_check(fpq: Fpq, out_path: str | None, counters: bool,
               progress=None) -> int:
    print("self-check:", fpq.describe())
    if progress is not None:
        progress.phase("building token stream")
    stream = build_stream(fpq, counter_rows=14 if counters else None)
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(stream)
        print(f"wrote {len(stream)} token-stream bytes to {out_path}")
    st = Stream(stream)
    ok = True

    # 1) the three RMS/Frequency items of each row (rows may lack F when
    # the source .fpq itself has G-without-F rows; V/N stay row-aligned)
    bad = [0, 0, 0]
    freq_near = [0]      # frequency fields quantized to the closest code
    n = len(st.V)
    if progress is not None:
        progress.phase("checking row fields")
    for i in range(n):
        if progress is not None and i and i % 5000 == 0:
            progress.tick()
        for w in range(2):
            wire = (st.V, st.N, st.F)[w]
            want = (fpq.G[i][1] if w == 0 else fpq.G[i][2])
            for gotv, wantv in zip((wire[i][j] / 8.0 for j in range(3)),
                                   (want[1], want[2], want[0])):
                if f32_bytes(gotv) != f32_bytes(wantv):
                    bad[w] += 1
    n_f = min(len(st.F), len(fpq.F))
    for i in range(n_f):
        want = fpq.F[i][1]
        for gotv, wantv in zip((60.0 + st.F[i][j] * FREQ_B for j in range(3)),
                               (want[1], want[2], want[0])):
            if f32_bytes(gotv) != f32_bytes(wantv):
                if abs(gotv - wantv) <= FREQ_B + 1e-9:
                    freq_near[0] += 1
                else:
                    bad[2] += 1
    labels = ["L1N volts ", "N-G volts ", "frequency"]
    counts = (3 * len(st.V), 3 * len(st.N), 3 * len(st.F))
    for w in range(3):
        total = counts[w]
        print(f"  {labels[w]}: {total - bad[w]}/{total} fields byte-exact"
              + ("   OK" if not bad[w] else "   MISMATCH")
              + (f", {freq_near[0]} quantized-closest (within 1 code)"
                 if w == 2 and freq_near[0] else ""))
    ok = ok and not any(bad)

    # 1b) transient captures: count and every sample must match the source
    bad_t = abs(len(st.T) - len(fpq.T))
    for i in range(min(len(st.T), len(fpq.T))):
        if st.T[i][1] != fpq.T[i][1]:
            bad_t += 1
    print(f"  captures      : {len(st.T)}/{len(fpq.T)} present, "
          f"{len(st.T) - bad_t if bad_t <= len(st.T) else 0} sample-exact"
          + ("" if not bad_t else "   MISMATCH"))
    ok = ok and not bad_t

    # 1c) stored D/P events: the wire items must reproduce every record's
    # row, voltage and duration (Power Log derives the records from these
    # items; it does not recompute them from the 5 s rows — the durations
    # are sub-interval and the count/values match the dump 1:1).
    gts = [r[0] for r in fpq.G]

    def check_events(name, recs, items, volts_of):
        bad_n = abs(len(items) - len(recs))
        bad_f = 0
        for k in range(min(len(items), len(recs))):
            ets, edur, evolts = recs[k]
            it = items[k]
            want_row = bisect.bisect_left(gts, ets)
            if (it["row"] != want_row
                    or abs(it["dur"] / 240.0 - edur) > 1e-6
                    or abs(volts_of(it) - evolts) > 1e-6):
                bad_f += 1
        exact = len(recs) - max(bad_n, bad_f) if bad_f <= len(recs) else 0
        print(f"  {name:<13} : {len(items)}/{len(recs)} present, "
              f"{max(0, exact)}/{len(recs)} exact (row+voltage+duration)"
              + ("" if not (bad_n or bad_f) else "   MISMATCH"))
        return not (bad_n or bad_f)

    def dip_volts(it):
        lo = it.get("lo")
        if lo is not None:
            nib = (lo >> 8) & 0x0F
            if nib <= 7:
                return it["code"] / 8.0 + 32.0 * nib
        r = it["row"]
        span = max(1, int(math.ceil(
            (it["dur"] / 240.0) / (fpq.interval or 1.0))))
        row_min = None
        for j in range(max(0, r - span), min(len(fpq.G), r + 1)):
            m = fpq.G[j][1][0]
            if m >= 32.0 and (row_min is None or m < row_min):
                row_min = m
        return it["code"] / 8.0 + dip_volt_offset(row_min=row_min,
                                                  vnom=fpq.vnom)

    ok = ok and check_events("D events", fpq.D, st.D, dip_volts)
    ok = ok and check_events(
        "P events", fpq.P, st.P, lambda it: it["code"] / 8.0)

    # 1d) I and j are host-derived from the row/counter timeline; the wire
    # has no 0x49/0x6a item. The replay emits every source row in order and
    # derives the counters from the .fpq timestamps, so the gaps survive;
    # nothing is fabricated here.
    gap_sites = sum(
        1 for a, b in zip(fpq.G, fpq.G[1:])
        if (b[0] - a[0]) * 86400.0 > (fpq.interval or 0) + 0.01)
    no_ij = not ({0x49, 0x6A} & st.types)
    print(f"  I/j (host)    : {fpq.events['I']} I, {fpq.events['j']} j in "
          f"source; {gap_sites} row gap(s) > interval kept, no 0x49/0x6a "
          f"item emitted ({'OK' if no_ij else 'FAIL'})")
    ok = ok and no_ij

    # 2) the 10-min block: Pst, harmonics, THD
    m = len(st.blocks)
    bad_k = bad_c = bad_h = 0
    if progress is not None:
        progress.phase("checking 10-minute blocks")
    for i in range(m):
        if progress is not None and i and i % 1000 == 0:
            progress.tick()
        pp, lanes = st.blocks[i]
        if f32_bytes(pp / 256.0) != f32_bytes(fpq.K[i][1]):
            bad_k += 1
        for j in range(LANES):
            if f32_bytes(lanes[j] * 100 / 1024.0) != f32_bytes(fpq.C5[i][1][j]):
                bad_c += 1
        thd = math.sqrt(sum((lanes[j] * 100 / 1024.0) ** 2
                            for j in range(LANES)))
        if f32_bytes(thd) != f32_bytes(fpq.H[i][1]):
            bad_h += 1
    print(f"  Pst (PP/256)  : {m - bad_k}/{m} byte-exact")
    print(f"  5C lanes      : {LANES * m - bad_c}/{LANES * m} byte-exact")
    print(f"  THD via lanes : {m - bad_h}/{m} byte-exact")
    ok = ok and not (bad_k or bad_c or bad_h)

    # 3) Plt: cbrt(mean of the 12 preceding Pst^3), host-computed. A file
    # with fewer L records than windows (or missing L entirely) must not
    # index past the end — check only the windows the source actually has.
    win = 12
    windows = min(m // win, len(fpq.L))
    exact = near = far = 0
    for k in range(windows):
        ps = [fpq.K[win * k + i][1] for i in range(win)]
        v = (sum(p ** 3 for p in ps) / win) ** (1 / 3)
        diff = int.from_bytes(f32_bytes(v), "little") \
            - int.from_bytes(f32_bytes(fpq.L[k][1]), "little")
        absd = abs(diff)
        exact += absd == 0
        near += absd == 1
        far += absd > 1
    print(f"  Plt (12 Pst^3): {exact}/{windows} byte-exact, "
          f"{near} within 1 ULP" + (f", {far} off" if far else "")
          + (f"  (source has {len(fpq.L)} Plt record(s))"
             if len(fpq.L) != m // win else ""))
    if far:
        ok = False

    # 4) page framing: the wire layout must be the meter's own, byte for
    # byte (footer `02 k ~k`, 1,024-byte payload, CRC-16/XMODEM big-endian,
    # 1,029-byte stride). Stripping footer+CRC must give back the flat item
    # stream exactly, and every page CRC must verify. This is a strict
    # gate: no padding, no tolerance.
    paged = build_stream(fpq, counter_period_s=None, counter_rows=None,
                        paged=True)
    flat_plain = build_stream(fpq, counter_period_s=None, counter_rows=None)
    foots = []
    pos, kk, frame_ok = 0, 1, True
    while pos < len(paged):
        want = bytes((0x02, kk & 0xFF, (~kk) & 0xFF))
        if paged[pos:pos + 3] != want:
            frame_ok = False
            print(f"  page framing : FAIL at {pos} (expected k={kk})")
            break
        foots.append(pos)
        pos += PAGE_STRIDE
        kk += 1
    if frame_ok and len(paged) % PAGE_STRIDE:
        frame_ok = False
        print(f"  page framing : FAIL - {len(paged)} B is not a whole "
              f"number of {PAGE_STRIDE}-B pages")
    if frame_ok:
        body = bytearray()
        crc_bad = 0
        for i, s in enumerate(foots):
            if progress is not None and i and i % 500 == 0:
                progress.tick()
            end = foots[i + 1] if i + 1 < len(foots) else len(paged)
            payload = paged[s + 3:end - PAGE_CRC]
            body += payload
            if (len(payload) != PAGE_PAYLOAD
                    or crc16_xmodem(payload)
                    != struct.unpack(">H", paged[end - PAGE_CRC:end])[0]):
                crc_bad += 1
        target = flat_plain[3:]
        frame_ok = crc_bad == 0 and bytes(body[:len(target)]) == target
        if body[len(target):]:
            frame_ok = frame_ok and not any(body[len(target):])
        print(f"  page framing : {len(foots)} pages, {PAGE_PAYLOAD}-B + "
              f"CRC-16 payloads ({PAGE_STRIDE}-B stride), {crc_bad} bad "
              f"CRC(s); strip == flat stream: "
              f"{'OK' if frame_ok else 'MISMATCH'}")
    ok = ok and frame_ok

    # 5) timeline counters: every emitted 06 must equal the canonical clock
    # at its row's elapsed time (the decoder then inverts it exactly).
    if counters and st.counters:
        p, ph, pd = counter_phases(fpq.G[0][0])
        bad_ctr = 0
        if progress is not None:
            progress.phase("checking counters")
        for _n, (row, v) in enumerate(st.counters, start=1):
            if progress is not None and _n % 5000 == 0:
                progress.tick()
            idx = min(row, len(fpq.G) - 1)
            secs = (fpq.G[idx][0] - fpq.G[0][0]) * 86400.0
            want = COUNTER_BASE + round(counter_clock(round(secs), p, ph, pd))
            if v != want:
                bad_ctr += 1
        mono = all(a <= b for (_ra, a), (_rb, b) in
                   zip(st.counters, st.counters[1:]))
        print(f"  counter clock : {len(st.counters) - bad_ctr}/"
              f"{len(st.counters)} counters equal the step model "
              f"(p={p:g} ph={ph:g}), monotone "
              f"{'OK' if mono else 'FAIL'}"
              + ("" if not bad_ctr else "   MISMATCH"))
        ok = ok and mono and not bad_ctr

    # 6) end-to-end timeline: decode the built stream with the production
    # parser and rebuild a .fpq; row times/values must survive the round trip.
    if counters:
        rt_ok, detail = _timeline_roundtrip(fpq, stream)
        print(f"  timeline RT   : {detail}"
              + ("   OK" if rt_ok else "   MISMATCH"))
        ok = ok and rt_ok

    # 6b) I/j event round-trip: every source interruption must come back as
    # an `I` gap with the same duration, and every measurement gap as `j`.
    # The wire encodes that only through the 09/0A pause/resume pair.  This
    # is a tool-side check: `serve` keeps the synthetic timeline off because
    # Power Log's display decoder rejects it (A5b).
    if counters:
        import fpq_serial_meter as fm
        raw = struct.pack("<I", len(stream)) + stream + b"\x04"
        parsed = fm.parse_dump(raw, interval=fpq.interval)
        gap = parsed.get("gap") or {}
        gaps = gap.get("gaps") or []
        # Every source I record is one 09+0A pair, including a pair on a
        # row with no row gap (zero-duration I).  Those pairs are in
        # `i_pairs`, not in `gaps`, so count them from there.
        pairs = gap.get("i_pairs") or {}
        got_i = sorted(round(p["i_dur"])
                       for ps in pairs.values() for p in ps)
        got_j = sorted(round(g["dur"]) for g in gaps if g["kind"] == "j")
        want_i = sorted(round(d) for _t, d in fpq.I)
        want_j = sorted(round(d) for _t, d in fpq.j)
        ij_ok = got_i == want_i and got_j == want_j
        print(f"  I/j markers   : {len(got_i)} I {got_i}, {len(got_j)} j "
              f"{got_j} (source {want_i}, {want_j}) "
              f"{'OK' if ij_ok else 'MISMATCH'}")
        ok = ok and ij_ok

    ev = ", ".join(f"{k}={v}" for k, v in fpq.events.items() if v)
    print(f"  wire events   : D/P/T items emitted (counts above); "
          f"timestamps PC-clock anchored; I/j host-derived from row gaps "
          f"(source: {ev or 'none'})")
    if progress is not None:
        progress.done()
    print("RESULT:", "PASS — every wire-recoverable value is byte-exact"
          if ok else "FAIL")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# serve: be the recorder
# ---------------------------------------------------------------------------

BANNER = "\r\nFLUKE 1710 V1.15 25/06/2009\r\n"  # X 0 [verified]

ECHOED = b"WCXijr"    # commands the recorder echoes before the argument
SILENT = "IJRv"       # commands answered without an echo


class Meter:
    """Device side of the recorder protocol (idle + dump)."""

    def __init__(self, fpq: Fpq, opts, log):
        self.fp = fpq
        self.o = opts
        self.log = log
        # Live umbral state: Power Log applies settings by stepping `K1`/`k1`
        # and then reads `r 1` back. Answering the fixture value would break
        # the Apply loop, so track the steps.
        self.umbral = None
        raw_file = getattr(opts, "stream_file", None)
        self._stream_bounds = None
        self._file_preamble = None
        pad = None
        # The pad is part of the GENERATED stream; a verbatim stream
        # (--stream-file) already contains whatever the meter sent.
        if (not raw_file and getattr(opts, "pad_now", False) and fpq.G):
            fixed = getattr(opts, "pad_seconds", None)
            pad = (max(0.0, float(fixed)) if fixed is not None else
                   max(0.0, (datetime.now()
                             - ole(fpq.G[-1][0])).total_seconds()))
        if raw_file:
            data = open(raw_file, "rb").read()
            if len(data) >= 4:
                # The file's own 4-byte V/v preamble is the expected dump
                # size; len(data) also counts the preamble and EOT, which
                # makes Power Log NAK the EOT and skip the prompt.
                self._file_preamble = int.from_bytes(data[:4], "little")
            if data.endswith(b"\x04"):
                data = data[:-1]
            if not data.startswith(DUMP_PREFIX) and data[4:7] == DUMP_PREFIX:
                data = data[4:]
            self.stream = data
            # Captured streams can contain dropped bytes, so their footer
            # positions are IRREGULAR. The host ACK unit ends where a
            # footer begins; derive the boundaries from the real footers
            # instead of assuming the theoretical 1,029-byte stride.
            foot = []
            if data[:3] == DUMP_PREFIX:
                foot.append(0)
            want, i = 2, 3 if foot else 0
            while i + 3 <= len(data):
                if (data[i] == 0x02 and data[i + 1] == want
                        and (data[i + 1] + data[i + 2]) & 0xFF == 0xFF):
                    foot.append(i)
                    want = (want + 1) & 0xFF
                    i += 3
                    continue
                i += 1
            self._stream_bounds = ([0] + [f for f in foot[1:]]
                                   + [len(data)] if foot else None)
            self.log(f"stream source: verbatim {raw_file} ({len(data)} B, "
                     f"{len(foot)} footers)")
        else:
            track = None
            tticks = None
            if getattr(opts, "counter_track", None):
                # Diagnostic: borrow the real meter's counter values and
                # positions from a raw dump, to separate counter effects
                # from block/data effects. Lazy import keeps the normal
                # path independent.
                import fpq_serial_meter as fm
                with open(opts.counter_track, "rb") as f:
                    parsed = fm.parse_dump(f.read())
                track = parsed.get("counter_items") or []
                tticks = [t["ticks"]
                          for t in (parsed.get("transients") or [])]
                self.log(f"counter track: {len(track)} item(s) from "
                         f"{opts.counter_track}"
                         + (f"; {len(tticks)} capture tick(s)"
                            if tticks else ""))
            self.stream = build_stream(
                    fpq,
                    lead=opts.lead_counter,
                    counter_rows=(None if (not opts.counters
                                           or getattr(opts, "counter_period",
                                                      None))
                                  else getattr(opts, "counter_rows", 14)),
                    counter_period_s=(None if not opts.counters
                                      else getattr(opts, "counter_period",
                                                   None)),
                    counter_steps=getattr(opts, "counter_steps", False),
                    day_step=getattr(opts, "counter_day_step", True),
                    paged=opts.pages,
                    pad_seconds=pad,
                    counter_rate=getattr(opts, "counter_rate", None),
                    counter_track=track,
                    transient_ticks=tticks,
                    pad_kind=getattr(opts, "pad_kind", "09a06"),
                    transients=getattr(opts, "transients", False))
            max_pages = int(getattr(opts, "max_pages", 0) or 0)
            if max_pages and getattr(opts, "pages", False):
                self.stream = self.stream[:max_pages * PAGE_STRIDE]
                self.log(f"max-pages: truncated to {max_pages} pages "
                         f"({len(self.stream)} B)")
        if pad:
            self.log(f"timeline: appending a {pad:.0f} s no-data pad "
                     "(time since the recording ended) so the host can "
                     "anchor the session back to its capture date")
        # The live meter's 4-byte V preamble IS its current `v` counter
        # (verified: v=755,718 and V replied 06 88 0b 00 = 755,718). Keep
        # the two answers consistent — Power Log can cross-check them.
        # Power Log also uses V/v as the expected dump size: a verbatim
        # cut served with the full session's V never completes. With
        # --stream-file, size it from the stream itself.
        # The meter's V/v answer IS the expected dump size (Power Log NAKs
        # bytes beyond it). For a generated stream that is the exact byte
        # length we will send, not a row-count estimate.
        if raw_file:
            est = self._file_preamble
        elif getattr(self.o, "pages", False):
            # Power Log sizes the dump from V/v in 1,024-byte payload units,
            # not total bytes including page footers/CRCs.
            est = (len(self.stream) // PAGE_STRIDE) * PAGE_PAYLOAD
        else:
            est = len(self.stream)
        if opts.preamble == "auto":
            self.preamble = struct.pack("<I", est)
        else:
            self.preamble = bytes.fromhex(opts.preamble)
        # `v` is a FIXED-WIDTH 7-digit counter on the real meter ('0755718').
        # A short reply desyncs Power Log's fixed-length read.
        if opts.v_reply == "auto":
            self.v_reply = f"{est:07d}"
        else:
            self.v_reply = opts.v_reply
        self.state = "IDLE"            # IDLE | DUMP_READY | DUMP
        self.dumplen = len(self.stream)
        # ACK boundaries: one unit per page, footer start to footer start
        # (3-B footer + 1,024 B payload + 2-B CRC = 1,029 B).
        if getattr(self, "_stream_bounds", None):
            self.page_bounds = self._stream_bounds
        else:
            bounds = [0]
            while bounds[-1] < self.dumplen:
                bounds.append(bounds[-1] + PAGE_STRIDE)
            self.page_bounds = bounds
        self.sent = 0
        self.pushed = 0
        self.stray_polls = 0
        self.acks = 0
        self.naks = 0
        self.polls15 = 0
        self.cycles = 0

    def arg_reply(self, cmd: bytes, arg: bytes) -> bytes:
        o = self.o
        if cmd == b"C":
            if arg == b"C":
                cfg = getattr(o, "config_reply", None)
                if cfg:
                    try:
                        data = open(cfg, "rb").read()
                        self.log(f"< C C (config read) -> {len(data)} bytes "
                                 f"from {cfg}")
                        return data
                    except OSError as e:
                        self.log(f"< C C config file error: {e}")
                self.log(f"< C C (config read) -> {o.c_reply} [open]")
            else:
                self.log(f"< C {arg.decode('ascii','replace')!r} ? -> "
                         f"{o.c_reply}")
            return o.c_reply.encode()
        if cmd == b"X":
            self.log(f"< X {arg.decode('ascii','replace')!r} -> banner")
            return o.banner.encode()
        if cmd == b"W":
            self.log(f"< W {arg.decode('ascii','replace')!r} -> {o.w_reply}")
            return o.w_reply.encode()
        if cmd in (b"i", b"j", b"r"):
            v = {"i": self.fp.dip, "j": self.fp.pico, "r": self.fp.umbral}[
                cmd.decode()]
            tag = "from .fpq"
            if cmd == b"r" and self.umbral is not None:
                v, tag = self.umbral, "live (stepped)"
            s = f"{v:03d}" if v is not None else o.c_reply
            self.log(f"< {cmd.decode()} {arg.decode()} -> {s} ({tag})")
            return s.encode()
        return b""

    def idle_reply(self, cmd: bytes) -> bytes:
        o = self.o
        name = cmd.decode()
        v = {"I": o.i_reply and bytes.fromhex(o.i_reply),
             "J": o.j_reply.encode() if o.j_reply else None,
             "R": o.r_reply.encode() if o.r_reply else None,
             "v": self.v_reply.encode() if self.v_reply else None}[name]
        self.log(f"< {name} -> {escape(v) if v else '(no reply)'}")
        return v or b""

    def _next_byte(self, ser):
        """Next byte, honouring one byte pushed back by the K handler."""
        if self._pending:
            b, self._pending = self._pending[:1], self._pending[1:]
            return b
        return ser.read(1)

    def run(self, vr: VR1710):
        ser = vr.ser
        self._pending = b""
        self.log(f'serving {len(self.stream)} dump bytes; waiting for the '
                 'host wake-up " . . "')
        cmd = None
        while True:
            b = self._next_byte(ser)
            if not b:
                continue
            c = b[0:1]
            if cmd is not None:
                ser.write(self.arg_reply(cmd, c))
                cmd = None
            elif c == b"?":
                ser.write(b"?")
            elif c == b"V":
                ser.write(self.preamble)
                self.state = "DUMP_READY"
                self.log(f"< V -> preamble {escape(self.preamble)} "
                         "(dump armed)")
            elif c == b"C" and self.state == "DUMP_READY":
                self.start_dump(ser)
            elif c in b"Kk":
                # `?K` alone is a state/value query (Power Log's transient
                # block); `?K1`/`?k1` are the umbral -1/+1 steps (channel
                # arg). Distinguish by peeking at the next byte.
                ser.write(c)
                nxt = ser.read(1)
                if nxt and nxt in b"123":
                    ser.write(nxt)            # step form: op + channel echo
                    if self.umbral is None:
                        self.umbral = self.fp.umbral
                    if self.umbral is not None:
                        self.umbral += 1 if c == b"K" else -1
                        self.umbral = max(0, min(1000, self.umbral))
                    self.log(f"< {c.decode()}{nxt.decode()} -> step "
                             f"(umbral {self.umbral})")
                else:
                    if nxt:
                        self._pending = nxt + self._pending
                    reply = (getattr(self.o, "k_reply", "") or "").strip()
                    ser.write(b"K" + reply.encode())
                    self.log(f"< K -> {reply or '(empty)'} (transient state)")
            elif c in b"WCXijr":
                ser.write(c)
                cmd = c
            elif c.decode("latin-1") in SILENT:
                ser.write(self.idle_reply(c))
            elif c == b"\x06":
                self.log("< stray 0x06 outside the dump: ignored")
            elif c == b"E" and getattr(self.o, "resume_items", 0):
                self.send_resume(ser, self.o.resume_items)
            else:
                self.log(f"< ignoring stray byte {escape(c)}")

    def send_resume(self, ser, n: int):
        """Diagnostic: after `E` (resume), send `n` live-looking samples.

        The host's post-download step appears to wait for the meter to
        resume; this sends valid V/N/F items built from the last recorded
        row so the effect on Power Log can be observed.
        """
        fp = self.fp
        if not fp.G:
            self.log("< E -> no rows to resume with")
            return
        l1n, ng = fp.G[-1][1], fp.G[-1][2]
        fr = fp.F[-1][1] if fp.F else None
        base = freq_base_of(fp)
        items = bytearray()
        for _ in range(n):
            items += encode_item(TYPE_VOLTAGE, 0, *mirror(*l1n, volt_code))
            items += encode_item(TYPE_VOLTAGE, 1, *mirror(*ng, volt_code))
            if fr is not None:
                items += encode_item(TYPE_FREQ, 1, *mirror(*fr, lambda hz: round((hz - base) / FREQ_B)))
        delay = getattr(self.o, "resume_delay", 0.0)
        if delay:
            time.sleep(delay)
        ser.write(bytes(items)); ser.flush()
        self.log(f"< E -> sent {n} live sample(s), {len(items)} B"
                 + (f" after {delay:g} s" if delay else ""))

    def start_dump(self, ser):
        self.dumplen = len(self.stream)
        self.sent = 0
        self.state = "DUMP"
        paced = getattr(self.o, "packet_gap", 0) > 0
        self.log(f"C: dump begins ({self.dumplen} bytes, "
                 f"{'FLOOD' if getattr(self.o, 'flood', False) else ('paced ' + format(self.o.packet_gap, 'g') + 's' if paced else 'ACK-gated')})")
        if paced:
            while self.sent < self.dumplen:
                self.push_page(ser)
                self.cycles += 1
                if self.cycles <= 5 or self.cycles % 50 == 0:
                    self.log(f"  > page {self.cycles}: {self.sent}/"
                             f"{self.dumplen} B")
                self.pace_and_drain(ser, self.o.packet_gap)
            # Wait for the host's closing ACK before the EOT (the meter
            # only answers a 0x06 with 0x04). If it doesn't come, close
            # anyway after a grace period.
            t_end = time.monotonic() + 15.0
            while time.monotonic() < t_end:
                ser.timeout = 0.3
                c = ser.read(1)
                if not c:
                    continue
                if c in (b"\x06", b"\x15"):
                    self.log(f"    <- {escape(c)} (closing ACK)")
                    break
                self.log(f"    <- {escape(c)} after dump")
            ser.write(b"\x04"); ser.flush()
            self.state = "DUMP_READY"
            self.log(f"< dump complete (paced): 0x04 after {self.cycles} "
                     f"pages, {self.acks} ACKs")
            return
        if getattr(self.o, "flood", False):
            nxt = 0
            while nxt < self.dumplen:
                b = next((x for x in self.page_bounds if x > nxt), self.dumplen)
                b = min(b, self.dumplen)
                ser.write(self.stream[nxt:b]); ser.flush()
                nxt = b
            ser.write(b"\x04"); ser.flush()
            self.state = "DUMP_READY"
            self.log("< dump complete (flood)")
            return
        while self.sent < self.dumplen:
            before = self.sent
            self.push_page(ser)
            self.cycles += 1
            if self.cycles <= 5 or self.cycles % 50 == 0:
                self.log(f"  > page {self.cycles}: {self.sent}/"
                         f"{self.dumplen} B sent")
            verdict = self.wait_ack(ser, timeout=self.o.ack_wait)
            if verdict == "ack":
                self.quiet_ticks = 0
                continue
            if verdict == "restart":
                self.log("  host re-sent C: restarting at page 1")
                self.sent = 0
                self.cycles = 0
                self.quiet_ticks = 0
                time.sleep(0.05)
                continue
            if verdict == "reply" or verdict == "nak":
                # 0x15: the host rejected the page (bad CRC / bad framing)
                self.sent = before
                self.quiet_ticks = getattr(self, "quiet_ticks", 0) + 1
                if self.quiet_ticks > 20:
                    self.log("  host NAKed the same page 20 times: "
                             "aborting (page encoding is wrong)")
                    self.state = "IDLE"
                    return
                continue
            # silence: resend the page once, then give up
            self.quiet_ticks = getattr(self, "quiet_ticks", 0) + 1
            if self.quiet_ticks <= 2:
                self.sent = before
                self.log(f"  no host response for {self.o.ack_wait:g} s: "
                         f"resending page {self.cycles}")
                continue
            self.log("  host stopped responding: dump aborted")
            self.state = "IDLE"
            return
        ser.write(b"\x04")
        ser.flush()
        self.state = "DUMP_READY"
        self.log(f"< dump complete: 0x04 after {self.cycles} pages, "
                 f"{self.acks} ACKs")

    def pace_and_drain(self, ser, gap: float):
        """Wait one inter-packet gap while consuming any host bytes."""
        end = time.monotonic() + gap
        while time.monotonic() < end:
            ser.timeout = max(0.01, end - time.monotonic())
            c = ser.read(1)
            if not c:
                continue
            if c == b"\x06":
                self.acks += 1
                if self.acks <= 5:
                    self.log(f"    <- 06 ACK #{self.acks}")
            elif c == b"\x15":
                self.polls15 += 1
                if self.polls15 <= 5:
                    self.log(f"    <- 15 poll #{self.polls15}")
            else:
                self.log(f"    <- {escape(c)} during dump")

    def wait_ack(self, ser, timeout: float | None = None) -> str:
        """Wait for the host's page verdict: 'ack' (0x06), 'nak' (0x15)
        or 'quiet' (nothing within the short page window)."""
        timeout = self.o.ack_wait if timeout is None else timeout
        t0 = time.monotonic()
        seen = 0
        deadline = t0 + timeout
        while time.monotonic() < deadline:
            ser.timeout = 0.2
            c = ser.read(1)
            if not c:
                continue
            seen += 1
            if seen <= 6:
                self.log(f"    <- {escape(c)} (+{time.monotonic()-t0:.2f}s "
                         f"after page)")
            if c == b"\x06":
                self.acks += 1
                return "ack"
            if c == b"C":
                return "restart"  # host asks for the dump again
            if c == b"\x15":
                self.polls15 = getattr(self, "polls15", 0) + 1
                if self.polls15 <= 10:
                    self.log("    <- 15 NAK (page rejected: resending)")
                return "nak"
            if c == b"?":
                self.stray_polls += 1
                continue
            self.log(f"< non-ACK byte {escape(c)} while waiting for ACK")
        return "quiet"

    def push_page(self, ser):
        """Send one ACK unit: footer + payload + CRC, up to the next
        footer boundary (1,029 B, or the measured capture grid)."""
        nxt = next((b for b in self.page_bounds if b > self.sent),
                   self.dumplen)
        nxt = min(nxt, self.dumplen)
        page = self.stream[self.sent:nxt]
        # small writes: some bridges drop multi-hundred-byte bursts
        step = getattr(self.o, "write_chunk", 64)
        gap = getattr(self.o, "write_gap", 0.004)
        for k in range(0, len(page), step):
            ser.write(page[k:k + step])
            ser.flush()
            if gap:
                time.sleep(gap)
        self.sent = nxt
        self.pushed += 1


def cmd_serve(args):
    fpq = Fpq(args.fpq)
    # The identification pass must report the source's own interval: Power
    # Log reads `I`/`J` at startup and renders the document from it.  An
    # explicit --i-reply/--j-reply still wins ("" silences the reply).
    auto_i, auto_j = interval_replies(fpq.interval)
    if getattr(args, "i_reply", None) in (None, "auto"):
        args.i_reply = auto_i
    if getattr(args, "j_reply", None) in (None, "auto"):
        args.j_reply = auto_j
    print("replay target:", fpq.describe())
    ev = [k for k, v in fpq.events.items() if v]
    if ev:
        print("note: source records:", ", ".join(
            f"{k}={fpq.events[k]}" for k in ev),
            "- D/P/T are emitted as wire items; I/j are host-derived")
    print("note: I/j are gap-derived by the host; the replay keeps the "
          "source row gaps and does not fabricate event items")
    print("note: absolute timestamps are re-assigned by Power Log from the "
          "downloading PC's clock (the dump is time-relative only)")
    if getattr(args, "counters", True):
        print("note: dip/swell events are emitted from the source records; "
              "the counter items carry the source session's gaps")
        if getattr(args, "transients", True):
            print("note: 0x15 transient captures are emitted from the source "
                  "records")
    else:
        print("note: synthetic 06/09/0A timeline items and 0x15 transient "
              "captures are OFF (A5b): Power Log's post-download decoder "
              "deadlocks on a replayed counter clock or capture. The "
              "document renders on the uniform .fpq interval; row gaps and "
              "captures are not represented on the wire unless --counters / "
              "--transients are used for tool/self-check dumps")
    vr = VR1710(args.port, args.baud, args.quiet)
    meter = Meter(fpq, args, lambda s: print(" ", s, flush=True))
    sys.stdout.flush()
    print(f"port {args.port} @ {args.baud} 8N1; Ctrl-C to stop")
    try:
        meter.run(vr)
    except KeyboardInterrupt:
        print("stopped.")


def interval_replies(seconds) -> tuple[str, str]:
    """The `I`/`J` identification replies for a recording interval.

    `I` is three bytes ``b0 <0x30+tens> <0xb0|units>`` carrying the
    interval's whole minutes as BCD; `J` is the remaining seconds as ASCII.
    Examples: 5 s -> ``b030b0``/``005``, 10 s -> ``b030b0``/``010``,
    20 min -> ``b032b0``/``000``.  Serving the fixed 5 s pair to a 10 s
    dump is what once made Power Log render a 10 s session at 5 s.
    """
    try:
        total = max(0, int(round(float(seconds))))
    except (TypeError, ValueError):
        total = 5
    minutes, sec = divmod(total, 60)
    tens, units = divmod(minutes, 10)
    i_reply = f"b0{0x30 + tens:02x}{0xB0 | units:02x}"
    return i_reply, f"{sec:03d}"


DEFAULTS = dict(preamble="auto", i_reply="auto", j_reply="auto",
                w_reply="007", v_reply="auto", c_reply="003",
                r_reply="001:002:011 017/002/000", banner=BANNER)


def common_args(p: argparse.ArgumentParser, suppress: bool = False):
    """Port/baud/quiet are accepted before AND after the subcommand.

    The subparsers' copy suppresses its defaults so an option given before
    the subcommand is not clobbered by the subparser (argparse quirk)."""
    def arg(*a, **kw):
        if suppress:
            kw["default"] = argparse.SUPPRESS
        p.add_argument(*a, **kw)

    arg("-p", "--port", default="/dev/ttyUSB0")
    arg("--baud", type=int, default=500000)
    arg("--quiet", type=float, default=0.12,
        help="silence window marking the end of a reply, s")


def main():
    print("* " + "-" * 72, file=sys.stderr)
    print("* fpq_serial_replay is an independent, reverse-engineered client.",
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
    print("* that a replayed session reproduces the instrument's behaviour,",
          file=sys.stderr)
    print("* readings or event records. You use this software and rely on its",
          file=sys.stderr)
    print("* output entirely at your own risk; the authors accept no liability",
          file=sys.stderr)
    print("* for any loss, damage or decision made using it. For measurement,",
          file=sys.stderr)
    print("* compliance or safety decisions use a calibrated instrument and",
          file=sys.stderr)
    print("* the vendor's official software.", file=sys.stderr)
    print("* " + "-" * 72, file=sys.stderr)
    ap = _HelpParser(
        prog="fpq_serial_replay.py",
        description="Replay a .fpq as the VR1710 meter side of the Power Log "
                    "Classic 4.60 download, with an offline self-check; see "
                    "docs/serial.md.")
    common_args(ap)
    sub = ap.add_subparsers(dest="mode", required=True,
                            parser_class=_HelpParser)
    SUB_COMMON = argparse.ArgumentParser(add_help=False)
    common_args(SUB_COMMON, suppress=True)

    p = sub.add_parser("serve", parents=[SUB_COMMON],
                       help="act as the recorder; Power Log (or "
                            "fpq_serial_meter.py) talks to you")
    p.add_argument("fpq_pos", nargs="?", default=None,
                   metavar="FPQ",
                   help="the .fpq/.fpq.gz to replay (positional form of "
                        "--fpq; either works)")
    p.add_argument("--fpq", dest="fpq_opt", default=None,
                   help="the .fpq/.fpq.gz to replay")
    p.add_argument("--flood", action="store_true",
                   help="send the whole dump back-to-back, no ACK gating "
                        "(diagnostic)")
    p.add_argument("--write-chunk", type=int, default=64,
                   help="bytes per serial write inside a page (default 64)")
    p.add_argument("--write-gap", type=float, default=0.004,
                   help="pause between serial writes, s (default 0.004)")
    p.add_argument("--packet-gap", type=float, default=0.12,
                   help="pause between dump packets, s (default 0.12 = "
                        "the real meter's macro pace; 0.10-0.15 works)")
    p.add_argument("--ack-wait", type=float, default=5.0,
                   help="per-page silence window; if the host sends nothing "
                        "the next page is sent anyway (s)")
    p.add_argument("--ack-timeout", type=float, default=60.0,
                   help="accepted for compatibility; no longer used (ACK "
                        "gating uses --ack-wait)")
    p.add_argument("--counter-track", default=None, metavar="RAW_DUMP",
                   help="diagnostic: take the counter-family items (06, and "
                        "the 09/0A pause/resume pairs) and their exact row "
                        "positions from this raw dump instead of the "
                        "meter's step clock")
    p.add_argument("--k-reply", default="200", metavar="TEXT",
                   help="reply to a bare K query (transient block); default "
                        "200 (raw umbral register value)")
    p.add_argument("--counter-rate", type=float, default=None,
                   metavar="U_PER_S",
                   help="deprecated (accepted, ignored with a note): the 06 "
                        "counter now follows the meter's step model, "
                        "128 units/s plus minute/hour/day steps")
    p.add_argument("--counters", action=argparse.BooleanOptionalAction,
                   default=False, help="emit the synthetic 06/09/0A timeline "
                   "items (values follow the .fpq's own timestamps and the "
                   "meter's step clock). OFF by default: Power Log Classic's "
                   "post-download display decoder deadlocks on the synthetic "
                   "counter clock (A5b), so a generated replay that must "
                   "render after 'Download complete' is served without "
                   "timeline items; gaps are then drawn on the uniform "
                   "interval. Enable only for tool/self-check dumps")
    p.add_argument("--transients", action=argparse.BooleanOptionalAction,
                   default=False, help="emit the stored 0x15 capture items. "
                   "OFF by default: Power Log's post-download display decoder "
                   "deadlocks on a replayed capture even when every sample "
                   "round-trips (A5b). Enable only for tool/self-check dumps")
    p.add_argument("--counter-rows", type=int, default=14, metavar="N",
                   help="rows between periodic 06 counters (default 14, the "
                        "meter's observed cadence at 5 s and 2 s)")
    p.add_argument("--counter-period", type=float, default=None,
                   metavar="S", help="optional time-based counter cadence "
                        "in seconds; overrides --counter-rows")
    p.add_argument("--counter-steps", action="store_true",
                   help="deprecated (accepted, no effect): the hourly and "
                        "daily steps are always part of the counter clock")
    p.add_argument("--counter-day-step",
                   action=argparse.BooleanOptionalAction, default=True,
                   help="emit the meter's once-a-day +32768 counter step at "
                        "17:43:15 local (default on: build_fpq always applies "
                        "the day term once it has an anchor; disable only for "
                        "diagnostic streams that do not cross the step)")
    p.add_argument("--resume-items", type=int, default=0, metavar="N",
                   help="diagnostic: after `E` (keep measuring), send N "
                        "live-looking samples built from the last row")
    p.add_argument("--resume-delay", type=float, default=0.0, metavar="S",
                   help="wait S seconds after `E` before sending resume "
                        "samples (lets the host purge first)")
    p.add_argument("--config-reply", default=None, metavar="FILE",
                   help="reply to the `C C` configuration read with the raw "
                        "bytes in FILE (e.g. a captured config dump)")
    p.add_argument("--stream-file", default=None, metavar="FILE",
                   help="serve these raw dump bytes verbatim (diagnostic: "
                        "preamble + item stream + EOT accepted)")
    p.add_argument("--pad-now", action=argparse.BooleanOptionalAction,
                   default=False,
                   help="append a final no-data counter jump covering the "
                        "time since the recording ended, so the replayed "
                        "timeline can be anchored back to its capture date. "
                        "OFF by default: a trailing counter with no rows "
                        "after it makes Power Log stay on 'Drawing Graphs...' "
                        "(verified 2026-10-25, pad size does not matter)")
    p.add_argument("--pad-seconds", type=float, default=None, metavar="S",
                   help="diagnostic: use this fixed pad length instead of "
                        "the time since the recording ended (0 = empty pad)")
    p.add_argument("--pad-kind", default="09a06",
                   choices=["06", "0a", "0a06", "09a", "09a06", "09",
                            "0609", "09a09"],
                   help="item pattern used for the pad (default 09a06). The "
                        "meter marks a no-data interval with 09 (pause) and "
                        "0a (resume at the advanced counter); the bare 06 "
                        "pad stalls Power Log. Size does not move the date "
                        "anchor, so this only avoids the stall")
    p.add_argument("--pages", action=argparse.BooleanOptionalAction,
                   default=True, help="emit the meter's real page layout "
                   "(3-B footer + 1,024-B payload + 2-B CRC-16/XMODEM, "
                   "1,029-B stride [fact 2026-10-24]; --no-pages gives "
                   "the flat stream)")
    p.add_argument("--max-pages", type=int, default=0, metavar="N",
                   help="experiment: truncate a generated paged stream to N "
                        "pages (A5b page-count sweeps)")
    p.add_argument("--lead-counter", type=int, default=COUNTER_BASE,
                   help="leading 06 counter value in units of 128 (meter start = 0x6e8e5900/128 = 14490802)")
    for k, v in DEFAULTS.items():
        p.add_argument(f"--{k.replace('_', '-')}", default=v,
                       help=f"[open] default {v!r} (I/J 'auto' derive from "
                            "the source interval; empty string silences the "
                            "reply)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("self-check", parents=[SUB_COMMON],
                       help="offline: rebuild the stream from "
                       "a .fpq, decode it back and verify byte-exactness")
    p.add_argument("fpq_pos2", nargs="?", default=None, metavar="FPQ",
                   help="the .fpq/.fpq.gz to verify (positional form)")
    p.add_argument("--fpq", dest="fpq_opt2", default=None,
                   help="the .fpq/.fpq.gz to verify")
    p.add_argument("-o", "--out", help="also write the built stream to a file")
    p.add_argument("--counters", action=argparse.BooleanOptionalAction,
                   default=False, help="include 06 counter items in the "
                                       "self-check stream")
    p.add_argument("--no-progress", action="store_true",
                   help="do not print phase/heartbeat progress on stderr")
    p.set_defaults(func=None)

    args = ap.parse_args()
    fpq = (getattr(args, "fpq_opt", None) or getattr(args, "fpq_pos", None)
           or getattr(args, "fpq_opt2", None)
           or getattr(args, "fpq_pos2", None))
    if fpq and not os.path.isfile(fpq):
        sys.exit(f"fpq_serial_replay.py: error: file not found: {fpq}")
    # resolve the fpq from either form (both subcommands)
    a = args
    fpq_path = getattr(a, "fpq", None) or getattr(a, "fpq_pos", None) \
        or getattr(a, "fpq_opt", None) or getattr(a, "fpq_pos2", None) \
        or getattr(a, "fpq_opt2", None)
    if a.mode == "self-check":
        if not fpq_path:
            ap.error("self-check needs an .fpq: "
                     "fpq_serial_replay.py self-check SESSION.fpq")
        a.fpq = fpq_path
        a.func = lambda x: sys.exit(self_check(
            Fpq(x.fpq), x.out, x.counters,
            progress=Progress("self-check",
                              enabled=not getattr(x, "no_progress", False))))
    if a.mode == "serve":
        if not fpq_path:
            ap.error("serve needs an .fpq — example: "
                     "fpq_serial_replay.py serve SESSION.fpq "
                     "(--fpq SESSION.fpq also works)")
        a.fpq = fpq_path
    try:
        args.func(args)
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
