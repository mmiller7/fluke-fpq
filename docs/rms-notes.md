# RMS extraction notes (VR1710 `.fpq`)

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
> - 50 Hz operation is unsupported and unverified (60 Hz only).
> - High-range/208 V behaviour is derived or interpolated, not raw-captured.
> - Byte-exact parity holds only where the guards/evidence reach, i.e. on the
>   verified sample sessions.
> - Hardware operations are at your own risk.

The encoding of the RMS / frequency / THD / harmonics / flicker records, and
how `fpq_export.py spreadsheet` turns them into a Power Log-identical CSV. The record
layouts live in `docs/format.md`; this file covers the row/CSV semantics and
the verified extraction rules.

## Verified against ground truth

On the sample sessions the `rms` output matches Power Log's own CSV exports
field for field (and byte for byte where row sets match — including the
missing newline after the last row):

- one session's export was reproduced whole (all 69,564 data rows);
- a second session: all 10,188 exported rows plus 5,763 additional
  pre-period rows that Power Log's export suppressed (the values were
  verified against the same screenshots/export downstream);
- a short session with no THD/harmonic/flicker records reproduced the
  12-field export layout;
- a fourth session reproduced its ~110k-row export byte-identically, and
  all recordings with two same-second G/F pairs merged into one row
  exactly the way Power Log merges them.
Numbers for the specific sample recordings are in the project's
verification notes; the repo text stays sample-neutral.

## Row semantics

- One RMS row = one G (voltage) + one F (frequency) record sharing a
  timestamp. The timestamp marks the **END** of the measurement interval;
  its fractional part is within a few microseconds of a whole second
  (usually exactly on it, sometimes `X.999999`), so extraction snaps to the
  nearest whole second (+0.5 s, then microseconds truncated). That snapped
  time is what appears in the CSV.
- G holds half-cycle RMS values (min/max over the interval, plus the
  interval average of the half-cycle RMS) for both channels (L1N, NG).
- F holds frequency min/max/avg.
- Every ~10 minutes (a multiple of the interval: ~600 rows at 1-s cadence,
  ~120 at 5-s, ~60 at 10-s, ~10 at 60-s) one row carries a harmonic
  cluster: H (THD avg), K (Pst) and the 5C lanes (harmonics 2..50). Plt
  appears every 2 h on rows that carry an L record.
- Rows immediately before/after a record-stream gap (see `events`) can be
  invalid (e.g. ~0.5 V averages during a supply restart); cross-check
  before trusting them.
- A second with two G/F pairs (a recorder restart) merges into ONE row:
  both pairs' voltage values, the last pair's frequency. These rows have
  6 extra fields (170-field rows under the 164-column layout).
- A G row whose second has no F record at all (the first second of a `j`
  gap) exports with empty Frequency cells; `rms` prints a `note:` line to
  stderr for each such group.

## CSV rules (verified)

- Date `dd/mm/YYYY`, time `HH:MM:SS`; 3-decimal values, half-up rounding.
- When |v| < 1 the leading `0.` is suppressed (`.098`) for voltage,
  frequency, THD and harmonics; kept (`0.480`) for Pst/Plt.
- CRLF line endings; no newline after the last row.
- 12-field layout when the file has no harmonic-/flicker-type records;
  otherwise 164 columns (see format.md); `--all-columns` forces 164.

## Error behaviour

- `rms` validates the whole stream before writing and exits 1 with a
  one-line message on: a stream it cannot frame (unknown record bytes or a
  truncated record), a record-count mismatch with the header's count (u32
  at 0x383), a G/F count or order mismatch, or an incomplete harmonic
  cluster (H without its K/5C partners).
- Output is either a complete, well-formed CSV or an error — a truncated
  recording cannot produce a silently truncated export.
