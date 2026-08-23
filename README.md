# ippfix

A small IPP proxy that repairs print jobs for HP LaserJet Pro printers whose
firmware silently drops anything with too many embedded fonts.

Jobs arrive over IPP, have their text converted to vector outlines so that no
font program reaches the printer, and are forwarded on. Everything else — the
printer's capabilities, its live status, its job handling — is relayed
untouched.

## The Problem: Jobs That Vanish

An affected printer accepts the job, reports it `completed` over IPP, runs its
warm-up cycle, and produces nothing. Sometimes it prints the first few pages
and then stops. No error reaches the client, and none appears on the panel.

The cause is the printer's combined PostScript/PDF interpreter, which has a
fixed per-page budget for embedded fonts and the glyphs drawn from them.
Exceeding it aborts the interpreter mid-job. On devices that record it, the
event log shows:

```
ASSERT FAILED
Task: POSTSCRIPT
File: fontcache.c  Line: 2494
```

The budget covers both the glyphs drawn and the embedded font programs they are
drawn from, and the two trade against each other. Measured on a Color LaserJet
Pro MFP M283fdw: with one fully embedded font, 527 distinct glyphs render and
534 do not; add a second fully embedded font and the page fails at 300. The
cost of a font scales with how many glyphs its embedded program declares, not
with a flat per-font constant, so a heavily subsetted font is much cheaper than
a complete one.

That means **there is no safe threshold to design against**. The figures above
come from probes embedding complete, unsubsetted fonts; ordinary jobs from a
browser subset aggressively and sit far below them. Whether a given document
crosses the line depends on the typefaces, how they were subsetted, and how
many distinct characters the page uses.

The defect is not new — it is present in firmware builds years apart, and it is
not going to be fixed. Client-side changes affect how often it is reached:
Chrome 130 through 144 emitted a separate embedded font program for each
*strike* of a typeface, including one per text colour, which multiplied the
cost of an ordinary page considerably. Chrome 145 removed the colour component
and normalises text size out of the key, so current versions embed far fewer
font programs than that era did.

Printing "as an image" avoids the problem, because a bitmap contains no fonts.
It is also lossy, produces very large jobs, and on ChromeOS the setting resets
between jobs.

## The Solution: Remove the Fonts, Keep the Vectors

`ippfix` runs every PDF through Ghostscript's `-dNoOutputFonts`, which converts
glyphs to filled paths. No font program reaches the printer, so the entire
failure mode is structurally impossible rather than merely less likely.

Text remains **vector**, so the printer's own rasteriser still renders it at
full device precision and its edge-enhancement still applies. This is the
important difference from rasterising the page, which commits the geometry to
the device grid before the printer sees it, discards the benefit of the
printer's own halftoning, and inflates a small job into tens of megabytes.

In practice the converted files are usually *smaller* than the originals,
because a subsetted font program is bulkier than the outlines actually used.

## Capabilities

- **Driverless.** Clients see a standard IPP Everywhere / AirPrint printer.
- **Full feature parity, with nothing to maintain.** Printer attributes are
  mirrored from the real device on every request, so duplex, trays, media
  sizes and types, colour modes, print quality and page ranges are always
  exactly what the printer supports, including after a firmware update.
- **Live status, not a fake ready queue.** Supply levels, tray state, jams and
  error conditions come straight from the printer.
- **Several printers, one daemon.** Each is published as its own queue.
- **IPv4 and IPv6**, plaintext IPP and implicit TLS on the same port.
- **Fails safe.** If conversion fails for any reason, the original job is
  forwarded unchanged. A job that might not print is better than one that
  prints something wrong.
- **Leaves scanning alone.** Only `_ipp` and `_ipps` are published; eSCL
  scanning is a separate service on the printer and is untouched.

## System Requirements

- Python 3.9 or later, with the `venv` module
- Ghostscript (`gs`) 10.0 or later
- `systemd`, for the supplied unit file

The `zeroconf` Python package is installed into the virtual environment by the
installer, and is used to publish the queues over DNS-SD.

## Installation

```sh
git clone https://github.com/gutschke/ippfix.git
cd ippfix
sudo ./install.sh
```

The installer copies everything into its own directory (by default
`/usr/local/lib/ippfix`), builds the virtual environment, installs the man
page, creates an unprivileged `ippfix` user, and enables the systemd unit.

Installing the daemon is the easy part; making clients use it rather than the
printer is where deployments go wrong. See **[DEPLOYMENT.md](DEPLOYMENT.md)**
for that, including the cheap options for networks with no special
infrastructure.

If another mDNS responder is already running on the machine, it must be told to
release UDP port 5353. With `systemd-resolved` that means setting
`MulticastDNS=no` in `/etc/systemd/resolved.conf` and restarting it.

## Configuration

Edit `ExecStart` in `/etc/systemd/system/ippfix.service` and restart the
service. Each printer is given as `NAME=URI`:

```
ExecStart=/usr/local/lib/ippfix/venv/bin/ippfix /usr/local/lib/ippfix/ippfix.py \
    upstairs=ipp://printer1.example/ipp/print \
    downstairs=ipp://printer2.example/ipp/print
```

Those are published as `ipp://this-host:631/ipp/upstairs` and
`ipp://this-host:631/ipp/downstairs`.

A bare URI with no `NAME=` is published as `/ipp/print`.

### Certificates

TLS uses `/etc/ippfix/ippfix.crt` and `/etc/ippfix/ippfix.key`, generated by
the installer as a self-signed pair. Printers ship self-signed certificates
too, so clients treat this no differently.

### Discovery

If the printers are also reachable directly, clients will see both them and the
proxy queues, and users will pick whichever comes first — so half the jobs
still vanish. Suppressing the printers' own advertisements is the step people
most often skip, and skipping it is the usual reason for concluding that
`ippfix` does not work.

There are several ways to do it, ranging from one setting on the printer to
full network isolation, with different trade-offs around scanning, fallback
printing and effort. **[DEPLOYMENT.md](DEPLOYMENT.md) walks through all of
them** and explains how to verify the result.

Whichever you choose: suppress only `_ipp`, `_ipps` and `_pdl-datastream`.
`_uscan` and `_uscans` must keep working or scanning breaks.

## Usage

```sh
systemctl status ippfix
journalctl -u ippfix -f
```

Each job is logged with the queue, the operation, and what conversion did:

```
upstairs   Print-Job   HTTP 200  [outlined 393836 -> 76789 bytes in 0.2s]
```

`relayed` in that field means the job was passed through untouched — either it
was not a PDF, or conversion declined to alter it.

## Verifying the Fix

To confirm a printer is affected before deploying, or to check that a change
helped, watch the printer's own event log rather than trusting IPP, which
reports success either way:

```sh
curl -s http://PRINTER/DevMgmt/ProductLogsDyn.xml
```

A job that dies leaves the total impression count unchanged. Comparing that
count before and after is a more reliable signal than `job-state`.

To compare directly, run a second instance with `--no-convert` on another port
and print the same document to both.

## Troubleshooting

**Jobs still vanish.** Check the log for `relayed`. If conversion is declining
to run, `gs` may be missing or failing; run `defont < in.pdf > out.pdf` by hand
to see its output.

**The printer does not appear on clients.** Confirm nothing else holds UDP
5353, and that the address in `--advertise` is the one clients can reach. Some
clients cache a previous entry; removing and re-adding the printer clears it.

**Two identical printers appear.** The printer's own advertisement is still
being seen. See *Discovery* above.

**Conversion is slow.** Time scales with the number of embedded fonts, not
page count. Documents with dozens of distinct faces take a few seconds; ordinary
ones take a fraction of one.

## License

MIT — see [LICENSE](LICENSE).
