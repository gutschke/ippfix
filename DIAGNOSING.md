# The faults, and how to reproduce them

What is established about the printer's three known faults, how to provoke each
one, and how to gather evidence about the parts that are still unknown. What is
*not* established is in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

Everything here was measured on a Color LaserJet Pro MFP M283fdw. It is one
device, and another model will differ.

| | how it fails | cause | fixed by this proxy |
|---|---|---|---|
| **1. Font cache** | accepted, reported complete, nothing marked | embedded hinted TrueType fonts, in quantity | **yes**, by prevention |
| **2. Vector colour fonts** | rejected with `document-format-error` | unidentified; transparency and shading are involved | no |
| **3. Soft-mask `/BC` arity** | accepted, reported complete, nothing marked | `/BC` array length ≠ its colour space's component count | no |

## First: do not trust the print system

An affected printer accepts the job, runs its warm-up, reports `job-state =
completed`, and marks nothing. Every layer above it repeats that success. The
page counter is the only honest signal, so `scripts/probe-printer.py` reads it
over SNMP (RFC 3805, so any manufacturer) and judges the job on that rather
than on what IPP claims:

```
  before: printer-state=3 (toner-low-warning)  pages=10130
  submitted: job 226
    completed impressions=0 reasons=job-completed-successfully
  after:  printer-state=3 (toner-low-warning)  pages=10130 (+0)
  VERDICT: SILENT-NO-OUTPUT  job reported completed but the page counter did not move
```

An affected printer costs nothing to test, because a job that provokes fault 1
or 3 marks no paper. Only an unaffected printer costs a sheet.

### A warning about method

One measurement in this investigation was taken while the printer was out of
paper. The harness only checked whether the page counter moved, so it recorded
that as a font failure. That single wrong data point was then used to fit three
different models, and it took a deliberately extreme test to expose it.

**Always record the printer's state before and after**, and treat any job where
the printer was not ready as saying nothing at all. `probe-printer.py` enforces
this: it refuses to judge a document when the printer was not ready beforehand,
reporting INCONCLUSIVE and naming the panel text, and it distinguishes an
advisory `-warning` severity from a genuinely blocking condition so that a
merely low tray does not discard good measurements.

A run where the printer could not print is not a data point about the document,
and filing it as one is how three successive models came to be fitted to a
measurement that meant nothing.

Results near a boundary are not reproducible run to run either. Measure well
inside the failing region — 900 glyphs of a hinted font, not 530 — or the data
will be noise.

## Fault 1: the font cache

The fault this proxy exists for. `scripts/make-reproducer.py` builds a PDF that
provokes it reliably:

```sh
python3 scripts/make-reproducer.py repro.pdf /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf 900
python3 scripts/probe-printer.py ipp://192.0.2.10/ipp/print repro.pdf
```

One page, 900 distinct glyphs, one embedded hinted font. The browser path
reaches the same fault when a page supplies its own web font;
`scripts/make-html-reproducer.py` writes two pages that differ only in whether
the font they carry is hinted.

### What is established

**The font's hinting is causally involved.** The clearest result of the
investigation, from a single-variable comparison — the same document, 900
glyphs, one page:

| | result |
|---|---|
| DejaVuSans, hinted | fails |
| DejaVuSans, hinting bytecode removed | prints |
| Arimo (what ChromeOS uses for Arial) | prints |

**It is a property of the font, not of the application.** DejaVuSans fails at
900 glyphs; Arimo prints the same 900. This is why printing web pages has been
largely reliable while printing PDFs has not: a browser renders pages in the
system's own fonts, whereas a PDF arrives carrying whatever font its author
embedded.

**It is not the quantity of hinting.** Arimo carries about 72 bytes of hinting
per glyph and DejaVuSans about 20, and it is DejaVuSans that fails. What
differs is how much work that hinting has to do: DejaVuSans declares
`maxPoints` 852 and composite depth 4, against Arimo's 338 and 1.

**Glyph count matters, but only in combination.** With DejaVuSans the boundary
lies somewhere above 523 glyphs and below 700. With Arimo, 1264 glyphs on one
page print. With a synthetic font of simple unhinted outlines, 26000 glyphs on
one page print.

**Where the device records it at all**, its event log shows an assertion
failure in the interpreter:

```
ASSERT FAILED
Task: POSTSCRIPT
File: fontcache.c  Line: 2494
```

Some failures leave that entry and some leave nothing at all. Whether these are
the same defect at two severities is unknown.

### What is not established

**There is no working formula.** Four models were fitted to measurements and
each was falsified by a later test: the glyph count a font declares (a font
declaring 65535 while drawing 27 printed), the glyphs a page draws (1264
printed where 700 failed), the size of the embedded font program, and the
outline complexity of the glyphs used (519 glyphs in 47 kB failed where 519 in
50 kB printed). Whatever the firmware counts is not visible in the document.

This is why `ippfix` converts every PDF rather than screening. The estimate the
daemon logs is a diagnostic, not a decision: nothing acts on it unless
`--convert-threshold` is set, which it is not by default.

**This is one code path, and there are others.** Everything above concerns
embedded TrueType fonts with hinting. Nothing has been established about CFF
and OpenType fonts at scale. A printer that still loses jobs with `ippfix` in
place is evidence of a further path, and is worth investigating rather than
dismissing.

## Fault 2: vector colour fonts

A printer with a conservative interpreter has no support for colour fonts, and
does not need any: **a colour font never reaches it as a font.** PDF has no way
to represent one, so the browser takes it apart first. What it turns into
depends on the format, and the two families are not equally safe.

Rendered by Chrome 149, one page each:

| source | becomes | printer |
|---|---|---|
| CBDT/CBLC bitmap emoji | Type 3 fonts drawing 80x76 images — 348 image objects for 176 emoji | prints |
| COLR/CPAL vector colour | form XObjects, shadings and transparency groups | **aborts** |

The bitmap case is fine. The vector case is not, and it fails differently from
the font fault: the printer returns `job-state = aborted` with
`document-format-error` rather than silently reporting success, so the client
is told. Colour is not what distinguishes them — **transparency and shading**
are. No assert is logged and no font program is involved.

**What causes it is not known.** The failing page contained 345 form XObjects,
240 shadings and 690 transparency groups, and those counts were mistaken for
the cause. They are not: 700 transparency groups alone, 250 shadings alone, and
240 shadings nested two deep inside 480 groups with blend modes and constant
alpha — deliberately shaped like the COLRv1 page — all print. Do not treat any
of those numbers as a limit.

Ruled out individually: shading types 4/5/6/7, knockout groups, alpha soft
masks, all sixteen blend modes, isolated groups, constant alpha, and
well-formed luminosity masks in quantity. The page needs bisecting again by
construct rather than by count.

**This proxy does not fix it.** That is worth stating plainly, because a first
measurement suggested otherwise: converted through Ghostscript 10.02.1 the page
did print. It printed because that release destroyed most of what the page
contained — ink coverage fell from 5.51% to 2.40%, and better than half the
artwork was gone. Converted through a release that renders it correctly, the
page aborts exactly as the original does. The abort is a real limit in the
printer, and outlining fonts has no bearing on it.

Rasterising such a page would probably print it, but nothing attempts that: the
raster tier is chosen on document size before the job is sent, and nothing in
the proxy reacts to a rejection. That is a plausible improvement and it is not
implemented.

## Fault 3: a malformed soft mask, lost silently

A luminosity soft mask names a colour space in the group it points at, and a
backdrop colour in `/BC`. When the length of `/BC` does not match the number of
components that colour space has, the printer accepts the job, reports
`job-state = completed`, and marks nothing.

Every row below differs in one respect only — the arity. All the values are
zero:

| group `/CS` | components | `/BC` | result |
|---|---|---|---|
| DeviceGray | 1 | `[0]` | prints |
| DeviceGray | 1 | `[0 0 0]` | **silent, no output** |
| DeviceRGB | 3 | `[0]` | **silent, no output** |
| DeviceRGB | 3 | `[0 0 0]` | prints |
| DeviceRGB | 3 | absent | prints |

It fails in both directions. Omitting `/BC` is safe. The reproducer is 855
bytes: one page, one rectangle, one soft mask, no font of any kind —
`reproducers/bc-rgb-bc1.pdf`.

Two nearby explanations were tested and eliminated. It is not duplicate
dictionary keys: a file with a duplicated `/CS` whose values agree prints, and
the duplicate mattered only because this device takes the *first* key where
Ghostscript and poppler take the last. And it is not a general weakness with
malformed array lengths: an image with a `/Decode` array of the wrong length
prints normally.

**Ghostscript and poppler both render the failing file correctly.** That is
what makes it dangerous. Nothing on a desktop shows a problem, and the printer
reports success, so no layer anywhere reports the loss.

**Conversion does not fix it**, confirmed on the printer rather than assumed:
there is no font to remove, so outlining is a no-op, and Ghostscript passes the
mask through verbatim.

`scripts/check-softmask.py` finds the construct in a document without using
paper, so an archive of real jobs can be checked for it. Whether any real
producer emits one is unknown; see [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

## Fidelity: the conversion must not change the page

Removing fonts is worth nothing if the page comes out different. A silently
wrong page is worse than one that fails to print, because nobody checks.

So the conversion is measured, not assumed. `scripts/fidelity-check.py` renders
the original and the converted file with **poppler** — which had no part in the
conversion, so a fault in Ghostscript cannot hide behind a matching fault in
the rasteriser — and compares them pixel by pixel. Rasterising both with
Ghostscript would have missed the defect below entirely, because its renderer
dropped the same construct its writer did.

Text is expected to differ very slightly: outlining removes hinting, so stems
land on the pixel grid differently. Geometry must not move and effects must not
disappear.

### What this found

A test page exercising rounded corners, linear/radial/conic gradients, alpha
and `opacity`, blend modes, shadows, transforms, `clip-path`, hairlines down to
0.0625px, dashed and dotted borders and inline SVG:

| Ghostscript | mean difference | ink coverage | conic gradient |
|---|---|---|---|
| 10.02.1 | 2.28 | 5.53% → **4.79%** | **dropped** |
| 10.07.1 | 0.33 | 5.53% → 5.57% | preserved |

Browsers emit conic and repeating CSS gradients as a `/ShadingType 1`
function-based shading driven by a `/FunctionType 4` PostScript calculator
program. Ghostscript before 10.05 discards them, reporting only `error in
pattern`. The page still prints, with the gradient simply absent.

| Ghostscript | ships in | function-based shadings |
|---|---|---|
| 10.02.1 | Ubuntu 24.04 LTS | **discarded** |
| 10.05.1 | Debian 13 | preserved |
| 10.06.0 | Ubuntu 26.04 LTS | preserved |
| 10.07.1 | upstream | preserved |

### Why there is no version pin

`defont` checks its own output instead. Every class of drawing construct in the
input — `/ShadingType`, `/PatternType`, `/FunctionType` — must still be present
afterwards; if a class has vanished, the conversion is discarded and the
original is sent unchanged. The document then keeps its appearance, and gives
up only the protection against the font fault.

This is deliberately coarse. Ghostscript legitimately merges identical objects,
so comparing exact counts would raise false alarms; a whole class disappearing
is unambiguous. Measured against every version above, it fires only on 10.02.1
and only for documents that actually contain the affected constructs.

Nothing needs to be configured, and nothing needs to be undone later: when the
underlying Ghostscript improves, the check stops firing on its own and those
documents start being converted again.

    defont --selfcheck

reports which behaviour the installed Ghostscript has.

## Asking the printer what it did

**The portable way: SNMP.** The Printer MIB (RFC 3805) is implemented by
essentially every network printer, and the page counter is one OID:

```sh
snmpget -v2c -c public PRINTER 1.3.6.1.2.1.43.10.2.1.4.1.1   # pages printed
snmpget -v2c -c public PRINTER 1.3.6.1.2.1.43.16.5.1.2.1.1   # panel text
snmpwalk -v2c -c public PRINTER 1.3.6.1.2.1.43.18.1.1        # alert table
```

Read the page counter before and after a job. If it has not moved, nothing
printed, whatever the print system claimed. On the printer used to develop
this, that OID returned exactly the same number as the vendor's own counter, so
the standard route loses nothing. SNMP is sometimes disabled by default on
newer firmware; the embedded web server's networking page will have a switch.

**The vendor-specific way, and why it is worth finding.** Manufacturers usually
expose more than the standard MIB does — including, on the printer studied
here, the interpreter's own assertion log, which is what identified fault 1 in
the first place. On HP LaserJet devices this is LEDM, plain XML over HTTP with
no authentication:

```sh
curl -s http://PRINTER/DevMgmt/DiscoveryTree.xml     # index of what exists
curl -s http://PRINTER/DevMgmt/ProductUsageDyn.xml   # page counters
curl -s http://PRINTER/DevMgmt/ProductLogsDyn.xml    # event and error log
```

**Those paths are HP's and nothing else's.** `ippfix` does not use them and
does not depend on them; they are recorded here because they were decisive, and
because the equivalent almost certainly exists under another name on other
hardware. Look for an event log, a service or diagnostics page in the embedded
web server, or a printable configuration or event-log report on the front
panel. Without a log entry naming the failing component, the failure is
indistinguishable from a network problem; with one, the cause is not in doubt.
It is worth spending an hour finding the equivalent on your hardware before
theorising.

## Collecting evidence from real jobs

If jobs are still being lost, the useful thing is the document itself.

```
--archive /var/lib/ippfix/archive --archive-max 50
```

This stores what users print, so it is off by default; see the manual page
before enabling it, and turn it off once the question is answered. Each job is
saved exactly as it arrived, with a sidecar recording the queue, job name,
format and what conversion did.

`--alert-mail` is the cheaper first step: it follows each job to its terminal
state and mails a report when the printer marked nothing, including the
document's structure — producer, embedded font programs, shading and pattern
types, transparency groups, soft masks, a digest — without copying any of its
content.

With a job in hand, the questions worth answering are:

- **What produced it?** `/Producer` distinguishes a browser rendering a page
  (`Skia/PDF`) from a viewer printing a PDF (`PDFium`). They behave
  differently.
- **Which fonts does it embed?** `/FontFile2` for TrueType, `/FontFile3` for
  CFF. A CFF font failing would be new information.
- **Does it carry hinting?** `fpgm`, `prep`, `cvt ` tables and per-glyph
  instructions. Stripping them and re-testing is a one-variable experiment.
- **How many distinct glyphs does one page draw?** For `Identity-H` these are
  the hex strings in the content stream.
- **Does it contain a mismatched soft mask?** `python3
  scripts/check-softmask.py FILE.pdf` answers fault 3 without printing
  anything.
- **Does the fault survive conversion?** If a job fails even after `ippfix`
  outlines it, no font program reached the printer and the cause is something
  else entirely. That is the most interesting result available and worth
  reporting.

## A note on client versions

This was investigated in 2026, against ChromeOS with `Skia/PDF m149`. At that
point web pages printed reliably in testing and only PDFs failed. Earlier
Chrome versions embedded fonts far more liberally — through Chrome 144 a
separate font program was emitted for each *strike* of a typeface, including
one per text colour — so the same printer was very likely much easier to
provoke from an ordinary web page a few revisions earlier.

If you are chasing this on an older ChromeOS, expect the HTML path to matter
much more than it appears to here, and capture jobs early: an archive is far
more valuable while the fault is still frequent than after it becomes rare.
