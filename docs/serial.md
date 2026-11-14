# VR1710 serial protocol (reverse-engineered)

> **Disclaimer:** This document describes reverse-engineered behaviour and is
> provided **as is**, without warranty of any kind, express or implied,
> including accuracy or fitness for any purpose. Use at your own risk; the
> authors accept no liability for any loss or damage. Not affiliated with or
> endorsed by Fluke Corporation; "Fluke", "VR1710" and "Power Log" are
> trademarks of their owner. This is not a calibrated instrument or safety
> device. See the [README disclaimer](../README.md#disclaimer).
>
> **License:** MIT License (see [LICENSE](../LICENSE)).
>
> **Limitations:**
> - 50 Hz operation is unsupported and unverified (60 Hz only); the frequency
>   codec, D/P duration unit and replay generator are 60 Hz-derived.
> - High-range/208 V event offsets are derived or interpolated, not
>   raw-captured; the official supported minimum is 70 V (below is
>   best-effort).
> - Byte-exact parity holds only where the guards/evidence reach.
> - Hardware operations (dump, erase, resume, configure) are at your own risk.

Verified against captured Power Log Classic 4.60 sessions with the recorder
on a CP2102 USB/UART bridge at **500 000 baud, 8-N-1, no flow control**.
Everything here is data-derived. Confidence tags: [verified] = seen directly
in the captures; [fact] = same meaning, kept in older entries; [theory] =
best current hypothesis; [inferred] = fitted to the captures; [open] =
behaviour not yet understood.

## Link and session start

Power Log opens the port and immediately sends a wake-up:

    " . . "   (0x20 2E 20 2E 20)          [verified, at connect]

Between commands it emits single `?` heartbeat polls; the device echoes each
`?`. Replies arrive fast (single-digit milliseconds through the bridge).

## Command set

Commands are ASCII characters sent singularly. Two-phase commands echo the
command char, wait for an argument char, then reply. A record:

| Tx      | Reply                                  | Meaning | Status |
|---|---|---|---|
| `I`     | `B0 30 B0` (3 bytes, binary)           | status/state | [verified], meaning [open] |
| `J`     | `005` (decimal)                        | counter | [verified], meaning [open] |
| `W` + `Q` | `007` (decimal)                      | query, arg `Q` | [verified], meaning [open] |
| `i` + `1` | `198`                                | **Dip threshold, channel** (matches the .fpq `Dip=` field) | [verified] |
| `j` + `1` | `218`                                | **Swell threshold** (the `.fpq` header spells the field `Pico=`) | [verified] |
| `r` + `1` | `100`                                | **Umbral threshold** (matches .fpq `Umbral=`) | [verified] |
| `C` + `Q` | `003` (large session: `004:008:000 017/003/001`-style info followed) | query, arg `Q` | [verified], meaning [open] |
| `R`     | `001:002:011 017/002/000` (large session: different numbers) | record-count info string | [verified], layout [open] |
| `X` + `0` | `\r\nFLUKE 1710 V1.15 25/06/2009\r\n` | **banner**: model, firmware, date | [verified] |
| `V`     | `4E 00 00 00` older capture; live 2026-10-24: `d8 88 0b 00` = LE 755 928, equal to the `v` reply | memory-dump preamble **= the meter's `v` counter**; send it packed as `?V` (bare `V` can lose the first byte) | [verified] |
| `C`(again) | very large binary                   | **memory-dump body** (see below) | [verified] |
| `v`     | `000078` (7-digit decimal; large session: different) | ? — appeared right before Power Log's second pass | [verified], meaning [open] |
| echo behaviour | `? W C X i j r` echoed; `I J R V v` not (as observed). The dump-starting `C` is *not* echoed — the meter streams its reply immediately | | [verified] |

## The download: `V` + `C` + `0x06` flow control  [verified]

Power Log's download sequence is:

1. `V` → replies immediately with 4 bytes: the meter's own `v` counter
   (memory-write counter), little-endian. Live 2026-10-24: `v` = 755 928
   and the `V` reply was `d8 88 0b 00` = 755 928 exactly. Send it packed
   as `?V` (the same heartbeat form used for every other command): a bare
   `V` sometimes loses the first reply byte on live units.
   **`v` is a memory-write counter, not a record count.** The ring is
   2^21 = 2,097,152 write units; Power Log's fill is
   `min(v, 2^21) / 2^21` (calibrated 2026-11-10: v = 1,092,192 at 2 s →
   52.07%). The per-interval "Typical Continuous Measurement" is the
   record capacity for display (2 s → 89,220 records, ~23.5 units per
   record), not the denominator for the percentage.
2. `C` → the device starts streaming a large binary reply (no echo of the
   `C`).
3. The device-side pacing is **ACK-then-block, one ACK per page**: the
   meter sends one complete 1,029-byte page (footer + 1,024 payload + CRC)
   and waits for the host's `0x06` before the next page. In the healthy
   capture there are 686 `0x06` ACKs for 686 pages. Unpaced, the same
   transfer needs only ~14 s at 500 kbaud; the real session took ~154 s, so
   the handshaking is genuine flow-control.
4. `0x15` is the page verdict **NAK**: the host rejected that page and
   expects it again. A wrong page CRC produces exactly this (verified
   2026-10-25: pages with zeroed CRCs were NAKed, pages with the real CRC
   were ACKed with `0x06`). The replay resends the page on `0x15`.
5. A final `0x06` (after the last page) gets `0x04` back — that closes the
   dump; the host answers with one more `0x06`.

Host-side implementation note [verified from the captures]: the `0x04`
EOT is only distinguishable from dump data by context — blocks are raw
binary and a block may legitimately *start* with a `0x04` byte. The meter
never sends anything after a true EOT, and always continues immediately
after a non-final ACK, so the receiver probes: on a `04`, wait for more
data; if bytes follow, it was data (keep pumping `0x06`); if the line
stays silent, the dump is complete. `fpq_serial_meter.py` implements
exactly this. Right-capture tail quirk: the captured dump stream ends
mid-item (3 stray bytes before EOT), so a replay's stream need not end on
an item boundary.

Page-boundary ACK (the default, `--page-ack`) [verified on br2
2026-11-01]: the host ACKs as soon as the 1,029th byte of a page arrives
instead of waiting for line silence; `--no-page-ack` restores the older
quiet-window path. The next page starts immediately, so the byte read back
after an ACK is that page's first byte and must be counted; otherwise every
later page is one byte short and the boundary never fires. The real meter
can also append the closing `0x04` to the final page, leaving the following
ACK with no reply: a lone `0x04` past a whole page grid is the EOT. Measured
on a full 1 s ring: **0.1546 s/page** page-ack vs **0.4803 s/page**
quiet-window, byte-identical dumps (C12 retest).

In one capture the dump finished and Power Log immediately re-ran the whole
identification pass (`X 0`, `I`, `J`, `v`, thresholds) before the file got
written.

### Dump format — item grammar and record mapping [verified]

The dump is **not** the `.fpq` byte stream: no fragment of the resulting
`.fpq` appears verbatim in the wire data. It is a compact native stream that
decodes as a sequence of fixed **6-byte items**. For item bytes `b[0..5]`:

    type  = b[0]
    group = b[1] >> 4
    v1 = ((b[1] & 0x0F) << 8) | b[2]
    v2 =  (b[3] << 4) | (b[4] >> 4)
    v3 = ((b[4] & 0x0F) << 8) | b[5]

Each of `v1, v2, v3` is a **12-bit two's-complement** value (subtract 4096
when ≥ 2048). Three item kinds carry the half-cycle RMS records, each with
`(v1, v2, v3) = (max, avg, min)`:

| item | meaning |
|---|---|
| `type 0x12`, `group 0` | L1N voltage item |
| `type 0x12`, `group 1` | N-G voltage item |
| `type 0x17` | frequency item |

A normal frame is 18 bytes and holds exactly these three items, in the order
frequency, voltage, neutral-ground. The item stream is therefore
`… V N F V N F …` — a voltage/NG pair immediately followed by its
frequency item — and **each (V, N, F) triple maps 1:1, in order, onto one
`.fpq` G record plus its F record**. The `.fpq` floats are rebuilt from the
12-bit codes exactly:

| item | `.fpq` record fields | conversion |
|---|---|---|
| L1N / N-G item | G min `@18`, max `@22`, avg `@26` and N-G min `@45`, max `@49`, avg `@53` | `float32(code / 8)` volts |
| frequency item | F min `@18`, max `@22`, avg `@26` | `float32(60.0 + code * 1.1106768e-3)` Hz |

The voltage resolution is 1/8 V; the frequency resolution is about
1/900.35 Hz (`1.1106768e-3`). The frequency constant is fixed (the same for
every capture tested) and reproduces every stored frequency float byte for
byte.

**50 Hz is a documented limitation, not a pending fixture.** The codec
above was derived from 60 Hz sessions. At 50 Hz the quarter-cycle duration
is 1/200 s (not 1/240), the D/P duration unit and the replay frequency
generator are only validated at 60 Hz, and `fpq_serial_replay.py` refuses a
50 Hz median. No 50 Hz supply is available here (the local mains is 60 Hz
and 50 Hz is not possible in this region), so a 50 Hz fixture cannot be
collected. Decoding a 50 Hz `.fpq` may still display, but meter-side replay,
event durations and derived frequency must be treated as unverified; do not
rely on this tool on 50 Hz power.

Frames are variable-length in real captures: a dropped byte shortens a
frame and an extra item lengthens it, so a decoder must resynchronise on the
item grammar rather than trust a fixed stride. Extra items seen so far:

- `type 0x06`, `0x09`, `0x0A`, payload `02 <4 bytes>` — the counter family
  (big-endian, always a multiple of 64). **Model (2026-10-27):** between
  two periodic counters the value advances by
  `128 * seconds + 512 * (counter items)`; at the observed ~70 s item
  cadence that is the familiar ~135.3 units/s. On top of that the counter
  takes **+2^15 = 32,768 once per 3,600 s** and **+2^22 = 4,194,304 once
  per day at ~17:43 local**. The old "4.23 M daily jump" is
  `2^22 + one hourly step + one normal pair`; the old "135.3 vs 145.5"
  split is the same model seen over a short vs a long window
  (135.3 + 32,768/3,600 = 144.4). Validation against Power Log's own gap
  durations: +-0.5 % on multi-hour gaps, and about +one counter cadence
  (~70 s) on short gaps, because Power Log anchors at rows while the
  counter bracket is anchored at items. The shipped solver is
  `_analyse_counter_gaps` in `src_common/fpq_gap.py`.
  **2 s validation (2026-10-27 read: 624 pages, 0 bad CRC, 34,085 rows,
  18.9 h):** 300 rows in each of 111 complete 10-minute blocks (the others
  are the partial first block and a pause boundary); the daily jump at 2 s
  is `2^22 + 32,768 + one ~28 s pair`; counter items are ~15 rows apart
  (~30 s). So the item cadence is **row-based** (~14-15 rows), not
  time-based: ~70 s at 5 s rows, ~30 s at 2 s rows. A full ring holds about
  **88.6k rows** regardless of interval (about 49 h at 2 s, 5 d 3 h at 5 s);
  this 18.9 h / 34,085-row capture had simply not filled it.
  **Longer interval (1 m, 2026-10-27):** a 26 min capture shows 10 rows per
  10-minute block (600 s / interval) and 5 blocks; the interval readback
  dropped once during `set` but the setting applied (later reads show
  60 s), so a missing post-apply readback is not proof of failure.
  **Event rows:** a unit whose dip/swell thresholds do not suit the supply
  writes extra rows on events (the 220 V thresholds 207/253 on a 120 V
  outlet produced 46 rows in 26 min at 1 m instead of 26, and one block
  held 14 rows). Row/block cadence checks need thresholds appropriate to
  the supply, e.g. Dip 100 / Swell 140 for 120 V.
  **Counter subtypes:** the second unit writes `06 00` counter items
  (values ~570 M, deltas exact multiples of 128) while the first writes
  `06 02` (~1.9 G, deltas 128*t + 512 per item). The parser accepts both;
  treat the per-item +512 as a first-unit property.
  **Pause rate cross-check (second unit):** a host-clock 147 s pause gave
  a 20,736-unit span = 147 s at 141 units/s, against ~128 u/s while
  recording (+10%), matching the first unit's 144.4 vs 135.3 (+7%).
  - `0x06` is the normal periodic counter.
  - `0x09` and `0x0A` appear at real no-data intervals [observed shape,
    meaning is interpretation]: the reading below treats `09` as a pause
    marker at the current counter and `0A` as the resume marker carrying
    the advanced counter, because the observed sequence is `09` -> `0A`
    (counter jumps across the interval) -> `06` (about +5-6 s of units)
    -> rows resume. Example: rows 37,574 and 37,581 of
    the 2026-10-24 meter2 dump; 3 x `09` and 9 x `0A` in the 42,019-row
    2026-10-25 memory against 2,999 x `06`.
  - Clean-session confirmation (2026-10-26, 6,467 rows, 8 h 59 m): exactly
    one `0A`, at row 13, preceded by 13 rows and followed by a `06` tick
    and the rest of the session. The 13 rows are the short segment written
    before the last pause; the `0A` marks the resume that starts the main
    run. This supports the pause/resume reading, and it means a clean
    session can contain a marker even with no Power Log gap event.
  - Not every gap carries markers: some are plain `06` jumps. A pause
    does not consume row indices, so a marker-less jump cannot be told
    from a mid-recording counter step by the counter alone; `09`/`0A`
    prove a pause, and Power Log's own gap rows settle the rest.
  - Serial (dump/resume) pauses leave **no** `09`/`0A`: both scheduled
    dumps contain exactly one `0A` (the session-start one) and no `09`.
    The markers correlate with power-off/unplug events instead.
  - **Power-cycle counter reset [verified A/B 2026-11-01]:** a forward
    `06` jump larger than the ring capacity (~90k rows at any interval)
    is the counter base restarting, not elapsed time. Power Log keeps the
    pre-reset segment and drops the final **28-row ring-start block**; it
    does not add the jump to the timeline. On the br1 power-cycle dump
    (1,846 parsed rows) the official save kept 1,818 rows, wrote **I=1**
    with the meter's **3 s** interruption duration, and the row timeline
    still added the **5,202 s** counter gap. Kept G values and timestamps
    matched the tool within 3.14 µs (2026-11-01 official power-cycle
    session). The tool implements the same trim
    (`_reset_suffix_start`, `RING_STALE_ROWS=28`).
    Ground truth: the 12:00-12:03 scheduled-read pause appears in the
    17:50 dump as a plain `06 -> 06` delta of 33,536 units = 183 s after
    subtracting the 65 s of rows - the wall-clock pause, at the base rate.
  - Large `06`-only jumps of about 4.23-4.24 M units recur at roughly the
    same clock time each day (rows 4,431 / 21,705 / 38,528 / 41,603 of the
    2026-10-25 memory). They have no `09`/`0A` marker and no gap in Power
    Log's own save [observed]. With the model above they are exactly the
    daily +2^22 step plus one hourly step plus the normal pair advance.
    Whether the daily step also advances while the meter is powered off is
    [open]: one 6.1 h power-off gap (row 37,574) fits only with it, while a
    16.1 h power-off gap (row 37,581) fits better without it.
  - **Confirmed on a clean continuous session (2026-10-26 17:50 read):**
    rows 15,465 -> 15,480 of the scheduled dump, delta 4,237,696 units
    (31,321 s at 135.3), types `06` -> `06`, no marker, and the 15 rows
    between those counters follow the normal ~14-row cadence. Measurements
    continued every 5 s straight through the jump, so it is a counter
    value/field jump, not elapsed time. Ignore these unmarked jumps when
    reconstructing a timeline; use `09`/`0A` for real no-data intervals.
- `type 0x0b` — the 10-minute report block (see below).

The dump starts with a short prefix; the first record's voltage and NG items
follow it immediately and its frequency item is the first byte tagged
`0x17`.

#### The 10-minute `0b` block  [fully decoded]

Once per 10-minute report tick the dump carries an `0x0b` block: a 6-byte
header followed by 2–6 six-byte groups. It holds the block's complete
flicker and harmonic summary:

- **Header** `0b <u16 PP BE> 00 00 00`: `PP = round(Pst * 256)`, so the
  `.fpq` K record value is `PP / 256`. The u16 is big-endian; blocks with
  `Pst > 1` have a nonzero high byte (`PP` 267-297 on the 2026-10-28
  capture), which the earlier one-byte reading mistook for a flags byte.
- **Groups** `01 II h0 h1 h2 h3`: `II` selects a group of four 5C harmonic
  lanes and `h0..h3` are their magnitudes as `code / 100` (the 5C record
  stores `code / 1024`, and every code is a multiple of 100, so this is
  lossless). An even `II` covers the *odd* lanes of an 8-lane octave
  (`base = 8*(II/2) + 1`); an odd `II` covers the *even* lanes
  (`base = 8*((II-1)/2)`); in both cases the group holds
  `base, base+2, base+4, base+6`. Groups appear in `II` order
  `0,1,2,4,…`; a lane whose group is absent is zero.

Rebuilding all 49 lanes from the block reproduces the 5C record's float
values **byte-for-byte**. The other summary records follow from it:

- **THD** (H record) `= sqrt(Σ (code_j/1024)²)` over all 49 lanes — computed
  by the host, not sent verbatim.
- **Plt** (L record) `= (mean of the 12 preceding Pst³)^(1/3)`.

So every non-timestamp record class seen in a capture (G, F, H, K, L, 5C)
is recoverable from the dump.

#### Absolute time is anchored to the PC clock  [verified 2026-10-24]

The dump carries no absolute time: neither the first/last record
timestamps, the recording interval, nor `1/86400` appear anywhere in it.
What it does carry is the *relative* timeline (record order, per-record
interval ticks and the declared pauses/gaps). Power Log combines that with
one absolute anchor taken at download time and walks the records back from
it, so the most recent record lands about one interval before the download
starts.

Anchor provenance was proven with a clock-shift experiment: a second
download was captured with the PC clock set several days ahead
(a clock-shifted capture). Against the timed capture:

- all 40,513 overlapping G/F rows have **byte-identical values** and the
  **same internal gap structure** (same 4 s ticks, same 21,993 s / 57,937 s
  / 2,752 s holes);
- every timestamp is shifted by **exactly +349,195 s** (4 d 00:59:55).

So the absolute anchor comes from the PC clock at download; the meter
supplies only elapsed time. Consequences:

- a replay served to Power Log is necessarily re-timestamped from the
  downloading PC's clock — values and cadence survive, absolute dates do
  not;
- an `.fpq` rebuilt from a dump needs the download-time PC clock as its
  anchor (or the "last record ≈ download start" convention); and
- two downloads made with a correct PC clock reproduce the same record
  timestamps for the same memory content, which is why the older capture
  and the timed capture agree byte-for-byte on their shared prefix.

Caveat [verified 2026-10-25]: the clock is the anchor, but Power Log's
displayed/saved dates are not the clock value. With a correct guest clock
(2026-10-25), two downloads of the same 13-row fixture one hour apart were
stamped 2027-11-21 06:07:43 and 07:14:55 — the same constant offset of
+391 d 16:48:33. That offset is a Power Log date-conversion quirk, not a
clock error; the *relative* timeline is unaffected. Treat absolute dates
from downloads as Power Log's own and not reproducible.

The `0x06` counter item is decoded as the stepped 1 s clock described in
the gap-rule section below; the dump's `I` reply meaning and the dump prefix
are not yet decoded.

**Cross-checking two recordings of the same memory.** A later read (after a
`resume`) has newer rows, so it is never byte-identical to an earlier save,
but the shared RMS values are. `fpq_export.py compare first.fpq second.fpq`
aligns them by those values and reports the overlap rows, the clock offset
(median and spread) and the stop times. This is the validation flow that
uses the official software only as an oracle: official pause -> tool
`resume` -> tool read. The tool's pause is logged, so the earlier,
unlogged stop is `second stop - offset`. On 84_ the offset is a constant
offset 0 on the canonical 84_ build. The old 84_ repause file showed +667 s
because the official save had paused the meter before the tool re-paused it;
that is the read-order gap, not a meter or tool offset, and standalone use
cannot produce it. The download is implemented by two companion tools:
`fpq_serial_meter.py download` (host side) and `fpq_serial_replay.py` (device-side
replay, with an offline `self-check` that rebuilds the item stream from a
`.fpq` and verifies it value-by-value).

#### Building an .fpq from a dump  [verified]

`fpq_serial_meter.py download` (and the offline `build-fpq` on a saved raw
dump) writes a complete `.fpq`: header, G/F rows, K/H/5C 10-minute blocks and
the L (Plt) rows computed from the K values. The fixed header is built from
the layout in `docs/format.md`; the thresholds come from the identification
pass, the interval is inferred from the 10-minute block cadence (median rows
between blocks, snapped to a standard setting), and the newest record is
anchored one interval before the **arm instant** (the moment recording
paused; `download` captures it and stores it in the sidecar, and `build-fpq`
reuses it). `--anchor` overrides it; only when neither exists does the build
clock stand in, with a warning.

**No start anchor is needed for the normal (tool-only) path, and the
official save is data-deterministic.** Two 85_ official saves 32 minutes
apart, across a power cycle + reboot, overlap 115,172 rows with **zero
clock offset** and identical values: the save time does not move the
timeline, so the official is a canonical target. The rule derived from the
data is: minute `p = 12.5 s`, hour `ph = 2886 s`, the daily `+32768` phase
from the bracket visible in the dump, **forward floor step counting** over
`[ta, ta+D]`, and the I timestamp at the **previous row**
(`D = resume - prev - interval`). Checked against the official saves with
only the tool's pause anchor: 82_ **31/32** (old inverse solver 29/32), 77_
**18/19** (16/19), 67_ 33/33, 84_ 44,608 s exact, 61_/65_/51_ exact.
Remaining: the official adds a 0/1/2 s row-placement tick at some gaps; the
last 82_/77_ 1,855 s event is +4 s because of it. That is a solver gap, not
an official ambiguity.

`download` and `resume` also append the host pause/resume times to a JSONL
session log (`~/.fpq-session.jsonl`, override with `FPQ_SESSION_LOG`;
`fpq_serial_meter.py session` prints it). That is the only source of
absolute time, and it is what makes the anchor automatic in tool-only use.

The meter tail is sent **before** the `.fpq` build: `download` erases
and/or resumes immediately after the dump (the dump is already in memory),
and only then parses/solves/writes. With `--erase` and no `--raw`, a
temporary raw file is written first and removed after the `.fpq` succeeds.
After `E` there is **no** `v` poll and no second `E`: the dump exchange has
just completed, and a poll would pause the meter again. A failed post-dump
settings readback is a read failure (nonzero exit), not a note.
Background: the 2026-11-04 gap postmortem (project notes).

The writer also emits the stored event records decoded from the dump —
dips (`0x04`), swells (`0x10`) and transient captures (`0x15`) — and the
host-derived interruption (`I`) and interval-without-measurements (`j`)
records derived from the `06`/`09`/`0A` counter timeline (37_gap_impl). It
does not fabricate events: a recording only carries the records for events
that actually happened in its window.

The recorder accepts only these recording intervals (owner-confirmed from
the official software): **1s, 2s, 5s, 10s, 30s, 1m, 5m, 10m, 15m, 20m**
(1, 2, 5, 10, 30, 60, 300, 600, 900, 1200 seconds). `--interval` is validated
against that list, and `interval_seconds()` is the single gate any future
interval-setting code must call before sending a value. Intervals outside
the list are rejected with the supported values named.

#### Recording time per interval - official-dialog method

Power Log's **FLUKE VR1710 Settings** dialog displays the recorder's own
maximum recording time for the selected interval. That text can be read
without a meter by pointing Power Log at a replay:

1. serve any `.fpq` through `fpq_serial_replay.py` on a pty and forward that
   pty to the XP COM port (a pty-to-COM forwarder);
2. open **Settings -> FLUKE VR1710 Settings**;
3. select each recording interval in turn and read the duration shown below
   the interval field;
4. record the values here or in the project notes.

This is the authoritative capacity check (for example whether 1 s really
fits 24 h). Do not infer capacity from the byte count of one download: a
dump is the **used** portion of the ring, so a small dump only shows how
long that session ran, not how much the memory can hold. `--interval`
(`configure --recording-interval`) prints the same pointer in its help text.

Measured through the replay with Power Log Classic 4.60 on 2026-10-27
("Typical Continuous Measurement" in that dialog):

| interval | typical continuous measurement |
|---|---|
| 1 s | 1 day 0 h 50 min (captured 2026-10-30) |
| 2 s | 2 days 1 h 34 min (captured 2026-10-30) |
| 5 s | 5 days 3 h 6 min |
| 10 s | 10 days 3 h 32 min |
| 30 s | 29 days 4 h 10 min |
| 1 m | 54 days 21 h 58 min |
| 5 m | 186 days 17 h 5 min |
| 10 m | 266 days 17 h 30 min |
| 15 m | 311 days 4 h 15 min |
| 20 m | 339 days 11 h 20 min |

The 5 s figure implies about 88,600 rows of capacity (not the ~35,000 rows
a single 758 KB session suggested). That matches the owner's observation
that a 1 s / 24 h capture just fits. The row capacity shrinks at longer
intervals because the per-10-minute block records and counters take space
regardless of the interval. The 1 s and 2 s rows were read from a real
meter's dialog on 2026-10-30.
string to confirm.

**Absence is normal (owner, 2026-10-27):** a recording only contains event
records for events that actually happened in the observed window - dip,
swell and transient records can each be present or absent independently.
An empty event list is therefore not evidence of a decoding gap; event
parity can only be compared on a session that actually contains one.

**Transients are stored in the meter [proven 2026-10-27].** The meter was
frozen with a tool download (no `E`) and then read by Power Log Classic 4.60
through a logged serial forward. Both reads were **byte-identical**
(762,494 bytes, zero differing bytes) and Power Log issued nothing beyond
the `?V` dump plus `0x06` page acknowledgements. Power Log's save of that
same memory contains **43 T records**, 128 D records and 5 j gaps, while the
tool rebuild had none on that date (the writer now emits D/P/T and derives
I/j; see "Building an .fpq from a dump"). Therefore the transient waveforms
are inside the dump; they are decoded below. The official save is 222 KB larger
than that early rebuild: each T record is 5,153 B (26-byte header, 1280 float32 LE
samples, 7-byte trailer), and T0..T2 have all-zero header/trailer bytes.
Samples are integral multiples of 10 in about -7700..+7660;
`volts = raw / (32 * sqrt(2))`.

**Transient container - decoded 2026-10-27 [exact].** The parser previously
rejected 110,524 of 757,760 payload bytes on that capture (14.6 %, versus
~1.2 % on quiet dumps) with every page CRC valid. That data is the stored
transients:

```
0x15 item:  15 02 <u32 BE capture ticks>     (6 bytes, ticks are 1/128 s)
payload:    2562 bytes = 427 x 6-byte frames
frame:      three samples + duplicated sign/status bits
```

Each frame decodes three consecutive samples (`s0,s1,s2`) of a capture:

```
s0 = s12((b1 << 4) | (b2 >> 4))
s1 = s12(((b2 & 0x0F) << 8) | b3) * 4 + ((b4 >> 6) & 3)
s2 = s12(((b4 & 0xF) << 8) | b5)
```

with `s12(v) = v - 4096 if v & 0x800 else v`. The middle sample is a 12-bit
code in `b2[3:0]`/`b3`, scaled by 4, with the two status bits `b4[7:6]`
appended as its low bits: it is not a plain 12-bit read in that packing.
The same expression is implemented in
`src_common/fpq_wire.py` (`decode_transient_payload`). A capture is 427 frames =
1281 samples; the `.fpq` T record keeps the first 1280, and sample 1281
equals sample 0 (pre-trigger ring wrap). `.fpq` values are 10 x these codes;
`volts = code * 10 / (32 * sqrt(2))`.

Verified against Power Log's save of the same memory: **43/43 records,
55,040/55,040 samples exact**, in both the 21:02 and 21:14 dumps. The
isolated pairs 332/333, 452/453, 480/481 are the bodies of T0-T2; the
contiguous pages 637-739 hold T3-T42 (the UPS-transfer burst). The 0x15
item's tick counter is the same timebase as the `06` counter items
(128 ticks/s); no absolute time is stored - Power Log assigns timestamps
from the stream position.

#### `15 00` — second-unit transient capture (variable length)

The meter also writes a transient capture as `15 00 <u32 BE ticks>` followed
by a **variable** number of 6-byte frames. Each frame starts with 0x14 or
0xD4 and holds three 12-bit samples, the same framing as `15 02`. The frame
run ends at the next normal item (a `09`/`0A`/`06` counter or a `12`/`17`
row); those following items are real stream data and must still be parsed as
counters/rows. Observed runs are 112 and 284 frames (336 and 852 samples) in
the 2026-11-03 br1 session.

Do **not** consume a fixed 2,562-byte payload for `15 00`: the old fixed
`15 02` rule missed the capture header, rescanned the waveform bytes as
items and produced false D/P events and skipped bytes. Decode the full 3k
samples and zero-pad to the 1,280-sample `.fpq` slot. The first `15 00`
header's tick base and the capture timestamp placement are still open (T
timestamps were 288 s and 8 s early in the 2026-11-03 session); the waveform samples themselves
are exact.

**Dip events [decoded 2026-10-27; layout corrected 2026-10-28].** Dips are
6-byte `0x04` items:

```
04 <lo:u16 LE> <code:u8> <dur:u16 BE>
```

`voltage = code / 8 + 32 * ((lo >> 8) & 0xF)`; the 12-bit event code is
`((lo >> 8) & 0xF) << 8 | code`.  `lo` packs the 32 V voltage window in bits
8-11, the 1/4 s counter low byte in bits 0-7 and the counter high nibble in
bits 12-15.  This is exact on **724/724** raw+official D records, including
events inside an outage (67_ 68.375 V), below the official 70 V support
floor, and the high-range 238 V records.  The older row-minimum inference is
kept only as a fallback for an item without a valid nibble.  `duration =
dur / 240 s`; the item's stream position is the row, and `lo` separates
several dips inside one row.  The earlier reading (`u16 code` + `u8 dur`) is
identical only when the duration's high byte is zero - true for all 128 dips
on the 2026-10-27 capture, but not for the 2026-10-28 variac dips
(1.07/3.51/36.01/20.21 s).

`0x0B` 10-minute blocks are `0b <u16 PP BE> 00 00 00` plus the lane groups.
A block with `PP >= 256` (Pst > 1, high byte nonzero) was previously left
unconsumed, so its harmonic lane bytes were rescanned as `0x04` items
(5 false dips) and 5 blocks were missed; the parser now matches the zero
padding, reads the u16 PP and consumes both Pst ranges. Results:
**128/128** D on the old capture, **36/36** D on the variac capture, exact
voltage and duration, and 113/113 10-minute blocks with exact Pst. Relative
timing follows the `lo` counter (last dip of a row at the row time, earlier
ones stepping back
by `(lo_max - lo)/4` s; a lone dip at the row midpoint); absolute timestamps
still depend on the gap-aware row timeline. `build_fpq()` writes these D
records.

**Swell (P) storage [decoded 2026-10-28].** Swells are stored as 6-byte
`0x10` items:

```
10 <u8 flags> <u16 code> <u16 dur>
voltage = (code & 0xFFF) / 8
duration = dur / 240 s
```

The item's stream position is the event **end**, which is the timestamp
Power Log writes. Verified on the 2026-10-28 variac capture: 4/4 official
P records match exactly on voltage and duration (140.125/2.5583 s,
140.125/4.1417 s, 141.875/54.1875 s, 128.125/23.0083 s). Parser and writer
support it. The verified voltage range is 128.125-141.875 V; the codec has
no offset, but the top of the configurable range (up to 300 V) is untested.

I (0x49, 18 B): timestamp = whole second of the event **start**; duration =
whole seconds below the recorder's internal interruption threshold
(including 0 for sub-second collapses). `Umbral=` is **not** that
threshold; it is the transient-capture sensitivity. The internal
interruption threshold is below about 45 % of nominal. I and j records are
gap-derived, not stored as `0x10`/`0x04` items: on the timed-outage capture
the official I records are 17:00:49 (265 s) and 17:16:03 (858 s) for owner
outages of 263.841 s and 856.164 s, and the j gaps are 30 s and 1742 s.

For **P**, the stored `0x10` item is sufficient: the 2026-10-28 variac
capture gives four of them, all exact, so no generator stream is needed to
write P from a dump. The underlying detector still runs on the recorder's
continuous 240 Hz quarter-cycle RMS stream (which a `.fpq` does not carry),
and P is **not** derived from the T captures (a 261.75 V P can sit beside a
capture whose peak/sqrt(2) is 364 V). For **I/j**, the events are
gap-derived rather than stored items: they need the row timeline/counter,
and the exact long-gap duration source is still [open].

The dump also carries 41 `0x04` dip-family items on that capture against
36 official D records: 32/36 match exactly, 4 official D records use a
second `0x04` variant, and 5 items have no official counterpart. D variant
filtering is [open].

**Gap (I/j) rule [solved 2026-10-28].** The dump carries a 1 s counter in
three 6-byte items `tt 02 <u32 BE>` (`06` row clock, `09` interruption
start, `0A` interruption end / pause end). It is not a plain clock:

```
counter/128 = t + 4*floor((t-p)/60) + 256*floor((t-ph)/3600)
                + 32768*floor((t-pd)/86400) + C
```

- **I** exists iff the resuming row carries a `09`+`0A` pair:
  timestamp = inverted `09`, duration = inverted `0A` - inverted `09`
  (whole seconds). A lone `0A` is a `j`.
- **j** exists for every other row gap: timestamp = the previous G row's
  exact double; duration = `t(resume) - t(prev) - interval` in OLE days
  (bit-exact in that order). Placement is immediately before the resuming
  G (after the row's D/P/T), plus a tail `j` at EOF.

A `09` without `0A` is not a realistic readable-capture case: reading the
meter requires power, and the recorder always starts recording when powered,
so a resume `0A` is present before any download.

Anchoring the `06` rows to the inverted counter time reproduces the official
row timelines exactly: **13,796/13,796** rows on the 2026-10-28 fixture and
**34,963/34,963** on the 2026-10-27 fixture, including Power Log's 4 s row
deltas. The earlier +1,136 s / +36 s long-gap errors were entirely the old
constant-135.3 u/s counter model; with the step grid they vanish. Owner
outages of 263.841 s and 856.164 s appear as 265 s and 858 s because the
record uses floor(off second) + ceil(on second) (and one confirmation tick).
Residual: <=1 ULP in timestamps; the daily rollover phase needs the anchor's
local time-of-day (17:43:15 local), which the writer has.

**Hour phase and the wrapped-ring day branch.** The hour step is a meter
constant, not a wall-clock grid: the measured `ph` (2886 s) is the same on
every session while the implied daily phase is not, so do not derive one
from the other. When a wrap/power reset leaves the marker span with several
day-step branches (73,408 / 44,608 / 15,808 s on 85_), the anchor still
fixes the wall time of the rows *after* the gap. Count the 17:43:15 daily
steps that would fall inside each candidate and keep the candidate whose
count equals the day steps its raw span implies. 85_ then gives 44,608 s
with only the tool's own pause time; the old "smallest candidate" rule gave
15,808 s. If the count does not discriminate, the build warns instead of
silently shortening the gap.

The 2026-10-28 fixture's row counts have three sides: the untrimmed parse
holds **13,829** rows, the parser keeps **13,796** after the wrap trim, and
the official save also has **13,796**. Rows 0..13,795 of the untrimmed parse
match the 13,796 official G rows exactly; the last 33 rows are stale ring
content. The trim detects the stale-segment boundary (a duplicated row run
pins its start) and drops only those 33 rows, so the final 1,742 s `j` is
writable. Verified rebuild: 1,666,787 bytes = official, 13,796/13,796 rows,
5 I + 2 j written.

Full derivation: the 2026-10-28 gap-rules notes.

Decoding work and verification scripts:
the 2026-10-27 waveform-decode notes.

Evidence for the A/B: the 2026-10-27 official A/B

(official `.fpq`, tool dump/rebuild, byte log of Power Log's whole command
stream, and the 43-transient screenshot).

XP file transfer: the guest has a VirtualBox shared folder mounted to the
host project tree. Power Log's Save As
dialog opens through the serial bridge on that share, so an official save can be
written straight into the repo; its old-style dialog rejects a full path in
the file-name box, so save into the current folder and move the file on the
host afterwards.

The counter items are deliberately not used for ordering. Values can jump
backwards (circular buffer) and carry artefact jumps tens of thousands of
seconds above their neighbours, so they are not a monotonic clock. Stream
order is used instead; it tracks the `.fpq` row order on the verified dumps.
For gap work, distinguish the item type: `0A` marks a resume after a real
no-data interval, while the recurring ~31,300 s `06`-only jumps have no
marker and no counterpart in Power Log's save.

Validation of the writer: the generated header is byte-identical to three
real sample files when given their field values. On the 230 V sample save
(`serial-interface/wok-right-plug-test-3.fpq`), a parse -> item-stream ->
parse round trip
reproduces all 37,581 G/F value fields byte-exactly (parse-only: the replay
encoder refuses a 230 V session because its D/P codec is 120 V-only). The
gap-aware writer now also reproduces whole-second row timestamps on both
matched fixtures (float 1-4 ULP; see the 2026-10-28 gap implementation).

Replay timeline: `fpq_serial_replay.py` can generate the `06` counters and
`09`/`0A` gap markers from the `.fpq`'s own record timestamps, but `serve`
leaves them **off by default** (`--counters` opt-in). Power Log Classic's
post-download decoder deadlocks on a synthetic counter timeline, even when
the values/positions are borrowed from a real dump (`--counter-track`); only
the meter's native counter cadence renders. With counters off, the
post-download time axis uses the uniform `.fpq` interval, so source pauses
are compressed; the `.fpq` and offline reconstruction remain exact
(`self-check --counters`). For native counters/gaps in Power Log use
`--stream-file` with a real dump.

When `--counters` is enabled, the row cadence is `--counter-rows` (default
14 rows) or `--counter-period SECONDS`; values follow the meter's stepped
model (128 units/s plus the minute/hour/day bookkeeping steps), matching the
decoder. `--counter-rate` is deprecated and ignored; `--counter-steps` is
accepted with no effect. The daily +32768 step is `--counter-day-step`
(default off). The date-anchoring pad is off by default (`--pad-now` to
enable): a final counter with no rows after it stalls Power Log, independent
of the jump size [verified 2026-10-25].

**Transient replay — stored exactly, omitted from `serve` by default.**
`fpq_serial_replay.py` encodes each T record back into the meter's container
(`15 02 <u32 ticks>` + 427 frames) and inserts it at the capture's
chronological row position. Offline, `self-check` reports all captures
sample-exact. `serve` leaves captures **off by default** (`--transients`
opt-in): Power Log's post-download decoder deadlocks on a replayed `0x15`
capture even when every sample round-trips and the tick is zero/relative.
With captures off the document renders, but the Transients tab is empty. For
native transient display in Power Log, serve a real raw dump with
`--stream-file`. This is a Power Log acceptance limit on synthetic capture
items, not a payload/codec error.

**Transient (umbral) encoding - SOLVED 2026-10-27 against the replay.**
The transient value lives in the umbral register (`r`); the register has an
affine volts map, measured from Power Log's own Apply:

    2 V = raw 100    5 V = raw 132    20 V = raw 290
    raw = round((95*V + 710) / 9)   (about 10.56 units/V, +79 offset)

This supersedes the earlier "50 units/V" note, which was a one-point fit.
Power Log displays the field from `r` through this map (`r=100` -> 2,
`r=132` -> 5, `r=290` -> 20). Writing is the usual step loop: one raw unit
per `?K1` (up) or `?k1` (down), the count equal to the raw delta.

The **"Disable Transient Recording" checkbox is not a separate op**: it
steps the umbral to a sentinel value.

- checked (disabled): step `r` to **300** - measured 100 -> 300 = 200 x
  `K1`, and 132 -> 300 = 168 x `K1`. With `r = 300` Power Log reopens the
  dialog with the box checked and the field blank/greyed.
- unchecked (enabled): step `r` to **290** (20 V, the default) - measured
  300 -> 290 = 10 x `k1`. It does not restore the previously typed value.

A bare `?K` (no channel digit) is still a separate query: when the
settings row is not available (no completed download in the session),
Power Log sends it up to three times, then re-reads `X0 I J v i1 j1 r1`
and leaves the transient row blank. A completed download populates the row
from `r`; no `C` config blob is needed. The replay answers the bare `?K`
with `--k-reply` (default 200) but does not need to for a normal Apply.

Against the replay, remember that Power Log verifies its steps by reading
`r` back: the replay must track the `K1`/`k1` steps (it does now) or the
Apply stalls at "Applying settings".


## After the dump

When the dump handshake is done (`0x06` → `0x04`, trailing `0x06`), Power
Log re-runs the identification pass (`X 0`, `I`, `J`, `v`, i/j/r threshold
reads) and then shows its own dialog: whether the device should be
**commanded to resume recording**, be **erased**, or Power Log should **do
nothing**. The recorder has no input of its own. In the recorded session
the operator chose *do nothing* ("don't continue recording") and Power Log
sent no further commands — the transmitted stream simply ends after the
threshold reads, with the operator's ~9 s dialog pause visible as a gap.
[verified: session structure and tx stream; dialog semantics from the
operator's report]

The host tool follows that choice explicitly: `download` leaves the meter
paused by default, `--resume` sends `E`, and `--erase --yes` sends `B` only
after the `.fpq` is written. [verified live 2026-10-25: erase drove `v` from
784,014 to 0 and the thresholds survived (Dip 114 / Swell 126 unchanged);
`E` restarted recording (`v` 306 -> 528 in 60 s); an erased memory answers
the `V` arm with a zero preamble and streams nothing after `C`. Reading an
empty memory is a legal flow (the official software does it): the arm is
cleared with `C` and `E` resumes normally [verified live 2026-10-27 with
`download --arm-empty --resume`]. The earlier wedge was arming and then
sending `E` without the `C` clear; the tool keeps the clear on that path.]
[verified live 2026-10-26, two independent cycles: a serial session leaves
the meter paused, including a plain `config` read. The recorder can either
record or talk serial, never both: any exchange stops the recording, and
`E` is what restarts it. Each cycle sent `E`, let
60 s pass (`v` +222 and +228), then read `config`: `v` stayed frozen at that
reading for the next 60 s. So `E` (`resume` or `--resume`) must be the last
command and the port left untouched afterwards. This also explains the
earlier "no new data" observations: every status check had stopped the
recording.]

Device-side output that occurred on its own while the host stayed idle
after that choice, meaning [open] (no command preceded it, so it is not
an answer to anything we sent):

- `Z Z Z`, a burst built only from bytes `0x00` and `0xF0`, the text
  `/2009\r\n` (the tail of its own banner date), then a stream of `.`
  progress tick characters.

## Caveats

- Only the commands listed above were exercised; unknown characters and
  arguments can have undocumented effects. `fpq_serial_meter.py` exposes
  the read set plus the verified single-step writes (`B`, `E`,
  `p/P/q/Q/K/k`); it never sends an unlisted command.
- The threshold argument is a channel number; only `1` was observed
  (the .fpq config lists channels from `Canal= 0`, so the mapping of the
  1-based serial argument to the .fpq channel index is [open]).

## Random-test capture (2026-10-24), settings/erase/resume

Byte facts below are from the second interactive capture
(the 2026-10-24 random-serial notes, socat -x, 14:30:39–14:53:49);
detailed write-up with every verbatim sample lives in the project notes
(the 2026-10-24 random-serial notes). The threshold
reply triple maps to
the .fpq header's per-channel (dip, pico, umbral):

- `i 1` -> Dip, `j 1` -> Swell (header `Pico=`), `r 1` -> Transient
  (header `Umbral=`); each a 3-char
  decimal (e.g. `198`/`218`/`100`).
- **Threshold ranges (owner-confirmed from the official software):**
  Dip 0-300 V, Swell 0-300 V, Transient 2-20 V as raw 100-290
  (the affine map above; raw 300 = disabled); integer
  values only. Dip and Swell are sent 1:1 in volts. The older
  "50 units per volt / 2-20 V = raw 100-1000" note was a one-point fit and
  is superseded by the affine map above (2 V=100, 5 V=132, 20 V=290).
  The tool validates these ranges before opening the port and refuses a
  value that is out of range or not an integer.
- `v` (queried only in later passes, never the first) -> 7-char decimal
  record counter (`0000060`); drops to `0000000` after erase.
- Erase = `B` (42): no echo, no reply. Both the connect-dialog
  "yes and erase" (`? B ? E`) and the panel's Erase Recorded Data
  (`? B`) reduce to `B`; the record counter zeroes on the next pass.
  [verified association; meter-internal completion is not signalled]
- Resume / keep measuring = `E` (45): no echo, no reply. Dialog mapping
  [bytes verified; meaning [theory]]:
  - "keep measuring? YES (no erase)" -> `? E`
  - "keep measuring? NO"             -> nothing at all
  - "YES and ERASE memory"           -> `? B ? E`
- **Recording-interval commands (captured 2026-10-26, live Power Log):**
  the interval is never sent as a value; it is stepped, one second or one
  minute per single-byte op, each echoed and never acknowledged:
  - `U` = interval +1 s, `u` = interval -1 s
  - `T` = interval +60 s, `t` = interval -60 s
  Power Log applies a new interval as a step loop from the current value.
  Verified transitions (write counts match the delta exactly):
  1m -> 1s = `t`1 + `U`1; 1s -> 2s = `U`1; 2s -> 5s = `U`3;
  5s -> 10s = `U`5; 10s -> 30s = `U`20; 30s -> 1m = `T`1 + `u`30;
  1m -> 5m = `T`4; 5m -> 10m = `T`5; 10m -> 15m = `T`5;
  15m -> 20m = `T`5. All ops are single-byte exchanges with only the
  `?`/op echo as a reply.
- **Interval readback (BCD minutes + seconds):** the settings pass returns
  it across `I` and `J`:
  - `I[1]` = tens of minutes, ASCII `0`/`1`/`2`
  - `I[2] & 0x0F` = minutes units (`b0`/`b1`/`b5` for 0/1/5)
  - `J` = seconds, 3-digit decimal
  so `minutes = 10*(I[1]-'0') + (I[2] & 0x0F)` and
  `seconds = 60*minutes + int(J)`. The tool exposes this as
  `VR1710.read_interval()` and `config` prints `main interval: N s`.
  `configure --recording-interval` validates the target, requires empty memory, plans the
  observed `T`/`t`/`U`/`u` steps (whole minutes, then the remainder in
  seconds, over-shooting then correcting), sends `X 0`, reads back, and
  leaves the setting pending until a replug. Every step is one
  single-byte exchange (`?u` repeated N times) - captured on the wire
  2026-10-26 (96 x `?U`, 99 x `?u`, 36 x `?T`, 36 x `?t`, none with a
  digit argument). The canonical Apply order is
  `B  I J v i1 j1 r1  <steps>  X0  I J v i1 j1 r1`; the tool performs the
  readback on both sides of the steps and leaves the erase (`B`) to the
  separate, approved `erase` command. The tool also refuses to proceed
  when the `v` readback is unreadable (fail closed).
  **Activation (2026-10-27):** a new interval takes effect at the next
  start of recording, not while paused. Power Log's own flow is
  erase -> set -> read memory -> continue; the memory read (`V`/`C` arm,
  cleared with `C` when empty) plus `E` is enough - no power cycle is
  needed. Verified live on the experiment unit: from 1 m,
  `configure --recording-interval 2s` sent `?t ?U ?U`, read back 2 s, and after
  `download --arm-empty --resume` the meter recorded at 2 s. On that unit
  a power cycle reverted the settings to its stored defaults, so the
  read+resume flow is the reliable activator.
  Verified against all ten settings:
  1s/2s/5s/10s/30s = `b0 30 b0` + 001/002/005/010/030; 1m/5m =
  `b0 30 b1`/`b0 30 b5` + 000; 10m/15m/20m = `b0 31 b0`/`b0 31 b5`/
  `b0 32 b0` + 000. The apparent "lag" was this misdecode, not a delayed
  read.
- **Circular Memory state and Apply (solved 2026-10-31, br1):** the `v`
  reply is seven characters. Its **first character** is the circular flag:
  a digit (`0000000`) = ON, `p` (`p000000`) = OFF; the rest is the record
  counter. Power Log's Apply is exactly
  `?X0 ?I ?J ?v ?i1 ?j1 ?r1 ?x0` — one `?x0` toggle, no trailing `X0`.
  Do **not** retry `?x0`: two toggles cancel and leave the state unchanged
  (this is what wedged the early tool attempt, not the missing `X0`).
  Read `v` back after a short settle to verify. Official OFF->ON->OFF
  applies and the tool both reproduce this on br1.
- The settings APPLY write path is SOLVED (live experiment 2026-10-24,
  recorded in the project notes): Apply is a step-count loop,
  one unit per poll iteration, all single-byte exchanges:
  - `p 1` = dip   -1 / `P 1` = dip   +1   (case pairs: lower decrements,
  - `q 1` = swell -1 / `Q 1` = swell +1    upper increments, umbral is
  - `K 1` = umbral +1 / `k 1` = umbral -1   special: K=increment)
  - `x 0` sent N times changed nothing on i/j/r [semantics open]
  Proof: PowerLog's capture bursts q 1 x92 == 218-92 = 126 and p 1 x84 ==
  198-84 = 114 (the exact strides typed in the dialog), and its K-loop
  x32 == umbral 100->132. The live meter stepped `K 1` -> r 100,101,102,
  103,104 and `k 1` -> 103, and `P 1`/`Q 1` restored 206->207, 252->253.
  Every op echoes itself; the `1` argument gets no reply; reading back is
  the separate `i 1`/`j 1`/`r 1` queries (each prefixed by `?`, replies
  come as `<op><3 digits>` e.g. `i207`, plus the op echo).
  So "apply" = PowerLog NUDGEs each parameter toward the target from its
  CURRENT value; delta == loop count; no digits ever cross the wire.
- Threshold persistence [CORRECTED with owner hardware info 2026-10-24]:
  the settings ARE persistent (NVRAM) and survive power cycles; the
  volatile part is the RECORDED DATA (battery-backed SRAM), which on the
  test unit is lost whenever power disconnects (owner-confirmed hardware
  fault — works flawlessly with continuous power; repair on the owner's
  TODO). Evidence chain in the transcript:
    - right after the "now erasing" press the readback still showed the
      applied values (114/126/132) — erase empties the record buffer, not
      the thresholds [fact];
    - the readout later showed r -> 300 [fact];
    - the final reboot read back i/j/r = 207/253/200 — the persistent/
      factory triple, i.e. what NVRAM held after that sequence
      [theory: the erase round resets the persistent copy to factory
      defaults]. A second unit power-cycled live booted its own baseline
      207/253/100 [fact] and was restored after the live write probe.
- Byte timing [verified live]: the meter only processes bytes spaced
  ~>=100 ms apart — a close-packed two-byte write got NOTHING (not even
  the `?` echo), while single paced bytes reply every time. This
  explains the capture's per-byte socat packets with ~80-230 ms gaps:
  the protocol is effectively one byte per keepalive tick.
- Stray-byte corruption is REPRODUCIBLE [fact]: `v` occasionally answers
  with a leading `p` byte (`p000084`), exactly the glitch seen in the
  transcript (`p000000`).
- Meter lifecycle [verified]: on power-up it reprints a plaintext boot
  log (`Registro OK`, `Mem=0000000`, `econstruyendo configuracion`,
  `Calibracion OK`, `Offs=… Coefs=… f > 1.13 calibration`, then `.` tick
  stream) — the only plain-boot output observed; recording resumes by
  itself after boot (the record counter/pointers advance without any
  host command). A crashed meter answers NOTHING to wakes (`?` polls
  unanswered 12–41 s in the capture; confirmed live once) until the
  meter is power-cycled. The recorded-data buffer is SRAM-backed: on the
  test unit (whose SRAM backup is faulty) a power loss wipes records
  (owner-confirmed fault; functioning meters keep them).
- `I`'s third reply byte is the interval field: `b0 30 b0` + `001/002/005/
  010/030` for 1/2/5/10/30 s, `b0 30 b1` + `000` for 1 m (and `b5`/`b0 31`/
  `b0 32` for 5/10/15/20 m). An early capture misread the 1 m `b1` as a
  transient-disable flag; the ten-interval decode above supersedes that.
  `I` does not carry the circular-memory state.
- `X 0` replies were observed byte-corrupted twice while writes were in
  flight (`FLUKEV1.15…`, `FLUKE 1710 V 25/`), consistent with the meter
  being mid-configuration rather than the link dropping.

### 2026-10-24 single send path + pump timeout

Corrections applied to `fpq_serial_meter.py` after peer review:

- all commands now go through ONE packed exchange form: single paced
  stream `?` + cmd [+ arg], bytegap-spaced, exchanges >= 1.45 s apart;
  the old two-phase/lone-`?` host behavior was removed (isolated lone
  `?` writes are answered only every other poll by real meters). [fact]
- `query()` retries a READ once when the value tail is missing; writes
  (p/P/q/Q/K/k nudges, B erase, E resume) are never blind-retried —
  writes must be verified by readback instead [fact] (E resume
  [theory]).
- the dump ACK pump aborts after 20 consecutive quiet ACK ticks with
  no data and no EOT (cable pulled / meter stopped) instead of pumping
  forever. [fact]
- resume via `E` is now VERIFIED live (2026-10-25): after `E`, `v` rose
  306 -> 528 in 60 s; the next `config` read then stopped it again, so any
  serial session pauses the meter and `E` must be the last command. Erase
  via `B` empties the record buffer and does NOT touch thresholds
  [verified 2026-10-25: Dip 114 / Swell 126 survived an erase].

### 2026-10-24b second unit, live pacing measurements

A second, SRAM-working VR1710 unit was connected (dmesg
`"Virtual Fluke VR1710 Voltage Recorder"`, USB re-enumeration ~14:15).
Probe over it: J=005, thresholds 198/218/100 (matches the captured-session
transcript-start anchor), R=020:016:046 017/003/001, v=0701376 (7-digit
counter). [fact]

- Reply latency [fact]: reply bytes arrive atomically ~0.46 s (sometimes
  0.59) after the last byte of the command stream; there is no delayed
  tail. A "missing value" is the meter NOT replying at all, not reply
  truncation latency.
- Close-packed writes [fact]: a single b"?i1" write (~1.0 s exchanges)
  got 2/10 replies — mostly total silence; the ~>=0.1 s inter-byte law
  stands on this unit. Isolated lone `?` is still answered only every
  other poll.
- Wake-per-exchange with only 0.4 s hydration [fact]: 0/10 replies — a
  wake needs a LONG settle (>= ~3 s) to hear anything after it.
- Drop events cluster [fact]: runs where ~1-2 of ~9 exchanges were
  eaten, and other runs where 6+ consecutive exchanges (including three
  V arm attempts) were all eaten. Recovery not achieved by: port close
  + 20 s, 4-8 s wake settles, DTR/RTS toggles on open. The unit went
  fully non-responsive after ~30 min of exchange testing; cause [open]
  — owner to check front-panel/power state.
- Code consequence already implemented: dump() now retries the V arm
  (up to 3 attempts, exchange-paced) before failing with "no 4-byte
  preamble after V"; no-arg reads I/J/R/v get one readback retry via
  READ_RETRY.

### 2026-10-24c second unit: download SUCCESS + real dump stream structure

Live download of the second (SRAM-working) unit succeeded after an owner
power-cycle [fact]: 705,899 B saved (`bc b4 0a 00` LE 701,628 preamble,
705,894 B body, closing EOT; sha256 34aa7555...5ddda6 in
the 2026-10-24 meter2 dump). Pacing was the blast form
(see the 2025-11 notes: whole pass at ~8 ms/byte, ~0.1 s between in-pass
exchanges); the 2026-10-24b slow-pacing notes are unit-specific samples.

The real dump stream (the thing `dump()` saves) is:

- 4 B preamble `bc b4 0a 00` [fact] (value = LE 701,628; relation to the
  record count [open]).
- `02 01 fe` DUMP_PREFIX (the replay tool's own constant) [fact].
- ~685 pages of item-stream payload, each closed by a 3-B footer
  `02 k ~k` (k XOR 0xFF companion; strict +1 k-cadence across the whole
  dump; start-to-start spacing exactly 1029 B) [fact]. PP-prefix 5-byte
  footer models are refuted arithmetically [fact, agent analysis].
- Page layout [fact, verified 2026-10-25]: `3-B footer + 1,024 payload
  bytes + 2-byte page CRC` = 1,029 B. The first footer is `02 01 fe`; the
  final page carries its CRC but no following footer. The CRC is
  **CRC-16/XMODEM** (poly `0x1021`, init `0x0000`, no reflection) of the
  1,024 payload bytes, stored **big-endian**: all 686 pages of
  the 2026-10-24 meter2 dump verify (`7d 13`, `f0 65`, ... are checksums,
  not junk). Power Log validates it and NAKs a page whose CRC is wrong.
- Footers INTERRUPT items: on 212+ pages a 6-B item is split across the
  footer; the item's tail continues immediately after the footer. Deleting
  ONLY the 3-B footer leaves the 2-B CRC in-stream, which corrupts the
  split item (this produced impossible voltages and a spurious `0b` block
  in earlier parses). Deleting footer + CRC reproduces every reference
  `.fpq` value: meter2 dump -> 37,676 rows, ticks every 120 rows, 0 voltage
  /frequency mismatches against the 2026-10-25 real-meter save for the whole
  overlapping prefix [verified].
- Counter items `06 02 <BE32>` (rare siblings 09/0a `02`): 2,662, all
  payloads multiples of 128, monotone non-decreasing within a recording,
  ~ every 276 B [fact]. The counter is what marks the ring wrap: in
  the 2026-10-24 meter2 dump it drops at row 37,631 (the dump then
  continues into an older segment). Power Log keeps the 37,631 rows before
  that drop and discards the 45 after it; the writer does the same
  [verified: Power Log's saved file from the same wire stream has exactly
  37,631 rows]. The writer applies this cut only when the page grid is
  fully CRC-verified; a lossy capture (dropped bytes) is never trimmed
  this way. Semantic meaning [open] (candidates: an internal elapsed-time
  counter or a memory-usage counter; NOT the every-14-record cadence that
  the replay-side `build_stream()` fabricates).

Boot-diagnostics text [fact] (seen on the UART right after power-on;
useful for future config-get): `Registro OK` / `Mem=0701622` /
`Configuracion OK` / `Calibracion OK` / `Offs=0016275,0016282
Coefs=0009009,0009008 Ruido=0000000,0000000` / `Using > 1.13
calibration` — Spanish; the meaning of Offs/Coefs/Ruido fields [open].
Parser rule: tolerate unsolicited boot text in any reply stream.
