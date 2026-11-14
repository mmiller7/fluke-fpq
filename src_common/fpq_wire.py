"""Shared wire primitives for fpq_serial_meter and fpq_serial_replay.

This module is the single definition of the byte-level pieces both tools must
agree on:

* the meter's page framing and CRC-16/XMODEM;
* the transient capture codec (427 six-byte frames -> 1,280 stored codes);
* the D/P event voltage and duration grid.

It is deliberately stdlib-only and has no I/O. The counter/gap solver and the
.fpq record writer stay in their own modules: those carry the verified
timeline logic and are guarded independently.

Disclaimer: provided "as is", without warranty of any kind, express or
implied, including accuracy or fitness for a particular purpose. Use at your
own risk; the authors accept no liability for any loss or damage. Not
affiliated with or endorsed by Fluke Corporation.
"""
from __future__ import annotations

import math
import struct

# ------------------------------------------------------------------ paging
PAGE_PAYLOAD = 1024          # item-stream bytes per real-meter page
PAGE_CRC = 2                 # big-endian CRC-16/XMODEM after each payload
PAGE_TRAILER = PAGE_CRC      # historical name used by the meter parser
PAGE_STRIDE = 3 + PAGE_PAYLOAD + PAGE_CRC    # footer + payload + CRC = 1,029


def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM (poly 0x1021, init 0x0000, no reflection)."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 \
                else (crc << 1) & 0xFFFF
    return crc


# --------------------------------------------------------------- transients
TRANSIENT_FRAMES = 427
TRANSIENT_PAYLOAD = TRANSIENT_FRAMES * 6          # 2,562 bytes
TRANSIENT_WIRE_SAMPLES = TRANSIENT_FRAMES * 3     # 1,281 on the wire
TRANSIENT_STORED = TRANSIENT_WIRE_SAMPLES - 1     # 1,280 kept in the .fpq
TRANSIENT_SAMPLES = TRANSIENT_STORED              # meter-side historical name


def s12(v: int) -> int:
    """Sign-extend a 12-bit sample."""
    return v - 4096 if v >= 2048 else v


def decode_transient_payload(payload: bytes) -> list[int]:
    """Decode one 0x15 payload to the 1,280 ADC codes stored in a .fpq.

    Each frame holds three signed 12-bit samples plus duplicated sign/status
    bits. The 1,281st wire sample repeats sample 0 (pre-trigger ring wrap)
    and is not written to the .fpq.
    """
    if len(payload) < TRANSIENT_PAYLOAD:
        raise ValueError("transient payload shorter than 2,562 bytes")
    out: list[int] = []
    for k in range(TRANSIENT_FRAMES):
        _b0, b1, b2, b3, b4, b5 = payload[6 * k:6 * k + 6]
        s0 = s12((b1 << 4) | (b2 >> 4))
        s1 = s12(((b2 & 0x0F) << 8) | b3) * 4 + ((b4 >> 6) & 3)
        s2 = s12(((b4 & 0x0F) << 8) | b5)
        out.extend((s0, s1, s2))
    return out[:TRANSIENT_STORED]


def _encode_frames(samples) -> bytes:
    """Encode a whole number of 3-sample, 6-byte capture frames."""
    out = bytearray()
    for k in range(len(samples) // 3):
        a, bb, c = samples[3 * k], samples[3 * k + 1], samples[3 * k + 2]
        a12, b14, c12 = a & 0xFFF, bb & 0x3FFF, c & 0xFFF
        hi = (b14 >> 2) & 0xFFF
        out += bytes((
            0x14 | (0xC0 if a < 0 else 0),
            (a12 >> 4) & 0xFF,
            ((a12 & 0x0F) << 4) | ((hi >> 8) & 0x0F),
            hi & 0xFF,
            ((((b14 & 3) << 2) | (3 if c < 0 else 0)) << 4)
            | ((c12 >> 8) & 0x0F),
            c12 & 0xFF,
        ))
    return bytes(out)


def encode_transient(codes, ticks: int) -> bytes:
    """One wire capture: `15 02 <u32 BE ticks>` + 427 six-byte frames.

    Exact inverse of decode_transient_payload(). The stored 1,280 codes are
    extended with sample 0 for the wire's 1,281st sample.
    """
    if len(codes) != TRANSIENT_STORED:
        raise ValueError(f"transient needs {TRANSIENT_STORED} codes, got "
                         f"{len(codes)}")
    if not 0 <= ticks <= 0xFFFFFFFF:
        raise ValueError(f"transient tick {ticks!r} outside u32")
    smp = list(codes) + [codes[0]]
    return b"\x15\x02" + ticks.to_bytes(4, "big") + _encode_frames(smp)


def encode_transient_variable(codes, ticks: int) -> bytes:
    """One wire capture in the variable `15 00 <u32 BE ticks>` form.

    The 67_ 2026-11-03 power-loss captures keep only 3k real samples and
    zero-pad the rest of the 1,280-sample slot.  The trailing zeros identify
    the native frame count, so a generated replay can reproduce the `15 00`
    framing exactly instead of the fixed 427-frame `15 02` block.
    """
    if len(codes) != TRANSIENT_STORED:
        raise ValueError(f"transient needs {TRANSIENT_STORED} codes, got "
                         f"{len(codes)}")
    if not 0 <= ticks <= 0xFFFFFFFF:
        raise ValueError(f"transient tick {ticks!r} outside u32")
    last = max((i for i, c in enumerate(codes) if c), default=-1)
    if last < 0 or (last + 1) % 3:
        raise ValueError("stored capture is not a whole number of frames")
    frames = last // 3 + 1
    if frames < 8 or frames >= TRANSIENT_FRAMES:
        raise ValueError("capture does not fit the variable 15 00 form")
    return (b"\x15\x00" + ticks.to_bytes(4, "big")
            + _encode_frames(codes[:3 * frames]))


def looks_transient(payload: bytes) -> bool:
    """Cheap guard against a stray `15 02` in the item stream.

    Every real frame starts with 0x14 or 0xD4 (sample-0 sign/status byte).
    """
    if len(payload) < TRANSIENT_PAYLOAD:
        return False
    return all(payload[6 * k] in (0x14, 0xD4)
               for k in range(TRANSIENT_FRAMES))


# -------------------------------------------------- transient sensitivity map
# The transient threshold is shown and entered in volts (2-20 V, integer), as
# the official software does. The recorder and the .fpq header store the raw
# umbral register. Measured from Power Log's own Apply: raw 100 = 2 V,
# 132 = 5 V, 290 = 20 V; raw = round((95*V + 710)/9).
TRANSIENT_MIN_V = 2
TRANSIENT_MAX_V = 20


def umbral_raw_for_volts(volts) -> int:
    """Transient volts -> raw umbral register."""
    return int(round((95 * float(volts) + 710) / 9.0))


def umbral_volts(raw) -> float:
    """Raw umbral register -> transient volts (inverse of the map above)."""
    return (9.0 * float(raw) - 710.0) / 95.0


# ----------------------------------------------------------- counter clock
# The meter's `06` value is in 1/128 s units but is not a plain clock: it adds
# fixed bookkeeping steps that depend on elapsed recording time.
#   counter/128 = t + 4*floor((t-p)/60) + 256*floor((t-ph)/3600)
#                 + 32768*floor((t-pd)/86400) + C
# `p`/`ph` place the minute/hour steps; `pd` places the once-a-day step at
# 17:43:15 local. Elapsed time is seconds from the first counter item.
COUNTER_STEP_MINUTE = 4.0
COUNTER_STEP_HOUR = 256.0
COUNTER_STEP_DAY = 32768.0
COUNTER_DAY_TOD = 17 * 3600 + 43 * 60 + 15

# Measured hour-step phase (`ph`) of the `06` counter, in seconds from the
# recorder's elapsed origin, used when a segmented schedule has no explicit
# phase and no start+stop pair.  `ph = 2886` (minute step `p = 12.5`) was
# measured against the official-save gap durations on 82_/77_ and checked on
# 67_ (33/33 I exact), 61_/65_/51_.  A session's own segment calibration is
# preferred when it has one: 84_ calibrates `ph = 2363.5` for its single
# gap and is exact either way, because one long gap admits several
# equivalent phases.  This is a measured model constant, not a tool-derived
# fact; a capture that disagrees by more than the one-minute quantisation is
# a counter-phase exception to record, not a reason to retune this value.
COUNTER_HOUR_PHASE_MEASURED = 2886.0


def counter_clock(t: float, p: float, ph: float,
                  pd: float | None = None) -> float:
    """Counter reading in seconds at elapsed recorder time `t`."""
    v = (t + COUNTER_STEP_MINUTE * math.floor((t - p) / 60.0)
         + COUNTER_STEP_HOUR * math.floor((t - ph) / 3600.0))
    if pd is not None:
        v += COUNTER_STEP_DAY * math.floor((t - pd) / 86400.0)
    return v


# ------------------------------------------------------------ D/P event grid
DIP_VOLT_OFFSET = 96.0       # 120 V channel's dip voltage offset [verified]
DIP_VOLT_WINDOW = 32.0       # high nibble of the 12-bit dip code
EVENT_DUR_SCALE = 240.0      # detector update rate: 1/240 s counts


def dip_volt_offset(row_min=None, vnom=None) -> float:
    """Dip-event voltage offset from the event row's 32 V window.

    The `04` item stores only the low 8 bits of a 12-bit code.  The high
    nibble is the 32 V window that contains the event row's minimum L1-N
    voltage, so::

        offset = 32 * floor(row_min / 32)

    This gives 96 V for a 120 V system (row min ~120), 192 V for 208 V, and
    224 V for the br1 244.5 V high-range session.  The `.fpq` header's
    ``VNom`` is Power Log's fixed 230.00 for every high-range file; it is
    metadata, not the input voltage, so it must not be used to choose the
    window.  ``vnom`` is accepted only as a 120 V fallback when no row is
    available.
    """
    if row_min is not None:
        row_min = float(row_min)
        if row_min >= 32.0:
            return DIP_VOLT_WINDOW * math.floor(row_min / DIP_VOLT_WINDOW)
    if vnom is None or abs(float(vnom) - 120.0) <= 0.01:
        return DIP_VOLT_OFFSET
    raise ValueError(
        "high-range dip offset needs the event row's minimum L1-N voltage; "
        "the header VNom=230.00 is fixed metadata, not the input voltage")


def event_code(volts: float, offset: float, maximum: int, what: str) -> int:
    """Voltage -> code on the verified 1/8 V event grid."""
    code = round((volts - offset) * 8.0)
    if not 0 <= code <= maximum:
        raise ValueError(f"{what} {volts!r} V is outside the code range")
    if f32(code / 8.0 + offset) != f32(volts):
        raise ValueError(f"{what} {volts!r} V is not on the 1/8 V grid")
    return code


def quarter_cycles(dur_s: float) -> int:
    """Event duration -> integer 1/240 s counts."""
    n = round(dur_s * EVENT_DUR_SCALE)
    if not 0 <= n <= 0xFFFF:
        raise ValueError(f"event duration {dur_s!r} s outside u16 range")
    if abs(n / EVENT_DUR_SCALE - dur_s) > 1e-6:
        raise ValueError(f"event duration {dur_s!r} s is not a 1/240 s "
                         "multiple")
    return n


def f32(x: float) -> float:
    """Round to IEEE-754 binary32, the precision Power Log stores."""
    return struct.unpack("f", struct.pack("f", x))[0]
