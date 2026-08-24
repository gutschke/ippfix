# Open questions

What is not solved, what is not understood, and what would be worth
investigating next. Kept separate from `DIAGNOSING.md`, which records what *is*
established.

## The raster tier almost never helps, and long documents fall through it

Measured on this printer (`pdf-k-octets-supported = 0..75000`, so a 61.4 MB
working limit) with synthetic body copy at two densities:

| | outlined PDF | URF raster 600 dpi | outlining time |
|---|---|---|---|
| dense, ~4200 chars/page | 649 KB/page | 2.46 MB/page | 0.51 s/page |
| report, ~2500 chars/page | 387 KB/page | 1.53 MB/page | 0.31 s/page |

Three ceilings apply, and they are much closer together than they look:

| ceiling | dense | report |
|---|---|---|
| outlined PDF exceeds what the printer accepts | 92 pages | 155 pages |
| raster exceeds `MAX_CONVERTED` (256 MB) | 109 pages | 175 pages |
| conversion exceeds `--timeout` / `RuntimeMaxSec` (300 s) | 395 pages | 579 pages |

**The raster tier therefore only helps for documents between about 155 and 175
pages** (92–109 for dense text) — a window under twenty pages wide. Shorter
documents never reach it. Longer ones produce a raster larger than
`MAX_CONVERTED`, so conversion is abandoned and the **original is relayed
unconverted**, which is the one outcome the proxy exists to avoid. Verified by
lowering the ceiling and watching `convert()` return `relayed (too large)`.

So a 200-page report is sent to the printer with its fonts intact today. It
will usually print, because most pages are ordinary — but it is exactly the
unprotected case, and it is reached by an ordinary document rather than a
pathological one.

Raising `MAX_CONVERTED` widens the window but does not fix the shape: 500 report
pages would mean pushing 765 MB of raster at a printer, and the timeout ceiling
arrives soon after. The tier is the wrong shape for the problem.

**Splitting is the fix that has the right shape.** Convert everything, as now,
and when the outlined result will not fit, send it as several upstream jobs
whose pages add up to the original. That keeps vector text, keeps every page
converted, and has no size ceiling at all. What it costs is in
[the note below](#splitting-an-over-large-job).

### Splitting an over-large job

Not yet implemented; recorded so the design questions are not rediscovered.

`multiple-document-jobs-supported` is **false** on this printer, so several
chunks cannot be sent as one job with repeated `Send-Document`. It has to be
several separate jobs, and the client must not be able to tell:

- **Identity.** The client is told one job id and polls it. The proxy has to map
  that id onto N upstream ids, and answer `Get-Job-Attributes` by aggregating:
  the state of the chunk still running, and `job-impressions-completed` summed
  across those finished.
- **Cancellation.** `Cancel-Job` on the client's id must cancel every chunk not
  yet printed, and there is no way to recall what already came out.
- **Ordering.** Nothing else may interleave, so the queue lock has to be held
  across the whole sequence rather than around one exchange.
- **Partial failure.** If chunk three is rejected, chunks one and two are
  already on paper. The client has to be told the job failed, and the report
  needs to say how far it got.
- **Where the split happens.** Splitting a PDF means parsing one, which is
  deliberately confined to the converter. The cleanest form is a page range
  passed to the converter (`-dFirstPage`/`-dLastPage`) and called once per
  chunk, which re-reads a small input rather than inventing a framing protocol
  for several documents on one socket.
- **When it triggers.** Only when the outlined result would exceed what the
  printer declares it accepts. Never on page count, and never as a way to avoid
  converting.

## What this proxy does and does not protect against

Three firmware faults have been reproduced on one printer. The proxy addresses
one of them.

| | failure | fixed by this proxy |
|---|---|---|
| **1. Font cache** | job accepted, reported complete, nothing marked | **yes** — no font program reaches the printer |
| **2. Transparency/shading** | job rejected with `document-format-error` | no |
| **3. Soft-mask `/BC` arity** | job accepted, reported complete, nothing marked | no |

That table is the honest summary of the project's scope. It removes the fault
it was built for, and does not touch two others that were found while looking.

### The structural limit

Faults 1 and 3 both end with the printer reporting success. Nothing in the
proxy's handling of a job reacts to what the printer then does with it: the one
fallback it has — rasterising — is chosen from the document's size before the
job is sent, and never from the outcome. **A failure that reports success
cannot change how a job is prepared**, and no improvement to the fallback
changes that.

Fault 1 is nevertheless handled, because it is prevented rather than detected:
outlining removes the font programs, so the condition cannot arise. Fault 3
cannot be prevented that way, because the malformed construct is in the input
and conversion preserves it faithfully.

`--alert-mail` closes half of this. Each job is followed to its terminal state
and judged on what the printer says it marked, so a silent loss is at least
reported to a human rather than disappearing. It observes; it does not repair,
and it arrives after the job is gone.

Repairing this class means **inspecting documents before sending them**, which
is a different mechanism from anything the proxy does today. That is the single
largest open design question here.

## Ranked, with what it would take

### 1. Does any real producer emit a mismatched `/BC`?

The input that triggers fault 3 was constructed by hand. Nothing produced by a
browser has been seen to contain one, but that is weak evidence: a desktop
prints **existing PDF files** as readily as web pages, so the population is
every PDF anyone might open — word processors, typesetting systems, scanners,
export filters, long-dead generators — not just what one browser emits.

`scripts/check-softmask.py` answers this without using paper. Run it over a
corpus of real jobs; `--archive` in the proxy can collect one, though it stores
users' documents and should be switched off again afterwards.

If the answer is yes, repairing it is cheap and precise: walk each ExtGState
`/SMask`, resolve its group's colour space, and pad, truncate or delete `/BC`.
Deleting is provably safe, since omitting `/BC` prints.

### 2. What actually causes fault 2?

Unknown. The original description — "690 transparency groups, 240 shadings" —
described the failing page and was mistaken for its cause. It is not: 700 groups
alone, 250 shadings alone, and 240 shadings nested two deep inside 480 groups
with blend modes and constant alpha all print.

Ruled out individually: shading types 4/5/6/7, knockout groups, alpha soft
masks, all sixteen blend modes, isolated groups, constant alpha, and
well-formed luminosity masks in quantity. The COLRv1 page needs bisecting again,
by construct rather than by count.

### 3. Constructs the proxy itself creates

Outlining is not free of risk, and this is where "made it worse" would live:

- **Text used as a clip path** (`Tr 7`) becomes an enormous clip path when
  outlined. Untested.
- **Very long content streams and deep `q`/`Q` nesting**, because outlining
  inlines a full path at every glyph occurrence. Untested.

Both are worth testing precisely because they are artefacts of the fix rather
than of the document.

### 4. Font formats not yet examined

- **CFF at scale.** No fault at 854 glyphs, but that was the largest CFF face
  available for testing. Deep subroutine nesting, `seac`, hint replacement and
  CJK-scale faces are untested.
- **Type 3 fonts with path-heavy glyph procedures.** Bitmap Type 3 (colour
  emoji) is known good; drawing-heavy Type 3 is a different interpreter path.
- **Vertical writing** (`Identity-V`, `vhea`/`vmtx`) and `cmap` format 12 at CJK
  scale.

### 5. Smaller loose ends

- The daemon logs two harmless tracebacks at startup, from the DNS-SD library
  sending multicast on IPv6 loopback. Unhandled exceptions in a log are noise
  that hides real problems.
- Fault 1 sometimes leaves an assert in the device's log and sometimes leaves
  nothing at all. Whether these are one defect at two severities is unknown.
- Whether `--alert-mail` can deliver from inside the daemon's sandbox has not
  been confirmed on a machine with a real MTA. `NoNewPrivileges=` and
  `PrivateUsers=` both bear on a setgid submission binary such as Postfix's
  `postdrop`. If delivery fails the report is written to the journal, so
  nothing is lost silently, but the mail may simply never arrive.

## Things that are settled, so nobody re-tests them

- **No threshold predicts fault 1.** Four cost models were fitted and every one
  was falsified: declared glyph count, glyphs drawn, embedded font program size,
  and outline complexity. This is why every PDF is converted rather than
  screened.
- **Colour fonts need no support from the printer.** PDF cannot represent one,
  so a browser decomposes them first: bitmap colour fonts become Type 3 fonts
  full of images and print; vector colour fonts become forms, shadings and
  transparency groups and hit fault 2.
- **Conversion does not silently degrade pages.** Verified by rasterising
  before and after with an independent renderer. A Ghostscript older than 10.05
  does discard function-based shadings, which is checked for at run time rather
  than assumed.

## How to add to this

Reproduce it, in one variable, with the printer's state read before *and* after
the job — `scripts/probe-printer.py` enforces that. Then record what was
measured separately from what was inferred. Most of the corrections in this
project's history came from measurements that were assumed rather than checked.
