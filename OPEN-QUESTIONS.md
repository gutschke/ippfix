# Open questions

What is not solved, what is not understood, and what would be worth
investigating next. Kept separate from `DIAGNOSING.md`, which records what *is*
established.

## The over-limit path, after splitting was abandoned

The measurements that killed job splitting are in
[DIAGNOSING.md](DIAGNOSING.md) under *What this printer actually does*. In
short: the declared PDF cap is advisory, a 92.5 MB document printed, and a
quarter of a million paths on one page rendered to completion.

What remains open is narrower and worth stating plainly.

**Where the real limit is, nobody knows.** One success at 92.5 MB does not
locate a ceiling; it only shows the declared one is not it. The design response
is to stop guessing — send the outlined PDF and rasterise only if the printer
refuses it — rather than to substitute a better guess. A refusal is a returned
status, which means no job was created, which is what makes resending safe. A
*lost* response is a different thing entirely and must never be retried: the
printer may well have the job.

**Raster page geometry against a job-level `media` has not been measured.**
`defont` passes no `-dFIXEDMEDIA`, so Ghostscript writes each page's own
MediaBox into the URF, and a mixed-size document produces a stream of
differently-sized raster pages. On the PDF path a disagreement between document
geometry and the job's `media` is resolved by interpretation; on the raster path
the bitmap is already at device resolution and the printer can only scale, clip
or reject. Which of the three this device does is unknown and would cost paper
to find out.

If geometry ever does need normalising, `-dFIXEDMEDIA` alone **clips** — a
Legal page truncated at 792pt, which looks like a successful print.
`-dPDFFitPage` is required with it.

**`MAX_BODY` and the advisory cap now contradict each other.** The proxy refuses
an incoming document over 64 MB outright, so the 92.5 MB PDF that the printer
accepted directly could never have reached it through the proxy. That bound is
hardening — it decides how much a stranger on the LAN can make this daemon
buffer — and the document a client sends is normally far smaller than its
outlined form, so the two numbers describe different things. But a photo-heavy
document really can exceed it, and such a job prints when sent to the printer
directly and fails through the proxy, which is the wrong way round. Raising it
costs memory in a 512 MB cgroup. Not resolved.

**Whether this printer streams raster or spools it** is likewise unmeasured. The
format permits streaming and Ghostscript emits it in streamable form — it writes
zero into the page-count field and the printer accepts that, so the device
plainly is not validating it. That is evidence about the format, not about the
firmware.

## Two things in the relay path left alone, deliberately

Four of the six problems found while pinning that path are fixed. These two are
not, and the reasoning is here so it is not relitigated from scratch.

### Cancel-Job has no ownership check

Any client may cancel any job by id, and ids are sequential.

The fix that suggests itself -- refuse Cancel-Job for jobs this proxy did not
create -- is worse than the problem. "Ownership" needs authentication, and there
is none: `requesting-user-name` is a string the sender chooses. Tracking the ids
this proxy issued would break the cases that matter (cancelling across a proxy
restart, or from a second device) while stopping nobody who can send one more
packet, and would read as an access control that is not one.

What is worth doing, and is part of the splitting work rather than a fix here,
is narrower and well defined: once a job is several upstream jobs, refuse to
relay a Cancel-Job aimed at a *chunk* id. Those ids are the proxy's business,
not the client's, and cancelling the middle of somebody's document is a fault
with no legitimate form.

The exposure meanwhile is roughly what a directly reachable printer has, on a
network where the alternative is that anyone can also just print.

### printer-supply-info-uri is removed rather than re-served

The apparent inconsistency is with `printer-icons` and `printer-strings-uri`,
which are re-served from this daemon a few lines away. The difference is what
sits behind them. Icons and strings are small static files that can be fetched
and handed on. `printer-supply-info-uri` points at the device's own web
interface -- a page of vendor HTML and JavaScript, sometimes behind
authentication, on a host the client deliberately cannot route to. Re-serving it
means proxying arbitrary device HTML, which is a much larger surface than the
whole rest of this program and defeats the isolation the proxy exists to
provide.

Clients that want supply levels have them: `marker-levels` and `marker-names`
travel over IPP and are relayed untouched. Removing a link nobody can follow is
the honest answer; a dead link would be worse, and a proxied admin page worse
still.

## Also worth knowing

**The queue lock is only taken when the operation carries a document.**
Create-Job and Close-Job, and a Print-Job with an empty body, bypass conversion,
archiving and the lock entirely. "One job at a time" is a property of that
branch, not of the queue -- which matters before that branch is rewritten.

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
