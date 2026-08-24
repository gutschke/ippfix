# ippfix(8) - IPP proxy that repairs print jobs for printers with a limited font cache

1.0, August 2026

<a name="synopsis"></a>

# Synopsis

```
ippfix [-a|--advertise ADDRESS] [--also-advertise ADDRESS] [--alert-mail ADDRESS] [--alert-max-per-hour N] [--alert-timeout SEC] [--archive DIR] [--archive-max N] [--archive-max-bytes MB] [--cert FILE] [--converter PATH] [--key FILE] [--list [URL]] [--max-connections N] [--idle-timeout SECONDS] [--require-tls] [--convert-threshold N] [--max-pdf-bytes MB] [--all-formats] [--fail-closed] [--no-ipv6] [--no-advertise] [--no-convert] [-p|--port PORT] [--timeout SECONDS] [-v|--verbose] [NAME=]URI...
```

<a name="description"></a>

# Description

**ippfix**
accepts print jobs over IPP, rewrites each PDF so that it contains no embedded
font programs, and forwards it to a real printer. Everything else is relayed
untouched.

It exists because HP LaserJet Pro printers run a combined PostScript/PDF
interpreter with a fixed per-page budget for embedded fonts and the glyphs
drawn from them. Exceeding that budget aborts the interpreter: the printer
warms up, reports the job `completed`, and marks nothing. Sometimes the first
few pages emerge and the job then stops. No error reaches the client and none
is shown on the panel. Where the device records it at all, its event log
reports an assertion failure in `fontcache.c`.

The budget covers both the glyphs drawn and the embedded font programs they
come from, and the two trade against each other, but no rule relating the two
has survived testing. What is established is that the font's hinting is
causally involved — the same 900-glyph page fails with a hinted font and prints
with the hinting bytecode removed — and that it is a property of the font
rather than of the application, which is why printing web pages has been more
reliable than printing PDFs. Four cost models were fitted to measured outcomes
and every one was falsified; see [Calibration](#calibration) below.

The defect appears in firmware builds years apart and is unlikely to be fixed.
Client-side changes affect how often it is reached: Chrome 130 through 144
embedded a separate font program for every *strike* of a typeface, including
one per text colour, which multiplied the cost of an ordinary page. Chrome 145
removed the colour component and normalises text size out of the key, so
current versions embed far fewer font programs than that era did.

**ippfix**
converts glyphs to filled paths using Ghostscript's `-dNoOutputFonts` option.
No font program reaches the printer, which makes the failure structurally
impossible rather than merely less likely. Text remains vector, so the
printer's own rasteriser still renders it at full device precision and its edge
enhancement still applies. Rasterising the page instead would commit its
geometry to the device grid before the printer saw it, forfeit the printer's
halftoning, and inflate a small job into tens of megabytes.

This addresses one fault. Two others have been reproduced on the same device
and are not fixed here: a page of vector colour glyphs is rejected with
`document-format-error`, and a soft mask whose `/BC` backdrop array does not
match the component count of its group's colour space is accepted, reported
complete, and marked nowhere. Both are properties of the document that
conversion preserves faithfully. The distributed `DIAGNOSING.md` records how to
tell them apart.

<a name="arguments"></a>

# Arguments

* `[NAME=]URI`:
  A printer to proxy, given as an `ipp://` or `ipps://` URI. The optional *NAME*
  becomes the queue name, published at `/ipp/NAME`. Without one, the queue is
  named `print`. May be repeated to serve several printers from one daemon.

<a name="options"></a>

# Options

* `-a`, `--advertise` *ADDRESS*:
  Address that clients should use to reach this server. Autodetected by default,
  which is wrong on hosts with several addresses.

* `--also-advertise` *ADDRESS*:
  Additional address to publish in the DNS-SD records; may be repeated. By
  default the stable, globally scoped IPv6 addresses of the same interface as
  `--advertise` are published alongside it, so dual-stack clients are offered
  IPv6 without extra configuration.

  Only that interface's addresses are used. On a multi-homed host, publishing
  every address the machine happens to have invites clients to try one they
  cannot route to, which appears as a long stall rather than a clear failure.
  Privacy, deprecated and tentative addresses are excluded because they rotate
  or are not valid for new connections, and link-local addresses are excluded
  because they need a scope identifier a DNS-SD record cannot carry.

* `--no-ipv6`:
  Publish only the IPv4 address, for networks where IPv6 exists but is not
  routable.

* `--alert-mail` *ADDRESS*:
  Send mail to *ADDRESS* when a job does not print. Off unless set.

  The failures this proxy exists for are silent: the printer accepts the job,
  reports it completed, and marks nothing, so every layer above it repeats that
  success and nobody finds out. When this is set, each print job is followed to
  its terminal state and judged on what the printer says it marked rather than
  on whether the request succeeded. IPP reports this itself — a job that
  completes having marked nothing gives `job-impressions-completed` of zero —
  so no other protocol is needed.

  The report names the queue, the job, what conversion did, the sequence of
  states the printer went through, and the document's structure: producer,
  embedded font programs, shading and pattern types, transparency groups, soft
  masks and a digest. It contains no text and no images from the document; the
  aim is to make a fault reproducible, not to copy what somebody printed.

  This is off by default because a printer that does not report impressions
  honestly would report every job as lost. Delivery uses `/usr/sbin/sendmail`,
  so a local mail transport agent is required; if it is missing or refuses, the
  report is written to the log instead of being lost. Following a job happens
  after the client has been answered, so it never delays one.

* `--alert-max-per-hour` *N*:
  Send at most *N* alerts an hour, default 6. Suppressed ones are logged, and
  the count is carried into the next message, so a flood is reported as a flood
  instead of becoming one.

* `--alert-timeout` *SEC*:
  Give up following a job after *SEC* seconds, default 600. A job still
  unfinished then is reported as such.

* `--archive` *DIR*:
  Diagnostic only. Keep a copy of every job exactly as it arrived, before
  conversion, together with a short text file recording the queue, job name,
  document format and what conversion did.

  This writes the documents users print to disk. It is off by default. The
  directory is created mode 0700 and the files 0600, owned by the service
  account, but anyone who can read them can read everything printed while it
  was enabled. It exists because the failure this proxy works around is silent
  and depends on document content, so without the document that provoked it
  there is almost nothing to go on. Enable it while chasing a specific problem,
  and turn it off again afterwards.

* `--archive-max` *N*:
  Keep at most *N* archived jobs, deleting the oldest first. Default 50. Bounds
  the disk cost of a flag left on by accident.

* `--cert` *FILE*:
  TLS certificate. Default `/etc/ippfix/ippfix.crt`.

* `--converter` *PATH*:
  Helper that rewrites a PDF read on standard input and writes the result to
  standard output. Defaults to `defont` alongside the program, so it is correct
  whichever way this was installed.

* `--key` *FILE*:
  TLS private key. Default `/etc/ippfix/ippfix.key`.

* `--list` [*URL*]:
  Print the queues a running instance serves, then stop. Intended for
  configuring clients by address rather than by discovery: mDNS is not available
  everywhere, and some sites prefer printers pinned by address so that discovery
  cannot silently point users somewhere else. Defaults to the instance on this
  host. The same listing is served as JSON at `/queues.json` and as a table at
  the daemon's HTTP root.

* `--max-connections` *N*:
  Refuse connections beyond *N* concurrent ones. Default 64. Without a bound,
  connections that open and then say nothing accumulate until the service hits
  its task limit, after which it can accept nothing further until restarted.

* `--idle-timeout` *SECONDS*:
  Drop a connection that stops speaking for this long. Default 30. Applies to
  every stage of a request, so a client cannot hold a thread by sending a
  partial header and waiting.

* `--require-tls`:
  Refuse plaintext IPP and accept only implicit TLS. Off by default, because
  clients that discover a printer over DNS-SD commonly choose plaintext and
  would otherwise simply fail to print.

* `--convert-threshold` *N*:
  Leave a job untouched when its estimated font cost is at or below *N*.
  Default 0, which converts every PDF: the test is skipped entirely rather than
  compared against zero.

  Outlining is not free — it replaces every drawn glyph with an inline path,
  which costs about a third of a second and roughly doubles the size of a real
  job — so skipping it for documents that certainly do not need it would be
  worth having. The obstacle is that no estimate has been shown to predict the
  printer's behaviour; see [Calibration](#calibration) below. Set this only if
  you have measured your own workload, and treat it as an optimisation rather
  than as a safety margin. A file whose cost cannot be determined is always
  converted, never assumed cheap.

* `--max-pdf-bytes` *MB*:
  Rasterise rather than send an outlined PDF larger than this. Default 60.
  Overridden by the printer's own `pdf-k-octets-supported` where it reports one,
  so the shipped default only applies to a device that declares no limit.

* `--all-formats`:
  Offer clients every document format the printer supports, including
  PostScript.

  PostScript is withheld by default. It is interpreted by exactly the task that
  fails on these devices, and it cannot be converted the way PDF is: handing
  PostScript to Ghostscript means running its PostScript interpreter, which this
  program is careful never to do. Offering it would advertise a repaired queue
  that silently is not one. PCL and PCL-XL are offered either way — they are a
  separate interpreter on the device, and are relayed unchanged.

* `--fail-closed`:
  Reject a PDF that cannot be converted, instead of forwarding it unchanged. By
  default a conversion failure relays the original, on the grounds that a job
  which might not print beats one that prints wrongly. That is the safe choice
  for printing and the unsafe one for the guarantee this proxy exists to give,
  because whoever sends the document also decides whether conversion fails.
  This option inverts that trade.

* `--archive-max-bytes` *MB*:
  Total size cap for the archive, in megabytes. Default 512. A count alone is
  not a bound when the documents are chosen by whoever is printing.

* `--no-advertise`:
  Do not publish the queues over DNS-SD. Useful when discovery is handled
  elsewhere.

* `--no-convert`:
  Relay jobs untouched. Intended for comparison against a converting instance,
  to confirm that conversion is what makes the difference.

* `-p`, `--port` *PORT*:
  Port to listen on. Default 631. Under the supplied units systemd binds that
  port and passes the descriptor, so no capability is needed; started by hand it
  requires `CAP_NET_BIND_SERVICE` or an adjusted
  `net.ipv4.ip_unprivileged_port_start`.

* `--timeout` *SECONDS*:
  Time allowed for each conversion and each request to the printer. Default 300.

* `-v`, `--verbose`:
  Log protocol detail.

<a name="conversion"></a>

# Conversion

A job passes through up to three stages, stopping at the first that suffices.

* **Relayed untouched**:
  The document is not a PDF, conversion failed, the converted form still
  contained a font program, or the conversion lost a whole class of drawing
  construct and was therefore discarded. A non-zero `--convert-threshold` also
  lands a cheap job here, but that is off by default.

* **Text outlined**:
  Glyphs become filled paths, so no font program reaches the printer. Text stays
  vector, so the printer's own rasteriser still renders it at full device
  precision.

* **Rasterised**:
  Only when the outlined form would exceed what the printer will accept as a
  PDF. These devices advertise `pdf-k-octets-supported` of 0-75000, and
  outlining inlines an outline at every glyph occurrence, so a long document can
  pass that. The page is then sent as 600 dpi contone URF. Fidelity is lower,
  because the geometry is committed to the device grid before the printer sees
  it, but the printer still applies its own halftoning and edge enhancement, and
  the job prints rather than being rejected. The job's document format is
  updated to match.

Which raster format, colour space, resolution and PDF size limit apply is read
from the printer's own attributes and travels with each document, because the
converter has no network and cannot ask. The environment variables
`MAX_PDF_BYTES` (default 60000000), `RASTER_DPI` (600) and `RASTER_COLORSPACE`
(19 for sRGB; 18 for 8-bit grey, which halves the size and avoids
composite-black fringing on text) supply the defaults used when `defont` is run
by hand, without that header.

The raster tier is chosen from the size of the converted document before the
job is sent. Nothing reacts to what the printer does with a job afterwards, so
a document the printer rejects, or accepts and does not print, is not retried
in another form.

<a name="calibration"></a>

# Calibration

**By default every PDF is converted, because predicting which ones need it did
not work.** Four models were fitted to measured outcomes and each was
falsified: the glyph count a font declares — a font declaring 65535 glyphs
while drawing 27 printed perfectly — the glyphs a page draws — 1264 printed
where 700 failed — the size of the embedded font program, and the outline
complexity of the glyphs actually used, where 519 glyphs in 47 kB failed and
519 glyphs in 50 kB printed. Whatever the firmware counts is not visible in the
document.

Converting unconditionally costs roughly a third of a second and about double
the file size on a real job, which is a better trade than a prediction that has
been wrong repeatedly. A cost estimate is still computed and written to the log
for diagnosis.

`--convert-threshold` can skip conversion for jobs scoring at or below a given
value, for a site that has measured its own workload and wants the
optimisation. It is off by default and nothing depends on the estimate being
accurate.

To investigate a printer directly, note that a job which exceeds its limit
marks no paper, so only the passing probes cost anything. The device's own page
counter is the reliable signal, because an affected printer reports the job
completed either way. The Printer MIB (RFC 3805) exposes it on any network
printer:

```
snmpget -v2c -c public PRINTER 1.3.6.1.2.1.43.10.2.1.4.1.1
```

<a name="behaviour"></a>

# Behaviour

Both plaintext IPP and implicit TLS are accepted on the same port, and both IPv4
and IPv6 are served.

Printer attributes are mirrored from the real device on every request. Only
identity and URIs are rewritten — the queue name, its UUID, the URIs clients
should use, and the locations of the printer's icons and localised strings,
which are re-served by this daemon because clients may have no route to the
printer's own web server. Capabilities and status pass through unchanged, so
duplex, trays, media, colour modes and live supply and error state are always
correct without configuration.

Documents that are not PDF are relayed untouched, as are PDFs whose conversion
fails or leaves any font program behind. A job that might not print is
preferable to one that prints something wrong.

Jobs are forwarded one at a time, because affected printers report
`multiple-document-jobs-supported` as false and mishandle a second job arriving
mid-transfer.

<a name="files"></a>

# Files

Two layouts are supported. The packages install under `/usr/lib/ippfix`, with
the command line in a conffile; `install.sh` installs under
`/usr/local/lib/ippfix` instead, with the command line in the unit.

* `/etc/ippfix/ippfix.conf`:
  Packaged installations only. Read by the unit as an environment file;
  `IPPFIX_ARGS` holds the whole command line. The units are conditional on this
  file existing, so nothing runs and no port is taken until it does. An example
  is installed as `/usr/share/doc/ippfix/ippfix.conf.example`.

* `/etc/systemd/system/ippfix.service`:
  `install.sh` installations only: the service unit, symlinked from the
  installation directory. Edit `ExecStart` to configure the printers.

* `/usr/lib/ippfix/defont`:
  PDF conversion helper, run as its own confined service. May be run by hand for
  diagnosis; asked to self-check, it reports whether the installed Ghostscript
  preserves function-based shadings. See `DIAGNOSING.md`.

* `/etc/ippfix/ippfix.crt`, `/etc/ippfix/ippfix.key`:
  Self-signed TLS credentials, generated on first start if absent.

* `/var/lib/ippfix/archive`:
  Default location for `--archive`. Aged out after seven days by a `tmpfiles.d`
  rule.

* `/usr/share/ippfix/scripts/`:
  Diagnostic tools, packaged installations only: `probe-printer.py`,
  `make-reproducer.py`, `make-html-reproducer.py`, `check-softmask.py` and
  `fidelity-check.py`.

<a name="examples"></a>

# Examples

Serve one printer:

```
ippfix ipp://printer.example/ipp/print
```

Serve two, under explicit names:

```
ippfix upstairs=ipp://printer1.example/ipp/print \
       downstairs=ipp://printer2.example/ipp/print
```

Run a second, non-converting instance for comparison:

```
ippfix --port 6310 --no-advertise --no-convert \
       ipp://printer.example/ipp/print
```

<a name="diagnostics"></a>

# Diagnostics

Each job is logged with its queue, operation, and the result of conversion:

```
upstairs   Print-Job   HTTP 200  [outlined 393836 -> 76789 bytes in 0.2s (font cost 465)]
```

`relayed` indicates the job was passed through unchanged, with the reason in
brackets. The font cost is recorded for diagnosis; nothing is decided by it
unless `--convert-threshold` is set.

Because an affected printer reports success whether or not it printed, the
reliable check is the device's own total impression count, which does not
advance for a job that died. `--alert-mail` performs that check on every job by
following it to its terminal state. To do it by hand, read the RFC 3805 counter
over SNMP before and after; on HP devices that counter and the interpreter's
assertion log are also readable over HTTP from
`/DevMgmt/ProductUsageDyn.xml` and `/DevMgmt/ProductLogsDyn.xml`, which are
vendor paths this program neither uses nor depends on.

<a name="notes"></a>

# Notes

If clients can also reach the printers directly, they will see both those and
the proxy queues, and users will choose arbitrarily between them. Suppress the
printers' own `_ipp`, `_ipps` and `_pdl-datastream` advertisements, but leave
`_uscan` and `_uscans` alone or scanning will stop working.

Only one mDNS responder may hold UDP port 5353. With `systemd-resolved`
present, set `MulticastDNS=no` in `/etc/systemd/resolved.conf` and restart it,
or run with `--no-advertise`.

<a name="see-also"></a>

# See Also

gs(1), cups(1), snmpget(1), systemd.service(5)

The distributed documentation: `README.md` for what the fault looks like and
how to install, `DEPLOYMENT.md` for making clients use the proxy,
`DIAGNOSING.md` for the three known firmware faults and how to reproduce them,
`INTERNALS.md` for the code, and `OPEN-QUESTIONS.md` for what is unsolved.
Packaged installations keep these in `/usr/share/doc/ippfix/`.

<a name="license"></a>

# License

MIT.
