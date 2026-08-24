# Diagnosing this fault

What is known, how to reproduce it, and how to gather evidence about the parts
that are still unknown.

## Reproducing it

`scripts/make-reproducer.py` builds a PDF that provokes the fault reliably on
an affected printer:

```sh
python3 scripts/make-reproducer.py repro.pdf /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf 900
```

One page, 900 distinct glyphs, one embedded hinted font. Send it with:

```sh
python3 scripts/probe-printer.py ipp://PRINTER/ipp/print repro.pdf
```

**Do not trust the print system.** An affected printer accepts the job, runs its
warm-up, reports `job-state = completed`, and marks nothing. Every layer above
it repeats that success. The page counter is the only honest signal, so
`probe-printer.py` reads it over SNMP (RFC 3805, so any manufacturer) and
judges the job on that rather than on what IPP claims:

```
  before: printer-state=3 (toner-low-warning)  pages=10130
  submitted: job 226
    completed impressions=0 reasons=job-completed-successfully
  after:  printer-state=3 (toner-low-warning)  pages=10130 (+0)
  VERDICT: SILENT-NO-OUTPUT  job reported completed but the page counter did not move
```

An affected printer costs nothing to test, because a job that provokes the fault
marks no paper. Only an unaffected printer costs a sheet.

## What is established

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
per glyph and DejaVuSans about 20, and it is DejaVuSans that fails. What differs
is how much work that hinting has to do: DejaVuSans declares `maxPoints` 852 and
composite depth 4, against Arimo's 338 and 1.

**Glyph count matters, but only in combination.** With DejaVuSans the boundary
is somewhere above 523 glyphs and below 700. With Arimo, 1264 glyphs on one page
prints. With a synthetic font of simple unhinted outlines, 26000 glyphs on one
page prints.

## What is not established

**There is no working formula.** Four were fitted to measurements and each was
falsified by a later test: the glyph count a font declares, the glyphs a page
draws, the size of the embedded font program, and the outline complexity of the
glyphs used. Do not trust a threshold derived from any of them. This is why
`ippfix` converts every PDF by default rather than predicting which ones need
it.

**This is one code path, and there are others.** Everything above concerns
embedded TrueType fonts with hinting. A second, unrelated failure is documented
below. Nothing has been established about CFF and OpenType fonts, or about
failures arising from anything other than fonts and transparency. A printer that
still loses jobs with `ippfix` in place is evidence of a further path, and worth
investigating rather than dismissing.

**The fault has also been recorded as an assertion.** Some failures leave
`ASSERT FAILED / Task: POSTSCRIPT / File: fontcache.c` in the device's log; the
ones reproduced here leave no trace at all. Whether these are the same defect at
different severities is unknown.

## A second failure: transparency, not fonts

A printer with a conservative interpreter has no support for colour fonts, and
does not need any: **a colour font never reaches it as a font.** PDF has no way
to represent one, so the browser takes it apart first. What it turns into
depends on the format, and the two families are not equally safe.

Rendered by Chrome 149, one page each:

| source | becomes | printer |
|---|---|---|
| CBDT/CBLC bitmap emoji | Type 3 fonts drawing 80x76 images -- 348 image objects for 176 emoji | prints |
| COLR/CPAL vector colour | 345 form XObjects, 240 shadings, **690 transparency groups** | **aborts** |

The bitmap case is fine. The vector case is not, and it fails differently from
the font fault: the printer returns `job-state = aborted` with
`document-format-error` rather than silently reporting success, so the client is
told. Colour is not what distinguishes them -- **transparency and shading** are.
No assert is logged and no font program is involved.

**This proxy does not fix it.** That is worth stating plainly, because a first
measurement suggested otherwise: converted through Ghostscript 10.02.1 the page
did print. It printed because that release destroyed most of what the page
contained -- ink coverage fell from 5.51% to 2.40%, and better than half the
artwork was gone. Converted through a release that renders it correctly, the
page aborts exactly as the original does. The abort is a real limit in the
printer, and outlining fonts has no bearing on it.

For such documents the only thing that would help is rasterising, which is not
currently attempted on a format error. That is a plausible improvement and is
not implemented.

## Fidelity: the conversion must not change the page

Removing fonts is worth nothing if the page comes out different. A silently
wrong page is worse than one that fails to print, because nobody checks.

So the conversion is measured, not assumed. `fidelity.py` in this repository
renders the original and the converted file with **poppler** -- which had no
part in the conversion, so a fault in Ghostscript cannot hide behind a matching
fault in the rasteriser -- and compares them pixel by pixel. Rasterising both
with Ghostscript would have missed the defect below entirely, because its
renderer dropped the same construct its writer did.

Text is expected to differ very slightly: outlining removes hinting, so stems
land on the pixel grid differently. Geometry must not move and effects must not
disappear.

### What this found

A test page exercising rounded corners, linear/radial/conic gradients, alpha
and `opacity`, blend modes, shadows, transforms, `clip-path`, hairlines down to
0.0625px, dashed and dotted borders and inline SVG:

| Ghostscript | mean difference | ink coverage | conic gradient |
|---|---|---|---|
| 10.02.1 | 2.28 | 5.53% -> **4.79%** | **dropped** |
| 10.07.1 | 0.33 | 5.53% -> 5.57% | preserved |

Browsers emit conic and repeating CSS gradients as a `/ShadingType 1`
function-based shading driven by a `/FunctionType 4` PostScript calculator
program. Ghostscript 10.02.1 discards them, reporting only `error in pattern`.
The page still prints, with the gradient simply absent.

| Ghostscript | ships in | function-based shadings |
|---|---|---|
| 10.02.1 | Ubuntu 24.04 LTS | **discarded** |
| 10.05.1 | Debian 13 | preserved |
| 10.06.0 | Ubuntu 26.04 LTS | preserved |
| 10.07.1 | upstream | preserved |

### Why there is no version pin

`defont` checks its own output instead. Every class of drawing construct in the
input -- `/ShadingType`, `/PatternType`, `/FunctionType` -- must still be present
afterwards; if a class has vanished, the conversion is discarded and the
original is sent unchanged. The document then keeps its appearance, and gives up
only the protection against the font fault.

This is deliberately coarse. Ghostscript legitimately merges identical objects,
so comparing exact counts would raise false alarms; a whole class disappearing
is unambiguous. Measured against every version above, it fires only on 10.02.1
and only for documents that actually contain the affected constructs.

Nothing needs to be configured, and nothing needs to be undone later: when the
underlying Ghostscript improves, the check stops firing on its own and those
documents start being converted again.

    defont --selfcheck

reports which behaviour the installed Ghostscript has.

## A warning about method

One measurement in this investigation was taken while the printer was out of
paper. The harness only checked whether the page counter moved, so it recorded
that as a font failure. That single wrong data point was then used to fit three
different models, and it took a deliberately extreme test to expose it.

**Always record the printer's state before and after**, and treat any job where
the printer was not ready as saying nothing at all. `probe-printer.py` does
this: it refuses to judge a document when the printer was not ready beforehand,
reporting INCONCLUSIVE and naming the panel text, and it distinguishes an
advisory `-warning` severity from a genuinely blocking condition so that a
merely low tray does not discard good measurements.

That distinction is the whole lesson. A run where the printer could not print
is not a data point about the document, and filing it as one is how three
successive models came to be fitted to a measurement that meant nothing.

Results near the boundary are not reproducible run to run. Measure well inside
the failing region — 900 glyphs of a hinted font, not 530 — or the data will be
noise.

## Collecting evidence

If jobs are still being lost, the useful thing is the document itself.

```
--archive /var/lib/ippfix/archive --archive-max 50
```

This stores what users print, so it is off by default; see the manual page
before enabling it, and turn it off once the question is answered. Each job is
saved exactly as it arrived, with a sidecar recording the queue, job name,
format and what conversion did.

With a job in hand, the questions worth answering are:

- **What produced it?** `/Producer` distinguishes a browser rendering a page
  (`Skia/PDF`) from a viewer printing a PDF (`PDFium`). They behave differently.
- **Which fonts does it embed?** `/FontFile2` for TrueType, `/FontFile3` for
  CFF. A CFF font failing would be new information.
- **Does it carry hinting?** `fpgm`, `prep`, `cvt ` tables and per-glyph
  instructions. Stripping them and re-testing is a one-variable experiment.
- **How many distinct glyphs does one page draw?** For `Identity-H` these are
  the hex strings in the content stream.
- **Does the fault survive conversion?** If a job fails even after `ippfix`
  outlines it, no font program reached the printer and the cause is something
  else entirely. That is the most interesting result available and worth
  reporting.

## A note on timing

This was investigated in 2026, against ChromeOS with `Skia/PDF m149`. At that
point web pages printed reliably in testing and only PDFs failed. Earlier Chrome
versions embedded fonts far more liberally — through Chrome 144 a separate font
program was emitted for each *strike* of a typeface, including one per text
colour — so the same printer was very likely much easier to provoke from an
ordinary web page a few revisions earlier.

If you are chasing this on an older ChromeOS, expect the HTML path to matter
much more than it appears to here, and capture jobs early: the archive is far
more valuable while the fault is still frequent than after it becomes rare.
