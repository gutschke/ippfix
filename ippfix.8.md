# ippfix(8) - IPP proxy that repairs print jobs for printers with a limited

1.0, August 2026

font cache.

<a name="synopsis"></a>

# Synopsis

```
ippfix [-a|--advertise ADDRESS] [--also-advertise ADDRESS] [--archive DIR] [--archive-max N] [--cert FILE] [--converter PATH] [--key FILE] [--no-advertise] [--no-convert] [--list [URL]] [--max-connections N] [--idle-timeout SECONDS] [--require-tls] [--convert-threshold N] [--fail-closed] [--archive-max-bytes MB] [--no-ipv6] [-p|--port PORT] [--timeout SECONDS] [-v|--verbose] [NAME=]URI...
```

<a name="description"></a>

# Description

**ippfix**
accepts print jobs over IPP, rewrites each PDF so that it contains no embedded
font programs, and forwards it to a real printer. Everything else is relayed
untouched.

It exists because HP LaserJet Pro printers run a combined PostScript/PDF
interpreter with a fixed per-page budget for embedded fonts and the glyphs
drawn from them. Exceeding that budget aborts the interpreter: the printer warms
up, reports the job `completed`, and marks nothing. Sometimes the first few
pages emerge and the job then stops. No error reaches the client and none is
shown on the panel. Where the device records it at all, its event log reports an
assertion failure in `fontcache.c`.

The budget covers both the glyphs drawn and the embedded font programs they come
from, and the two trade against each other. On a Color LaserJet Pro MFP M283fdw,
one fully embedded font renders 527 distinct glyphs but not 534; add a second
fully embedded font and the page fails at 300. A font's cost scales with how
many glyphs its embedded program declares rather than being a flat per-font
constant, so a heavily subsetted face is far cheaper than a complete one.

Those figures come from probes embedding complete, unsubsetted fonts. Jobs from
a browser subset aggressively and sit well below them: sampled from real Chrome
output, two subsets declaring 93 and 668 glyphs, with 451 distinct glyphs drawn
between them, printed without trouble. No fixed threshold is therefore safe to
design against — whether a document crosses the line depends on its typefaces,
how they were subsetted, and how many distinct characters appear.

The defect appears in firmware builds years apart and is unlikely to be fixed.
Client-side changes affect how often it is reached: Chrome 130 through 144
embedded a separate font program for every *strike* of a typeface, including one
per text colour, which multiplied the cost of an ordinary page. Chrome 145
removed the colour component and normalises text size out of the key, so current
versions embed far fewer font programs than that era did.

**ippfix**
converts glyphs to filled paths using Ghostscript's `-dNoOutputFonts` option. No
font program reaches the printer, which makes the failure structurally
impossible rather than merely less likely. Text remains vector, so the printer's
own rasteriser still renders it at full device precision and its edge
enhancement still applies. Rasterising the page instead would commit its
geometry to the device grid before the printer saw it, forfeit the printer's
halftoning, and inflate a small job into tens of megabytes.

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
  standard output. Default `/usr/local/lib/ippfix/defont`.

* `--key` *FILE*:
  TLS private key. Default `/etc/ippfix/ippfix.key`.

* `--list` [*URL*]:
  Print the queues a running instance serves and exit. Intended for configuring
  clients by address rather than by discovery: mDNS is not available everywhere,
  and some sites prefer printers pinned by address so that discovery cannot
  silently point users somewhere else. Defaults to the instance on this host.
  The same listing is served as JSON at `/queues.json` and as a table at the
  daemon's HTTP root.

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
  Default 2500; 0 converts everything.

  Outlining is expensive. It replaces every drawn glyph with an inline path and
  Ghostscript emits no reusable form for them, so a fifty-page document grows
  from half a megabyte to thirty-three and takes about half a second per page.
  Most jobs are nowhere near the printer's limit, so the cost of each embedded
  font program and the glyphs drawn from it are estimated first and cheap jobs
  are relayed untouched, which is both free and perfectly faithful. Measured on
  a Color LaserJet Pro MFP M283fdw, ordinary browser jobs estimate between 1200
  and 1900 and print, while the lowest observed failure estimates about 4000. A
  file whose cost cannot be determined is always converted, never assumed
  cheap.

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
  Port to listen on. Default 631, which requires `CAP_NET_BIND_SERVICE` or an
  adjusted `net.ipv4.ip_unprivileged_port_start`.

* `--timeout` *SECONDS*:
  Time allowed for each conversion and each request to the printer. Default 300.

* `-v`, `--verbose`:
  Log protocol detail.

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

* `/usr/local/lib/ippfix/`:
  Installation directory.

* `/usr/local/lib/ippfix/defont`:
  PDF conversion helper. May be run by hand for diagnosis.

* `/etc/ippfix/ippfix.crt`, `/etc/ippfix/ippfix.key`:
  Self-signed TLS credentials created by the installer.

* `/etc/systemd/system/ippfix.service`:
  Service unit; edit `ExecStart` to configure the printers.

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
upstairs   Print-Job   HTTP 200  [outlined 393836 -> 76789 bytes in 0.2s]
```

`relayed` indicates the job was passed through unchanged.

Because an affected printer reports success whether or not it printed, the
reliable check is the device's own total impression count, which does not
advance for a job that died. The Printer MIB (RFC 3805) exposes that counter on
essentially any network printer, as `1.3.6.1.2.1.43.10.2.1.4.1.1`. Vendors
usually expose more besides — on the printer this was developed against, an
assertion log naming `fontcache.c` is what identified the defect — but those
paths are vendor specific and this program neither uses nor depends on them.

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

gs(1), cups(1), systemd.service(5)

<a name="license"></a>

# License

MIT.
