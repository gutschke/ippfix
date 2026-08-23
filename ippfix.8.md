# ippfix(8) - IPP proxy that repairs print jobs for printers with a limited

1.0, August 2026

font cache.

<a name="synopsis"></a>

# Synopsis

```
ippfix [-a|--advertise ADDRESS] [--cert FILE] [--converter PATH] [--key FILE] [--no-advertise] [--no-convert] [-p|--port PORT] [--timeout SECONDS] [-v|--verbose] [NAME=]URI...
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

The budget covers fonts as well as glyphs. On a Color LaserJet Pro MFP M283fdw,
roughly 527 distinct glyphs from a single embedded font succeed where 534 fail,
and each additional embedded font on the page consumes about 300
glyph-equivalents of the same allowance. The figures vary between typefaces, so
no fixed threshold is safe.

The defect appears in firmware builds years apart and is unlikely to be fixed.
What changed is the client: Chrome 130 and later embed a separate font program
for every *strike* of a typeface — each distinct size, and until Chrome 145 each
distinct colour — so an ordinary page can embed many copies of one font and
exhaust a limit that has not moved in years.

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

* `--cert` *FILE*:
  TLS certificate. Default `/etc/ippfix/ippfix.crt`.

* `--converter` *PATH*:
  Helper that rewrites a PDF read on standard input and writes the result to
  standard output. Default `/usr/local/lib/ippfix/defont`.

* `--key` *FILE*:
  TLS private key. Default `/etc/ippfix/ippfix.key`.

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
advance for a job that died. On HP devices both that counter and the assertion
log are readable over HTTP from `/DevMgmt/ProductUsageDyn.xml` and
`/DevMgmt/ProductLogsDyn.xml`.

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
