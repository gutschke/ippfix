# How ippfix is put together

For anyone changing the code. What the printer's faults are and what has been
measured about them is in [DIAGNOSING.md](DIAGNOSING.md); what is still unknown
is in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

## Shape

Two processes, deliberately.

```
client ──IPP/TLS──▶ ippfix.service ──unix socket──▶ ippfix-convert@.service
                          │                              (defont → gs)
                          └──────────IPP─────────▶ printer
```

`ippfix.service` speaks HTTP and IPP, rewrites a few attributes, and relays.
It never executes Ghostscript. Each document is handed over
`/run/ippfix/convert.sock` to a per-connection instance of
`ippfix-convert@.service`, which runs `defont` with no network at all
(`PrivateNetwork=true`, `IPAddressDeny=any`), no capabilities, a private `/tmp`,
and bounded memory, tasks and run time. The socket is mode 0600 and owned by
the proxy's account, so nothing else can reach the converter.

Neither half is privileged. `ippfix.socket` binds port 631 and passes the
descriptor, so the daemon's capability bounding set is empty. Running the
daemon by hand without systemd works too — it binds the port itself, and
`--converter` then defaults to the `defont` script sitting beside `ippfix.py`.

## The files

| | |
|---|---|
| `ippfix.py` | everything the daemon does: HTTP, IPP relaying, conversion, DNS-SD, alerting |
| `ippcodec.py` | byte-exact IPP message codec |
| `defont` | the converter: a shell script around Ghostscript, run in its own service |
| `ippfix` | small launcher, so `ps` shows `ippfix` rather than `python3` |
| `ippfix.service`, `ippfix.socket`, `ippfix-convert*` | units for the `/usr/local` layout; `debian/rules` rewrites them for the packaged one |
| `debian/` | the two packages; `debian/pkg/` holds the packaged units and the conffile example |
| `install.sh`, `uninstall.sh` | the non-package install path |
| `scripts/` | offline self-test and the diagnostic tools |
| `reproducers/` | the smallest inputs that provoke fault 3 |

## The codec

`ippcodec.py` keeps every attribute group as an *ordered* list of raw
`(tag, name, value)` triples. An additional value of a multi-valued attribute
is a triple with an empty name, exactly as it appears on the wire, and a
collection is an ordinary `begCollection … endCollection` run. Nothing is
modelled semantically, which is what makes `serialize(parse(x)) == x` hold for
anything a printer emits — including attributes this code has never heard of.
The self-test asserts that round trip.

Bounds matter here: every byte below 0x10 starts a new group, so `MAX_GROUPS`
and `MAX_ITEMS` exist because an unbroken run of delimiter bytes once allocated
one object per input byte.

## The request path

`Handler.serve` peeks at the first byte: 0x16 means a TLS ClientHello and the
socket is wrapped, anything else is plaintext IPP unless `--require-tls`. Both
live on the same port because clients choose for themselves, and a client that
discovered the queue over DNS-SD commonly chooses plaintext.

`resolve()` maps a path to a queue and is deliberately lax — `/ipp/name` and
`/name` both work, case is ignored, a trailing job id is tolerated, and a
single-queue daemon answers on any path. These are addresses people type from
memory.

Then, in order:

1. **The operation is checked against `ALLOWED_OPS`.** Only the ten a print
   client needs are relayed. The printer may be reachable *only* through this
   proxy, so relaying `Set-Printer-Attributes`, `Purge-Jobs` or `Print-URI`
   would hand every host on the LAN administrative control of it.
2. **`rewrite_request`** points `printer-uri` at the real printer and removes
   `FORBIDDEN_ATTRS` — the attributes that would make the printer fetch a
   resource of the sender's choosing.
3. **The document is converted** if it is a PDF (see below).
4. **The job is forwarded** while holding `queue.lock`. One job at a time:
   affected printers report `multiple-document-jobs-supported` as false and
   mishandle a second job arriving mid-transfer. The lock is released before
   the client is answered, so one slow reader cannot stall the queue.
5. **`rewrite_response`** replaces identity and URIs only — queue name, UUID,
   the URIs clients should use, and the icon and localised-string URIs, which
   are re-served by this daemon because clients may have no route to the
   printer's own web server. Capabilities and status are passed through
   untouched, which is where feature parity and live status come from for free.
6. **The job is followed**, if `--alert-mail` is set, after the client has its
   answer.

`GET` serves three things: `/queues.json` (and `/queues`) for sites that pin
printers by address, `/ipp/NAME/icon-small.png`, `icon-large.png` and `strings`
proxied from the printer, and an HTML table at the root.

## Conversion

`convert()` in `ippfix.py` decides, `defont` does the work.

**Nothing chooses the interpreter but us.** Ghostscript picks its interpreter
from the start of the file: `%PDF-` at the beginning means the hardened C PDF
interpreter, anything else means the full PostScript interpreter, where the
historical `-dSAFER` escapes live. `normalise_pdf()` therefore accepts a
document only if `%PDF-` appears at the start of a line within the first
kilobyte and the file does not open with `%!`, and returns the bytes from that
header onwards; `defont`
independently refuses anything whose first five bytes are not `%PDF-`. The
self-test pins both. Do not relax either.

**Every PDF is converted.** `--convert-threshold` defaults to 0, which skips
the comparison entirely. The cost estimate is still computed, because it is
useful in a log, but nothing acts on it by default. See
[DIAGNOSING.md](DIAGNOSING.md) for why prediction was abandoned; the short
version is that four models were fitted and all four were falsified.

**The converter is told what the printer accepts**, because it cannot ask: a
one-line `%%ippfix device=… colorspace=… dpi=… maxpdf=…` header precedes the
document. `defont` validates every field of that header rather than trusting
it — the device name reaches a Ghostscript command line, and Ghostscript has
devices that have been used to defeat `-dSAFER`, so only the raster devices
this tool actually emits are accepted. The header is validated even though the
proxy writes it, because `defont` is also runnable by hand on a document that
could carry a line that looks like one.

**Three tiers, stopping at the first that suffices:**

| tier | when |
|---|---|
| relayed untouched | not a PDF, conversion failed, or the conversion was rejected below |
| text outlined | the normal path for every PDF |
| rasterised | only when the outlined PDF would exceed what the printer accepts |

The raster tier is chosen from the document's size before the job is sent.
Nothing anywhere reacts to what the printer does with a job afterwards — see
the structural limit in [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md).

**Every conversion is checked before it is used.** `defont` discards its own
output and passes the original through if the output is empty, if any
`/FontFile` survived, or if a whole class of drawing construct
(`/ShadingType`, `/PatternType`, `/FunctionType`) present in the input is
missing from the output. That last check is what makes a version pin
unnecessary: Ghostscript before 10.05 discards function-based shadings, and
those documents are relayed unconverted rather than degraded. The check is
coarse on purpose — Ghostscript legitimately merges identical objects, so
comparing counts would raise false alarms, but a class vanishing entirely is
unambiguous. Both checks look inside compressed object streams, because
Ghostscript 10.06 writes PDF 1.7 with object streams by default and a plain
`grep` stopped seeing dictionaries that were certainly there.

`ippfix.py` repeats the `/FontFile` check on what comes back, and relays the
original if a font program survived. `--fail-closed` inverts the whole
fail-safe policy: reject rather than relay. The default is to relay, on the
grounds that a job which might not print beats one that prints wrongly — but
note that this leaves the sender deciding whether conversion happens.

## The cost estimate

`estimate_font_cost()` returns, for the most expensive *page* in a document,
the number of distinct glyphs it draws plus the size of the font programs
reachable from it divided by 4096. Three properties matter more than the
number:

- **It is per page**, because the printer's budget is. Summing a document
  over-states long ones, and over-stating is not harmless: it inflates jobs
  that did not need it.
- **It follows form XObjects.** A PDF printed through a viewer typically
  arrives with the whole page wrapped in one, so the page's own resources name
  no fonts and everything that matters is a level down. Missing that scores
  exactly the jobs that fail as free.
- **It returns `None` when the file cannot be read confidently**, and the
  caller must treat that as "convert", never as "safe". Object streams, an
  unresolvable `/Contents`, a decompression bomb and a document over
  `MAX_PAGES_INSPECTED` pages all come back `None`.

What a font *declares* is deliberately not counted. A test that rewarded it
would re-introduce a model that measurement disproved, which is why the
self-test builds a font declaring 65535 glyphs and asserts the estimate stays
small.

## Following a job

With `--alert-mail`, `maybe_watch()` starts a thread per job (bounded by
`alert_max_watchers`, 32) that polls `Get-Job-Attributes` every five seconds
until the job reaches a terminal state or `--alert-timeout` expires. The
verdict comes from `job-impressions-completed`: a job that completes having
marked nothing is `LOST SILENTLY`, which is the failure this proxy exists for
and means something got through it. Aborted, cancelled, unfinished and
unanswered jobs are reported with their own verdicts; a job that completed
having marked pages is not reported at all.

`describe_document()` builds the structural summary that goes in the report:
producer, embedded font programs, shading/pattern/function types, transparency
groups, soft masks, images, a digest. **No text and no images from the
document.** `printer_snapshot()` adds what the printer says about itself right
then — model, firmware, state reasons, marker levels — which is how the
alternative explanations get ruled out.

`PageCounter` holds the second opinion. It reads `prtMarkerLifeCount` before
and after each followed job and compares the movement with
`job-impressions-completed`, which matters because those two numbers come from
different parts of the printer and only one of them is tied to the marking
engine.

It has two levels of confidence and they carry different consequences.
*Trusted* means the printer answers and says (via `prtMarkerCounterUnit`) that
its counter counts impressions or sheets — enough to print the numbers in a
report. *Proven* means the counter has been seen to move in step with a job
that printed — enough to let it contradict the printer's own job accounting and
raise an alert. That gate is the reason a broken counter cannot cry wolf: it
must first demonstrate that it works.

Trust is revoked from behaviour: backwards twice, a jump larger than
`MAX_PLAUSIBLE_JUMP`, `MISS_LIMIT` unanswered reads, or `FROZEN_LIMIT` jobs
that reported impressions without moving it. The third frozen job also
*cancels* its own finding — having concluded the instrument is broken, acting
on its last reading would be incoherent — and the log says outright that
earlier reports were probably wrong. Nothing is persisted: trust is established
from what the printer states about itself, which needs no history, and revoked
from behaviour, which does. So it works on the first job after a restart and
leaves no state file to go stale.

Forward jumps are never treated as suspicion of a fault. The proxy is not the
only route to a printer; copies, received faxes and internal pages all advance
the counter. Only a jump too large to be paper is evidence, and then it is
evidence about the OID rather than about the job.

`SnmpRelay` is the other direction: answering SNMP for a printer clients cannot
reach. `snmpmini` parses the request for a policy decision and the datagram is
then forwarded **verbatim** — re-encoding would mean an encoder whose bugs are
reachable from the network, to gain nothing, since the bytes already say
exactly what was asked. The response is parsed again only to check the request
id and that no OID walked outside the allowlist; a `GETNEXT` that leaves a
subtree is dropped rather than answered, so a walk stops at the boundary.
`GETBULK` is refused because `max-repetitions` is the amplification knob;
measured against the real printer, the worst response inside the allowlist is
143 bytes for a ~45-byte request.

`gather_evidence()` attaches the documents, and only when `--archive` is on is
there an original to attach. It attaches two when they differ: the job as the
client sent it, read back from the archive, and the job as the proxy handed it
to the printer. Attachments go out raw so they can be fed straight to the tools
the report names; compression is a fallback used to make something fit, not a
default, and a document that still will not fit is named rather than silently
missing. `--alert-max-attachment` is the bound.

The summary stays free of text and images regardless: it is the part that is
always sent. The attachments are the part that is opt-in, gated on a flag whose
whole documentation says it stores what people print. Keep that split if you
extend either.

Delivery is `/usr/sbin/sendmail -t`. If that fails the body is written to the
log rather than dropped.

## Learning the printer

`Queue.learn()` reads `document-format-supported`, `urf-supported`,
`printer-resolution-supported`, `pdf-k-octets-supported`, `color-supported`
and `pwg-raster-document-resolution-supported` once, and uses them to pick the
raster device, colour space and resolution, and the maximum PDF size (80% of
what the device declares, to stay clear of the limit rather than sit on it).

Failure is retried rather than remembered: a printer that is down when the
daemon starts must not disable the raster tier until the next restart. Only
success is final, and a failed attempt is not repeated for a minute, so an
unreachable printer costs one stalled request per minute rather than one per
job.

`connect_upstream()` tries every address a printer name resolves to and
remembers the one that worked. Python's HTTP client uses only the first, which
on a dual-stack LAN in front of an IPv4-only printer segment means every job
hangs until it times out.

## Discovery and addressing

`advertise()` publishes each queue over DNS-SD through `zeroconf`. Only stable,
globally scoped, non-tentative IPv6 addresses *of the interface named by
`--advertise`* are published alongside it. Publishing everything a machine
happens to have — privacy addresses that rotate, deprecated ones, addresses on
an interface clients cannot route to — produces queues that stall rather than
fail. `global_ipv6()` parses `/proc/net/if_inet6` for the flags, which is why
`ProcSubset=pid` is *not* set on the service.

## Hardening rules that must not regress

The self-test enforces the first four; the rest are the reasoning behind them.

- The converter has `PrivateNetwork=true` and an empty
  `CapabilityBoundingSet=`.
- The proxy has an empty `CapabilityBoundingSet=` and never names `defont` in
  an `ExecStart=`.
- Administrative IPP operations are not in `ALLOWED_OPS`; the operations a
  print client needs are.
- `application/postscript` is never in `SAFE_FORMATS` — it is interpreted by
  exactly the task that fails, and converting it would mean running
  Ghostscript's PostScript interpreter. `--all-formats` overrides the filter
  for someone who knows what they are giving up. PCL and PCL-XL stay, being a
  different interpreter on the device.
- Every attacker-controlled length is bounded: `MAX_BODY`, `MAX_HEADERS`,
  `MAX_KEEPALIVE`, `MAX_INFLATE`, `MAX_CONVERTED`, `MAX_XOBJECT_DEPTH`,
  `--max-connections`, `--idle-timeout`.
- A `Content-Type` read from the printer is stripped of control characters
  before it is written into a response header.

## Packaging

Two layouts exist and both are supported. `debian/rules` rewrites the
`/usr/local` paths in the shipped units, so there is one source of truth for
the unit contents and the differences stay visible in the `sed` line rather
than in a second copy.

| | packages | `install.sh` |
|---|---|---|
| program | `/usr/lib/ippfix` | `/usr/local/lib/ippfix` (asked for) |
| units | `/usr/lib/systemd/system` | symlinked into `/etc/systemd/system` |
| configuration | `/etc/ippfix/ippfix.conf` (`IPPFIX_ARGS`) | `ExecStart=` in the unit |
| scripts | `/usr/share/ippfix/scripts` | not installed |
| accounts, directories | `sysusers.d`, `tmpfiles.d` | `useradd`, `mkdir` |

The package ships no configuration. Both units carry
`ConditionPathExists=/etc/ippfix/ippfix.conf`, and the service additionally
runs `ippfix-configured` as `ExecCondition=`, so an unconfigured machine has an
inactive unit with a reason in the journal rather than a failed one — and holds
no port. A file the package always creates could not express that distinction,
which is why the example lives in `/usr/share/doc`.

The Python virtual environment is built in `postinst`, not shipped: it is tied
to the interpreter's minor version and would break on a release upgrade. That
needs the network, so a failure there is reported and left for later rather
than being fatal. `ippfix-selfbuild` rebuilds it when the interpreter or a
dependency moves, swaps it in, and restarts the service; if the service does
not come back up it restores the previous environment and mails the failure.

Condition directives belong in `[Unit]`; in `[Service]` they are silently
ignored. The service `Wants=` its sockets rather than `Requires=` them, because
a condition-skipped unit counts as a *failed* dependency for anything that
requires it.

## Testing

```sh
./scripts/selftest.sh
```

44 checks, all offline: no printer, no network, nothing installed. It compiles
the sources, round-trips IPP messages, exercises queue parsing and URL
construction, checks IPv6 address selection against a synthetic
`/proc/net/if_inet6`, runs `defont` over a PDF that really does embed a font
(including inside an object stream), asserts the hardening rules above,
verifies the units with `systemd-analyze`, and checks that the manual page
renders without warnings and lists exactly the options `--help` does. That last
check is why the manual page and `build_parser()` have to be changed together.

`scripts/fidelity-check.py` is the test that cannot be automated here: it
rasterises a document before and after conversion with poppler — an
independent renderer, so a fault in Ghostscript cannot hide behind a matching
fault in the rasteriser — and compares them pixel by pixel.

## House rules

- **Distinguish what was measured from what was inferred**, in comments and in
  commit messages both. Most of the corrections in this project's history came
  from measurements that were assumed rather than checked.
- **Do not add a heuristic that decides not to convert** without measurements
  to back it. Four have been tried; all four were falsified.
- **A conversion that might have changed the page is not used.** The failure
  mode this tool must never introduce is a page that prints wrongly, because
  nobody checks a page that printed.
- **Do not put real network detail in the tree.** Examples use the RFC 5737
  ranges and `.example` names. `scripts/selftest.sh` checks for leaked home
  directory paths; the rest is manual.
