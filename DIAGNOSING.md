# Diagnosing this fault

What is known, how to reproduce it, and how to gather evidence about the parts
that are still unknown.

## Reproducing it

`scripts/make-reproducer.py` builds a PDF that provokes the fault reliably on
an affected printer:

```sh
python3 scripts/make-reproducer.py repro.pdf /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf 900
```

One page, 900 distinct glyphs, one embedded hinted font. Send it to the printer
and compare the page counter before and after:

```sh
snmpget -v2c -c public PRINTER 1.3.6.1.2.1.43.10.2.1.4.1.1
```

**Do not trust the print system.** An affected printer accepts the job, runs its
warm-up, reports `job-state = completed`, and marks nothing. Every layer above
it repeats that success. The page counter is the only honest signal.

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

**This is one code path, and there may be others.** Everything above concerns
embedded TrueType fonts with hinting. Nothing has been established about CFF
and OpenType fonts, Type 3 fonts, bitmap and colour fonts, or about failures
arising from anything other than fonts. A printer that still loses jobs with
`ippfix` in place is evidence of a different path, and worth investigating
rather than dismissing.

**The fault has also been recorded as an assertion.** Some failures leave
`ASSERT FAILED / Task: POSTSCRIPT / File: fontcache.c` in the device's log; the
ones reproduced here leave no trace at all. Whether these are the same defect at
different severities is unknown.

## A warning about method

One measurement in this investigation was taken while the printer was out of
paper. The harness only checked whether the page counter moved, so it recorded
that as a font failure. That single wrong data point was then used to fit three
different models, and it took a deliberately extreme test to expose it.

**Always record the printer's state before and after**, and treat any job where
the printer was not ready as saying nothing at all:

```sh
snmpget -v2c -c public PRINTER 1.3.6.1.2.1.25.3.5.1.1.1   # hrPrinterStatus
snmpget -v2c -c public PRINTER 1.3.6.1.2.1.43.16.5.1.2.1.1  # panel text
```

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
