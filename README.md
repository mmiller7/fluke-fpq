# fluke-fpq

Disclosure: This project was in part to learn how good or bad AI assisted
software development can work.  It took a bit over a month to do and was
using Opencode Go to expand on my simple serial-remote-bridge-to-VM method
to develop a CLI in Python.  Files in "bootstrap_files_(no-AI)" were created
by hand by me, along with this disclosure path.  Everything else in the
project was created by AI agents.

> **Disclaimer:** Provided **as is**, without warranty of any kind, express or
> implied; no guarantee of accuracy, completeness or fitness for any purpose.
> Use at your own risk; the authors accept no liability for any loss or
> damage. Not affiliated with or endorsed by Fluke Corporation. This is not a
> calibrated instrument or a safety device.
>
> **License:** MIT License (see [LICENSE](LICENSE)).
>
> **Limitations:** 50 Hz operation is unsupported (60 Hz only); the official
> supported minimum is 70 V (below is best-effort); high-range/208 V
> behaviour is derived or interpolated, not raw-captured; byte-exact parity
> holds only where the guards/evidence reach; hardware operations are at your
> own risk. Details in [Limitations](#limitations) and
> [Disclaimer](#disclaimer).

Command-line tools for the **Fluke VR1710 voltage quality recorder** and its
proprietary `.fpq` session files (written by **Fluke Power Log Classic
4.60**), reverse-engineered, not affiliated with or endorsed by Fluke.

## Disclaimer

**Development target: byte-exact parity with Power Log Classic 4.60's output
for the session types covered by the tests** (see the status/validation notes
in this README). This project was built from observed device traffic and the
vendor software's own outputs; it uses no vendor source code, proprietary
algorithms or privileged access. The legal notice below limits liability — it
does not lower that engineering target.

This software is provided **"as is"**, without warranty of any kind, express
or implied, including but not limited to the warranties of merchantability,
fitness for a particular purpose, title and non-infringement. The authors do
not warrant or guarantee that the software, its decoding, its exports or any
reconstructed session is accurate, complete, timely or suitable for any
purpose. Decoded values, event records and reconstructed waveforms can differ
from the instrument and from Power Log's own output.

You use this software and rely on its output **entirely at your own risk**.
To the maximum extent permitted by law, the authors and contributors accept
no liability for any direct, indirect, incidental, special, consequential or
exemplary damages, or for any loss of data, profit, revenue or goodwill,
arising out of or in connection with the software or its output, whether in
contract, tort (including negligence) or otherwise.

50 Hz operation is not verified and may not behave correctly; see
[Limitations](#limitations). This project is an independent
reverse-engineering effort. It is not affiliated with, authorised by or
endorsed by Fluke Corporation. "Fluke",
"VR1710" and "Power Log" are trademarks of their owner and are used only to
describe interoperability. Nothing here is a substitute for a calibrated
instrument, the manufacturer's documentation, or the vendor's official
software when making measurement, compliance or safety decisions.

## Limitations

- Reverse-engineered from sample recordings made with Power Log Classic
  4.6 (firmware 1.15). The format's cleartext fields ("Fichero FPQ", the
  threshold lines) are written in Spanish by that software version
  regardless of its UI language; the parser keys on them, so the standard
  English download decodes fine. Other Power Log versions (e.g. PowerLog
  430-II) are different instrument families and out of scope. If your
  files decode oddly, report it.
- Decode of *saved* 50 Hz sample files (RMS/THD/harmonics) matched Power
  Log's exports on the sample recordings; the meter-side frequency codec,
  D/P duration unit and replay generator are 60 Hz-only. See the 50 Hz
  bullet below. Decode has been checked on 1, 5 and 60 s recordings; a
  10 s file also decodes but was not compared with a Power Log export, and
  other intervals/settings are untested.
- **50 Hz operation is not verified — a fixed limitation, not a data item.**
  The meter-side frequency codec, D/P duration unit and replay generator
  were derived from 60 Hz sessions.  No 50 Hz supply is available here (the
  local mains is 60 Hz and 50 Hz is not possible in this region), so a
  50 Hz fixture cannot be collected.  The 1/200 s quarter-cycle unit and the
  50 Hz frequency codec remain unvalidated inferences; replay refuses a
  50 Hz median.  Decoding a 50 Hz `.fpq` may display, but do not rely on
  50 Hz operation.
- **High-range event offsets come from the `lo` nibble; 208 V is
  interpolated.**  The dip voltage is `code/8 + 32*((lo >> 8) & 0xF)`: the
  `04` item's `lo` high nibble carries the 32 V window, so 120 V (96 V),
  208 V (160/192 V) and the br1 244.5 V row (224 V) all decode exactly,
  including events inside an outage.  The event-row minimum is only a
  fallback for items without a valid nibble.  The vendor 208 V saves are
  consistent with the rule; no dedicated 208 V source is required.
- **Official supported minimum is 70 V.**  Below 70 V is outside the
  vendor's supported range and is best-effort.  The decoder now reproduces
  such excursions exactly when the `04` item's `lo` nibble is present
  (67_ 68.375 V), but do not treat them as supported measurements.
- **1 s lone-event timestamps carry ±1 s — a documented information limit.**
  The wire's `lo` field orders events inside a burst but does not encode
  the burst's wall second.  A single-event dip/swell on a 1 s recording is
  therefore written on one of two adjacent whole seconds.  2 s/5 s/10 s
  lone events are exact (their row grid is wider than the ambiguity).  The
  2026-11-08 paired 1 s/2 s bench capture (`89_bench_dips_2026-11-08/`)
  put the 16 burst dips at one constant official offset (+23 s) and the
  lone 0.05 s dip 1 s off (+22 s); a sub-0.1 s externally anchored event is
  not achievable with the bench equipment, so ±1 s is accepted, not a
  pending fix.  Do not rely on the exact second of a lone 1 s event; the
  row grid and duration are exact.
- **Download cancellation is not supported.**  A transfer must run to
  completion.  Interrupting it can leave the meter waiting for dump ACKs;
  Power Log has no cancel control and behaves the same way, so recovery is
  not guaranteed and a power-cycle may be needed.  If an interrupted run is
  followed by another read, the tool sends a best-effort `C` clear first.
- **Sub-microsecond residuals:** timestamps and Plt can differ from Power
  Log by 1-4 ULP (around 2.5 us for timestamps); values and whole seconds
  are exact.
- **D/P channel/flags:** the trailer is zero in every observed record and
  the P flags/D `lo` epoch are not stored in a `.fpq`; multi-channel event
  behaviour is not exercised.
- "Verified" means the output matches Power Log Classic's own export or
  screen display for the sample recordings — it is not a statement about
  the recorder's measurement accuracy or calibration. For that, use the
  VR1710 manual and Fluke's specifications.

One file per job:

- **`fpq_export.py`** — decodes saved `.fpq` files into standard formats:
  CSV tables, event timelines, and waveform data/graphs.
- **`fpq_serial_meter.py`** — interfaces the recorder itself over the
  serial port: identification/status/threshold/banner probes with the
  same byte pacing Power Log uses, and the memory-dump download — it
  performs the identification pass, arms the dump (`V`), starts it
  (`C`), pumps the `0x06` ACK flow control and saves the raw dump bytes
  (what a real recorder or a `fpq_serial_replay.py` session streams
  back) to a file, and rebuilds the `.fpq` including the stored transient
  (T) waveforms decoded from that dump. It never writes anything to the
  recorder's memory except through the explicit configuration/erase
  commands.
- **`fpq_serial_replay.py`** — the meter side, inverted: replays an
  existing `.fpq` **as** the VR1710 (`serve`), answering Power Log's
  command set and streaming the equivalent dump — so Power Log (or
  `fpq_serial_meter.py download`) can download the same session right
  back out, including the stored transient captures. Its offline
  `self-check` rebuilds a `.fpq`'s item stream and verifies every value
  byte-for-byte: on the two reference saves it reproduces every
  wire-recoverable field exactly (104,889 G/F fields + 5,635 harmonic
  lanes on the 2 s A/B pair, 34,963 rows; 41,388 G/F fields + 5,537 lanes
  on the 5 s power pair, 13,796 rows), plus all 43/43 and 7/7 captures
  sample-exact. Plt recomputes to 9/9 on the rebuilds and differs from the
  stored float32 by 1-2 ULP on the official saves. See
  the 2026-10-28 parity-matrix notes.
- **`src_common/fpq_wire.py`** — internal shared primitives used by both serial
  tools: the page CRC/framing constants, the transient capture codec, and
  the D/P voltage/duration grid. It has no I/O and no CLI.

Source layout: the three CLI files (`fpq_serial_meter.py`,
`fpq_serial_replay.py`, `fpq_export.py`) sit at the top of `fluke-fpq/`;
shared modules live in `src_common/`; `build_portable.py` sits beside the
CLIs and writes the generated `dist/` zipapps.

From one `.fpq` file you get:

- the full RMS table exactly as Power Log's own CSV export (voltages,
  frequency, THD, harmonics, flicker)
- a timeline of power-quality events: dips, swells, interruptions,
  intervals without measurements (recorder-side pauses), and when the
  recorder captured transient waveforms
- the captured transient waveforms as numeric arrays and PNG graphs
- a whole-session PNG graph in the Power Log style: L1N and N-G traces,
  grey outage bands, hatched measurement gaps, and dip/swell/transient
  markers
- the recorder's configuration (thresholds, nominal settings, interval)
- a summary overview (start/stop times, duration, row counts, event
  counts) like Power Log's Summary tabs

The decode tool only reads `.fpq` files; it never talks to the recorder.

### Portable three-tool build

    python3 build_portable.py

writes `fluke-fpq/dist/` with three no-install zipapps:

    fpq-meter.pyz    fpq-replay.pyz    fpq-export.pyz
    fpq-meter ...    fpq-replay ...    fpq-export ...      (Unix launchers)
    fpq-meter.cmd    fpq-replay.cmd    fpq-export.cmd     (Windows)
    README.txt       LICENSE

Each zipapp contains the shared modules and, for the serial tools, pyserial;
the source stays modular.  Python 3.11+ is required on the target.  The
export zipapp works without numpy/matplotlib; transient/PNG export still
needs them installed because they contain OS/architecture-specific compiled
code.

An easy way to get everything from a recording in one step is the
`all` subcommand (see Commands).

Input flexibility: a `.fpq` argument may be

- a plain file
- gzip-compressed, as `FICH.fpq.gz` (detected by magic bytes — the
  extension does not matter)
- `-` to read the file from stdin:
  `cat FICH.fpq | python3 fpq_export.py spreadsheet - -o out.csv`

Decoding output and report text go to **stdout**; the disclaimer banner
and one-line error messages go to **stderr**, so stdout stays clean for
piping (e.g. `... config FICH.fpq 2>/dev/null | jq`).

## Talking to the recorder — `fpq_serial_meter.py`

`python3 fpq_serial_meter.py info -p /dev/ttyUSB0` (Windows: `-p COM3`)
opens the link at the observed 500 000 baud, sends the wake-up, and prints a
summary: firmware banner, status/counters, interval, and Dip/Swell/Transient
thresholds. `--json` gives the same data machine-readable; `--transcript`
prints the raw command/reply trace instead:

    port /dev/ttyUSB0 @ 500000 8N1 — sending wake-up + queries
      ?      reply: ?   (heartbeat echo)
      I      reply: b0 30 b0   (3-byte status, meaning open)
      J      reply: 005   (decimal counter, meaning open)
      i 1    reply: 198   ch1 Dip threshold
      j 1    reply: 218   ch1 Swell threshold
      r 1    reply: 100   ch1 Transient threshold = 2.0 V
      R      reply: 001:002:011 017/002/000   (record-count info string)
      X 0    banner: b'FLUKE 1710 V1.15 25/06/2009'
      v      reply: '000078'  (meaning open)

Commands by purpose:

| command | purpose |
|---|---|
| `info` | read-only: identity, counters, interval, thresholds, banner and the memory-fill estimate in percent (`--json`, `--transcript`, `--resume`) |
| `configure` | change Dip/Swell/transient sensitivity, transient recording on/off, circular memory on/off, or the recording interval; readback-verified; prints current settings, previews by default, `--apply` writes, prints the new settings and re-reads the full `info` state from the meter for comparison |
| `erase`, `resume` | recording control (`erase` is destructive; `--yes`) |
| `download` | read the meter and write a `.fpq` |
| `build-fpq` | offline conversion of a saved raw dump or portable raw `.tar.gz`; no port is opened |
| `debug send` | send one raw command and print the reply; no readback or verification, so it can change settings or erase |
| `debug listen` | passive read for N seconds; usually silent unless another process drives the link |

The old `probe`, `config`, `banner`, `set`, `send` and `sniff` names are
gone; use `info`, `configure`, `debug send` and `debug listen`.

Serial safety and visibility: every written byte is checked (a short write
aborts rather than leaving a half command), `erase` refuses to send `B` when
the counter is unreadable, and `info` stops after a missing identity reply
instead of sending the remaining fields. `-v/--verbose` prints each
command/reply and phase progress on stderr; `--debug-quiet` only sets the
silence window and is not a verbosity control. `info --resume` and
`erase --resume` send `E` when they finish.

Real meters drop bytes written less than ~0.1 s apart, so
command bytes are spaced by 0.15 s. Protocol timing overrides
(`--debug-byte-gap`, `--debug-exchange-gap`, `--debug-quiet`) are in the
"debug timing" help section and normally not needed. The full protocol — command set,
item-stream dump format, `0x06` flow-control and the 10-minute report
blocks — is in `docs/serial.md`.

The meter tool uses a single unified send path (one packed `?` + cmd
exchange form) and aborts a stalled dump instead of pumping ACKs forever.

Write operations are single-byte nudges on the meter, never a value
transfer: `erase` is destructive and requires `--yes`; `resume` continues
recording; `configure` steps a threshold one unit at a time with a readback
burst after every group of steps. No write is ever retried, because a
resend would double-step a threshold or erase twice. `configure`
accepts only integer values in the recorder's ranges: Dip 0-300 V
(`--dip-voltage`), Swell 0-300 V (`--swell-voltage`) and transient
sensitivity 2-20 V (`--transient-sensitivity`), as the official software
enters them. `--transient-recording off` steps the meter to its disabled
state; `on` enables at the given sensitivity or 20 V (the official
behaviour). Circular memory is readback-verified: the leading character of
the `v` reply is the flag (a digit = on, `p` = off), and `info`/`configure`
show it. `configure --circular-memory on|off --apply` reads the current
state, sends the single captured `?x0` toggle only when a change is needed,
waits for the meter to commit, and verifies the new state from `v`.
`--toggle-circular-memory` flips it once. Both require empty memory (the
official erase gate). Never retry `?x0`: two toggles cancel and leave the
state unchanged. Threshold and interval writes still close with `X0` in
Power Log's order; the circular Apply has no trailing `X0`. The recorder's
raw transient register is internal and shown only as volts or "disabled".

`configure --recording-interval INT` changes the recording interval (`1s,
2s, 5s, 10s, 30s, 1m, 5m, 10m, 15m, 20m`). `configure --help` and the
no-argument guide list Power Log's "Typical Continuous Measurement" for each
interval, read from the official dialog. **Use this option on its own:**
combining it with threshold or circular-memory options is refused — apply
those in a separate `configure` pass. The new interval activates at the
next start of recording: use Power Log's flow (erase -> configure -> read
memory -> continue), e.g. `erase --yes`,
`configure --recording-interval 2s --apply`, then
`download --arm-empty --resume`. It validates the value, reads the current settings
(`I J v i1 j1 r1`, Power Log's Apply read pass), refuses unless the memory is
empty (a missing `v` readback aborts instead of passing), sends the step loop
as repeated single-byte exchanges (`?T`/`?t` = ±60 s, `?U`/`?u` = ±1 s -
verified against the captured Power Log Apply), sends `X 0` like Power Log,
and reads the settings back. The change takes effect at the next start of
recording (Power Log's erase -> configure -> read memory -> continue); no
power cycle is needed, and on the experiment unit a power cycle reverted the
settings to defaults. Recording stays paused unless `--resume` is given.
Omitting `--apply` prints the plan (including the exact wire plan,
`?u ?u ...`) and writes nothing. The erase is deliberately left to the separate, approved `erase`
command.

Erase is
confirmed by the `v` record counter dropping to zero; erase alone leaves
the meter paused, so add `--resume` if recording should continue (Power
Log's "Yes and erase memory" sends `? B ? E`).

A download reads the recorder's interval from the settings pass after the
dump and writes the `.fpq` header from that value, not from the 10-minute
block spacing (short sessions have no blocks). With `--raw FILE`, it also
writes `FILE.interval` (JSON) so a later `build-fpq FILE` uses the same
value; `--interval` still overrides both, and a dump with neither shows a
warning instead of silently writing 5 s.

### Raw-only and portable captures

On a slow machine the counter analysis can take minutes. The meter read can
be separated from the `.fpq` build:

    # raw dump + sidecar only; no .fpq processing
    python3 fpq_serial_meter.py download -p /dev/ttyUSB0 \
        --raw br1.bin --raw-only --resume

    # one portable tar.gz (raw + .interval sidecar + README)
    python3 fpq_serial_meter.py download -p /dev/ttyUSB0 \
        --raw-archive br1.tar.gz --raw-only --resume

    # later, on a faster machine
    python3 fpq_serial_meter.py build-fpq br1.tar.gz -o br1.fpq

`--raw-only` requires `--raw` or `--raw-archive`; it exits after the dump,
sidecar and archive are written. `--resume` sends `E` before that exit, so the
meter continues recording while the `.fpq` is built elsewhere. Without
`--resume` the meter stays paused. `--raw-archive` with no `--raw` derives
`NAME.bin` from `NAME.tar.gz`, archives it, and removes the loose raw/sidecar
afterwards; the archive is the only raw output. `build-fpq` detects a
`.tar.gz`/`.tgz`, extracts the raw and sidecar to a temporary directory, and
writes the same `.fpq` as a direct raw build.

Do not combine `--raw-only` with `--erase` unless the archive/raw is already
written and the erase is separately approved; the erase still follows the raw
save and is independent of the `.fpq` build.

### Downloading the memory

    python3 fpq_serial_meter.py download -p /dev/ttyUSB0 -o session.fpq

runs Power Log's exact connect-and-download sequence (wake-up, one full
identification pass, `V` + `C`, `0x06` ACK pump, final `0x04`) and writes a
ready-to-open **`.fpq`** file — the same format `fpq_export.py` and Power
Log use. The item stream has no absolute time, so the newest record is
placed one interval before the anchor and rows are walked back by the
recording interval:

- `--anchor "YYYY-MM-DD HH:MM:SS"` — end-of-recording anchor
  (default: the **arm instant**, the moment the meter paused recording,
  captured by `download`; `build-fpq` reuses it from the sidecar. Only when
  neither exists does the build clock stand in, with a warning. The
  microsecond part is dropped so every record timestamp lands on a whole
  second, as in Power Log's files). **Normal use needs no anchor argument:**
  the tool pauses the meter itself, and the pause/resume host times are also
  written to a session log (`~/.fpq-session.jsonl`, or `FPQ_SESSION_LOG`).
  Standalone use has **no offset**: the arm is the recording end. Only an A/B
  run where another client already paused the meter and the tool re-pauses
  it later carries a read-order gap; that is not a meter or tool offset, the
  recording data is unchanged, and `fpq_export.py compare` can report the
  gap from a later read (see the test flow below);
- `--hour-phase SECONDS` — explicit counter hour-step override for the
  segmented I-duration correction. Without it the data-relative rule is
  used (minute 12.5 s, hour 2886 s, daily phase from the visible daily
  brackets, forward floor step counting), so no recording-start anchor is
  required. 82_ reaches 31/32 and 77_ 18/19 against the official saves with
  only the tool's own pause time; the official save is deterministic over
  the dump bytes, so this is a parity target, not an ambiguity. It also
  takes precedence over the automatic start+stop phase;
- `--start-anchor "YYYY-MM-DD HH:MM:SS"` — optional recording-start wall
  time. With `--anchor` (stop) it selects the hour-phase plateau and the
  day-step branch by total duration instead of the measured default. It is
  not needed in normal use and not required for the wrapped-ring 85_ case;
  the value is stored in the `.interval` sidecar and reused by `build-fpq`;
- the interval is inferred from the 10-minute blocks when present, else 5 s;
  `--interval` overrides it and accepts only the recorder's settings:
  `1s, 2s, 5s, 10s, 30s, 1m, 5m, 10m, 15m, 20m` (unsupported values are
  rejected);
- thresholds (`Dip`/`Swell`/`Transient`) are taken from the identification pass;
- the nominal is chosen automatically from the measured L1-N voltage
  (120 V system -> header 120.00; 208/240 V system -> Power Log's high-range
  value 230.00). `--nominal 120|208|240` overrides it. The recorder has no
  nominal setting. The header `VNom=230.00` is fixed high-range metadata,
  not the input voltage; dip events use the `04` item's `lo` high nibble
  (the 32 V window: 96 V at 120 V, 224 V on a 244.5 V row), with the event
  row's minimum only as a fallback, so 208/240 V dips decode without a
  blanket high-range offset;
- `--fnom` sets the nominal frequency in the header (60 Hz only);
- `--page-ack` (the default) acknowledges at each 1,029-byte page boundary
  instead of waiting for line silence. On a full 1 s ring this measured
  **0.155 s/page vs 0.480 s/page** (3.1x faster) with byte-identical raw
  dumps. `--no-page-ack` restores the older quiet-window fallback;
- `--gzip` writes the `.fpq` gzip-compressed; an output name ending `.gz`
  also triggers compression, and `--gzip` appends `.gz` when needed. The
  decompressed bytes are the exact plain `.fpq`, and `fpq_export.py`,
  replay, self-check and `Fpq` read `.fpq.gz` transparently;
- `--resume` sends `E` **immediately after the dump**, before the `.fpq` is
  parsed and written, so the meter is not held paused through file
  processing; without it the meter is left paused (the default);
- `--erase --yes` erases the recorded data after the raw dump is saved and
  **before** the `.fpq` is built. If `--raw` is not given, a temporary raw
  file is written first and removed after the `.fpq` succeeds (it is kept if
  the build fails). The meter stays paused unless `--resume` is also given;
  `--erase --resume --yes` is Power Log's "yes and erase" flow;
- **meter tail before the `.fpq`:** erase/resume are sent immediately after
  the dump, before the parse/solve/write. `--erase` without `--raw` writes a
  temporary raw first (removed after success, kept on failure). It does
  **not** poll `v` or send a second `E`: the dump exchange has just proved
  the link, and a poll would pause the meter again. A failed post-dump
  settings
  readback exits nonzero. Rationale:
  the 2026-11-04 gap postmortem (project notes);
- `--stats` prints a structure fingerprint (preamble, pages, CRCs, rows,
  blocks, counters, skipped bytes, wrapped rows);
- an erased memory answers the dump arm with a zero preamble and streams
  nothing, so the tool reports "memory is empty", writes no `.fpq`, and
  never arms the dump when `v` reads 0.
- a power-up counter reset — including a short **backwards** base reset in
  the tail (2026-11-08 br1) — cuts the new segment's ring-start block (what
  Power Log keeps) and the CLI warns with the dropped row count. The block
  is page-granular, and an **interior** duplicate page run (footer `k`
  replay) is dropped in the middle rather than cutting the whole suffix; the
  short replay suffix before the reset clock is found by comparing rows one
  ring period earlier. `--keep-after-reset` disables the reset rule for the
  case it cannot distinguish: a genuine pause longer than the ring capacity
  (about 25 h at 1 s). A counter stream it cannot solve is reported as
  `gap_error`, not silently written as a uniform timeline;
- a dip shorter than one recording interval that shares its row with an
  interruption (I) record is not written, matching Power Log in the 2026-11-01/02 sessions.

**No manual anchor, and the official-first test flow.** `download` records
the pause (dump arm) and `resume` / `download --resume` record the resume
host time in a JSONL session log (`session` prints it). In normal use this
tool is the only client, so every pause time is known and no `--anchor` or
`--start-anchor` is needed. To validate against the official software
without making it a dependency: let Power Log pause the meter and save,
`resume` with this tool, then read with this tool. The second read has newer
rows, so the files are not byte-identical, but the shared RMS rows align
exactly and `fpq_export.py compare first.fpq second.fpq` reports the overlap
and the clock offset (0 when both reads are anchored correctly). A read-order
gap from not resuming between the reads is reported as a constant offset;
it is not a tool behavior and the recording data is unchanged. See
`useful_information/86_dependency_removal_plan_2026-11-07/TEST_FLOW.md`.

`--raw FILE` additionally keeps the raw item stream. The same conversion is
available offline for an already-saved dump:

    python3 fpq_serial_meter.py build-fpq saved_dump.bin -o session.fpq

The generated file carries the G/F measurement rows, the 10-minute
K/H/5C records and the computed L (Plt) rows. It also carries the stored
event records decoded from the dump: dips (`0x04`), swells (`0x10`) and
transient captures (`0x15`). Interruptions (`I`) and intervals without
measurements (`j`) are host-derived from the `06`/`09`/`0A` counter
timeline and are written too; no event is fabricated.

**Ring wrap and the trim.** When the `06` counter goes backwards the
dump ends in an older ring segment. The parser trims that segment only when
the page grid is fully CRC-verified; a lossy capture with dropped bytes is
left untrimmed, since a counter jump there is as likely to be a lost byte
as a real wrap. For a plain wrap this matches Power Log: the 2026-10-24 meter2 dump
yields 37,631 rows, the same count Power Log saves (45 older-segment rows
are discarded).

A dump that is not a plain wrap can also contain a stale tail. On the 5 s power raw dump the untrimmed parse holds **13,829 rows**:
rows 0..13,795 match the official file's **13,796** G rows exactly on L1N
Min/Max/Avg, and the remaining **33 rows** match no window of the official
file (stale ring content). The trim now detects the stale-segment boundary
(a duplicated row run pins the start) instead of cutting at the last `06`
counter, so it keeps **13,796 rows**, drops only the 33 stale rows, and
writes the final 1,742 s `j`. Verified: a rebuild from that dump is
**1,666,787 bytes**, exactly the official size, with 13,796/13,796 rows
and all 5 I + 2 j gaps (7/7) written. A build that still reports 13,789
rows is an old parser.

### Replaying a session — `fpq_serial_replay.py`

    python3 fpq_serial_replay.py self-check SESSION.fpq      # offline verify
    python3 fpq_serial_replay.py serve --fpq SESSION.fpq -p /dev/ttyUSB0

`self-check` proves the encoder round-trips a `.fpq` into the item stream
byte-exactly (values, 10-minute blocks, timeline counters and the meter's
real page framing: 3-byte footer + 1,024 payload bytes + 2-byte
CRC-16/XMODEM, 1,029 bytes per page). `serve` then behaves as the recorder
(defaults for the answers whose meaning is still open come from the
captured sessions and are overridable), letting a second Power Log install or the download
tool re-download the session:

    python3 fpq_serial_replay.py serve SESSION.fpq -p /dev/ttyUSB0

(the fpq may be given positionally as above or via --fpq; .fpq.gz is
accepted). The replay derives its rows from the `.fpq`'s own record
timestamps, and the identification replies `I`/`J` from its own interval
(`interval_replies()`), so a 10 s dump is advertised as 10 s, not the old
fixed 5 s.  `--i-reply`/`--j-reply` still override (an empty string silences
the reply). By default `serve` omits the synthetic timeline counters and
the synthetic transient captures: `--counters` and `--transients` are
opt-in. Power Log's post-download decoder deadlocks on either synthetic
class; with both off, a generated replay completes and renders. The cost is
display fidelity, not file data: pages are shorter (no counter items), the
post-download time axis uses the uniform `.fpq` interval (so `I`/`j` pauses
are compressed), and the Transients tab is empty. Voltage/frequency rows,
D/P events and all row values are unchanged.

For exact offline reconstruction use `self-check --counters` (the captures
are still stored); `serve --counters --transients` re-enables the synthetic
items that Power Log rejects, so use it only for tool-side tests.  The
generated stream now matches the native gap schedule on the 61_/65_/67_
references and reproduces the `15 00` variable capture form (2026-11-08),
but a live render test (2026-11-09) confirmed that Power Log still hangs or
shows a blank document on generated counters/captures, even with the native
counter track.  Power Log needs the meter's native item stream, which a
`.fpq` does not store.  To show native counters, gaps and transients, serve
the **real raw dump** with `--stream-file`; that is the only faithful
render path.  A `.fpq`-only replay uses the render-safe default (uniform
interval, no captures).
`--counter-rows`/`--counter-track` remain available for tool-side dumps and
timeline experiments, but a synthetic counter cadence is not accepted by
Power Log's document builder. `--counter-rate` is deprecated and ignored.
The pad is **off by default**. `--pad-now` appends the meter's no-data
marker shape instead of a bare counter: `09` (pause) -> `0A` (resume at the
advanced counter) -> `06` tick, selectable with `--pad-kind`. A bare `06` pad
leaves Power Log on "Drawing Graphs..." regardless of size; every marker kind
tested renders (verified 2026-10-25). The pad did not move Power Log's
absolute date anchor in that test (one fixture family; re-verify before
relying on it), so a generated replay stayed around the download time. A
verbatim dump preserves the captured bytes exactly, but its displayed dates
are still Power Log's download-time anchoring: two verbatim runs of the same
dump showed different From/To windows (see
the 2026-10-25 replay-night notes). Treat only the relative
timeline as meaningful. `--no-pad-now` is accepted for compatibility. With `--stream-file` the
captured bytes are served verbatim, no pad is added, and the `V`/`v` answers
are taken from the file's own 4-byte preamble unless given explicitly.
Bare `?K` is a query Power Log makes when the settings row is not yet
available (distinct from the `?K1` umbral step); the replay answers it with
`--k-reply` (default 200), and the replay tracks `K1`/`k1` steps so an
Apply verifies. The transient encoding is solved (see `docs/serial.md`):
`r` is affine in volts (2 V=100, 5 V=132, 20 V=290), the disabled state is
raw 300, and re-enabling returns to 290.

Power Log re-stamps absolute time from the PC clock either way (in our tests
the saved dates carried a fixed ~391.7-day conversion offset, so treat only
the relative timeline as meaningful), and re-derives dip/swell events from
the replayed values.

Verified in loopback: the large session replays byte-for-byte into the
download tool. Verified with Power Log Classic 4.6 (2026-10-25): a verbatim
meter dump downloads with one `0x06` ACK per page and renders its graph. A
generated stream renders when its counters follow the meter (rate and, for
long sessions, a real counter track); with the old flat 128 units/s ramp the
graph step stalled at the first counter after about 205 s. The page framing,
CRC and handshake are accepted in every case. Power Log assigns absolute
timestamps from the PC clock, so a saved session is not byte-identical to the
source; the readings and their relative spacing are preserved. If a page is
NAKed (`0x15`) the replay resends it. Details:
the 2026-10-25 replay-night notes.

When Power Log asks "Download complete. Keep measuring?", answer **No** for
a replay: the emulator accepts the dump but does not produce a live sample
stream, so "Yes" leaves Power Log polling for live data (verified with a
serial trace). "No" ends the meter session and lets Power Log process the
downloaded data.

## Requirements and install

Python 3.11 or newer. Install the dependencies once:

    # Debian/Ubuntu/Mint:
    sudo apt-get install python3-serial python3-numpy python3-matplotlib

    # Fedora/RHEL/CentOS:
    sudo yum install python3-pyserial python3-numpy python3-matplotlib

    # or in a virtualenv (any distribution; use this instead of installing
    # into the system Python, which Debian/Ubuntu/Mint mark externally
    # managed):
    python3 -m venv .venv
    . .venv/bin/activate
    python3 -m pip install -r requirements.txt   # inside the venv

Only `pyserial` is needed for the two serial tools. `numpy` and `matplotlib`
are used only by `fpq_export.py transients` / `all` (matplotlib for `--png`);
everything else, including `src_common/fpq_editor.py`, runs on the standard library.

If an optional package is missing, `fpq_export.py` prints a one-time note at
startup with the install command and keeps working for the commands that do
not need it. The note goes to stderr, so CSV/JSON on stdout stays clean.
`--help` (main and subcommands) ends with a "Machine status" block listing
which optional packages are present, which features are therefore unavailable,
and the install command. The serial tools show the same block for pyserial.

Run any tool with plain Python:

    python3 fpq_export.py summary session.fpq

On Windows create and activate a virtualenv, then use `python` (not
`py -3`) for both installs and runs:

    py -3 -m venv .venv
    .venv\Scripts\activate
    python -m pip install -r requirements.txt   # inside the venv
    python fpq_export.py summary session.fpq

`py -3` selects the system launcher and ignores an activated venv, so a
package installed in `.venv` can appear missing even though `python` can
import it. The `dist/` `.cmd` launchers prefer `%VIRTUAL_ENV%` and then
`python`; use `python` directly for source runs.

Do not run `pip install` into the system Python (or as root); use the
distribution packages or a virtualenv.

`uv` is optional. Each script keeps a PEP 723 header, so
`uv run fpq_export.py ...` still provisions the dependencies automatically if
you prefer that; nothing in the project requires uv.

On Linux/macOS each file is directly executable: `./fpq_export.py ...`,
`./fpq_serial_meter.py ...`, and so on.

## Quick start

`FICH.fpq` below is a saved Power Log Classic session file (the snippets are
real output from sample recordings, trimmed).

    $ python3 fpq_export.py events FICH.fpq
    2025-08-06T16:29:11 dip                   0.1917 s  V= 226.6 (228) L1N  trailer=0x00
    2025-08-06T16:29:12 interruption          4.0000 s  V=     -    (-)      trailer=0x00
    2025-08-06T16:29:18 transient_capture     -         V=     -    (T3)     -
    ...

    $ python3 fpq_export.py spreadsheet FICH.fpq         # -> FICH_rms.csv
    FICH.fpq: 10188 RMS rows (1018 with THD/Pst) [164-column layout]

    $ python3 fpq_export.py transients FICH.fpq --png -o out/
    FICH: 9 T records -> out/        # out/FICH_T.npz + out/FICH_T0_<timestamp>.csv/.png

    $ python3 fpq_export.py graph FICH.fpq -o session.png
    wrote session.png: 10188 rows, 3 D, 0 P, 9 T, 2 I, 4 j

    $ python3 fpq_export.py settings FICH.fpq            # -> JSON to stdout
    {"FICH.fpq": {"banner": ..., "channels": [
         {"canal": 0, "dip": 228, "pico": 252, ...}],
     "interval_seconds": 1.0, "record_count": 219817, ...}}

## Commands

| Command | Output |
|---|---|
| `fpq_export.py summary FICH.fpq...` | high-level overview to stdout (nominal settings, start/stop/duration, rows, event counts) — below |
| `fpq_export.py all FICH.fpq [-o DIR] [--png] [--all-columns] [--no-csv]` | one-shot full export: summary (stdout + `<stem>_summary.txt`), spreadsheet CSV, events CSV, transients [`<stem>_T.npz` + per-capture CSV/PNG], settings JSON — all into DIR (default: current directory) |
| `fpq_export.py spreadsheet FICH.fpq [-o OUT.csv] [--all-columns]` | Power Log Spreadsheet-identical CSV (below; output file keeps the `_rms.csv` name) |
| `fpq_export.py events FICH.fpq [-o OUT.csv]` | one row per event (below) |
| `fpq_export.py graph FICH.fpq [-o OUT.png]` | whole-session PNG: L1N/N-G min/avg/max traces, outage/gap bands, dip/swell/transient markers |
| `fpq_export.py transients FICH.fpq... [-o DIR] [--no-csv] [--png]` | per-capture CSV/PNG + one `<stem>_T.npz` |
| `fpq_export.py settings FICH.fpq...` | saved recorder settings JSON to stdout (pass several files to compare) |
| `fpq_export.py debug inventory FICH.fpq...` | tolerant record inventory/counts (survives a damaged stream) |
| `fpq_export.py debug records FICH.fpq...` | strict one-line-per-record listing (fails on a damaged stream) |
| `src_common/fpq_editor.py trim FICH.fpq -o OUT.fpq [--start T] [--end T] [--dry-run]` | cut the session to a date-time window (below) |

`-o` for `spreadsheet`/`events` is a CSV path (default: `<input>_rms.csv` next to the
input file; `<input>_events.csv` is not created unless asked).
`transients -o DIR` is a directory (default: the current directory).

### Cutting a session — `src_common/fpq_editor.py`

    python3 src_common/fpq_editor.py trim session.fpq -o cut.fpq \
        --start "2026-10-23 10:00:00" --end "2026-10-23 10:29:59"

Keeps records whose own timestamp falls inside the inclusive window and writes
a new `.fpq` Power Log Classic opens. The config header is copied unchanged
except the record count at `0x383`. G/F rows are kept by timestamp, the
10-minute / 2-hour K/H/L/5C records are atomic, and events are kept by their
start timestamp without clipping their duration. A window with no G row is
refused, so the output always has something to graph. `--start` and `--end`
are each optional (open-ended) and `--dry-run` prints the per-type counts
without writing. Boundary rules: `docs/editor.md`.

### Spreadsheet CSV

Two layouts, both reproducing what Power Log itself exports:

- **12 fields** (`Date`, `Time`, L1N min/avg/max, NG min/avg/max,
  Frequency min/avg/max, one empty trailing field) when the recording has no
  THD/harmonic/flicker data at all — a session too short for the ~10-minute
  calculations.
- **164 columns**: the above plus THD min/avg/max, harmonics 2..50
  (min/avg/max), `Pst  L1N`, `Plt  L1N`. Use `--all-columns` to force this
  layout for every file.
- A recorder-restart second can carry TWO G/F pairs; Power Log merges those
  into one row with 6 extra voltage fields (170-field rows).
- Formatting: `dd/mm/YYYY` + `HH:MM:SS` in the recorder's local clock
  (no time zone), times are the **end** of each averaging interval, values
  3-decimal half-up, CRLF endings, no newline after the last row — the file
  is byte-identical to Power Log's export (verified on sample recordings).
- Column details: "Half Cycle Voltage V RMS" = min/max/average of the
  half-cycle RMS values; THD is %; "Volts Harmonics N" are harmonic voltages
  in V; **THD and harmonic Min/Max are placeholder zeros (never measured),
  only their Avg is real**. Pst/Plt are IEC-flicker indices (10-min / 2-h).
  Rows next to a recording gap can be invalid.

### `events` CSV

| Column | Meaning |
|---|---|
| `type` | `dip` / `swell` / `interruption` / `interval_without_measurements` / `transient_capture` |
| `timestamp_iso` | event second (snapped, recorder clock) |
| `duration_seconds` | dip/swell: quarter-cycle resolution (1/240 s at 60 Hz; the 50 Hz 1/200 s value is unverified — no 50 Hz D/P record exists in the corpus); interruption/gap: whole seconds |
| `voltage` | dip: residual minimum; swell: excursion maximum (V RMS) |
| `threshold` | lowest `Dip` (dips) / highest `Swell` (swells) across the channel slots, in V (the `.fpq` header spells the field `Pico=`) |
| `channel` | `L1N` (line-to-neutral) for dips/swells |
| `t_index` | transient captures: capture number T0..Tn (matches `transients` file names) |
| `trailer_byte` / `record_offset` | D/P: byte at +21; I/j: byte at +17; blank for T. `record_offset` is the record's byte offset in the file |

Dips and swells use the thresholds **stored in the file**; Power Log allows
settings that do not match your nominal voltage (e.g. a 218 V "swell"
threshold on a 230 V setting) — check them in the `config` output.
"interruption" = the voltage was measured near zero; "interval without
measurements" = the recorder was not measuring for that time (data missing;
quality unknown for that period).

### `transients` output

- `<stem>_T.npz` (numpy archive) with arrays:
  `ts` (capture timestamps), `off` (record byte offsets), `raw` (stored
  float32, (N,1280)), `volts` (display-order volts, (N,1280)), `code`
  (integer sample codes = raw/10; the ADC word size is not established),
  `t_ms` (time axis), `meta`, `n`.
- Each capture: `<stem>_T<i>_<timestamp>.csv` (columns `idx, volts, t_ms`)
  and, with `--png`, `<stem>_T<i>_<timestamp>.png` (volts vs waveform
  time in ms).
- Transient captures are 1280 samples over a 33.33 ms window (2 cycles at
  60 Hz). Most captures stay below about ±450 V peak, but one observed
  capture reaches about 515 V with no visible clipping; the true full scale
  and clipping point are unresolved, so do not use captures to size large
  surges. The time base follows Power Log's transient display, which is what
  the decode is calibrated against.

### `summary`

Prints a Power Log-style overview — from what the file actually contains:

```
== FICH
Measurement summary
  Nominal: V=230.0 V  F=60.0 Hz  topology=2
  Thresholds: Dip=[228, ...] V  Swell=[252, ...] V  Transient=[2.0 V]
Recording summary
  Start: 2026-04-01T08:34:36   Stop: 2026-04-02T15:02:53
  Duration: 30:28:17 (109697 s)
  Interval: 1.0 s   RMS rows: 109595   Frequency rows: 109595
Events summary
  Dips: 7   (min residual 223.9 V)
  Swells: 2   (max excursion 259.9 V)
  Interruptions: 2   (longest 35.0 s)
  Intervals without measurements: 1   (longest 92.0 s)
  Transient captures: 54
```

### Settings JSON

Important fields: `channels[]` (`canal` 0..6, `dip`, `pico`, `umbral`,
`interrupciones`), `vnom`, `cnom`, `fnom`, `topol`, `idiom`, `nomfa`,
`plver`, `xml` (the firmware version is inside this blob),
`interval_seconds` (the configured recording interval), `record_count`
(total records — used as an integrity check), `config_end_offset`.

## Errors and exit codes

Exit code 0 = success. Any structural problem prints one message and exits
with code 1:

    FICH.fpq: 133851 records walked but header says 140918 at 0x383
    (file truncated or misdetected stream start)

which happens for: unknown record bytes, truncated records, header
record-count mismatch, RMS-pairing or harmonic-cluster anomalies. A missing
input file raises a normal Python traceback (exit code 1).

### Meter CLI (`fpq-meter`)

Exit code 0 = success; 1 = `TimeoutError`/`ValueError` (meter not answering,
bad data); 2 = serial/port error; **130 = interrupted (Ctrl-C)**.

- If the meter stops answering, the tool prints an actionable warning: it
  is likely hung waiting for dump ACKs; stop and retry, and `resume` or
  power-cycle the recorder if it stays silent. The official software can
  hang the same way (owner, 2026-11-09).
- Ctrl-C during a download sends a best-effort `C` clear and exits 130 with
  a warning instead of a traceback. A dump interrupted mid-stream may still
  need `info`/`resume` or a power-cycle; cancellation is not a supported
  operation (C34l).

## Format specification

The binary layout of `.fpq` files (header, record types, payload decoding)
is documented in `docs/format.md`, with row/CSV extraction details in
`docs/rms-notes.md`, the serial/dump protocol in `docs/serial.md`, and the
trim rules in `docs/editor.md`.

## Operations and evidence

This directory is self-contained; project-level evidence and hardware
bridge scripts are not part of it.

Hardware bridges, lock discipline, the official-vs-tool A/B procedure,
guard commands and the fixture index live in the project-level operations
notes (not shipped here); read them before opening a port.

Remote bridge notes:

- Use the project's meter-pty bridge for `fpq_serial_meter.py` and the
  serial bridge for Power Log. Never run
  arbitrary commands over a `usbip-*` alias: the remote end is a serial pipe,
  so the command text (and any reply) reaches the meter.
- The tool's lock preflight recognises `<FLUKE_LOCK_DIR>/meter-pty`. If the
  bridge is asked for a different pty path, set `FLUKE_LOCK_DIR` to the
  bridge's lock directory so both processes use the same lock.
