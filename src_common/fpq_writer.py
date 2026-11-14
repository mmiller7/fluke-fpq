"""`.fpq` header and record builders for the VR1710 meter writer.

This module assembles a complete `.fpq` (fixed header plus record stream)
from the dictionaries produced by fpq_dump.parse_dump: G/F rows, K/H/5C
10-minute blocks, L (Plt), the stored D/P/T events, and the counter-derived
I/j gap records. It was split out of fpq_serial_meter.py without behaviour
changes; the meter re-exports every name below.

Timestamps and the gap schedule come from fpq_gap; transient sample count
comes from fpq_wire.

Disclaimer: provided "as is", without warranty of any kind, express or
implied, including accuracy or fitness for a particular purpose. Use at your
own risk; the authors accept no liability for any loss or damage. Not
affiliated with or endorsed by Fluke Corporation.
"""
import bisect
import math
import struct
from datetime import datetime, timedelta

from fpq_wire import TRANSIENT_SAMPLES, dip_volt_offset
from fpq_gap import (
    OLE_EPOCH, COUNTER_SCALE,
    _gap_row_bytes, _anchor_pd_check, _solve_counter_step,
)

# Item-stream / .fpq constants (docs/serial.md and docs/format.md)
FREQ_B = 1.1106768e-3        # stored Hz = 60 + code * B  [verified 60 Hz]


def build_fpq(items: dict, anchor: datetime, interval: float,
              dip: int = 198, pico: int = 218, umbral: int = 100,
              vnom: float = 120.0, fnom: float = 60.0,
              *, stats: dict | None = None,
              hour_phase: float | None = None,
              start_anchor: datetime | None = None) -> bytes:
    """Assemble a complete .fpq (header + record stream) from a parsed dump.

    Timestamps: the dump has no absolute time, so the newest record is
    placed one interval before `anchor` (Power Log's own convention).  When
    the dump carries counter items, the row timeline is the exact
    counter-derived one: rows step by `interval` between `06` anchors and
    snap to the inverted counter at each anchor, which also preserves the
    meter's 4 s / 1 s row deltas and the restart pairs (two rows inside one
    second: the bracket holds one second less than it holds rows, so the
    anchor row repeats the previous row's timestamp and Power Log stores a
    duplicate second).  The whole timeline is then shifted under `anchor`;
    only that shift (and the daily-rollover phase check) uses absolute
    time.  Dumps with no counter evidence keep the old uniform walk.

    `I` (0x49) is written for every resuming row with a 09+0A pair, `j`
    (0x6A) for every other row gap (and for a zero-duration `I`).  Both go
    after the row's D/P/T/K/H/5C records and before its G, exactly as Power
    Log stores them.
    """
    rows, ticks = items["rows"], items["ticks"]
    # The D offset is the event row's 32 V window (96 V on the 120 V
    # fixtures, 224 V on the br1 244.5 V row) and the frequency grid
    # (60 + code*B) is verified for the 60 Hz recorder.
    # Refuse to stamp a header that describes data these codecs cannot
    # encode; exporting an official 230 V file is unaffected (this is the
    # writer, not the reader).
    if abs(vnom - 120.0) > 0.01 and abs(vnom - 230.0) > 0.01:
        raise ValueError("VNom must be 120.0 or 230.0 (Power Log's two "
                         "header values)")

    def _dip_offset(i: int, dur_s: float, lo: int | None = None) -> float:
        """32 V window carried by the `04` item's `lo` high nibble.

        The 12-bit event code is ``((lo >> 8) & 0xF) << 8 | code``.  That
        nibble is the window and is exact on every raw+official pair,
        including events inside an outage (67_).  The row-min lookback below
        is only a fallback for items without a valid nibble.
        """
        if lo is not None:
            nib = (lo >> 8) & 0x0F
            if nib <= 7:
                return 32.0 * nib
        row_min = None
        span = max(1, int(math.ceil(dur_s / interval)))
        for j in range(max(0, i - span), min(len(rows), i + 1)):
            m = rows[j][0][2] / 8.0
            if m >= 32.0 and (row_min is None or m < row_min):
                row_min = m
        return dip_volt_offset(row_min=row_min, vnom=vnom)

    if abs(fnom - 60.0) > 0.01:
        raise ValueError("FNom must be 60.0: the frequency codec is verified "
                         "only for 60 Hz")
    transients = items.get("transients", [])
    dips = items.get("dips", [])
    swells = items.get("swells", [])
    if not rows:
        raise ValueError("no voltage/frequency records in the dump")
    # Power Log timestamps always land on a whole second (verified in every
    # sample file); a datetime.now() anchor carries microseconds, so snap it
    # before the walk-back.
    anchor = anchor.replace(microsecond=0)
    freqs = sorted(60.0 + r[2][1] * FREQ_B for r in rows)
    mid = freqs[len(freqs) // 2]
    if not 59.0 <= mid <= 61.0:
        raise ValueError(
            f"decoded median frequency {mid:.2f} Hz is outside the verified "
            "60 Hz wire codec; 50 Hz sessions are [open]")

    got = _gap_row_bytes(items, anchor, interval, hour_phase=hour_phase,
                         start_anchor=start_anchor)
    gap = None
    gap_i: dict = {}
    gap_j: dict = {}
    gap_base = None
    if got is not None:
        ts_list, gap_base, gap = got
        gap_i = gap.get("i_by_row", {})
        gap_j = gap.get("by_resume", {})
        _anchor_pd_check(gap, anchor, interval)

        def ts_of(idx: int) -> float:
            if idx < 0:
                return ts_list[0] + idx * interval / 86400.0
            if idx < len(ts_list):
                return ts_list[idx]
            return (ts_list[-1]
                    + (idx - len(ts_list) + 1) * interval / 86400.0)
    else:
        first_ts = anchor - timedelta(seconds=interval * (len(rows) - 1)
                                      + interval)

        def ts_of(idx: int) -> float:
            return (first_ts + timedelta(seconds=interval * idx)
                    - OLE_EPOCH).total_seconds() / 86400.0

    def _at_days(t: float) -> float:
        """OLE days for a counter-relative elapsed time (gap events)."""
        return (gap_base + timedelta(seconds=t)
                - OLE_EPOCH).total_seconds() / 86400.0

    tick_map = {}
    for idx, pp, lanes in ticks:
        tick_map[idx] = (pp, lanes)

    # `15 02 <u32 ticks>` carries the same 1/128 s counter clock as the
    # `06` items (the replay writes `counter_value(secs) << 7`), so a
    # capture can be placed exactly against the nearest counter anchor.
    # Using the row midpoint instead put every 2026-10-28 capture 3 s late.
    # (row, counter value) pairs; a counter item at row r is the clock of
    # rows[r] (see `_analyse_counter_gaps`).
    tick_anchors = sorted(
        (r, v) for r, typ, v in items.get("counter_items", ())
        if typ == 0x06 and r < len(rows))
    # A `15 00` capture is taken when the meter loses power: its ticks are
    # the row's `09` (interruption-start) counter value, while the parsed row
    # points at the resuming row whose `06` anchor is one outage later.  Map
    # those by counter value (the anchor at/before the capture, i.e. the
    # pre-outage side of the gap) instead of by row; the short walk from that
    # anchor crosses no bookkeeping step.  67_ 2026-11-03 is the only session
    # with this form.  The fixed `15 02` captures never share a same-row `09`
    # value, so their nearest-row mapping below is unchanged (65_ keeps its
    # -7 s anchor offset).
    t09_by_row: dict[int, set[int]] = {}
    for r, typ, v in items.get("counter_items", ()):
        if typ == 0x09:
            t09_by_row.setdefault(r, set()).add(v)

    def _tick_days(tr: dict):
        """Exact OLE day of one capture from its counter-clock ticks."""
        if gap is None or not tick_anchors:
            return None
        if tr["ticks"] in t09_by_row.get(tr["row"], ()):
            before = [a for a in tick_anchors if a[1] <= tr["ticks"]]
            r, value = (max(before, key=lambda a: a[1]) if before
                        else min(tick_anchors,
                                 key=lambda a: abs(a[1] - tr["ticks"])))
            if not 0 <= r < len(gap["rows"]):
                return None
            dv = (tr["ticks"] - value) / float(COUNTER_SCALE)
            if not 0.0 <= dv <= 3600.0:  # foreign/wrapped clock
                return None
            try:
                elapsed = _solve_counter_step(
                    dv, gap["rows"][r], gap["p"], gap["ph"], gap.get("pd"))
            except (ValueError, KeyError):
                elapsed = dv
            return _at_days(gap["rows"][r] + elapsed)
        r, value = min(tick_anchors, key=lambda a: abs(a[0] - tr["row"]))
        if not 0 <= r < len(gap["rows"]):
            return None
        off = (tr["ticks"] - value) / float(COUNTER_SCALE)
        if abs(off) > 3600.0:       # wrapped/foreign counter, not this clock
            return None
        return _at_days(gap["rows"][r] + off)

    # Transients sit between rows in the item stream, so the row only bounds
    # the capture to one interval; the ticks give the exact instant when the
    # counter clock is available (see `_t_stamp`).
    t_by_row: dict[int, list[dict]] = {}
    for tr in transients:
        t_by_row.setdefault(tr["row"], []).append(tr)

    # Intra-row order: `fpq_dump.parse_dump` exposes the `09`/`0A` counter
    # markers and every D/P item with `offset` and `order`.  An `04` written
    # before the row's first 09/0A is the collapse that opened the
    # interruption; an `04` written after it is the residual that follows the
    # return.  The writer uses that order when it is available (67_, 51_, 61_,
    # 65_) and keeps the old corpus-fit duration rule only for streams parsed
    # from an older/order-less source.
    markers_by_row: dict[int, list[dict]] = {}
    for m in items.get("counter_items_meta", ()):
        if m.get("type") in (0x09, 0x0A):
            markers_by_row.setdefault(m["row"], []).append(m)

    def _i_edge(d: dict):
        """'start' / 'after' / None from the item's position vs 09/0A."""
        if "order" not in d:
            return None
        markers = markers_by_row.get(d["row"])
        if not markers:
            return None
        first = min(m["order"] for m in markers)
        return "start" if d["order"] < first else "after"

    def _start_edge_dip(row: int, d: dict) -> bool:
        """True for a dip written at the start edge of an interruption.

        Power Log writes a `04` item *before* the row's `09` marker when the
        dip is the collapse that opens the interruption (67_ 17:38:11,
        51_/61_ 13.57 s), and *after* the `0A` marker for the short residual
        that follows the return (61_/65_).  Item order is the exact
        discriminator when the parser supplies it.  For an older parsed dict
        (no `order`) the corpus-fit fallback remains: every kept start-edge
        dip is the only pair on its row and its interruption lasts at least
        two recording intervals; every after-interruption dip is on a
        shorter interruption or on a row with several pairs.
        """
        edge = _i_edge(d)
        if edge is not None:
            return (edge == "start" and gap is not None
                    and row in gap.get("by_resume", {}))
        if gap is None:
            return False
        pairs = gap.get("i_pairs", {}).get(row)
        g = gap.get("by_resume", {}).get(row)
        return bool(pairs and g and len(pairs) == 1
                    and g.get("i_dur", 0.0) >= 2.0 * interval)

    d_by_row: dict[int, list[dict]] = {}
    suppressed = 0
    for d in dips:
        # A residual shorter than one recording interval on a row carrying
        # an I is normally suppressed: Power Log lets the interruption
        # supersede the sub-interval dip.  The exception is the start-edge
        # dip, which Power Log keeps (67_ start case; the 51_/61_ 13.57 s
        # dip is longer than an interval and was never suppressed).
        if (gap_i and d["row"] in gap_i
                and d["dur_raw"] / 240.0 < interval
                and not _start_edge_dip(d["row"], d)):
            suppressed += 1
            continue
        d_by_row.setdefault(d["row"], []).append(d)
    if stats is not None:
        stats["dips_suppressed"] = suppressed
    w_by_row: dict[int, list[dict]] = {}
    for w in swells:
        w_by_row.setdefault(w["row"], []).append(w)

    # Multi-dip rows share one 1/4 s `lo` timeline: a burst can straddle two
    # stream rows while its events stay inside the earlier row's interval
    # (the 5 s rows 21-22: lo 13213 is the same second in both rows).  Re-anchor
    # the `lo`->time map only when a new burst starts; the old per-row
    # `mx = max(lo)` restarted it every row and moved row 22's events +4 s.
    d_anchor: dict = {"row": None, "lo": None, "ts": None}

    def _multi_base(i: int, base: float, mx: int) -> float:
        anchor_row, anchor_lo, anchor_ts = (d_anchor["row"],
                                            d_anchor["lo"], d_anchor["ts"])
        if (i < len(rows) and anchor_row is not None
                and i == anchor_row + 1 and anchor_lo is not None
                and 0 <= mx - anchor_lo
                <= (i - anchor_row + 1) * interval * 4):
            d_anchor["row"] = i
            return anchor_ts + (mx - anchor_lo) / 4.0 / 86400.0
        if i < len(rows):
            d_anchor.update(row=i, lo=mx, ts=base)
        return base

    def _d_stamp(i: int, group: list[dict]) -> list[float]:
        """Dip start times, on Power Log's whole-second row grid.

        A lone dip is stamped on the whole second before its item row.  At
        1 s that is the previous row; the observed event second is that row
        or the item row (~50/50), so this is within one second of every
        official save (exact on the 2 s/5 s lone dips, where the old
        midpoint was half a second early).  A start-edge dip on an
        interruption row is stamped at the interruption's `09` second
        instead: that is the collapse Power Log stores.  In a multi-dip row
        the last event still sits at the row time and earlier ones step
        back by (lo_max-lo)/4 s (the item's 1/4 s counter).
        """
        if i < len(rows):
            base = ts_of(i)
        else:
            base = ts_of(len(rows) - 1) + (interval / 2.0) / 86400.0
        group = sorted(group, key=lambda d: d["lo"])
        if len(group) == 1:
            if _start_edge_dip(i, group[0]):
                return [_at_days(gap["by_resume"][i]["i_ts"])]
            if i >= len(rows):
                return [base - (interval / 2.0) / 86400.0]
            # Whole seconds: floor the interval midpoint; for 1 s that is
            # the previous row, for 2 s/5 s the exact official offset.
            step = max(1.0, math.floor(interval / 2.0))
            return [base - step / 86400.0]
        mx = group[-1]["lo"]
        ref = _multi_base(i, base, mx)
        return [ref - (((mx - d["lo"]) // 4)) / 86400.0 for d in group]

    def _t_stamp(i: int, group: list[dict]) -> list[float]:
        if i > 0:
            base = ts_of(i - 1) + (interval / 2.0) / 86400.0
        else:
            base = ts_of(0) - (interval / 2.0) / 86400.0
        group = sorted(group, key=lambda t: t["ticks"])
        t0 = group[0]["ticks"]
        out = []
        for tr in group:
            exact = _tick_days(tr)
            if exact is not None:
                out.append(exact)
                continue
            # Fallback when the counter clock is unavailable (flat dumps):
            # midpoint of the interval plus the capture's tick delta.
            dt = min(max((tr["ticks"] - t0) / 128.0, 0.0), interval * 0.95)
            out.append(base + dt / 86400.0)
        return out

    def _p_stamp(i: int, w: dict) -> float:
        """Swell end time, on Power Log's whole-second row grid.

        The `0x10` item has no time field; its stream position is the only
        evidence, and it is written after the row that closed when the event
        ended.  A swell shorter than one interval ended in the second before
        the row; a longer one covers the previous interval.  On a row that
        resumes after a gap the item sits *before* the 09/0A marker pair, so
        the event belongs to the interval that ended before the outage: the
        post-gap row time moved a 2026-10-28 swell by 445 s.  The remaining
        sub-interval position (the item's flags) is not retained by the
        parser; the duration residue places the observed gap swells within
        1 s (exact on 35_ and 65_ 114101).
        """
        dur = w["dur_raw"] / 240.0
        if (gap is not None and i > 0
                and i in gap.get("by_resume", {})):
            return (ts_of(i - 1)
                    + math.floor(dur % interval) / 86400.0)
        if i >= len(rows):
            return ts_of(len(rows) - 1)
        step = interval if dur >= interval else 1.0
        return ts_of(i) - step / 86400.0

    # Stamps are computed in row order so `_multi_base` still sees the
    # consecutive-row bursts.  A start-edge dip is written before the first
    # G at/after its `09` second; every other record stays on its item row.
    d_at: dict[int, list[tuple[float, dict, int]]] = {}
    if interval == 1.0:
        # 1 s `lo` placement.  A D group on one row is rarely enough to fix
        # the absolute second (the `lo` epoch changes between detector
        # bursts), but a *coherent run* of dips across rows does: every
        # event's item second is either its row second or the one before it,
        # and the run shares one `lo` timeline.  Intersect those constraints
        # over the run; when exactly one integer anchor survives, place the
        # whole run with it.  A lone dip (or an ambiguous run) keeps the
        # whole-second fallback: the second before the item row.  This is the
        # wire limit -- `lo` only orders events inside one burst.
        start_groups: dict[int, list[dict]] = {}
        normal_rows: dict[int, list[dict]] = {}
        for _row in sorted(d_by_row):
            if _row >= len(rows):
                continue              # tail loop below handles these
            _group = sorted(d_by_row[_row], key=lambda d: d["lo"])
            _start = [d for d in _group if _start_edge_dip(_row, d)]
            _normal = [d for d in _group if not _start_edge_dip(_row, d)]
            if _start:
                start_groups[_row] = _start
            if _normal:
                normal_rows[_row] = _normal
        for _row, _group in start_groups.items():
            _ts = _at_days(gap["by_resume"][_row]["i_ts"])
            _slot = bisect.bisect_left(ts_list, _ts)
            d_at.setdefault(_slot, []).extend(
                (_ts, d, _row) for d in _group)

        # Split the remaining dips into `lo`-coherent runs.  A burst can
        # straddle a row gap (43_ rows 73211/73212/73214) and can repeat the
        # same `lo` on adjacent rows; the tolerance is one interval.
        _runs: list[list[tuple[int, list[dict], int]]] = []
        _run: list[tuple[int, list[dict], int]] = []
        _prev = None
        for _row in sorted(normal_rows):
            _group = normal_rows[_row]
            _mx = _group[-1]["lo"]
            if (_prev is None
                    or abs((_mx - _prev[1])
                           - 4.0 * (ts_of(_row) - ts_of(_prev[0])) * 86400.0)
                    <= 4.0 + 1e-4):
                _run.append((_row, _group, _mx))
            else:
                _runs.append(_run)
                _run = [(_row, _group, _mx)]
            _prev = (_row, _mx)
        if _run:
            _runs.append(_run)

        for _run in _runs:
            if len(_run) > 1:
                _row0, _g0, _lo0 = _run[0]
                _base = ts_of(_row0)
                _klo, _khi = -10 ** 9, 10 ** 9
                for _row, _group, _mx in _run:
                    _dr = round((ts_of(_row) - _base) * 86400.0)
                    for _d in _group:
                        _ds = (_d["lo"] - _lo0) / 4.0
                        _klo = max(_klo, math.ceil(_dr - 1 - _ds))
                        _khi = min(_khi, math.floor(_dr - _ds))
                if _klo == _khi:
                    _k = _klo
                    for _row, _group, _mx in _run:
                        for _d in _group:
                            _ts = (_base + (_k + (_d["lo"] - _lo0) / 4.0)
                                   / 86400.0)
                            d_at.setdefault(_row, []).append(
                                (_ts, _d, _row))
                    continue
            # Ambiguous run (or a single row): keep the fallback.
            for _row, _group, _mx in _run:
                if len(_group) == 1:
                    _ts = ts_of(_row) - 1.0 / 86400.0
                    d_at.setdefault(_row, []).append((_ts, _group[0], _row))
                else:
                    _ref = ts_of(_row)
                    for _d in _group:
                        _ts = (_ref - ((_mx - _d["lo"]) // 4) / 86400.0)
                        d_at.setdefault(_row, []).append((_ts, _d, _row))
    else:
        for _row in sorted(d_by_row):
            if _row >= len(rows):
                continue                  # tail loop below handles these
            _group = sorted(d_by_row[_row], key=lambda d: d["lo"])
            _stamps = _d_stamp(_row, _group)
            _slot = _row
            if len(_group) == 1 and _start_edge_dip(_row, _group[0]):
                _slot = bisect.bisect_left(ts_list, _stamps[0])
            d_at.setdefault(_slot, []).extend(
                (ts_d, d, _row) for ts_d, d in zip(_stamps, _group))

    recs = []
    n_i = n_j = 0
    tick_k = 0
    ticks_sorted = sorted(tick_map)
    for i, (v, n_, f) in enumerate(rows):
        if i in w_by_row:
            for w in sorted(w_by_row[i], key=lambda x: x["dur_raw"]):
                recs.append(_p_rec(_p_stamp(i, w), w["dur_raw"] / 240.0,
                                   w["code"] / 8.0))
        if i in d_at:
            for ts_d, d, row in d_at[i]:
                recs.append(_d_rec(ts_d, d["dur_raw"] / 240.0,
                                   d["code"] / 8.0 + _dip_offset(row, d["dur_raw"] / 240.0, d["lo"])))
        if i in t_by_row:
            for ts_t, tr in zip(_t_stamp(i, t_by_row[i]), sorted(
                    t_by_row[i], key=lambda t: t["ticks"])):
                recs.append(_t_rec(ts_t, tr["codes"]))
        if i in tick_map:
            pp, lanes = tick_map[i]
            ts = ts_of(i)
            p = pp / 256.0
            recs.append(_k_rec(ts, p, first=(tick_k == 0)))
            if tick_k >= 11 and (tick_k + 1) % 12 == 0:
                win = [tick_map[t][0] / 256.0 for t in ticks_sorted[
                    tick_k - 11:tick_k + 1]]
                plt = (sum(x ** 3 for x in win) / 12.0) ** (1 / 3)
                recs.append(_l_rec(ts, plt))
            lane_vals = [c * 100 / 1024.0 for c in lanes]
            thd = math.sqrt(sum(x * x for x in lane_vals))
            recs.append(_h_rec(ts, thd))
            recs.append(_c5_rec(ts, lane_vals))
            tick_k += 1
        # Interruption / interval-without-measurements.  Power Log writes
        # these after the row's own records and immediately before its G
        # (confirmed by the P-before-I order at 2026-10-28 16:45:52/53).
        if gap is not None:
            pairs = gap.get("i_pairs", {}).get(i)
            if pairs is None:
                gi = gap_i.get(i)
                pairs = [gi] if gi is not None else []
            for gi in pairs:
                recs.append(_i_rec(_at_days(gi["i_ts"]), gi["i_dur"]))
                n_i += 1
            gj = gap_j.get(i)
            if gj is not None and not pairs:
                recs.append(_j_rec(ts_of(i - 1), ts_of(i),
                                   interval / 86400.0))
                n_j += 1
        ts = ts_of(i)
        l1n = (v[2] / 8.0, v[0] / 8.0, v[1] / 8.0)     # min, max, avg
        ng = (n_[2] / 8.0, n_[0] / 8.0, n_[1] / 8.0)
        fmin = 60.0 + f[2] * FREQ_B
        fmax = 60.0 + f[0] * FREQ_B
        favg = 60.0 + f[1] * FREQ_B
        recs.append(_g_rec(ts, l1n, ng))
        recs.append(_f_rec(ts, (fmin, fmax, favg)))
    for row in sorted(r for r in w_by_row if r >= len(rows)):
        for w in sorted(w_by_row[row], key=lambda x: x["dur_raw"]):
            recs.append(_p_rec(_p_stamp(row, w), w["dur_raw"] / 240.0,
                               w["code"] / 8.0))
    for row in sorted(r for r in d_by_row if r >= len(rows)):
        for ts_d, d in zip(_d_stamp(row, d_by_row[row]),
                           sorted(d_by_row[row], key=lambda x: x["lo"])):
            recs.append(_d_rec(ts_d, d["dur_raw"] / 240.0,
                               d["code"] / 8.0 + _dip_offset(row, d["dur_raw"] / 240.0, d["lo"])))
    # A capture can follow the newest row (nothing closes its interval).
    for row in sorted(r for r in t_by_row if r >= len(rows)):
        for ts_t, tr in zip(_t_stamp(row, t_by_row[row]),
                            sorted(t_by_row[row],
                                   key=lambda t: t["ticks"])):
            recs.append(_t_rec(ts_t, tr["codes"]))
    if stats is not None:
        stats["i_records"] = n_i
        stats["j_records"] = n_j
        stats["dips_suppressed"] = suppressed
    header = _header(interval, len(recs), dip, pico, umbral, vnom, fnom)
    return header + b"".join(recs)


def _header(interval_s: float, count: int, dip: int, pico: int,
            umbral: int, vnom: float, fnom: float) -> bytes:
    """The .fpq fixed header, built to the byte layout in docs/format.md."""
    h = bytearray(b"Fichero FPQ V 4.2" + b"\x00" * 8)
    h += struct.pack("<I", 0x0C) + struct.pack("<I", 0x07) + b"\x00"
    h += b"Tipo=17\x00\x00\x00\x00"
    for ch in range(8):
        end = ";" if ch == 7 else "7"
        # Each channel slot is a fixed 59 bytes: 55 bytes of text + 4 NULs.
        # The text itself is variable-width (1-3 digit thresholds), so pad it
        # instead of relying on the value width (a 2-digit --dip used to
        # shrink the slot and trip the 0x387 assertion).
        slot = (f"Canal= {ch} Dip= {dip} Pico= {pico} Umbral= {umbral} "
                f"Interrupciones{end}").encode()
        h += slot.ljust(55, b"\x00")[:55] + b"\x00" * 4
    h += (b" Me=         0 PT=      1 ST=   1000 PC=      1 "
          b"SC=   1000U") + b"\x00" * 4
    h += (f" VNom={vnom:10.2f} CNom={1.0:10.2f} FNom={fnom:10.2f} "
          f"Topol={2:7d} Idiom={0:7d} NomFa=E").encode() + b"\xc1\x00\x00\x00\x00"
    xml = (b"<confMode>-1</confMode><appMode>0</appMode>"
           b"<measName>0</measName><fw>1.15</fw><ns>X</ns><pm>1</pm>"
           b"<curr></curr><pd>0</pd><mt>0</mt><ne>0</ne><np>0</np><cm>1</cm>")
    h += b" Armon=0 PLVer=4600 " + xml + b" "
    h += b"678901234"
    h += struct.pack("<I", 32) + b"\x00" * 15 + struct.pack("<f", 1.0)
    h += struct.pack("<d", interval_s / 86400.0) + b"\x00" * 8
    h += struct.pack("<I", count)
    if len(h) != 0x387:
        raise AssertionError(f"header layout error: {len(h):#x}")
    return bytes(h)


def _i_rec(ts: float, dur_s: float) -> bytes:
    """One interruption (0x49): event start + duration in OLE days.

    Power Log stores the duration as a difference of two OLE serials,
    `(ts + secs/86400) - ts` (matches 4/5 records bit-exactly; `secs/86400`
    matches 0/5).  `dur_s` is the whole-second 09 -> 0A span.
    """
    r = bytearray(18)
    r[0] = 0x49
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<d", r, 9, (ts + dur_s / 86400.0) - ts)
    r[17] = 0
    return bytes(r)


def _j_rec(prev_ts: float, next_ts: float, interval_days: float) -> bytes:
    """One interval without measurements (0x6A).

    `prev_ts` must be the *same* float the previous G carries and `next_ts`
    the same float as the resuming G; the duration is their difference
    against one interval, in that order (bit-exact on 7/7 fixture records).
    """
    r = bytearray(18)
    r[0] = 0x6A
    struct.pack_into("<d", r, 1, prev_ts)
    struct.pack_into("<d", r, 9, next_ts - (prev_ts + interval_days))
    r[17] = 0
    return bytes(r)


def _g_rec(ts, l1n, ng) -> bytes:
    r = bytearray(77)
    r[0] = 0x47
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<I", r, 9, 8)
    struct.pack_into("<I", r, 13, 1)
    struct.pack_into("<3f", r, 18, *l1n)
    struct.pack_into("<I", r, 40, 1)
    struct.pack_into("<3f", r, 45, *ng)
    return bytes(r)


def _f_rec(ts, f) -> bytes:
    r = bytearray(30)
    r[0] = 0x46
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<I", r, 9, 1)
    struct.pack_into("<I", r, 13, 1)
    struct.pack_into("<3f", r, 18, *f)
    return bytes(r)


def _d_rec(ts, dur_s, volts) -> bytes:
    """One dip event (0x44): timestamp, duration days, voltage, trailer."""
    r = bytearray(22)
    r[0] = 0x44
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<d", r, 9, dur_s / 86400.0)
    struct.pack_into("<f", r, 17, volts)
    return bytes(r)


def _p_rec(ts, dur_s, volts) -> bytes:
    """One swell event (0x50): same layout as a dip."""
    r = bytearray(22)
    r[0] = 0x50
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<d", r, 9, dur_s / 86400.0)
    struct.pack_into("<f", r, 17, volts)
    return bytes(r)


def _t_rec(ts, codes) -> bytes:
    """One transient capture (0x54): 26-byte header + 1,280 float32 + 7.

    Header layout is taken byte-for-byte from Power Log's own save:
    +17 = 8, +21 = 1, +22 = uint32 sample count.  `.fpq` sample values are
    10 x the meter's 12-bit codes.
    """
    if len(codes) != TRANSIENT_SAMPLES:
        raise ValueError(f"transient needs {TRANSIENT_SAMPLES} samples, "
                         f"got {len(codes)}")
    r = bytearray(26 + 4 * TRANSIENT_SAMPLES + 7)
    r[0] = 0x54
    struct.pack_into("<d", r, 1, ts)
    r[17] = 8
    r[21] = 1
    struct.pack_into("<I", r, 22, TRANSIENT_SAMPLES)
    struct.pack_into("<%df" % TRANSIENT_SAMPLES, r, 26,
                     *[c * 10 for c in codes])
    return bytes(r)


def _k_rec(ts, pst, first=False) -> bytes:
    r = bytearray(22)
    r[0] = 0x4B
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<I", r, 9, 1)
    if first:
        r[13:18] = b"\x01\x41\x00\x76\x00"
    else:
        struct.pack_into("<I", r, 13, 1)
    struct.pack_into("<f", r, 18, pst)
    return bytes(r)


def _h_rec(ts, thd) -> bytes:
    r = bytearray(65)
    r[0] = 0x48
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<I", r, 9, 8)
    struct.pack_into("<I", r, 13, 1)
    struct.pack_into("<f", r, 26, thd)
    return bytes(r)


def _l_rec(ts, plt) -> bytes:
    r = bytearray(22)
    r[0] = 0x4C
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<I", r, 9, 1)
    struct.pack_into("<I", r, 13, 1)
    struct.pack_into("<f", r, 18, plt)
    return bytes(r)


def _c5_rec(ts, lanes) -> bytes:
    r = bytearray(1262)
    r[0] = 0x5C
    struct.pack_into("<d", r, 1, ts)
    struct.pack_into("<I", r, 9, 0x401)
    struct.pack_into("<I", r, 13, 0x100)
    # +17 is zero; +19 carries the u16 value 51 in every Power Log file
    # (semantics open). It must NOT be written at +18: a reader that
    # consumes this field then mis-frames the stream, which is why Power Log
    # rejected earlier writer output ("cannot read file").
    struct.pack_into("<H", r, 19, 0x33)
    for j, v in enumerate(lanes):
        struct.pack_into("<f", r, 71 + 24 * j + 8, v)
    return bytes(r)
