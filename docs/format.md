# fluke-fpq binary file format

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
> - 50 Hz operation is unsupported and unverified (60 Hz only); the 1/200 s
>   D/P duration unit and the 50 Hz frequency codec remain inferences.
> - High-range/208 V event offsets are derived or interpolated, not
>   raw-captured; the official supported minimum is 70 V (below is
>   best-effort).
> - Byte-exact parity holds only where the guards/evidence reach.
> - The transient full scale and clipping point are unresolved; do not use
>   captures to size surges.
> - Hardware operations are at your own risk.

The `.fpq` session format of **Fluke Power Log Classic 4.60** for the Fluke
VR1710 voltage quality recorder. Every claim below was verified against the
binary files of the sample sessions and, where noted, against Power Log's own
CSV exports and screenshots. Byte offsets are **hexadecimal**; multi-byte
values are little-endian; "OLE date" = automation date, float64, days since
1899-12-30.

Confidence tags: **[verified]** — proven against independent ground truth.
**[verified fields]** — the named fields parse as shown, but not every field's
meaning is known. **[strong]** — consistent across all sample files.
**[theory]** — best current hypothesis. **[inferred]** — an interpretation
fitted to the data but not proven independently. **[open]** — meaning not yet
understood.

## File header

```
0x000  17 bytes  banner "Fichero FPQ V 4.2" (0,padded)   [verified]
0x011  4 bytes  unknown (padding)                         [open]
0x015  12 bytes = 3 x uint32 LE (0, 12, 7)              meaning open  [open]
0x021  1 byte zero
0x022  "Tipo=17\0\0\0\0"        device/config type id     [verified]
0x02d  8 threshold slots, 59 bytes each (0x02d..0x204, Canal=0..7); each is
       cleartext "Canal= n Dip= <dip_V> Pico= <swell_V> Umbral= <u>
       Interrupciones<n>"
       - slots 0..6 end with "Interrupciones<n>"; slot 7 ends with
         "Interrupciones;" (no digit), so `config` reports channels 0..6 only
       - Dip / Swell (header `Pico=`): absolute RMS volt thresholds as configured in Power Log
         (they may not track VNom: one sample has VNom=230 with Dip/Swell
         198/218)
       - the digit after Interrupciones was 7 in slots 0..6 of every sample;
         its meaning is unknown (not a ground-present bit: ground and neutral
         are different conductors)
       - Umbral: transient trigger setting, 100, 111, 121 or 200 observed;
         units open
~0x205 " Me= a PT= b ST= c PC= d SC= e"                   [verified fields]
~0x244 " VNom= v CNom= c FNom= f Topol= t Idiom= i NomFa= X"
       - VNom/FNom = nominal voltage/frequency (values differ per file)
       - CNom = 1.00 observed (unused on this recorder)
       - Topol = 2 in every sample (records L1-N and N-G voltages)
~0x29d " Armon=0 PLVer=4600 <confMode>...</cm>"           [verified fields]
       PLVer 4600 = Power Log 4.60; the XML blob keys (fw=1.15 = firmware,
       measName, etc.) are only partly interpreted
0x353  9 bytes  ASCII marker "678901234"                  [verified]
0x35c  0x2b bytes (0x35c..0x386) binary header block; `config_end_offset`
       in the `config` JSON = 0x35c
0x35c    uint32 LE = 32          candidate link to the transient scale [theory]
0x36f    float32 = 1.0                                             [open]
0x373    float64 LE = recording interval, OLE days (x86400 -> 1/5/10/60 s
         observed; matches the RMS row cadence)                     [verified]
0x383    uint32 LE = total record count of the stream               [verified]
0x387  first record; every further byte belongs to the record stream,
       optionally followed by zero padding to EOF                    [verified]
```

Note: the interval is **not** the `ST=`/`SC=` text fields — they were 1000 in
every observed file (meaning open). Every record timestamp in a Power-Log-
saved file lands on a whole second (a few microseconds of float64 round-trip
noise; some print as `X.999999`), so a writer must snap its time anchor to
whole seconds.

## Record stream 0x387..EOF

Each record: **1 type byte**, then an **OLE date** float64 at +1, then the
payload. All record types have the timestamp. Sizes include the type byte:

| Type | Name | Size | Notes |
|---|---|---|---|
| 0x54 | T — transient waveform | 5153 | see below |
| 0x47 | G — voltage-RMS row | 77 | see below |
| 0x46 | F — frequency row | 30 | pairs with G at the same second |
| 0x44 | D — dip event | 22 | below the `Dip` threshold |
| 0x50 | P — swell event | 22 | above the `Swell` threshold (header `Pico=`) |
| 0x49 | I — interruption | 18 | measured voltage near/below 0 |
| 0x6A | j — interval without measurements | 18 | recorder not measuring (e.g. its own supply lost) |
| 0x48 | H — THD row | 65 | ~10-min cadence |
| 0x4B | K — Pst row | 22 | short-term flicker, 10-min |
| 0x4C | L — Plt row | 22 | long-term flicker, 2-h |
| 0x5C | harmonics block | 1262 | 49 lanes |

The stream ends exactly at EOF (or optional zero padding). The last record is
usually a G/F pair or an I/j event.

### G record (77 B) — one voltage-RMS row [verified]

Offsets from the type byte. `f32` = float32, `u32` = uint32.

```
+1   f64  OLE ts (END of the averaging interval; snaps to whole seconds)
+9   u32  = 8 (same voltage-type descriptor as the T record)
+13  u32  = 1
+17  byte zero
+18  f32  L1N Min   +22 f32 L1N Max   +26 f32 L1N Avg
     (half-cycle RMS values: Min/Max = extremes within the interval,
     Avg = interval average; L1N = line-to-neutral voltage)
+30  10 bytes zero (through +39)
+40  u32  = 1                     channel-present flag for NG
+45  f32  NG Min    +49 f32 NG Max    +53 f32 NG Avg
     (NG stores Min, then Max, then Avg -- Max and Avg are swapped
      relative to the L1N slots at +18/+22/+26)
+57  20 bytes zero (through +76)
```
NG = neutral-to-ground voltage channel. The +40 flag was 1 in every
observed file (a recorder configuration without this channel is untested).

### F record (30 B) — one frequency row [verified]

```
+1   f64  OLE ts (same second as its G partner)
+9   u32  = 1     +13 u32  = 1     +17 zero
+18  f32  Frequency Min   +22 f32 Max   +26 f32 Avg   (Hz)
```
All 30 bytes are used; there is no trailing zero byte.

### H record (65 B) — one THD row [verified]

```
+1   f64  OLE ts
+9   u32  = 8
+13  u32  = 1
+17  5 bytes zero (+17..+21)
+22  f32  THD Min slot (0 in every observed record)
+26  f32  THD Avg (%)
+30  35 bytes zero (through +64); the extractor's THD Max slot is an
     unaligned f32 read at +31, so it is always 0
```

Only the Avg is a measurement. The Min and Max slots are never populated
and the CSV prints them as 0.000.

### D (dip) / P (swell) record (22 B) [verified]

```
+1   f64  OLE ts (event)
+9   f64  duration in DAYS (x86400 -> seconds; quantized to 1/4 cycles:
     1/240 s at 60 Hz. The 1/200 s value at 50 Hz is unverified: no
     50 Hz file in the corpus contains a D or P record)
+17  f32  event extreme voltage (V RMS): dip = residual minimum,
     swell = excursion maximum; measured on the L1N channel
+21  trailing byte (0 observed)
```
The event voltage often equals the half-cycle L1N Min/Max of a nearby G row,
but the match depends on the recording interval: with 1-s rows most events
match a row, while 60-s rows store coarser extremes. Some events fall
outside every stored Min/Max because the detector uses a finer window than
the half-cycle rows.

### I (interruption) / j (interval without measurements) record (18 B) [verified]

```
+1   f64  OLE ts (start of the affected interval)
+9   f64  duration in DAYS (x86400 -> s; whole seconds observed)
+17  trailing byte (0 observed)
```
Both coincide with a stop in the G/F row stream spanning about the duration.
Interpretation [theory]: I = the supply was interrupted while the recorder
kept running; j = the recorder itself lost supply / wasn't measuring.
The meter cannot record while it is talking to a PC over serial, so a short
j gap is usually a serial read/configure session rather than line trouble;
longer j gaps can also mean the recorder had no supply. Distinguish
accordingly: I = measured line event, j = recorder-side pause (serial
session or unpowered).
Observed nuance (one 1-s session): the gap's FIRST second can still carry a
lone G record (voltage row) with no F partner — see the CSV-layout section.

### K (Pst, 22 B) / L (Plt, 22 B) [verified]

```
+1   f64  OLE ts
+9   u32  = 1
+13  5 bytes, usually `01 00 00 00 00`
+18  f32  Pst (K) / Plt (L) value (IEC-style indices)
```
The 5 bytes at +13 are mostly `01 00 00 00 00`. The first K of a file
usually carries `01 41 00 76 00` instead, but a few files have that pattern
twice, and some K/L records carry other `01 xx xx xx xx` values (meaning
open; the extractor ignores the field). Most L records have
`01 00 00 00 00` there, with the same kind of occasional variants.

### 5C harmonics block (1262 B) [verified]

```
+9   u32 = 0x401    +13 u32 = 0x100     (constant in every record)
+17  u16 = 0        +19 u16 = 0x0033 (51; semantics open)
+71  49 lanes of 24 B: lane j (j=0..48) = harmonic j+2:
     Min f32 (always 0), Max f32 (always 0), Avg f32 = harmonic voltage
```
The header bytes +17..+70 are constant across every record. Only the Avg
value of each lane is stored. The 0x0033 field sits at +19 (bytes
`00 00 33 00 00` from +17); writing it at +18 instead corrupts the header
a reader uses to frame the block.

### T record (5153 B) — transient waveform [verified]

```
+0    type byte 0x54
+1    8 B   OLE float64 capture timestamp
+9    8 B   metadata: usually zero; some captures carry two uint32 counters
      (meaning open)
+17   4 B   uint32 = 8 (same voltage-type descriptor as G; meaning open)
+21   1 B   = 1 (open)
+22   4 B   uint32 sample count (1280 in all observed records)
+26   5120 B = 1280 x float32 samples, stored in REVERSE display order
+5146 7 B   zero trailer
```

Decode [verified against the captured transient screenshots of the sample
sessions, corr >= 0.99994, max RMS error 0.91 V]:

    volts = float32_samples[::-1] / (32 * sqrt(2))
    t_ms  = arange(1280) * 1000 / 38400

- The stored floats are integer sample codes x 10 (every sample is an exact
  multiple of 10). All observed captures stay within +/-2048 except one,
  which reaches 2330 (about 515 V after scaling). The ADC word size is not
  established, so "code" here means the integer sample value, not a proven
  12-bit word.
  The 1/(32*sqrt(2)) scale reads a code of 2048 as the peak of a 320 V RMS
  sine [inferred: it fits the captures, but the true full scale is open].
- Sample clock: fixed ~38.4 kHz (640 samples per 60 Hz cycle; 768 per cycle
  in the 50 Hz sample file, so the clock is not mains-locked). The 33.33 ms
  window = 2 cycles at 60 Hz.
- **Amplitude limits:** most captures stay below about +/-450 V peak; the
  largest observed sample is about 515 V peak (one capture) and no
  flat-topped clipping is visible there. The true full scale and clipping
  point are unresolved, so do not use captures to size surge magnitudes.

Transient captures: `15 02` (fixed 427 frames) and `15 00` (variable frame
run; see docs/serial.md). Both decode to 1,280 stored samples.

## CSV output layouts (`rms` subcommand)

The `rms` CSV reproduces Power Log's export layout:

- **12-field layout** (`Date, Time, L1N Min/Avg/Max, NG Min/Avg/Max,
  Frequency Min/Avg/Max, <empty>`): written when the file holds no
  THD/harmonic/flicker records (typically a recording shorter than the
  ~10-min harmonic cadence). `--all-columns` forces the full layout.
- **164-column layout**: the above plus `THD V L1N Min/Avg/Max`,
  `Volts Harmonics2..50 L1N Min/Avg/Max`, `Pst  L1N`, `Plt  L1N`, trailing
  empty field. THD/harmonic Min/Max are placeholder slots (always 0 — the
  values are not stored; only THD Avg and lane Averages are real).
- A second with **two G/F pairs** (recorder restart seconds) merges into
  ONE row with both pairs' 6 voltage values, the last pair's frequency —
  such rows have 6 extra fields (170-column rows).
- **Lone-G gap seconds**: one observed file (a gap of ~6 s during a
  measurement gap, G=116,647 vs F=116,646) carries a single G record in
  the FIRST second of a `j` gap with no F partner at that second. `rms`
  tolerates groups of G records whose second has no frequency record at
  all: the row keeps its voltage values but the three Frequency cells
  are empty (lossless), and a stderr `note:` line reports the count.
  Any other G/F mispairing (F without G, out-of-order pairs, leftover
  F records) still fails loudly.
- **Merged-row data may be shifted**: make sure any downstream statistics
  script handles the 170-field merged rows above, otherwise it reads
  the shifted cells -- unexpected NG values in the THD columns of a file
  mean it is reading a merged row. In the wild this affected ~30-60 rows
  per 1-s recording (none in 5-s/10-s files); see
  the 2026-10-24 power-quality dataset notes.
- Formatting: date `dd/mm/YYYY`, time `HH:MM:SS` (local recorder clock, no
  time zone), 3-decimal values half-up-rounded; for |v| < 1 the leading
  `0.` is suppressed (e.g. `.098`) except Pst/Plt; CRLF line endings; no
  newline after the last row.
