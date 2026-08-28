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

Rasterising such a page would probably print it, and the proxy now does attempt
that: a job the printer refuses is converted again as raster and sent once more.
Whether it helps for this fault has not been measured — the abort is
`document-format-error`, which is on the retry list, so the machinery fires, but
nobody has confirmed the raster then prints.

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

## What this printer actually does

Measured on an HP Color LaserJet Pro MFP M283fdw, firmware 20251014, and where
noted confirmed on paper. Written down because almost none of it can be derived
from the specification, and several items contradict what the device itself
advertises. A printer of another make will differ; the method is the point.

### Sizes and limits

| | |
|---|---|
| `pdf-k-octets-supported` | `0..75000`, i.e. a 76.8 MB ceiling |
| what actually printed | **92.5 MB**, 20% over, rendered completely |
| `job-k-octets` | not supported; `Validate-Job` cannot be used to probe a size |
| any raster size limit | none declared — no `urf-k-octets-supported`, no `job-k-octets-supported` |
| reported memory | `MEM:MEM=213MB` in `printer-device-id` |

**The declared PDF cap is advisory.** Both images in that 92.5 MB document
rendered, including one occupying the second half of the file, so nothing was
truncated. Do not treat the declared figure as a limit to design around without
testing it; equally, do not assume the next printer ignores it.

Only PDF declares a ceiling at all, which is probably not a coincidence: a PDF
cannot be streamed, because the cross-reference table is at the end, so a
printer must buffer or spool the whole thing. Raster is self-describing per page
with no backward references and can be consumed as it arrives.

### Page complexity

250,000 filled paths on a single page rendered to completion. The test puts one
marker as the first drawing operation of the page and another as the last; both
appeared on paper. A renderer that gave up part way through a page would print
the first and not the second. Nothing we can produce provokes it.

### Duplex, and two ways to get it silently wrong

**`sides` is ignored unless `media` is sent in the same job group.** Alone it
comes back `0x0001` with `sides` in unsupported-attributes; with
`media=na_letter_8.5x11in` beside it, `0x0000` and a genuinely duplexed sheet.
This is a firmware defect: the device publishes
`job-constraints-supported: duplex-unsupported-media` and fails closed when
`media` is absent, where RFC 8011 §5.2 requires it to apply `media-default`.
Every mainstream client always sends `media`, which is why nobody has noticed.

That constraint is an exact-size allowlist, so **a custom page size cannot be
duplexed at all**.

**On the raster path the URF stream wins.** Ghostscript writes a duplex byte
into each URF page header and defaults it to one-sided. A two-page URF carrying
that default was sent with `sides=two-sided-long-edge`; the printer answered
`0x0000` with nothing unsupported, reported two impressions, completed
successfully — **and printed two simplex sheets**. So on raster the document
decides and the IPP attribute is ignored. The converter must therefore be told
what the job asked for; see the `sides=` field on the `%%ippfix` header.

### Never synthesise a media value

`media-supported` advertises `custom_min_3x5in` and `custom_max_8.5x14in`, and
the printer **rejects both when they are requested**. They are range
descriptors, not selectable media. So "choose a supported size" can choose one
the device refuses.

More generally, the absence of `media` is a client asking for automatic
selection, not a gap to be filled. Supplying one pins the tray, suppresses the
device's own size matching, and can scale A4 onto Letter without saying so. This
proxy relays `media` and `sides` untouched and adds neither.

### Failure and status

| condition | what the printer reports |
|---|---|
| out of paper | `job-state 6` processing-stopped, **held indefinitely**, reason `printer-stopped` |
| why it stopped | not in IPP — `other-error` only. The tray is visible **only over SNMP** (`prtInputStatus`) |
| malformed PDF | `job-state 8` aborted, `document-format-error`, within three seconds |
| malformed raster | `job-state 8` aborted, 0 impressions, **and a physical error page** naming a URP parser fault |
| `Cancel-Job` on a held job | `0x0000`, clears cleanly |
| `Cancel-Job` on a terminal job | `0x0404` client-error-not-possible |
| unknown requested-attributes | silently dropped; no `unsupported-attributes` group, so absence is not evidence of a bad request |

### Counters, and which to believe

`job-media-sheets-completed` reported **2** for a job that physically produced
**one** duplex sheet. It counts impressions. Do not use it.

`prtMarkerCounterUnit` is 7 — impressions — on all three HP printers tested
(M283fdw, M553, M430 MFP), so `prtMarkerLifeCount` compares directly with
`job-impressions-completed` even on duplex jobs. **None of the three exposes a
page counter over IPP at all**, which is why the cross-check reads it over SNMP.

`hrPrinterStatus` is `other(1)` on a sleeping printer, not `idle(3)`. A
readiness check built on it would call a healthy printer broken.

### Supply levels, and a printer that argues with itself

Measured 2026-08-26. The M283fdw reports:

```
marker-names               Cyan | Magenta | Yellow | Black
marker-levels              0, 0, 0, 54
marker-low-levels          1, 1, 1, 1
printer-state              3 (idle)
printer-is-accepting-jobs  true
printer-state-reasons      toner-low-warning
printer-alert              code=markerTonerAlmostEmpty
```

Three supplies at **0**, which is below the printer's own low mark of 1 — that
is the printer calling them empty — while every other field in the same message
says it is idle, taking jobs, and merely *low*. It has read this way for months
and prints colour perfectly well, so the levels are the part that is wrong.

This is not cosmetic. Clients split on which half they believe:

* ChromeOS reads the reason, shows a low-toner warning, and prints.
* Android's Mopria service believes the number, decides three cartridges are
  empty, and **refuses to create a job at all**. The proxy's log shows
  `Get-Printer-Attributes` polled every 1.3 s and not one `Validate-Job` — the
  refusal happens client-side, before anything is submitted.

The pre-flight check runs in the print dialog. A job created while the printer
was unreachable — so the dialog had no attributes to object to — is handed to
the spooler and submitted later without that gate being re-applied. That is why
one queued job printed normally through the same proxy, with the same supply
levels, minutes before the next attempt was refused.

The proxy therefore reports a level the printer would call empty at the
printer's own low mark instead, and only while the printer is contradicting
itself. See `supply-levels=clamped|raw` in **ippfix(8)**.

### Pages placed off the sheet

A job whose sheet has a wide unprinted band along one edge and clipped content
along the opposite one, while the sender's preview looked correct, is usually
not the printer and not the PPD. Two real jobs looked like this:

```
page   /MediaBox [ 0 0 576 657 ]                    <- the source page's TrimBox SIZE
       /Contents: q q 1 0 0 1 0 0 cm /X1 Do Q Q     <- nothing else at all
form   /BBox   [ 72 103.5 648 760.5 ]               <- the source page's TrimBox
       /Matrix [ 1 0 0 1 -72 -103.5 ]               <- minus its ORIGIN
       content: q 12 60.10498 588 671.79004 re W* n
                1.02083337 0 0 1.02083337 -61.5 -45.000023 cm
```

Read the prepended transform on its own and it is correct, for Letter:
`1.02083337` is 588/576 where 588 is 612 less two 12pt margins, which is what
the printer advertises in `media-left-margin-supported`; the clip's corner
`(12, 60.105)` is `(612-588)/2` and `(792-671.79)/2`, so it is centred on a
612x792 sheet; and the translation maps the source page's CropBox corner onto
it exactly. The fit, the ticket and the margins are all right. What is wrong is
that the page kept the wrapper it was imported with -- `/MediaBox` at the
source box's size, and a `/Matrix` subtracting that box's origin a second time
-- so both placements apply and the content lands at `(-60, -43)` on a 576x657
page instead of `(12, 60)` on a 612x792 one.

Useful facts when reading one of these:

- **Do not trust `/Producer`.** These jobs said `PDFium` and began
  `% This file was generated by pdftopdf`: the wrapper was written by CUPS's
  own filter, which preserved the `Info` dictionary of its input. The producer
  names the wrong program.
- **The trigger is a non-zero box origin.** A page with `MediaBox [0 0 w h]`
  and no CropBox produces an identity `/Matrix` and the fault does not appear.
  Press-ready files, with the trimmed page inset inside a larger sheet, are
  where it shows up -- and then on every page of the document.
- **"Save as PDF" does not reproduce it.** That destination emits a verbatim
  page copy with no fit transform at all, because there are no hardware margins
  to fit to. Reproducing this needs a destination that advertises them;
  `scripts/fakeprinter.py` is one that marks no paper.
- **The discriminator is one equation.** Push the form's `/BBox` through the
  leading scale. If it lands on the leading clip, the `/BBox` is in the space
  the content was in *before* the fit, which is the mistake. Measured, the real
  jobs miss by 1.1pt -- the gap between the source page's CropBox and TrimBox,
  which is the fingerprint of one program using both -- while healthy documents
  of a similar shape miss by tens to hundreds of points.

- **It has an expiry date.** The stale box is reset upstream as of Chromium
  M153 (August 2026), and the filter that reads it was replaced independently
  in libcupsfilters 2.2.0. Once a client stops leaving the box behind, the
  wrapper's `/Matrix` becomes the identity and the proxy's rule stops matching
  on its own -- no flag has to be cleared. If jobs from a fixed client are
  still being repaired, that is a bug here, not there.

Four jobs captured off the wire against a printer that marks no paper say two
more things, both of which shaped what the proxy will and will not do:

- **The Send-Document carries no media.** Every one of them opened with
  Create-Job, stated the media there, and repeated nothing on the request that
  carried the pages. So on the path this fault actually arrives by, the ticket
  is silent, and the sheet has to be recognised as a size paper comes in rather
  than read from the job.
- **There is more than one way a sender places the page, and only one is
  handled.** Three of the four carried the identical wrapper -- the same fault
  -- differing only in the leading scale: 1.5, 0.9 and 1.03298616. The clip is
  always the scaled page box, and where it lands says how the sender placed it:

  ```
  scale 1.03298616   clip x  -0.000.. 595.000   y   80.606.. 760.394
  scale 1.5          clip x   0.000.. 864.000   y -195.120.. 792.000
  scale 0.9          clip x   0.000.. 518.400   y  199.728.. 792.000
  ```

  The first is centred -- equal margins top and bottom, flush to the paper
  edges left and right -- so doubling a margin gives A4 and it is repaired. The
  other two are **top-left aligned**: left edge exactly 0, top edge exactly
  792, which is a Letter sheet at a scale the sender chose. That is just as
  much the same fault, and the discriminator confirms it (0.81pt and 0.49pt).

  They are still left alone, because a centred clip fixes both dimensions of
  the sheet while a top-left one fixes only the height: nothing in the file
  says how wide the paper is, and at 150% the content overruns the width
  anyway. Recognising Letter from a height of 792 alone would be a guess where
  every other case here is arithmetic. Two samples is not enough to widen the
  rule on, and the ticket that would settle it -- `media` and `print-scaling`
  -- is stated on the Create-Job, which is why `scripts/fakeprinter.py` is
  worth pointing a client at: it can record that half.

Later captures answered two things that had been open:

- **The sheet derived from the clip is the sheet the client asked for.** The
  proxy cannot see the media on the request that carries the pages, so it
  recognises the sheet from the fit rectangle instead. Against tickets recorded
  on the Create-Job, that inference has agreed every time -- Letter where the
  client sent 612x792, A4 where it sent 210x297mm. The recognition is doing the
  same job the ticket would.
- **Landscape is not affected.** A client turning pages on their side puts the
  rotation in the form's `/Matrix`, which absorbs the source box's origin
  instead of applying it a second time, and the page comes out correct. Two
  book pages set two to a sheet rendered with nothing clipped. The demand that
  `/Matrix` be a pure translation is what keeps the repair away from it.

Two limits worth knowing, both of which cost a repair rather than risk one:

- The fit rectangle is centred in the **printable area**, not on the sheet, so
  deriving the sheet from it only works while the hardware margins are
  symmetric. They are on the printer measured here (4.23 mm on all four
  edges); a printer with a deeper bottom margin, as many inkjets have, yields a
  sheet that is not a real size and the job is left alone.
- A client that opens with Create-Job states its media there, and the
  Send-Document carrying the pages repeats nothing. This proxy keeps no
  per-job state to remember it with, so on that path the sheet has to be one
  that paper actually comes in before it is believed.

See `page-geometry=repair|raw` in **ippfix(8)** for what the proxy does about
it, and what it refuses to do.

### Jobs

`multiple-document-jobs-supported` is false, so a job carries exactly one
document. `multiple-operation-time-out` is 120 seconds with action
`abort-job` — shorter than a large conversion takes, so Create-Job followed by
Send-Document is not a way to hold a job open while working on it.

## Collecting evidence from real jobs

If jobs are still being lost, the useful thing is the document itself.

```
--archive /var/lib/ippfix/archive --archive-max 50
```

This stores what users print, so it is off by default; see the manual page
before enabling it, and turn it off once the question is answered — schedule
`ippfix-archive-reminder --schedule DATE` at the same time and that becomes the
only step left to remember. Each job is saved exactly as it arrived, with a
sidecar recording the queue, job name, format and what conversion did.

`--alert-mail` is the cheaper first step: it follows each job to its terminal
state and mails a report when the printer marked nothing, including the
document's structure — producer, embedded font programs, shading and pattern
types, transparency groups, soft masks, a digest — without copying any of its
content, plus what the printer said about itself at the time.

The two flags are worth more together than apart. With both on, a report about
a lost job arrives with the document that provoked it attached, and with the
version the proxy actually sent alongside it — which is the difference between
knowing a job was lost and being able to reproduce it. Without `--archive` the
report can describe the document but not hand it over, and by the time anyone
reads it the job is gone.

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
