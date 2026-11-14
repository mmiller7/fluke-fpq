# src_common/fpq_editor.py — trimming a `.fpq` session

> **Disclaimer:** This document describes reverse-engineered behaviour and the
> tool is provided **as is**, without warranty of any kind, express or
> implied, including accuracy or fitness for any purpose. Use at your own
> risk; the authors accept no liability for any loss or damage. Not affiliated
> with or endorsed by Fluke Corporation; "Fluke", "VR1710" and "Power Log" are
> trademarks of their owner. This is not a calibrated instrument or safety
> device. See the [README disclaimer](../README.md#disclaimer).
>
> **License:** MIT License (see [LICENSE](../LICENSE)).
>
> **Limitations:**
> - 50 Hz operation is unsupported and unverified (60 Hz only).
> - High-range/208 V event offsets are derived or interpolated, not
>   raw-captured.
> - Byte-exact parity holds only where the guards/evidence reach; boundary
>   behaviour is verified on the documented sessions.
> - Trimming is offline; any hardware operation remains at your own risk.

`src_common/fpq_editor.py` cuts an existing Fluke VR1710 `.fpq` (Power Log Classic 4.60
format, "Fichero FPQ V 4.2") down to a date-time window and writes a new
`.fpq`. It reuses the strict record walker and timestamp helpers from
`fpq_export.py`; it does not contain a second parser.

```
src_common/fpq_editor.py trim INPUT -o OUT [--start "YYYY-MM-DD HH:MM:SS"]
                              [--end   "YYYY-MM-DD HH:MM:SS"] [--dry-run]
```

* `--start` / `--end` are inclusive and use the recorder's local wall-clock
  time (the same naive time shown by `fpq_export.py summary` and Power Log).
  Either side may be omitted (open-ended).
* `--start` after `--end` is rejected before any file is written.
* `--dry-run` prints the kept record counts per type, the kept first/last
  timestamp and the kept G-row range, then exits without writing.
* Gzip-compressed input (`.fpq.gz`, detected by magic) is accepted; output is
  always a plain `.fpq`.

## Boundary rules (explicit)

A record is **kept iff its own timestamp is inside the closed interval**
`[start, end]`. Timestamps are snapped to whole seconds exactly as
`fpq_export.py` does (Power Log-saved files land on whole seconds). Original
record order and record bytes are preserved exactly.

| Record class | Types | Rule |
|---|---|---|
| Measurement rows | `G` (0x47), `F` (0x46) | Kept by timestamp. A normal G/F pair shares its second, so both survive or both are dropped. A lone G or F (possible at a gap edge) is kept on its own. |
| Interval/report records | `K` (0x4B), `H` (0x48), `L` (0x4C), `5C` harmonics | Kept by timestamp. Every record of one reporting tick shares one second (order in the stream is K, optional L, H, 5C, then the G/F pair), so a tick block is **atomic**: it is either wholly inside or wholly outside. A block cannot be split by the window edge. `L` also follows this rule; its timestamp is the 12th `K` tick of the completed 12-tick Pst window. |
| Events | `D` (0x44), `P` (0x50), `I` (0x49), `j` (0x6A), `T` (0x54) | Kept when the **start** timestamp is inside the window. Durations are not clipped, so an event that starts near `--end` may extend beyond it, and an event already in progress at `--start` is dropped (it is not synthesised or shortened). |

If the resulting stream contains **no `G` row**, the edit is refused with a
non-zero exit and a clear message; no output file is created. This covers
windows entirely before the first row, entirely after the last row, and any
window that misses every measurement row. Power Log would have no voltage
data to graph, so refusing is safer than writing a degenerate session.

Interval records that happen to share a second with a G/F row are kept
together with it by the same per-record rule.

## Header handling

The entire header/config block (bytes `0x000` up to the first record, normally
`0x387`) is copied byte-for-byte, with exactly one change:

* `0x383` — `uint32 LE`, total record count of the stream — is rewritten to
  the number of kept records. `fpq_export.walk_records()` uses this field as a
  truncation/misdetection check, and Power Log relies on it as well.
  (`README`/`docs/format.md` and
  the 2026-10-23 header notes document it as
  verified.)

No other header field is touched. In particular:

* `0x373` (recording interval, float64 OLE days) stays as configured, so the
  trimmed file keeps the source's 1/5/60 s cadence (`fpq_export.py summary`
  shows the same `Interval:`).
* The channel Dip/Swell/Transient text slots (header spellings `Pico=` /
  `Umbral=`), `VNom`/`CNom`/`FNom`/`Topol`, the
  XML blob and `PLVer` stay unchanged, so thresholds and nominal settings
  match the source.
* The format has **no documented start/stop timestamp field** in the header
  (see `docs/format.md`); Power Log derives Start/Stop from the record
  timestamps. Nothing else is invented or patched.

## Known limitation

The first `K` record of a source file carries a special 5-byte marker at
`+13` (documented in `docs/format.md`; semantics open). Trimming can make a
different `K` the first one in the output; its bytes are left exactly as in
the source (the per-record bytes are never modified). A trimmed file was
opened successfully in Power Log Classic 4.6 with K/H/5C interval records
present, so this is treated as cosmetic.

## Verification workflow

```
python3 src_common/fpq_editor.py trim in.fpq -o out.fpq \
    --start "2026-10-23 10:00:00" --end "2026-10-23 10:30:00"
python3 fpq_export.py summary out.fpq
python3 fpq_export.py spreadsheet     out.fpq -o out_rms.csv
python3 fpq_export.py events  out.fpq
```

`rms` rows for the window are byte-identical to the source rows, the header
interval/thresholds are unchanged, and the event set is exactly the source
events whose start timestamps fall inside the window.
