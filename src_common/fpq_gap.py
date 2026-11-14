"""Counter/gap model for the VR1710 item stream.

This module is the anchor-free part of the meter timeline: the `06`/`09`/`0A`
counter clock, its phase calibration, the per-row elapsed-time schedule and
the I (interruption) / j (interval without measurements) gap records.

It was split out of fpq_serial_meter.py without behaviour changes; the meter
re-exports every name below so existing callers and guards keep working.
`fpq_wire` stays the shared primitive module (clock constants and the plain
counter function).

Disclaimer: provided "as is", without warranty of any kind, express or
implied, including accuracy or fitness for a particular purpose. Use at your
own risk; the authors accept no liability for any loss or damage. Not
affiliated with or endorsed by Fluke Corporation.
"""
import copy
import math
import statistics
import sys
from datetime import datetime, timedelta

from fpq_wire import (
    counter_clock,
    COUNTER_STEP_MINUTE,
    COUNTER_STEP_HOUR,
    COUNTER_STEP_DAY,
    COUNTER_DAY_TOD,
    COUNTER_HOUR_PHASE_MEASURED,
)

# Item-stream / .fpq constants (docs/serial.md and docs/format.md)
OLE_EPOCH = datetime(1899, 12, 30)

# Counter clock (`06`/`09`/`0A` items, `tt 02 <u32 BE>`).  The value is in
# 1/128 s units but is not a plain clock: the meter adds fixed bookkeeping
# steps that are functions of elapsed recording time.  Exact on every counter
# item of both matched br2 fixtures :
#
#   counter/128 = t + 4*floor((t-p)/60) + 256*floor((t-ph)/3600)
#                 + 32768*floor((t-pd)/86400) + C
#
# `09`/`0A` are the interruption start/end pair of the resuming row.
COUNTER_SCALE = 128
# Counter step constants and the clock come from fpq_wire (shared with replay).
DAY_SECONDS = 86400.0              # wall-clock day (bracket reporting, not counters)
COUNTER_DAY_CAP = 4096             # sanity cap on enumerated daily steps
# Calibration scoring tolerances (seconds).  A bracket whose slack is
# negative beyond the noise floor, or a small positive slack where only a
# real row gap can be, marks a wrong minute/hour phase.
CAL_SLACK_MIN = 1.5
CAL_P_SLACK_MAX = 20.0             # < 1 hour: only a real row gap is this big
CAL_PH_SLACK_MAX = 249.0           # a daily/hour step region, not a row gap
# The recorder's ring holds about 88-89k rows at every supported interval
# (1 s -> ~89,400 rows; 10 s -> ~87,552; the longer intervals are similar).
# A counter jump larger than this many rows cannot be elapsed time inside one
# recording segment: it is the counter base resetting at power-up.  Keep the
# rows and continue at `interval`, but do not add that jump to the timeline.
RING_ROW_CAP = 90_000
# Power Log's ring-start stale block is 28 rows in the 2026-11-01 br1
# power-cycle A/B (the official save dropped exactly the last 28 rows and
# kept the pre-reset segment).  The same 28-row copy appears in the A3b
# wrap fixtures.  Used only when a counter base reset is detected.
RING_STALE_ROWS = 28
# Counter-jump bands, one documented model shared by the reset trim and the
# segmented fallback:
#
#   < 600 s                      an ordinary pause; the global solver handles it
#   600 .. 32768 s               a long outage that can reset the minute/hour
#                                phase -> analyse the stream in segments
#   ~32768 s (daily step)        the meter's own +32768 bookkeeping term; left
#                                to the global solver
#   > interval * RING_ROW_CAP    a power-up base reset -> trim (see
#                                `_reset_suffix_start`)
#
# The upper bound is the daily step, not an independent constant: a jump at
# the daily term is provable from the phase and must not split a segment.
# `_boundary_day_excess()` removes whole +32768 terms first, so a jump that
# is a daily step *plus* a real outage (93_/94_: 56,831 s = 32,768 + 24,063)
# still splits and its gap is solved exactly.
SEGMENT_GAP_MIN = 600.0
SEGMENT_GAP_MAX = COUNTER_STEP_DAY
# One shared tuple for every gap-analysis call site (parse, build re-run):
# a malformed counter stream must take the documented uniform fallback, not
# crash build_fpq with an IndexError/OverflowError.
GAP_ANALYSIS_ERRORS = (ValueError, ZeroDivisionError, OverflowError,
                       IndexError)


# ---------------------------------------------------------------- gap model
def _counter_clock(t: float, p: float, ph: float,
                   pd: float | None = None) -> float:
    """Counter reading (in seconds) at elapsed recorder time `t`."""
    return counter_clock(t, p, ph, pd)


def _forward_elapsed_step(dv: float, ta: float, p: float, ph: float):
    """Elapsed T with the official's forward (floor) step convention.

    `dv` must already have the daily +32768 terms removed.  Iterates
    `T = dv - 4*Nm - 256*Nh` with the boundary counts evaluated over
    `[ta, ta+T]`, which is the same convention the official save uses.
    Returns None if the fixed point does not settle.
    """
    T = float(dv)
    for _ in range(24):
        nm = (math.floor((ta + T - p) / 60.0)
              - math.floor((ta - p) / 60.0))
        nh = (math.floor((ta + T - ph) / 3600.0)
              - math.floor((ta - ph) / 3600.0))
        t2 = dv - COUNTER_STEP_MINUTE * nm - COUNTER_STEP_HOUR * nh
        if abs(t2 - T) < 1e-9:
            return T
        T = t2
    return None


def _solve_forward_day(dv: float, ta: float, p: float, ph: float,
                       pd: float):
    """Smallest D consistent with the data-relative daily phase `pd`.

    Enumerates the day-step count, solves the minute/hour part forward, and
    keeps a candidate only when the day count implied by `[ta, ta+D]` equals
    the number removed.  This is the official's data-relative rule: the
    daily step must be visible in the interval, not supplied by a clock.
    """
    best = None
    for nd in range(0, int(dv // COUNTER_STEP_DAY) + 1):
        T = _forward_elapsed_step(dv - nd * COUNTER_STEP_DAY, ta, p, ph)
        if T is None or T < 0:
            continue
        implied = (math.floor((ta + T - pd) / DAY_SECONDS)
                   - math.floor((ta - pd) / DAY_SECONDS))
        if implied == nd and (best is None or T < best):
            best = T
    return best


def _boundary_day_excess(dv: float, dr: float, interval: float):
    """Smallest daily-step count whose residual is a segment-gap width.

    A forward `06` jump can carry the meter's +32768 s/day bookkeeping term
    in addition to a real outage (93_/94_: a 21,109 s loss read as one
    continuous segment because the raw 56,831 s jump exceeded the
    `SEGMENT_GAP_MAX` cap).  Returns `(nd, residual)`, or `(0, None)` when
    no day count puts the residual in the long-outage band.
    """
    for nd in range(0, int(dv // COUNTER_STEP_DAY) + 1):
        ex = dv - nd * COUNTER_STEP_DAY - dr * interval
        if SEGMENT_GAP_MIN < ex < SEGMENT_GAP_MAX:
            return nd, ex
    return 0, None


def _counter_item_phases(value) -> tuple[float, float] | None:
    """Minute/hour phases implied by one counter reading.

    The reading's own frame puts the bookkeeping steps at whole
    minutes/hours, so inverting it and taking the negative remainder gives
    the phases relative to that reading.  Used to seed a segment's analysis
    instead of the fragile score fit (a short segment cannot fix its hour
    phase, and a wrong phase moves a gap by 240 s).
    """
    v = value / float(COUNTER_SCALE)
    for pd in (0.0, None):
        try:
            t = _solve_elapsed_step(v, 0.0, 0.0, 0.0, pd)
        except GAP_ANALYSIS_ERRORS:
            t = None
        if t is not None:
            return (-t) % 60.0, (-t) % 3600.0
    return None


def _counter_origin_hour_phase(counter_items) -> float | None:
    """Hour phase of the recorder's elapsed origin, decoded from the counter.

    The meter's reading is `t + 4*minute + 256*hour + 32768*day` in its own
    frame.  Inverting the first reading gives the meter's elapsed time at
    the recorder's origin; its remainder modulo 3600 is the hour phase the
    segmented schedule needs.  The day term moves the result by multiples
    of 86,400, so the phase is stable across the two inversions.  Returns
    None when the reading falls inside a bookkeeping step.
    """
    if not counter_items:
        return None
    phases = _counter_item_phases(counter_items[0][2])
    return None if phases is None else phases[1]


def _pd_from_daily(g: dict):
    """Daily-step phase `pd` from the daily brackets visible in the dump.

    A bracket narrower than an hour is a genuine counter day step (a gap
    bracket can be much wider).  Its midpoint is the best data-only estimate;
    any point inside the bracket gives the same day count for normal events.
    Returns None when the dump shows no usable bracket.
    """
    vals = []
    for d in (g.get("daily") or []):
        width = float(d.get("T", 0.0) or 0.0)
        if 0.0 < width < 3600.0:
            vals.append((float(d.get("t_start", 0.0)) + width / 2.0)
                        % DAY_SECONDS)
    if not vals:
        return None
    vals.sort()
    # Keep the largest cluster (brackets should repeat every 86,400 s).
    best = [vals[0]]
    run = [vals[0]]
    for v in vals[1:]:
        if v - run[-1] <= 120.0:
            run.append(v)
        else:
            if len(run) > len(best):
                best = run
            run = [v]
    if len(run) > len(best):
        best = run
    return statistics.median(best)


def _solve_elapsed_step(dv: float, ta: float, p: float, ph: float,
                        pd: float | None):
    """Monotone inversion of one clock step (None when `dv` is skipped).

    `counter_clock` is t plus upward bookkeeping jumps, so its difference
    is strictly increasing: a binary search finds the only candidate T, and
    the exact check rejects a `dv` that fell inside a jump.
    """
    if dv < 0:
        return None

    def diff(T: float) -> float:
        return (_counter_clock(ta + T, p, ph, pd)
                - _counter_clock(ta, p, ph, pd))

    lo, hi = 0.0, float(dv) + 1.0
    while diff(hi) < dv:
        hi = hi * 2.0 + 1.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if diff(mid) >= dv:
            hi = mid
        else:
            lo = mid
    base = int(math.floor(hi))
    for T in range(max(0, base - 2), base + 3):
        if abs(diff(T) - dv) < 1e-6:
            return float(T)
    return None


def _solve_counter_step(dv: float, ta: float, p: float, ph: float,
                        pd: float | None = None) -> float:
    """Elapsed seconds T with clock(ta+T) - clock(ta) == dv.

    Enumerates the plausible daily-step count (`0..dv//32768`) and keeps the
    day phase `pd` in the clock for every candidate, so a gap spanning two
    or more daily +32768 steps is solved exactly.  The fixed-point fallback
    is gone: if no candidate closes the clock exactly (R1) this raises
    ValueError, which callers turn into the uniform-timeline fallback.
    """
    if dv < 0:
        raise ValueError(f"counter step is negative ({dv:g} s)")
    if abs(dv - round(dv)) > 1e-6:
        raise ValueError(f"counter step is not a whole second ({dv!r})")
    dv = float(round(dv))
    # With pd=None the caller has already removed the day steps it
    # identified; the day term is not part of that clock.
    max_nd = 0 if pd is None else min(int(dv // COUNTER_STEP_DAY) + 1,
                                      COUNTER_DAY_CAP)
    for nd in range(max_nd + 1):
        T = _solve_elapsed_step(dv - nd * COUNTER_STEP_DAY,
                                ta, p, ph, pd)
        if T is None:
            continue
        if abs(_counter_clock(ta + T, p, ph, pd)
               - _counter_clock(ta, p, ph, pd) - dv) < 1e-6:
            return T
    raise ValueError(
        f"counter step not solvable exactly: dv={dv:g} s at t={ta:g} s "
        f"(p={p:g}, ph={ph:g}, pd={pd!r})")


def _solve_counter_step_lenient(dv: float, ta: float, p: float,
                                ph: float, pd: float | None = None) -> float:
    """Unverified estimate used ONLY to score phase candidates.

    Calibration deliberately scores wrong phases (where the strict inverted
    clock has no exact step); it keeps the old grid search + fixed-point
    iteration so the phase landscape is unchanged.  Its output is never
    written to a file; the production path is `_solve_counter_step`.
    """
    best = None
    # The offset at `ta` is the same for every candidate; compute it once.
    # This is the hot scoring path (about half of the calibration time on
    # the 67_ profile), and it is read-only.
    base = _counter_clock(ta, p, ph, pd)
    max_dh = min(int(dv // COUNTER_STEP_HOUR) + 1, 512)
    for dh in range(0, max_dh):
        n0 = int((dv - COUNTER_STEP_HOUR * dh) / 64.0)
        for dn in range(max(0, n0 - 4), n0 + 5):
            T = dv - COUNTER_STEP_MINUTE * dn - COUNTER_STEP_HOUR * dh
            if T < 0 or abs(T - round(T)) > 1e-9:
                continue
            if _counter_clock(ta + T, p, ph, pd) - base == dv:
                if best is None or T < best:
                    best = T
    if best is not None:
        return best
    T = max(0.0, dv) * 15.0 / 16.0
    for _ in range(30):
        e = _counter_clock(ta + T, p, ph, pd) - base
        if e == dv:
            return T
        T += (dv - e) * 15.0 / 16.0
    return max(0.0, T)


def _counter_chain(counter_items, p, ph, daily=None, pd=None, strict=True,
                   progress=None):
    """Walk the counter items; yield (row, type, t, T, dv, row_delta).

    `t` is the item's elapsed time (rounded each step like gap_rules.py),
    `T` the elapsed increment, `dv` the raw counter increment in seconds and
    `row_delta` the number of rows since the previous counter item.
    `daily` holds {1-based item index: count} of once-a-day +32768 steps;
    that amount is removed before solving when `pd` is unknown (the
    anchor-independent parse pass).  With `pd` set, `_solve_counter_step`
    enumerates the day steps itself against the clock's day term.
    `strict=False` is for calibration scoring only (see
    `_solve_counter_step_lenient`).
    """
    daily = daily or {}
    solve = _solve_counter_step if strict else _solve_counter_step_lenient
    t = 0.0
    vprev = counter_items[0][2] / float(COUNTER_SCALE)
    prev_row = counter_items[0][0]
    for k, (r, typ, v) in enumerate(counter_items[1:], start=1):
        if progress is not None and k % 500 == 0:
            progress.tick()
        dv = v / float(COUNTER_SCALE) - vprev
        if pd is None:
            T = solve(dv - daily.get(k, 0) * COUNTER_STEP_DAY,
                      t, p, ph, None)
        else:
            T = solve(dv, t, p, ph, pd)
        if abs(T - round(T)) < 1e-6:
            T = float(round(T))
        t = round(t + T, 6)
        yield r, typ, t, T, dv, r - prev_row
        vprev = v / float(COUNTER_SCALE)
        prev_row = r


def _counter_score_p(items, interval, p, tmax=1800.0):
    pen = 0
    prev_t, prev_row = 0.0, items[0][0]
    try:
        for r, typ, t, T, dv, dr in _counter_chain(items, p, 0.0,
                                                   strict=False):
            if t > tmax:
                break
            if typ == 0x06:
                slack = (t - prev_t) - (r - prev_row) * interval
                if (slack < -CAL_SLACK_MIN
                        or CAL_SLACK_MIN < slack < CAL_P_SLACK_MAX):
                    pen += 1
                prev_t, prev_row = t, r
    except ValueError:
        return 10 ** 9
    return pen


def _counter_score_ph(items, interval, p, ph, limit=None, tmax=None):
    """Penalise spurious 0..249 s slack brackets at a candidate hour phase.

    `limit` aborts as soon as the score cannot beat it; the scan only needs
    to know that a candidate is worse than the current best, which makes the
    narrow-phase search affordable.
    """
    pen = 0
    prev_t, prev_row = 0.0, items[0][0]
    try:
        for r, typ, t, T, dv, dr in _counter_chain(items, p, ph,
                                                   strict=False):
            if tmax is not None and t > tmax:
                break
            if typ != 0x06 or prev_row == r:
                continue
            slack = (t - prev_t) - (r - prev_row) * interval
            if (slack < -CAL_SLACK_MIN
                    or CAL_SLACK_MIN < slack < CAL_PH_SLACK_MAX):
                pen += 1
                if limit is not None and pen > limit:
                    return pen
            prev_t, prev_row = t, r
    except ValueError:
        return 10 ** 9
    return pen


def _counter_frac_penalty(items, interval, p, ph):
    """Count non-integral elapsed steps: a wrong minute phase shows up here.

    `p` ties are common (any value in the meter's 1 s window scores 0), but
    an off-by-one phase leaves one bracket at T = 74.99994 s instead of 75 s,
    which later shows as a ~57 us timestamp shift.
    """
    try:
        return sum(1 for _r, _t, _ta, T, _dv, _dr
                   in _counter_chain(items, p, ph, strict=False)
                   if abs(T - round(T)) > 1e-9)
    except ValueError:
        return 10 ** 9


def _search_counter_phase(items, interval, p, progress=None):
    """Find the 3600 s step phase; exact minima are narrow (~+-8 s).

    A coarse 10 s scan alone misses that basin: on the 2 s fixture the
    true phase (3,032 s) scores 3, but its 10 s grid neighbours 3,030 and
    3,060 score 211 and 22, while unrelated phases score 15-21.  So
    refine every one of the 15 best coarse candidates by +-10 s at 1 s
    (the approach that found the phase in the original calibration script).
    Scores abort early once they cannot beat the running best.
    """
    coarse = []
    for ph in range(0, 3600, 10):
        if progress is not None and ph and ph % 600 == 0:
            progress.tick()
        coarse.append((_counter_score_ph(items, interval, p, ph, limit=60), ph))
    coarse.sort()
    near = set()
    for _s, ph0 in coarse[:15]:
        near.update((ph0 + d) % 3600 for d in range(-10, 11))
    scores = []
    best = 10 ** 9
    for ph in sorted(near):
        score = _counter_score_ph(items, interval, p, ph, limit=best)
        scores.append((score, ph))
        if score < best:
            best = score
    scores.sort()
    tied = [ph for s, ph in scores if s == best]
    if len(tied) > 1:
        tied.sort(key=lambda ph: (_counter_frac_penalty(items, interval,
                                                        p, ph), ph))
    return tied[0]


def _strict_chain_ok(items, p, ph):
    """True when every counter step inverts exactly at (p, ph)."""
    try:
        for _ in _counter_chain(items, p, ph):
            pass
    except ValueError:
        return False
    return True


def _calibrate_counter(items, interval, progress=None):
    """Recover (p, ph) from the dump alone (no official data, no anchor).

    The meter's 60 s and 1 h bookkeeping steps can land on a half second;
    with a restart-pair bracket (two rows inside one second) an integer
    hour phase then has no exact step and the whole schedule used to fall
    back to the uniform walk.  When the best integer pair cannot walk the
    dump strictly, the half-second hour phases of every minute-phase tie are
    tried (nearest first) and the first pair whose every counter step
    inverts exactly is used.
    """
    if progress is not None:
        progress.phase("calibrating counter minute phase")
    cand = []
    for k in range(0, 600, 5):
        if progress is not None:
            progress.tick()
        cand.append((_counter_score_p(items, interval, k / 10.0), k / 10.0))
    cand.sort()
    ties = [p for s, p in cand if s == cand[0][0]]
    p0 = ties[0]
    if progress is not None:
        progress.phase("calibrating counter hour phase")
    ph = _search_counter_phase(items, interval, p0, progress=progress)
    # Tie-break the minute phase: prefer the value that keeps every elapsed
    # step on a whole second (5.0/31.0 are edge values of the 0-score tie).
    best_p = p0
    best_frac = _counter_frac_penalty(items, interval, p0, ph)
    for p in ties[1:]:
        frac = _counter_frac_penalty(items, interval, p, ph)
        if frac < best_frac:
            best_p, best_frac = p, frac
    # Refine the hour phase for the chosen minute phase.
    near = [(best_p, (ph + d) % 3600) for d in range(-12, 13)]
    ranked = sorted((_counter_score_ph(items, interval, pv, hv), pv, hv)
                    for pv, hv in near)
    best = ranked[0][0]
    tied = [(pv, hv) for s, pv, hv in ranked if s == best]
    if len(tied) > 1:
        tied.sort(key=lambda x: (_counter_frac_penalty(items, interval,
                                                       x[0], x[1]), x[1]))
    chosen = tied[0]
    if progress is not None:
        progress.phase("checking counter phase candidates")
    if _strict_chain_ok(items, *chosen):
        return chosen
    # A score tie can hide a pair whose strict solver cannot walk the dump
    # (the wrong-phase else-branch of one bracket).  The meter's 60 s and
    # 1 h bookkeeping steps can also fall on a half second: a restart-pair
    # bracket (two rows inside one second) then has no integral step at the
    # integer phase, and the whole schedule falls back to a uniform
    # timeline.  Try the half-second phases of every minute-phase tie,
    # nearest first; failed pairs abort at their first unsolvable bracket.
    offsets = [d / 2.0 for d in (-1, 1, -2, 2)]
    offsets += [d / 2.0 for d in (-3, 3, -4, 4)]
    alt = sorted((_counter_score_ph(items, interval, pv, hv), pv, hv)
                 for pv in ties
                 for hv in ((chosen[1] + d) % 3600.0 for d in offsets)
                 if _strict_chain_ok(items, pv, hv))
    if alt:
        return alt[0][1], alt[0][2]
    # The minute phase can land exactly on a bookkeeping boundary (2026-11-08
    # br1: p=54.0 makes the very first 1 s->15 s step cross the t=54 boundary,
    # so dv=15 cannot invert; p=54.5 walks the whole pre-reset segment).  The
    # score is flat across the whole 1 s window, so the tie-break above can
    # pick the boundary edge.  Before giving up, try the half-second minute
    # phases of every tie plus the chosen phase and the half-second hour
    # offsets.  A wrong pair aborts at its first bracket, so this is cheap.
    rescue = []
    pcs = list(ties) + [(chosen[0] + d) % 60.0
                        for d in (-0.5, -0.4, -0.3, -0.2, -0.1,
                                  0.1, 0.2, 0.3, 0.4, 0.5)]
    hcs = [chosen[1]] + [(chosen[1] + d) % 3600.0 for d in offsets]
    for pv in pcs:
        for hv in hcs:
            if _strict_chain_ok(items, pv, hv):
                rescue.append((_counter_frac_penalty(items, interval, pv, hv),
                               abs(pv - chosen[0]), pv, hv))
    if rescue:
        rescue.sort()
        return rescue[0][2], rescue[0][3]
    return chosen


def _analyse_counter_gaps(counter_items, n_rows, interval, phases=None,
                          pd=None, progress=None):
    """Counter-derived row timeline + gap records (relative seconds).

    Returns a dict:
      rows       per-row elapsed seconds relative to the first counter item
      cum        cumulative gap seconds (row gaps) at each row
      gaps       [{prev, resume, kind, row_gap, dur, i_ts, i_dur, markers}]
      by_resume  {resume_row: gap}
      i_by_row   first 09+0A pair per row (even without a row gap)
      i_pairs    every sequentially paired 09+0A span, per row
      marks      {row: {0x09: t, 0x0A: t}} (last value per type)
      daily      detected +32768 brackets (the anchor confirms their phase)
    `pd` is the elapsed-time offset of the daily step.  The anchor-free
    parse pass leaves it None and removes the day steps it can identify
    heuristically; the writer's second pass supplies it from the anchor so
    `_solve_counter_step` can enumerate every daily step exactly.
    No absolute time is used here: the writer only shifts this timeline so
    the newest row lands one interval before its anchor.
    """
    if phases is None:
        p, ph = _calibrate_counter(counter_items, interval,
                                   progress=progress)
    else:
        p, ph = phases

    if progress is not None:
        progress.phase("solving counter chain")
    chain = list(_counter_chain(counter_items, p, ph, pd=pd,
                                progress=progress))
    daily_counts = {}
    if pd is None:
        # Anchor-free pass: one +32768 step per bracket is all that can be
        # proven from the counter alone.  A bracket with several daily
        # steps is ambiguous without the anchor (it looks like extra elapsed
        # time), so the writer re-solves it with `pd`.
        for k, (r, typ, t, T, dv, dr) in enumerate(chain, start=1):
            if dv > COUNTER_STEP_DAY and T - dr * interval > 20000:
                try:
                    Tt = _solve_counter_step(
                        dv - COUNTER_STEP_DAY, t - T, p, ph)
                except ValueError:
                    continue
                if (0 <= Tt < T - 1000
                        and Tt - dr * interval > -CAL_SLACK_MIN):
                    daily_counts[k] = 1
        if daily_counts:
            chain = list(_counter_chain(counter_items, p, ph,
                                        daily=daily_counts,
                                        progress=progress))

    origin_row = counter_items[0][0]
    times = {origin_row: 0.0}
    mark_seq = {}                   # row -> ordered [(type, elapsed), ...]
    daily_info = []
    for k, (r, typ, t, T, dv, dr) in enumerate(chain, start=1):
        tt = round(t, 6)
        if typ == 0x06:
            times[r] = tt
        else:
            # Keep wire order: several markers can share a row (e.g.
            # 09 -> 0A -> 09), and last-writer-wins used to pair the second
            # 09 with the first 0A and emit a negative-duration I.
            mark_seq.setdefault(r, []).append((typ, tt))
        if pd is None:
            nd = daily_counts.get(k, 0)
        else:
            nd = int(math.floor((t - pd) / DAY_SECONDS)
                     - math.floor((t - T - pd) / DAY_SECONDS))
        if nd:
            daily_info.append({"row": r, "t_start": round(t - T, 6),
                               "T": round(T, 6), "count": nd})

    # Backwards-compatible summary (last occurrence per type) plus the
    # sequentially paired 09/0A spans actually used below.
    marks = {r: {typ: tt for typ, tt in seq}
             for r, seq in mark_seq.items()}
    pairs_by_row = {}
    for r in sorted(mark_seq):
        pending = None
        for typ, tt in mark_seq[r]:
            if typ == 0x09:
                if pending is not None:
                    raise ValueError(
                        f"unpaired interruption start marker at row {r}")
                pending = tt
            else:                       # 0x0A resume marker
                if pending is None:
                    continue            # lone resume marker -> j
                if tt < pending:
                    raise ValueError(
                        f"negative interruption span at row {r}: "
                        f"{tt} < {pending}")
                dur = int(round(tt - pending))
                if dur < 0:
                    raise ValueError(
                        f"negative interruption duration at row {r}: {dur}")
                pairs_by_row.setdefault(r, []).append(
                    {"i_ts": pending, "i_dur": dur})
                pending = None
        if pending is not None:
            raise ValueError(
                f"unpaired interruption start marker at row {r}")

    anchors = sorted((r, t) for r, t in times.items() if r < n_rows)
    if not anchors:
        return None
    rows = [None] * n_rows
    for r, t in anchors:
        rows[r] = t
    first = anchors[0][0]
    for i in range(first - 1, -1, -1):
        rows[i] = rows[first] - (first - i) * interval
    for (a, _ta), (b, _tb) in zip(anchors, anchors[1:]):
        for k in range(a + 1, b):
            rows[k] = rows[a] + (k - a) * interval
    last = anchors[-1][0]
    for i in range(last + 1, n_rows):
        rows[i] = rows[last] + (i - last) * interval

    # Gaps are row-clock gaps between consecutive 06 anchors.  An anchor can
    # sit one row past the kept data (the wrap trim); keep it for analysis.
    all_anchors = sorted(times)
    gaps = []
    by_resume = {}
    for a, b in zip(all_anchors, all_anchors[1:]):
        tb = times[b]
        tprev = (rows[b - 1] if b - 1 < n_rows
                 else times[a] + (b - 1 - a) * interval)
        delta = tb - tprev
        if delta - interval <= 0.5:
            continue
        m = marks.get(b, {})
        pairs = pairs_by_row.get(b, [])
        g = {"prev": b - 1, "resume": b, "delta": round(delta, 6),
             "row_gap": round(delta - interval, 6), "markers": dict(m)}
        if pairs:
            first = pairs[0]
            g["kind"] = "I"
            g["i_ts"] = first["i_ts"]
            g["i_dur"] = first["i_dur"]
            g["dur"] = g["i_dur"]
        else:
            g["kind"] = "j"
            g["dur"] = g["row_gap"]
        gaps.append(g)
        by_resume[b] = g

    i_by_row = {r: ps[0] for r, ps in pairs_by_row.items()}

    cum = []
    run = 0.0
    for i in range(n_rows):
        g = by_resume.get(i)
        if g is not None:
            run += g["row_gap"]
        cum.append(run)

    return {"interval": float(interval), "p": p, "ph": ph, "pd": pd,
            "rows": rows, "cum": cum, "gaps": gaps,
            "by_resume": by_resume, "i_by_row": i_by_row,
            "i_pairs": pairs_by_row,
            "marks": marks, "daily": daily_info,
            "origin_row": origin_row,
            "last_anchor": max(times.items()),
            "counter_end": max((c[2] for c in chain), default=0.0)}


def _analyse_counter_gaps_robust(counter_items, n_rows, interval,
                                 phases=None, pd=None, progress=None):
    """Global counter-gap analysis with a segmented fallback.

    A long marker-less outage can reset the meter's minute/hour counter
    phase.  The global solver then cannot fit one phase across the dump and
    raises; Power Log still writes the pauses.  In that case split the
    counter stream at the large marker-less jumps (> 10 min, below the
    daily +32768 step) and analyse each segment with its own phase, then
    stitch the segments with the measured boundary gaps.
    """
    try:
        g = _analyse_counter_gaps(counter_items, n_rows, interval,
                                  phases=phases, pd=pd,
                                  progress=progress)
        if g is not None:
            return g
    except GAP_ANALYSIS_ERRORS:
        pass
    if progress is not None:
        progress.phase("solving counter gaps")
    return _segmented_counter_gaps(counter_items, n_rows, float(interval),
                                   progress=progress)


def _marker_pairs(counter_items):
    """Pair every `09` interruption-start with the next `0A` resume marker.

    The two markers sit on the resuming row (they are adjacent items in the
    stream), but a power-loss outage can also put them on the last pre-gap
    row.  Pairing by stream order (not by row) is what Power Log's I rule
    follows, so a boundary that carries a pair must be written as `I`, never
    as the `j` of `_segmented_counter_gaps`.  Returns {row: [pair, ...]} in
    stream order; a lone `0A` (serial pause) yields no pair.
    """
    out = {}
    pending = None
    for r, typ, v in counter_items:
        if typ == 0x09:
            pending = (r, v)
        elif typ == 0x0A:
            if pending is None:
                continue                  # lone resume marker -> j
            out.setdefault(r, []).append(
                {"start": pending[0], "i_ts": None,
                 "i_dur": (v - pending[1]) / float(COUNTER_SCALE)})
            pending = None
    return out


def _segmented_counter_gaps(counter_items, n_rows, interval, progress=None):
    """Fallback: per-segment counter analysis + explicit boundary gaps."""
    pairs_by_row = _marker_pairs(counter_items)
    prev = None
    bounds = []
    for r, typ, v in counter_items:
        if typ != 0x06:
            continue
        if prev is not None:
            dr = r - prev[0]
            dv = (v - prev[1]) / float(COUNTER_SCALE)
            _nd, excess = _boundary_day_excess(dv, dr, interval)
            if excess is not None:
                bounds.append(r)
        prev = (r, v)
    # A 09/0A pair is an outage boundary even when its 06 jump is below the
    # long-outage band (the br1 2026-11-05 150 s outage is one).  Do not
    # split every marker row, though: on a session whose interruptions are
    # already inside a longer segment (65_) an extra split changes the
    # proven row timeline.  Only add a marker boundary when the normal
    # segmentation did not already produce a gap at that row.
    base_bounds = sorted({b for b in bounds if 0 < b < n_rows})
    if not base_bounds:
        return None

    def _build_segs(bs, only06=False):
        starts = [0] + bs
        ends = bs + [n_rows]
        out = []
        for seg_no, (a, b) in enumerate(zip(starts, ends), start=1):
            # Announce the fallback once; per-segment calibration phases
            # would print dozens of lines on a day-long outage session.
            if progress is not None:
                progress.tick()
            items = [(r - a, t, v) for r, t, v in counter_items
                     if a <= r < b and (t == 0x06 or not only06)]
            g = None
            if len(items) >= 2:
                try:
                    g = _analyse_counter_gaps(
                        items, b - a, interval,
                        phases=_counter_item_phases(items[0][2]))
                except GAP_ANALYSIS_ERRORS:
                    g = None
                if g is None:
                    try:
                        g = _analyse_counter_gaps(items, b - a, interval)
                    except GAP_ANALYSIS_ERRORS:
                        g = None
            out.append((a, b, g))
        return out

    # Timeline pass: the 09/0A pair is not a clock step, so the row clock is
    # solved from 06 items only.
    segs = _build_segs(base_bounds, only06=True)

    def _internal_segs(ss):
        """All-item analysis only for segments with a pair strictly inside.

        65_ has six such pairs; re-running every segment with all items
        doubled the 65_/67_ runtime for nothing.
        """
        out = []
        for a, b, _g in ss:
            if any(a < r < b for r in pairs_by_row):
                items = [(r - a, t, v) for r, t, v in counter_items
                         if a <= r < b]
                ga = None
                try:
                    ga = _analyse_counter_gaps(
                        items, b - a, interval,
                        phases=_counter_item_phases(items[0][2]))
                except GAP_ANALYSIS_ERRORS:
                    ga = None
                if ga is None:
                    try:
                        ga = _analyse_counter_gaps(items, b - a, interval)
                    except GAP_ANALYSIS_ERRORS:
                        ga = None
                out.append((a, b, ga))
            else:
                out.append((a, b, None))
        return out

    segs_all = _internal_segs(segs)
    # A marker is covered only when the all-item pass actually solved its
    # 09/0A pair.  A 06-only by_resume gap is not enough: br1's final 150 s
    # outage has a gap but the all-item segment failed, and leaving it
    # internal wrote j (or a 152 s I instead of 150).
    covered = set(base_bounds)
    for a, _b, ga in segs_all:
        if ga is not None:
            covered.update(a + r for r in ga.get("i_pairs", {}))
    # A zero/sub-interval I (65_ row 114089) is already represented by the
    # segment's own I pair; splitting there only perturbs the timeline.
    missing = sorted(
        a for a in set(pairs_by_row) - covered
        if max(p["i_dur"] for p in pairs_by_row[a]) > interval + 0.5)
    bounds = sorted(set(base_bounds) | set(missing))
    if missing:
        segs = _build_segs(bounds, only06=True)
        segs_all = _internal_segs(segs)
    if not any(g is not None for _a, _b, g in segs):
        return None

    rows = []
    cum = []
    gaps = []
    by_resume = {}
    i_pairs = {}
    marks = {}
    daily = []
    last_anchor = None
    counter_end = 0.0
    uniform_rows = 0
    offset = 0.0
    cum_offset = 0.0
    prev_start = 0
    prev_g = None
    p = ph = 0.0
    for idx, (a, b, g) in enumerate(segs):
        ga = segs_all[idx][2] if idx < len(segs_all) else None
        gp = None
        if idx > 0:
            # `segs[idx-1][2]` is the immediately previous segment's local
            # end when its analysis succeeded; `prev_g` is the last one that
            # did.  They differ only when a segment failed analysis, in
            # which case `t9` uses a non-adjacent end.  No fixture reaches
            # this; a pre-gap row with a local time is the safer source.
            gp = segs[idx - 1][2] or prev_g
            last06 = max(((r, t, v) for r, t, v in counter_items
                          if prev_start <= r < a and t == 0x06),
                         key=lambda x: x[0], default=None)
            first06 = min(((r, t, v) for r, t, v in counter_items
                           if a <= r < b and t == 0x06),
                          key=lambda x: x[0], default=None)
            row_gap = 0.0
            delta = 0.0
            if last06 is not None and first06 is not None:
                dv = (first06[2] - last06[2]) / float(COUNTER_SCALE)
                dr = first06[0] - last06[0]
                delta = dv
                nd, _excess = _boundary_day_excess(dv, dr, interval)
                cands = []
                if gp is not None:
                    try:
                        ta = gp["rows"][min(last06[0] - prev_start,
                                            len(gp["rows"]) - 1)]
                        cands.append(_solve_counter_step(
                            dv - nd * COUNTER_STEP_DAY,
                            ta, gp["p"], gp["ph"], None))
                    except GAP_ANALYSIS_ERRORS:
                        pass
                if g is not None:
                    # This fallback reuses `nd`, which was solved for the
                    # previous frame; with `ta=0` the current frame has no
                    # better day-term evidence.  No fixture reaches it, and
                    # the duration guards reject a bad value.
                    try:
                        cands.append(_solve_counter_step(
                            dv - nd * COUNTER_STEP_DAY,
                            0.0, g["p"], g["ph"], None))
                    except GAP_ANALYSIS_ERRORS:
                        pass
                if cands:
                    # The previous segment's frame holds the pre-gap clock;
                    # its decoded phase solves the span exactly (48233:
                    # 1,202, not the 1,442 a 47-row fit gave).  Keep the
                    # post-gap frame only as a fallback when the previous
                    # segment's analysis is missing.
                    delta = cands[0]
                row_gap = max(0.0, delta - dr * interval)
            prev_row_time = offset
            pairs = pairs_by_row.get(a)
            if pairs and gp is not None:
                # The 06 step across a power-loss outage can be off by a
                # whole hour/day bookkeeping branch (the 2026-11-05 br1
                # fixture solved one 878 s outage as 29,918 s).  The 09/0A
                # pair is the authoritative span; use it to keep the
                # boundary on the shorter branch.  Try the meter's daily
                # term explicitly: a reset can add one 32768 s step.
                # Recover the two raw values in stream order.
                raw_markers = [(r, t, v) for r, t, v in counter_items
                               if a <= r < b and t in (0x09, 0x0A)]
                if len(raw_markers) >= 2:
                    dv = (raw_markers[1][2] - raw_markers[0][2]) / float(
                        COUNTER_SCALE)
                    # Solve in the previous segment's own frame: its
                    # phases are relative to its first counter item, so the
                    # clock must be its local end, not the stitched global
                    # time (82_: that read the 878 s outage's day step as
                    # 1,118 s).
                    t9 = gp["rows"][-1] + interval
                    cands = []
                    for nd in range(0, 4):
                        try:
                            cand = _solve_counter_step(
                                dv - nd * COUNTER_STEP_DAY,
                                t9, gp["p"], gp["ph"], None)
                        except GAP_ANALYSIS_ERRORS:
                            continue
                        if 0 < cand <= dv:
                            cands.append(cand)
                    if cands:
                        row_gap = min(cands)
            offset += interval + row_gap
            cum_offset += row_gap
            gb = {"prev": a - 1, "resume": a, "delta": round(delta, 6),
                  "row_gap": round(row_gap, 6), "markers": {},
                  "kind": "j", "dur": round(row_gap, 6)}
            pairs = pairs_by_row.get(a)
            if pairs:
                # A 09/0A pair at the resuming row is an interruption, not a
                # recorder pause.  The row gap is the best available width
                # until the marker clock is solved; the marker's own span is
                # carried for the writer/next pass.  Placement is the last
                # pre-gap second (Power Log puts I before the first G at or
                # after the 09 instant).
                first = pairs[0]
                # Keep the I record width equal to the gap actually written
                # in the row timeline.  The raw 09/10 span includes the
                # meter's 4 s/60 s, 256 s/h and 32768 s/day bookkeeping
                # steps, so it is not a duration; using it here would make
                # the grey band disagree with the rows.
                i_dur = int(round(max(0.0, row_gap)))
                if row_gap <= 0.5:
                    row_gap = float(i_dur)
                i_ts = prev_row_time + interval
                first.update({"i_ts": i_ts, "i_dur": i_dur})
                gb.update({"kind": "I", "i_ts": i_ts,
                           "i_dur": i_dur, "dur": i_dur,
                           "i_source": "marker",
                           "markers": {0x09: i_ts,
                                       0x0A: i_ts + i_dur}})
                gb["row_gap"] = round(row_gap, 6)
                gb["delta"] = round(row_gap + (a - (last06[0] if last06
                                                    else a)) * interval, 6)
            gaps.append(gb)
            by_resume[a] = gb
            if pairs:
                i_pairs.setdefault(a, []).extend(pairs)
        if g is None:
            for i in range(b - a):
                rows.append(offset + i * interval)
                cum.append(cum_offset)
            uniform_rows += b - a
            last_anchor = (b - 1, offset + (b - a - 1) * interval)
            offset = rows[-1]
            prev_start, prev_g = a, g
            continue
        if not p:
            p, ph = g["p"], g["ph"]
        for x in g["rows"]:
            rows.append(x + offset)
        for c in g["cum"]:
            cum.append(c + cum_offset)
        for gg in g["gaps"]:
            ng = dict(gg)
            ng["prev"] += a
            ng["resume"] += a
            gaps.append(ng)
            by_resume[ng["resume"]] = ng
        for r, ps in (ga or {}).get("i_pairs", {}).items():
            i_pairs[r + a] = ps
            # The 06-only timeline marks this row as an ordinary gap.  The
            # all-item pass proves it is an I; promote the shared gap dict
            # so the guard and the writer agree (65_ rows 19557/114055/…).
            gb = by_resume.get(r + a)
            if gb is not None and ps:
                gb.update({"kind": "I", "i_ts": ps[0]["i_ts"],
                           "i_dur": ps[0]["i_dur"],
                           "dur": ps[0]["i_dur"],
                           "i_source": "segment"})
        for r, m in (ga or {}).get("marks", {}).items():
            marks[r + a] = m
        for dd in g["daily"]:
            nd = dict(dd)
            nd["row"] += a
            nd["t_start"] += offset
            daily.append(nd)
        last_anchor = (g["last_anchor"][0] + a,
                       g["last_anchor"][1] + offset)
        counter_end = g.get("counter_end", counter_end)
        offset = rows[-1]
        cum_offset = cum[-1] if cum else cum_offset
        prev_start, prev_g = a, g
    if len(rows) != n_rows:
        return None
    # An internal raw 09/0A pair whose all-item segment failed (br1's 150 s
    # outage at the end of a long segment) still proves an I.  The 06-only
    # timeline already has the gap; use its width and the last pre-gap
    # second, and do not leave a j beside a pair.
    for r, gb in by_resume.items():
        if gb.get("kind") != "j" or r not in pairs_by_row:
            continue
        i_dur = int(round(max(0.0, gb.get("row_gap", 0.0))))
        i_ts = rows[r - 1] + interval if 0 < r < len(rows) else 0.0
        pair = dict(pairs_by_row[r][0])
        pair.update({"i_ts": i_ts, "i_dur": i_dur})
        gb.update({"kind": "I", "i_ts": i_ts, "i_dur": i_dur,
                   "dur": i_dur, "i_source": "internal"})
        i_pairs.setdefault(r, [pair])
    i_by_row = {r: ps[0] for r, ps in i_pairs.items()}
    return {"interval": float(interval), "p": p, "ph": ph, "pd": None,
            "rows": rows, "cum": cum, "gaps": gaps,
            "by_resume": by_resume, "i_by_row": i_by_row,
            "i_pairs": i_pairs, "marks": marks, "daily": daily,
            "origin_row": 0, "last_anchor": last_anchor,
            "counter_end": counter_end, "segmented": True,
            "uniform_rows": uniform_rows,
            "segments": [{"a": a, "b": b,
                          "rows": (g["rows"] if g is not None else None),
                          "p": (g["p"] if g is not None else None),
                          "ph": (g["ph"] if g is not None else None)}
                         for a, b, g in segs]}


def _daily_boundary_count(t_end_sod: float, duration: float,
                          max_days: int) -> int:
    """Number of meter daily +32768 steps inside one gap.

    The step sits at `COUNTER_DAY_TOD` local time every 86400 s.  A gap of
    `duration` seconds ending at a time-of-day `t_end_sod` starts at
    `t_end_sod - duration`; count the step positions in that half-open
    interval (endpoints are excluded because a step exactly at a boundary is
    a one-row ambiguity, not a whole day).
    """
    if duration <= 0:
        return 0
    start = (float(t_end_sod) - duration) % DAY_SECONDS
    count = 0
    sod = COUNTER_DAY_TOD
    # First step position at/after the gap start in the same day cycle.
    offset = (sod - start) % DAY_SECONDS
    while offset < duration and count <= max_days:
        count += 1
        offset += DAY_SECONDS
    return count


def _correct_segmented_marker_gaps(g: dict, counter_items, interval: float,
                                   anchor: datetime,
                                   hour_phase: float | None = None,
                                   target_total: float | None = None) -> dict:
    """Re-solve I durations with the absolute minute/hour phases.

    A segmented schedule stitches the row timeline from the `06` clock, but
    the `09`/`0A` pair spans a power-loss reset: its raw difference includes
    the meter's 4 s/minute, 256 s/hour and 32768 s/day bookkeeping steps,
    and a short segment may not span enough boundaries to fix the phase.
    With the anchor we can place the first row in absolute time, derive the
    minute/hour phase, and solve every marker pair in order.  This reduces
    the 2026-11-05 br1 residual from six I durations (max +376 s) to a few
    one-minute-step cases.

    The function is idempotent for an anchor and only touches `g`; the
    caller stores `_marker_anchor`.
    """
    if not g or not g.get("gaps") or not counter_items:
        return g
    # Raw 09 -> 0A pairs, keyed by the resume row (0A's row).
    pmap = {}
    pending = None
    for r, typ, v in counter_items:
        if typ == 0x09:
            pending = (r, v)
        elif typ == 0x0A and pending is not None:
            pmap[r] = (pending[1], v)
            pending = None
    if not pmap:
        return g
    first06 = {}
    for r, typ, v in counter_items:
        if typ == 0x06 and r not in first06:
            first06[r] = v
    rows06 = sorted(first06)

    def next06(row):
        import bisect
        k = bisect.bisect_left(rows06, row)
        return first06[rows06[k]] if k < len(rows06) else None

    PM = g.get("p")
    segs = g.get("segments") or []
    if (PM is None or PM == 0.0) and segs:
        PM = next((s["p"] for s in segs if s.get("p") is not None), PM)
    if PM is None:
        return g
    n = len(g["rows"])
    by_resume = g.get("by_resume") or {}
    gaps = g.get("gaps") or []

    orig_rows = list(g["rows"])
    fallbacks = 0

    def rebuild():
        # Preserve the counter chain's own row deltas (the meter's ~1 s
        # sampling clock has occasional 0 s / 2 s steps at the 06 markers);
        # only the gap rows take the corrected duration.  Re-walking every
        # row at exactly `interval` erased those steps and put 82_ 8 s off.
        out = [orig_rows[0]]
        for i in range(1, n):
            gb = by_resume.get(i)
            if gb is not None:
                step = interval + gb["row_gap"]
            else:
                step = orig_rows[i] - orig_rows[i - 1]
            out.append(out[-1] + step)
        return out

    rows = rebuild()

    def seg_for(row):
        for s in segs:
            if s.get("a") is not None and s["a"] <= row < s.get("b", 0):
                return s
        return None

    def marker_times(row, pair, base_rows):
        """Elapsed times of the 09 and 0A markers for the gap at `row`.

        Uses the gap's segment frame when available, else the schedule's
        global phases.  Their difference is the phase-cancelling span.
        """
        import bisect
        k = bisect.bisect_left(rows06, row)
        if k == 0 or k > len(rows06):
            return None
        l6 = rows06[k - 1]
        s = seg_for(row - 1)
        if s is not None and s.get("rows"):
            kk = l6 - s["a"]
            if not (0 <= kk < len(s["rows"])):
                return None
            ta, pp, phh = s["rows"][kk], s["p"], s["ph"]
        else:
            ta, pp, phh = base_rows[l6], PM, PH
        try:
            t09 = _solve_counter_step(
                (pair[0] - first06[l6]) / float(COUNTER_SCALE),
                ta, pp, phh, None)
            t0a = _solve_counter_step(
                (pair[1] - first06[l6]) / float(COUNTER_SCALE),
                ta, pp, phh, None)
        except GAP_ANALYSIS_ERRORS:
            return None
        return base_rows[l6] + t09, base_rows[l6] + t0a

    def marker_time(row, pair, base_rows):
        mt = marker_times(row, pair, base_rows)
        return None if mt is None else mt[0]

    def gap_step(d, row, pair, base_rows, prev):
        nonlocal fallbacks
        """Row step for a gap: 09 offset + duration + 0A->06 post.

        The official ends the I at the 0A time and resumes the row grid at
        the following 06, so its gap step is `(T09 - prev) + D + post`.
        The old `interval + D` was 1-2 s short (82_'s per-gap residual).
        """
        ta = marker_time(row, pair, base_rows)
        vf = next06(row)
        if ta is None or vf is None or pair is None:
            fallbacks += 1
            return interval + d
        post = (vf - pair[1]) / float(COUNTER_SCALE)
        if post < 0.0:
            fallbacks += 1
            return interval + d
        # The official's I endgap is 1-2 s at 1 s (never the full 0A->06
        # gap; 82_ row 25699 has a 6 s post but a 2 s endgap).
        post = min(post, interval + 1.0)
        return (ta - base_rows[prev]) + d + post

    def reconcile(d, row, pair, base_rows):
        nonlocal fallbacks
        """Exact 09->0A span, keeping the caller's day-branch choice.

        `T0A - T09` cancels the hour-phase error (77_/82_/93_/94_'s ±4 s),
        but a day branch can add a 28,800 s alias.  The caller's `d` picks
        the branch; only a <= 5 s (one minute-step) adjustment is applied.
        """
        mt = marker_times(row, pair, base_rows)
        if mt is None or d is None:
            return d
        base = mt[1] - mt[0]
        if base <= 0:
            return d
        k = round((base - d) / 28800.0)
        cand = base - k * 28800.0
        if k >= 0 and 0 < cand and abs(cand - d) <= 5.0:
            return cand
        fallbacks += 1
        return d

    total = rows[-1] - (n - 1) * interval
    if hour_phase is not None:
        PH = float(hour_phase) % 3600.0
    elif target_total is not None:
        # Start+stop anchors: derive the phase from the anchor, as before.
        first = anchor - timedelta(seconds=interval
                                   + (n - 1) * interval + total)
        sod = (first.hour * 3600 + first.minute * 60 + first.second
               + first.microsecond / 1e6)
        PH = (3600.0 - (sod % 3600.0)) % 3600.0
    else:
        # No start anchor: decode the hour phase from the counter's first
        # reading.  The meter packs the minute/hour/day steps into the
        # counter value, so the recorder-origin phase is recoverable from
        # the dump alone.  The old measured constant was specific to the
        # 82_/77_ recording and put the 93_/94_ I durations off by 240 s.
        decoded = _counter_origin_hour_phase(counter_items)
        PH = ((decoded if decoded is not None
               else COUNTER_HOUR_PHASE_MEASURED) % 3600.0)
    # Daily-step phase from the brackets visible in the dump.  This is the
    # data-relative part of the official rule: the step's position is in the
    # counter, so no clock or start anchor is needed to count it.
    pd_data = _pd_from_daily(g)
    changed_any = False
    ambiguous_daily = False
    for _ in range(12):
        if hour_phase is None and target_total is not None:
            first = anchor - timedelta(seconds=interval
                                       + (n - 1) * interval + total)
            sod = (first.hour * 3600 + first.minute * 60 + first.second
                   + first.microsecond / 1e6)
            PH = (3600.0 - (sod % 3600.0)) % 3600.0
        changed = False
        for gb in gaps:
            r = gb.get("resume")
            if r is None or r <= 0 or r >= n:
                continue
            pair = pmap.get(r)
            if pair is None:
                continue
            v9, v10 = pair
            dv_raw = (v10 - v9) / float(COUNTER_SCALE)
            if (gb.get("i_source") == "segment"
                    and dv_raw <= COUNTER_STEP_DAY):
                continue
            t9 = rows[gb.get("prev", r - 1)] + interval
            vf = next06(r)
            if vf is None:
                continue
            # Do NOT apply a row-gap "placement" correction from the 06
            # chain here.  2026-11-08: the chain gives D + 0..2 s per gap,
            # but its solve does not reproduce the official row grid (chain
            # says 951 s where the official grid says 952 at 82_ ev4), and
            # applying it over-shifted the grid and re-broke the duration
            # rule (82_ 1,787 -> 1,791).  Evidence and the refuting numbers:
            # useful_information/88_timing_residual_2026-11-07/README.md.
            if target_total is None and pd_data is not None:
                # Official's data-relative rule: forward minute/hour count
                # plus the visible daily step.  The official writes the I
                # timestamp at the *previous* row and counts from there
                # (`D = resume - prev - interval`), one interval before the
                # physical span start.  Using `t9` put the count on the other
                # side of a minute boundary (the old +/-4 s cases).
                t_rule = t9 - interval
                dfwd = _solve_forward_day(dv_raw, t_rule, PM, PH, pd_data)
                # A genuine pause can exceed 100,000 s (100_: 102,778 s);
                # the old cap rejected it and kept the short day branch.
                if dfwd is None or not (0 < dfwd <= dv_raw):
                    continue
                dfwd = reconcile(dfwd, r, pmap[r], rows)
                i_dur = int(round(dfwd))
                if (abs(gb.get("row_gap", 0.0) - dfwd) > 1e-6
                        or gb.get("i_dur") != i_dur):
                    changed = True
                gb["row_gap"] = gap_step(dfwd, r, pmap[r], rows,
                                         gb.get("prev", r - 1)) - interval
                gb["i_dur"] = i_dur
                gb["dur"] = i_dur
                gb["i_ts"] = t_rule
                for p in (g.get("i_pairs", {}).get(r) or []):
                    p["i_ts"] = t_rule
                    p["i_dur"] = i_dur
                continue
            cands = []
            cand_days: dict[float, int] = {}
            for nd in range(0, 4):
                try:
                    cand = _solve_counter_step(
                        dv_raw - nd * COUNTER_STEP_DAY, t9, PM, PH, None)
                except GAP_ANALYSIS_ERRORS:
                    continue
                # A genuine pause can exceed the old 100,000 s cap: 100_
                # keeps a 102,778 s outage, and the cap silently picked the
                # next day branch (73,978 s, 8 h short).  The elapsed time
                # cannot exceed the raw counter difference, so use that as
                # the sanity bound instead.
                if not (0 < cand <= dv_raw):
                    continue
                try:
                    d10 = _solve_counter_step(
                        (vf - v10) / float(COUNTER_SCALE), 0.0,
                        PM, PH, None)
                except GAP_ANALYSIS_ERRORS:
                    d10 = None
                cands.append((cand, d10, nd))
                cand_days[cand] = nd
            valid = [c for c, d10, _nd in cands
                     if d10 is not None and -0.5 <= d10 <= interval + 1.5]
            pool = valid or [c for c, _d10, _nd in cands]
            if not pool:
                continue
            if target_total is None:
                # No start anchor: the anchor still fixes the wall time of
                # every row *after* the gap (the counter chain resumes
                # there), so for each candidate count the daily +32768 steps
                # that would fall inside it.  The candidate is consistent
                # only when that count is the number of day steps its raw
                # span implies.  On the 85_ wrapped ring this picks the
                # 44,608 s branch (1 daily step) over 15,808 s (0) and
                # 73,408 s (2), which the old "smallest" rule got wrong.
                # Use the rebuilt local timeline: `rows[-1] - rows[r]` is the
                # elapsed time after this gap and does not change with the
                # candidate duration, so the gap end keeps one wall time
                # while the loop searches the candidates.  (Using the
                # pre-correction schedule shifted the phase on the second
                # pass and collapsed back to the smallest candidate.)
                row_abs = anchor - timedelta(
                    seconds=interval + rows[-1] - rows[r])
                end_sod = (row_abs.hour * 3600 + row_abs.minute * 60
                           + row_abs.second + row_abs.microsecond / 1e6)
                consistent = []
                for cand, nd in cand_days.items():
                    if (0 <= nd <= 3 and
                            _daily_boundary_count(end_sod, cand, 3) == nd):
                        consistent.append(cand)
                if consistent:
                    pool = consistent
                else:
                    # The daily-step count does not discriminate; the
                    # smallest candidate is a guess.  Flag it so the caller
                    # can warn instead of silently shortening the gap.
                    ambiguous_daily = True
            if target_total is not None:
                # With start+stop anchors the total gap sum is known.  The
                # raw 09/0A span can hide one or more 32768 s day steps, and
                # "smallest candidate" picks the over-subtracted branch on a
                # wrapped ring (85_ 44,608 -> 16,048).  Choose the candidate
                # that makes the whole session's gap sum closest to target.
                other = sum(float(gb2.get("row_gap", 0.0))
                            for gb2 in gaps if gb2 is not gb)
                desired = float(target_total) - other
                D = min(pool, key=lambda c: abs(c - desired))
            else:
                D = min(pool)
            D = reconcile(D, r, pmap[r], rows)
            i_dur = int(round(D))
            if abs(gb.get("row_gap", 0.0) - D) > 1e-6:
                changed = True
            gb["row_gap"] = gap_step(D, r, pmap[r], rows,
                                     gb.get("prev", r - 1)) - interval
            gb["i_dur"] = i_dur
            gb["dur"] = i_dur
            gb["i_ts"] = t9
            for p in (g.get("i_pairs", {}).get(r) or []):
                p["i_ts"] = t9
                p["i_dur"] = i_dur
        if changed:
            changed_any = True
        rows = rebuild()
        total = rows[-1] - (n - 1) * interval
        if not changed:
            break
    def place_i_at_marker(base_rows) -> None:
        """Place each I at the 09's own time (the official's placement).

        The duration solve is correct but the I timestamp used to be `t9`
        or `t9 - interval`, which drifts when earlier gaps resize the row
        timeline (77_ placed I 1-19 s into the gap) and leaves segment-local
        times in the global frame (100_'s 9 s/7 s gaps).  Re-anchor only
        `i_ts`; the row schedule and durations are untouched.
        """
        import bisect
        for gb in gaps:
            if gb.get("kind") != "I":
                continue
            r = gb.get("resume")
            pair = pmap.get(r)
            if pair is None:
                continue
            ts = marker_time(r, pair, base_rows)
            if ts is None:
                continue
            # The 09 time can carry the ±4 s hour-phase residual; keep the
            # I inside its own gap (100_ B's 9 s case solved 4 s early).
            lo = base_rows[gb.get("prev", r - 1)]
            hi = base_rows[r] - float(gb.get("i_dur") or 0.0)
            ts = max(lo, min(ts, hi))
            gb["i_ts"] = ts
            for q in (g.get("i_pairs", {}).get(r) or []):
                q["i_ts"] = ts

    g["_c27_fallbacks"] = fallbacks
    g["_marker_phase"] = PH
    if ambiguous_daily:
        g["_day_branch_ambiguous"] = True
    if not changed_any:
        # No I duration changed (65_/67_): keep the original row deltas, but
        # still apply the gap-end rule.  The rebuild preserves every non-gap
        # delta (65_'s ticks do not move) and only adjusts the I rows, which
        # 99_ needs for its 1,866 s gap step.
        grid_changed = False
        for gb in gaps:
            if gb.get("kind") != "I":
                continue
            r = gb.get("resume")
            pair = pmap.get(r)
            if pair is None:
                continue
            if gb.get("i_source") == "segment":
                dv_seg = (pair[1] - pair[0]) / float(COUNTER_SCALE)
                if dv_seg <= COUNTER_STEP_DAY:
                    # The duration loop skips short segment gaps; the row
                    # rule must skip them too (65_'s T placement).
                    continue
            d = float(gb.get("i_dur")
                      if gb.get("i_dur") is not None
                      else gb.get("row_gap", 0.0))
            new_gap = gap_step(d, r, pair, g["rows"],
                               gb.get("prev", r - 1)) - interval
            if abs(new_gap - float(gb.get("row_gap", 0.0))) > 1e-9:
                gb["row_gap"] = new_gap
                grid_changed = True
        # Rebuild unconditionally: the rebuild reproduces the original rows
        # when no gap changed (it preserves every non-gap delta), and it is
        # required whenever the loop already updated a gap's row_gap (99_).
        rows = rebuild()
        g["rows"] = rows
        run = 0.0
        cum = []
        for i in range(n):
            gb = by_resume.get(i)
            if gb is not None:
                run += gb.get("row_gap", 0.0)
            cum.append(run)
        g["cum"] = cum
        g["last_anchor"] = (n - 1, rows[-1])
        place_i_at_marker(g["rows"])
        return g
    g["rows"] = rows
    run = 0.0
    cum = []
    for i in range(n):
        gb = by_resume.get(i)
        if gb is not None:
            run += gb.get("row_gap", 0.0)
        cum.append(run)
    g["cum"] = cum
    g["last_anchor"] = (n - 1, rows[-1])
    place_i_at_marker(rows)
    g["i_by_row"] = {r: ps[0] for r, ps in (g.get("i_pairs") or {}).items()
                     if ps}
    g["_marker_anchor"] = anchor
    return g


def _gap_reference(g: dict, n_rows: int):
    """(row, elapsed) of the newest row the anchor refers to.

    Normally that is the newest kept row.  On a wrapped dump the counter's
    last `06` can sit one or more rows past the kept data (the pre-existing
    ring-wrap trim); the anchor still refers to that newest record, so use
    its counter time instead.  This keeps every kept row's absolute time
    right even when the final G itself is not in the file.
    """
    ref_row, ref_rel = n_rows - 1, g["rows"][-1]
    la = g.get("last_anchor")
    if la is not None and la[0] > ref_row:
        ref_row, ref_rel = la[0], la[1]
    return ref_row, ref_rel


def _pd_for_anchor(g: dict, anchor: datetime, interval: float) -> float:
    """Elapsed offset of the daily +32768 step for this anchor."""
    _ref_row, ref_rel = _gap_reference(g, len(g["rows"]))
    base = (anchor - timedelta(seconds=interval)
            - timedelta(seconds=ref_rel))
    sod = base.hour * 3600 + base.minute * 60 + base.second
    return float((COUNTER_DAY_TOD - sod) % 86400)


def _fast_gap_copy(g: dict) -> dict:
    """Copy just what the marker correction mutates.

    `_correct_segmented_marker_gaps()` rebuilds `rows`/`cum` from the gap
    schedule and only mutates the gap dicts and their `i_pairs`; `segments`
    and the input `rows` are read-only.  A full `deepcopy` therefore spends
    most of its time copying the 100k-row timeline (measured: ~0.08 s per
    candidate, ~390 candidates per anchored build).  Copying the mutable
    parts keeps the hour-phase search fast without changing its result.
    """
    out = dict(g)
    out["gaps"] = copy.deepcopy(g.get("gaps") or [])
    out["by_resume"] = {gb["resume"]: gb for gb in out["gaps"]
                        if gb.get("resume") is not None}
    out["i_pairs"] = copy.deepcopy(g.get("i_pairs") or {})
    return out


def _segment_hour_phase(g: dict, counter_items) -> float | None:
    """Median global hour phase implied by the calibrated long segments.

    A segment's local `ph` is relative to its first `06` item; adding that
    item's global elapsed time gives the session-wide phase.  Only segments
    longer than ~50 min (so they contain a real hour step) contribute.
    """
    rows = g.get("rows") or []
    if not rows:
        return None
    first06 = {}
    for r, typ, v in counter_items:
        if typ == 0x06 and r not in first06:
            first06[r] = v
    vals = []
    for seg in g.get("segments") or []:
        a, b, ph = seg.get("a"), seg.get("b"), seg.get("ph")
        if a is None or b is None or not ph or b - a < 3000:
            continue
        near = [r for r in first06 if a <= r < b]
        if not near:
            continue
        r0 = min(near)
        if r0 >= len(rows):
            continue
        vals.append((float(ph) + rows[r0]) % 3600.0)
    return statistics.median(vals) if vals else None


def _auto_hour_phase(g: dict, counter_items, interval: float,
                     anchor: datetime, start_anchor: datetime) -> float:
    """Choose the session hour phase from start/stop anchors + segments.

    The counter equation has a 240 s hour-step ambiguity that the stop
    anchor alone cannot break.  With the recording **start** wall time the
    total timeline is known, so the candidate phases whose total gap sum
    matches `stop - start` form a narrow plateau; the segment-calibrated
    phase (median of the long segments) picks the physically consistent
    point inside it.  82_/77_ then resolve to 2886 s, not the old 2124
    step-count value.
    """
    n = len(g.get("rows") or [])
    if n < 2:
        return 0.0
    target = (anchor - start_anchor).total_seconds() - (n - 1) * interval

    def total(ph: float) -> float:
        # The correction's "nothing changed" early return keeps the copied
        # timeline, so the copy must carry real `rows` (not placeholders):
        # zeroed rows made every candidate but one look catastrophic (77_).
        gg = _fast_gap_copy(g)
        _correct_segmented_marker_gaps(gg, counter_items, float(interval),
                                       anchor, hour_phase=float(ph),
                                       target_total=target)
        return gg["rows"][-1] - (n - 1) * interval

    best = None
    for ph in range(0, 3600, 10):
        d = abs(total(ph) - target)
        if best is None or d < best[0]:
            best = (d, ph)
    for ph in range(best[1] - 15, best[1] + 16):
        p = ph % 3600
        d = abs(total(p) - target)
        if d < best[0]:
            best = (d, p)
    gg = _fast_gap_copy(g)
    _correct_segmented_marker_gaps(gg, counter_items, float(interval),
                                   anchor, hour_phase=float(best[1]),
                                   target_total=target)
    # The counter's first reading fixes the phase directly.  Prefer it when
    # it matches the anchor total at least as well as the grid best; the
    # segment median is only a fallback (77_: the median drifted to 3200
    # after the per-segment decode, while the decoded 2893 reproduces
    # 18/19 exact).
    decoded = _counter_origin_hour_phase(counter_items)
    if decoded is not None:
        p = round(decoded) % 3600
        if abs(total(p) - target) <= best[0] + 1.0:
            return float(p)
    med = _segment_hour_phase(gg, counter_items)
    if med is not None:
        p = round(med) % 3600
        if abs(total(p) - target) <= best[0] + 1.0:
            return float(p)
    return float(best[1])


def _ensure_gap_schedule(items: dict, interval: float,
                         anchor: datetime | None = None,
                         hour_phase: float | None = None,
                         start_anchor: datetime | None = None):
    """Return items['gap'] for `interval`, reusing the calibrated phases.

    With an `anchor`, re-solve the day steps against the clock's daily term
    (`pd`), iterating until the phase is stable; that is what makes a gap
    spanning two or more daily steps exact.  Without one (parse_dump) the
    relative timeline stays anchor-free and only the provable single day
    step per bracket is removed.
    """
    g = items.get("gap")
    same = g is not None and g.get("interval") == float(interval)
    counter_items = items.get("counter_items") or []

    def _apply_marker_correction(sched: dict) -> None:
        """Duration/phase correction shared by both schedule paths."""
        auto_phase = hour_phase
        if auto_phase is None and start_anchor is not None:
            auto_phase = _auto_hour_phase(sched, counter_items,
                                          float(interval), anchor,
                                          start_anchor)
            sched["_auto_hour_phase"] = auto_phase
        target = None
        if start_anchor is not None:
            target = ((anchor - start_anchor).total_seconds()
                      - (len(sched["rows"]) - 1) * float(interval))
        _correct_segmented_marker_gaps(sched, counter_items,
                                       float(interval), anchor,
                                       hour_phase=auto_phase,
                                       target_total=target)
        sched["_marker_hour_phase"] = auto_phase
        sched["_marker_start_anchor"] = start_anchor
        sched["_marker_anchor"] = anchor
    if same and g.get("segmented"):
        # The segmented schedule already stitched the per-segment phases;
        # its relative timeline is valid with any anchor.  Do not re-solve
        # the global day phase, which is exactly what failed.  With an
        # anchor, apply the marker-duration correction once.
        if anchor is not None:
            auto_phase = hour_phase
            if auto_phase is None and start_anchor is not None:
                auto_phase = _auto_hour_phase(g, counter_items,
                                              float(interval), anchor,
                                              start_anchor)
                g["_auto_hour_phase"] = auto_phase
            if (g.get("_marker_anchor") != anchor
                    or g.get("_marker_hour_phase") != auto_phase
                    or g.get("_marker_start_anchor") != start_anchor):
                _apply_marker_correction(g)
                g["_pd_anchor"] = anchor
        return g
    if same and (anchor is None or g.get("_pd_anchor") == anchor):
        return g
    if (len(counter_items) < 2 or not items.get("rows")
            or not any(t == 0x06 for _r, t, _v in counter_items)):
        return None
    phases = (g["p"], g["ph"]) if same else None
    try:
        if anchor is None:
            g = _analyse_counter_gaps_robust(
                counter_items, len(items["rows"]), float(interval),
                phases, pd=None)
        else:
            prelim = (g if same else
                      _analyse_counter_gaps_robust(
                          counter_items, len(items["rows"]),
                          float(interval), phases, pd=None))
            if prelim is None:
                return None
            if prelim.get("segmented"):
                prelim["_pd_anchor"] = anchor
                items["gap"] = prelim
                # The re-analysed schedule needs the same duration/phase
                # correction as the cached one (100_ order B otherwise
                # keeps the uncorrected 131,578 s branch).
                _apply_marker_correction(prelim)
                return prelim
            phases = (prelim["p"], prelim["ph"])
            pd = _pd_for_anchor(prelim, anchor, interval)
            g = prelim
            for _ in range(3):
                try:
                    g2 = _analyse_counter_gaps(
                        counter_items, len(items["rows"]),
                        float(interval), phases, pd=pd)
                except GAP_ANALYSIS_ERRORS:
                    # The anchor-fixed daily phase cannot close a gap that
                    # crosses the meter's day term (for example a long
                    # power-off).  Keep the anchor-free relative schedule,
                    # which already removed the provable day steps; the
                    # anchor check below will report the phase mismatch.
                    g2 = None
                if g2 is None:
                    break
                new_pd = _pd_for_anchor(g2, anchor, interval)
                g = g2
                if new_pd == pd:
                    break
                pd = new_pd
            if g is not None:
                g["pd"] = pd
                g["_pd_anchor"] = anchor
    except GAP_ANALYSIS_ERRORS:
        g = None
    if g is not None and anchor is not None:
        # A marker gap wider than the recorder's row capacity cannot be a
        # normal pause: it is the >25 h O7 class, where the global chain can
        # choose the wrong daily branch (100_ order B: 131,578 vs 102,778).
        # Apply the marker correction only to that class; applying it to
        # every non-segmented schedule shifted 57_ by 5,181 s.
        wide = any((gb.get("row_gap") or 0.0)
                   > RING_ROW_CAP * float(interval)
                   for gb in (g.get("gaps") or []))
        if wide and (g.get("_marker_anchor") != anchor
                or g.get("_marker_hour_phase") != hour_phase
                or g.get("_marker_start_anchor") != start_anchor):
            _apply_marker_correction(g)
    items["gap"] = g
    return g


def _gap_row_bytes(items: dict, anchor: datetime, interval: float,
                   hour_phase: float | None = None,
                   start_anchor: datetime | None = None):
    """Gap-aware row timestamps as OLE days, plus the t=0 absolute datetime.

    Returns (ts, base, gap) with ts[i] the exact float used for row i's G/F
    (and copied verbatim into a `j`), base the wall time of t=0, and gap the
    schedule.  None when the dump carries no counter/gap evidence (the caller
    then keeps the old uniform walk).
    """
    g = _ensure_gap_schedule(items, interval, anchor,
                             hour_phase=hour_phase,
                             start_anchor=start_anchor)
    if g and g.get("_c27_fallbacks"):
        print("warning: the C27r marker fix fell back on "
              f"{g['_c27_fallbacks']} gap(s); the I duration/placement or "
              "row grid may be approximate", file=sys.stderr)
    if not g or not g.get("rows"):
        return None
    rel = g["rows"]
    _ref_row, ref_rel = _gap_reference(g, len(rel))
    last = anchor - timedelta(seconds=interval)
    base = last - timedelta(seconds=ref_rel)
    ts = [(last + timedelta(seconds=(x - ref_rel)) - OLE_EPOCH).total_seconds()
          / 86400.0 for x in rel]
    return ts, base, g


def _anchor_pd_check(g: dict, anchor: datetime, interval: float) -> bool:
    """Place the daily rollover from the anchor's local time-of-day.

    The dump alone fixes the *relative* timeline (the +32768 brackets are
    visible in the counter).  The anchor supplies the absolute wall clock, so
    every daily boundary must land in one of the detected brackets -- a
    useful guard against a wrong anchor or a mis-read daily step.
    """
    if not g or not g.get("rows"):
        return False
    pd = _pd_for_anchor(g, anchor, interval)
    ok = True
    total = 0
    for d in g.get("daily", []):
        count = int(d.get("count", 1))
        total += count
        expected = int(math.floor(
            (d["t_start"] + d["T"] - pd) / DAY_SECONDS)
            - math.floor((d["t_start"] - pd) / DAY_SECONDS))
        if expected != count:
            ok = False
    end = g.get("counter_end", 0.0)
    n = int(math.floor((end - pd) / DAY_SECONDS)
            - math.floor((0.0 - pd) / DAY_SECONDS))
    if n != total:
        ok = False
    g["pd"] = pd
    g["anchor_ok"] = ok
    return ok


def row_time_bounds(items: dict, anchor: datetime, interval: float):
    """(first, last) datetime of the rows actually written to the .fpq."""
    got = _gap_row_bytes(items, anchor, interval)
    last = anchor - timedelta(seconds=interval)
    if got is None:
        return last - timedelta(seconds=interval * (len(items["rows"]) - 1)), last
    _ts, base, g = got
    return (base + timedelta(seconds=g["rows"][0]),
            base + timedelta(seconds=g["rows"][-1]))


def _stale_suffix_start(counters, rows):
    """Earliest row of a stale ring copy at the end of the item stream.

    A dump read from a wrapped ring can end with content that is not the
    newest segment: a partially overwritten page repeats rows from a few
    dozen rows earlier, followed by the start of the ring again.  The
    counter proves the copy -- the `06` clock is strictly increasing, so an
    exact repeat of a counter value can only be stale content.  The row
    values pin the boundary: the stale block can start before its first
    counter item, so the duplicated row run is extended backwards.

    Returns the first stale row, or None when no copy is found.  The caller
    must have a CRC-verified page grid: on a lossy capture a repeated
    counter can also be a dropped byte.
    """
    best = None
    seen = {}
    n = len(rows)
    for r, typ, v in counters:
        # Only the `06` clock is strictly increasing.  The `09`/`0A`
        # pause/resume markers legitimately repeat a nearby clock value
        # (and can repeat it exactly), so admitting them here can trim a
        # real paused session whenever four steady rows also repeat.
        if typ != 0x06:
            continue
        r1 = seen.get(v)
        seen[v] = r
        if r1 is None:
            continue
        off = r - r1
        if off <= 0 or r >= n or rows[r] != rows[r1]:
            continue
        start = r
        while start - 1 - off >= 0 and rows[start - 1] == rows[start - 1 - off]:
            start -= 1
        forward = 1
        while r + forward < n and rows[r + forward] == rows[r + forward - off]:
            forward += 1
        # A single repeated row is not proof; require a short run so a
        # dropped/duplicated item cannot trim real data.
        if forward >= 4 and (best is None or start < best):
            best = start
    return best


def _duplicate_suffix_start(rows, max_period=512, min_run=3):
    """Start of a short stale copy immediately before a reset jump.

    The post-reset ring-start block can be smaller than one page and carry
    no `06` item of its own; the only proof is that its rows replay the rows
    one ring period earlier.  In the 2026-11-06 br1 capture the final five
    rows replay rows 28 rows earlier, and Power Log keeps everything before
    them.  Compare backwards from the penultimate row (the reset `06` sits
    on the last row) and return the earliest row of the longest replay.
    """
    n = len(rows)
    if n < min_run + 2:
        return None
    best = None
    for off in range(1, min(max_period, n - 1) + 1):
        length = 0
        i = n - 2
        while i - off >= 0 and rows[i] == rows[i - off]:
            length += 1
            i -= 1
        if length < min_run:
            continue
        start = n - 1 - length
        if (best is None or length > best[0]
                or (length == best[0] and start < best[1])):
            best = (length, start, off)
    return None if best is None else best[1]


def _reset_suffix_start(counter_items, n_rows, interval, rows=None):
    """Row index where Power Log cuts a post-reboot counter-base reset.

    A forward `06` jump larger than the ring's row capacity cannot be
    elapsed time inside one segment; it is the counter base restarting at
    power-up.  Power Log keeps the pre-reset segment and drops the new
    segment's ring-start block.

    The block is identified by content, not by the dump's 1 KB page grid:
    the 2026-11-01 br1 A/B block is the last 28 rows (`RING_STALE_ROWS`),
    and the 2026-11-06 br1 capture's 5-row tail is a replay of rows 28
    earlier, which `_duplicate_suffix_start()` finds.  Cutting at the
    enclosing 1 KB page instead was tried and reverted: on 57_ it removed
    52 rows where Power Log keeps 1,818 (the page starts 26 rows early).

    Returns None when no forward jump exceeds the capacity.
    """
    max_jump = interval * RING_ROW_CAP
    prev = None
    jump = None
    back = None
    for r, typ, v in counter_items:
        if typ != 0x06:
            continue
        if prev is not None:
            delta = (v - prev[1]) / float(COUNTER_SCALE)
            if delta > max_jump:
                jump = (r, v)
                break
            # A short tail whose counter base is far *below* the pre-reset
            # value is the other power-cycle shape (the meter reboots to a
            # new, smaller base; 2026-11-08 br1 row 2628: 5,046,982 ->
            # 4,589,662).  A genuine ring wrap replays older rows, so the
            # tail values must duplicate rows earlier in the dump; when they
            # do not, the tail is the new segment's ring-start block, which
            # Power Log drops exactly like the forward-reset block.
            # Only a ring-start-block-sized tail is the reboot shape Power
            # Log drops.  A longer post-reset segment is a genuine pause:
            # 57_ has 26 rows (<= 28, trimmed), the 2026-11-08 br1 case 10
            # rows (trimmed), but 100_ keeps all 425 rows after a 28.5 h
            # outage as a 102,778 s I.  The old 512-row bound trimmed 100_.
            if (delta < -max_jump and n_rows - r <= RING_STALE_ROWS
                    and (rows is None
                         or _duplicate_suffix_start(rows) is None)):
                back = (r, v)
        prev = (r, v)
    if jump is None:
        return None if back is None else back[0]
    r, v = jump
    if rows and n_rows - r <= 512:
        duplicate = _duplicate_suffix_start(rows)
        if duplicate is not None and 0 <= duplicate <= r:
            return duplicate
    # A 09/0A pair on the jump row is the meter's own interruption marker:
    # the counter advanced across a genuine pause, it did not reset.  100_
    # is the case (182,338 s forward jump, pair at the resume row; Power Log
    # keeps all 425 post-outage rows and writes one 102,778 s I).  57_'s
    # reset row has no pair, so it still trims its 28-row block.
    if any(rr == r and typ in (0x09, 0x0A)
           for rr, typ, _v in counter_items):
        return None
    tail = n_rows - r
    if tail <= RING_STALE_ROWS:
        return max(0, n_rows - RING_STALE_ROWS)
    return r


def _wrap_cut(counters, rows, k):
    """Row index where the older ring segment starts after the k-th counter.

    The provable boundary is the first counter of the stale segment
    (`counters[k][0]`), but on a replay of older ring content the stale
    block can start a few rows before its first counter item.  The counter
    values prove the copy: the first stale value re-occurs earlier in the
    dump.  When that happens, the duplicated row run pins the exact start,
    and only a start between the last pre-jump counter and the first stale
    counter is accepted.  Without such a duplicate (e.g. meter2's stale
    segment predates the dump window) the counter boundary is used.
    """
    stale_row, stale_val = counters[k]
    earlier = [r for r, v in counters[:k] if v == stale_val]
    if earlier:
        off = stale_row - earlier[-1]
        if (0 < off < stale_row and stale_row < len(rows)
                and rows[stale_row] == rows[stale_row - off]):
            start = stale_row
            while (start - 1 - off >= 0
                   and rows[start - 1] == rows[start - 1 - off]):
                start -= 1
            forward = 1
            while (stale_row + forward < len(rows)
                   and rows[stale_row + forward]
                   == rows[stale_row + forward - off]):
                forward += 1
            if (counters[k - 1][0] < start <= stale_row and forward >= 4):
                return start
    return stale_row
