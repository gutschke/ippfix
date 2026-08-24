# Open questions

What is not solved, what is not understood, and what would be worth
investigating next. Kept separate from `DIAGNOSING.md`, which records what *is*
established.

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

Faults 1 and 3 both end with the printer reporting success. The proxy's only
fallback — rasterising the page — is triggered by the printer *rejecting* a
document. **A failure that reports success is invisible to it**, and no
improvement to the fallback changes that, because the signal never arrives.

Fault 1 is nevertheless handled, because it is prevented rather than detected:
outlining removes the font programs, so the condition cannot arise. Fault 3
cannot be prevented that way, because the malformed construct is in the input
and conversion preserves it faithfully.

Defending against this class means **inspecting documents before sending them**,
which is a different mechanism from anything the proxy does today. That is the
single largest open design question here.

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
