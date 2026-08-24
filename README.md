# ippfix

An IPP print proxy for HP LaserJet Pro printers that accept a job, report it
finished, and print nothing.

Jobs arrive over IPP, every PDF has its text converted to vector outlines so
that no font program reaches the printer, and the job is forwarded on.
Everything else — the printer's capabilities, its live status, its job
handling, its scanner — is relayed untouched.

## Is this your problem?

The symptom is a job that disappears without an error anywhere:

- The printer accepts the job and runs its warm-up cycle.
- IPP reports `job-state = completed`, so the client's queue shows success.
- No sheet is marked. Sometimes the first few pages emerge and the job stops.
- Nothing appears on the front panel, and nothing appears in the client's log.

It is much more often met when printing an existing PDF than when printing a
web page, because a browser renders pages in the system's own fonts while a PDF
carries whatever font its author embedded.

**Do not judge this by what the print system tells you.** Every layer above the
printer repeats the printer's own claim of success. The only honest signal is
the device's page counter, which does not move for a job that died. Two scripts
in this repository do the check for you — one builds a document that provokes
the fault, the other sends it and reads the counter over SNMP before and after:

```sh
python3 scripts/make-reproducer.py repro.pdf /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf 900
python3 scripts/probe-printer.py ipp://192.0.2.10/ipp/print repro.pdf
```

A verdict of `SILENT-NO-OUTPUT` means the printer is affected. Testing costs
nothing on an affected printer, because the job that provokes the fault marks
no paper; only a printer that is *not* affected spends a sheet.

## What it fixes, and what it does not

Three separate firmware faults have been reproduced on one printer. This proxy
addresses one of them.

| | how it fails | fixed here |
|---|---|---|
| **1. Font cache** | job accepted, reported complete, nothing marked | **yes** — no font program reaches the printer |
| **2. Vector colour fonts** | job rejected with `document-format-error` | no |
| **3. Malformed soft mask** | job accepted, reported complete, nothing marked | no |

Fault 1 is prevented rather than detected: outlining removes the font programs,
so the condition cannot arise. Faults 2 and 3 are properties of the document
that conversion faithfully preserves. If your jobs vanish for one of those
reasons, this proxy will not help, and
[DIAGNOSING.md](DIAGNOSING.md) explains how to tell which one you have.

## How it works

Every PDF is run through Ghostscript's `-dNoOutputFonts`, which converts each
glyph to a filled path. Text stays **vector**, so the printer's own rasteriser
still renders it at full device precision and its edge enhancement still
applies. That is the important difference from printing "as an image", which
commits the geometry to the device grid before the printer sees it, forfeits
the printer's halftoning, and inflates a small job into tens of megabytes.

Every PDF is converted. There is no threshold and no attempt to guess which
documents need it: four cost models were fitted to measured outcomes and every
one was falsified, so predicting the printer's limit from the file was
abandoned. Converting unconditionally costs about a third of a second per job.
Converted files are often *smaller* than the originals, because a subsetted
font program is bulkier than the outlines actually drawn from it.

If anything about the conversion looks wrong — Ghostscript fails, a font
program survives, or a class of drawing construct disappeared — the original
bytes are forwarded instead. A job that might not print beats one that prints
something wrong.

## Installing

The Debian packages are the recommended route. They install under
`/usr/lib/ippfix`, keep the configuration in `/etc/ippfix/ippfix.conf`, create
the two service accounts, and take no port until a printer has been configured.

```sh
sudo apt install build-essential debhelper devscripts
git clone https://github.com/gutschke/ippfix.git
cd ippfix
dpkg-buildpackage -b -us -uc
sudo apt install ../ippfix_1.0.0_all.deb
```

Then name your printers and start the service:

```sh
sudo install -m0644 /usr/share/doc/ippfix/ippfix.conf.example /etc/ippfix/ippfix.conf
sudoedit /etc/ippfix/ippfix.conf     # set IPPFIX_ARGS
sudo systemctl start ippfix
```

`IPPFIX_ARGS` holds the command line; at minimum it names each printer as
`NAME=URI`:

```
IPPFIX_ARGS="upstairs=ipp://192.0.2.10/ipp/print downstairs=ipp://192.0.2.11/ipp/print"
```

Those are published as `ipp://this-host/ipp/upstairs` and
`ipp://this-host/ipp/downstairs`. A bare URI with no `NAME=` becomes
`/ipp/print`.

The optional `ippfix-selfbuild` package rebuilds the Python virtual environment
when the system Python or a dependency moves, which is otherwise something to
remember by hand across a release upgrade.

On a system where building a package is not an option, `sudo ./install.sh`
installs the same software into `/usr/local` instead, with the command line in
the unit file rather than in a conffile. Both routes are described in
**[DEPLOYMENT.md](DEPLOYMENT.md)**.

### The step people skip

Installing the daemon is the easy part. If clients can still see the printer
directly, they will see two queues that look alike, users will pick whichever
appears first, and half the jobs will still vanish — which is the usual reason
for concluding that `ippfix` does not work.

Suppressing the printer's own `_ipp`, `_ipps` and `_pdl-datastream`
advertisements is therefore part of the installation, not an optional extra.
Leave `_uscan` and `_uscans` alone, or scanning breaks.
**[DEPLOYMENT.md](DEPLOYMENT.md)** walks through the ways to do it, from one
setting on the printer to full network isolation, and how to verify the result.

Clients build the URI they remember out of the DNS-SD SRV record, and by
default that is the `--advertise` address rather than a `.local` name. The
distinction shows up after discovery, not during it: a `.local` name has to be
resolved by multicast DNS on *every* print, and multicast does not cross a VPN,
a routed subnet, or a wireless network with client isolation — so the printer
is found once and then quietly stops working from anywhere else. Use
`--advertise-hostname` to publish a name instead, or `auto` for this system's
`.local` name.

If another mDNS responder already holds UDP port 5353 the queues never appear
at all. With `systemd-resolved` that means setting `MulticastDNS=no` in
`/etc/systemd/resolved.conf` and restarting it.

## Watching for jobs that still vanish

A lost job reports success, so nothing complains. `--alert-mail` closes that
gap: each print job is followed to its terminal state and judged on what the
printer says it marked, and a job that completes having marked nothing produces
a mailed report naming the queue, the job, what conversion did, the document's
structure, and what the printer said about itself at the time — an empty
cartridge prints nothing and blames nobody, and that is worth checking before
blaming firmware.

```
IPPFIX_ARGS="--alert-mail admin@ops.example office=ipp://192.0.2.10/ipp/print"
```

With `--archive` also on, the report **carries the documents themselves**: the
job as the client sent it, and the job as the proxy handed it to the printer.
Those are the two things that make a fault reproducible, and they are not the
same document — a fault that survives conversion is a different bug from one
conversion introduced. Without `--archive` the report describes the document
but cannot attach it, because no copy was kept.

Every report also cross-checks the printer against **its own page counter**,
read over SNMP. `job-impressions-completed` comes from the firmware that has
just reported success for a job it did not print; the RFC 3805 page counter
comes from the marking engine — a different subsystem, and the number a service
contract bills on. When the two disagree, the counter is the one to believe:
a job the printer claims to have printed while its counter never moved is a
failure nothing else in the stack would have noticed.

It is checked before it is believed. The printer is asked what its counter
counts, and a counter that goes backwards twice, jumps implausibly, stops
answering, or repeatedly fails to move for jobs reporting impressions is
switched off with an error in the journal saying so. It is never trusted enough
to accuse a printer until it has been seen to move for a job that did print.
`--no-page-counter` turns it off, or `?page-counter=off` on one printer's URI.

That also means a report can carry something somebody printed. Send it
somewhere that reflects how sensitive the printing is, and see
`--alert-max-attachment` for the size bound.

It is off unless an address is set, because a printer that does not report
impressions honestly would report every job as lost. Delivery uses the local
`/usr/sbin/sendmail`; if that is missing or refuses, the report goes to the
journal instead. `--alert-max-per-hour` (default 6) bounds the noise, and
suppressed reports are counted into the next one.

This makes the loss visible. It does not repair it: nothing in the proxy reacts
to what the printer did with a job.

## What clients see

- **Driverless.** A standard IPP Everywhere / AirPrint printer.
- **The real printer's capabilities.** Attributes are mirrored from the device
  on every request, so duplex, trays, media sizes and types, colour modes,
  print quality and page ranges are always exactly what it supports, including
  after a firmware update. Nothing here needs maintaining by hand.
- **Live status.** Supply levels, tray state, jams and error conditions come
  straight from the printer.
- **Several printers, one daemon**, each published as its own queue.
- **IPv4 and IPv6**, plaintext IPP and implicit TLS on the same port.
- **Scanning untouched.** Only `_ipp` and `_ipps` are published; eSCL scanning
  is a separate service on the printer.

PostScript is deliberately not offered to clients. It is interpreted by exactly
the task that fails, and it cannot be converted the way PDF is. PCL and PCL-XL
are offered and relayed unchanged, being a different interpreter on the device.

## Security

The proxy accepts print jobs from anyone who can reach the port — IPP has no
authentication here, exactly as a printer would not — and then runs a document
parser over whatever they send. Both halves of that are treated as hostile.

The network daemon never executes Ghostscript. Documents are handed over a
private socket to `ippfix-convert@.service`, a short-lived instance started per
connection under its own account, with `PrivateNetwork=true`, `IPAddressDeny=any`,
no capabilities, nothing writable but a private `/tmp`, and hard limits on
memory, tasks and run time. A flaw in document parsing therefore reaches no
network, no TLS key, no archive, and nothing belonging to the next job.

Neither half holds any privilege: `systemd` binds port 631 and passes the
descriptor, so the daemon's capability bounding set is empty. `systemd-analyze
security` rates the daemon **1.1** and the converter **0.4**.

[INTERNALS.md](INTERNALS.md) describes the split and what each half is trusted
with. `--archive`, which keeps a copy of every job for diagnosis, stores the
documents people print; see the manual page before enabling it.

## Requirements

- Python 3.9 or later, with `venv`
- Ghostscript 10.0 or later (10.05 or later renders conic and repeating CSS
  gradients correctly; older releases silently discard them, and documents
  containing one are then relayed unconverted rather than degraded)
- `systemd`, for the supplied units
- A local MTA, only if `--alert-mail` is used

The `zeroconf` Python package is installed into a virtual environment at
install time and is used to publish the queues over DNS-SD.

## Documentation

| | |
|---|---|
| **[DEPLOYMENT.md](DEPLOYMENT.md)** | installing, making clients use the proxy, verifying, and operating it |
| **[DIAGNOSING.md](DIAGNOSING.md)** | the three firmware faults: what is established, and how to reproduce each |
| **[INTERNALS.md](INTERNALS.md)** | how the code is put together, for anyone changing it |
| **[OPEN-QUESTIONS.md](OPEN-QUESTIONS.md)** | what is unsolved, and what it would take to settle |
| `man 8 ippfix` | every option, in detail |

## Troubleshooting

**Jobs still vanish.** Check the log for `relayed`: if conversion is declining
to run, `gs` may be missing or failing. Run `defont < in.pdf > out.pdf` by hand
to see why. If the job is being outlined and still vanishes, no font program
reached the printer and the cause is something else — see
[DIAGNOSING.md](DIAGNOSING.md), and please report it.

**The printer does not appear on clients.** Confirm nothing else holds UDP
5353, and that the address in `--advertise` is one clients can reach. Some
clients cache a previous entry; removing and re-adding the printer clears it.

**Two identical printers appear.** The printer's own advertisement is still
being seen. See [DEPLOYMENT.md](DEPLOYMENT.md).

**Nothing starts after installing the package.** That is expected until a
printer is configured. Both units are conditional on `/etc/ippfix/ippfix.conf`
existing, so without it `systemctl status ippfix` simply reports `inactive`.
Once the file exists but names no printer, the journal says `no printer
configured; edit /etc/ippfix/ippfix.conf`.

**Conversion is slow.** An ordinary job takes a fraction of a second; a long or
type-heavy one takes proportionally longer, since a full outline is inlined at
every glyph occurrence. The log records the time each job took.

## License

MIT — see [LICENSE](LICENSE).
