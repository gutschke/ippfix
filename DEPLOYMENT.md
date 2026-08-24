# Deploying ippfix

Installing the daemon is the easy part. The part that causes confusion is
making clients actually *use* it, because the printer is perfectly happy to
keep advertising itself and users will pick whichever queue appears first.

This document covers both, from the cheapest approach to the most thorough.
Most networks want option 2 or 4 in Step 2.

## Contents

- [The one thing that goes wrong](#the-one-thing-that-goes-wrong)
- [Choosing an approach](#choosing-an-approach)
- [Step 1: install the daemon](#step-1-install-the-daemon)
  - [The packages](#the-packages)
  - [What installing the package does](#what-installing-the-package-does)
  - [The fallback: install.sh](#the-fallback-installsh)
  - [If the queues never appear](#if-the-queues-never-appear)
  - [If the host has several addresses](#if-the-host-has-several-addresses)
- [Step 2: stop the printer competing with it](#step-2-stop-the-printer-competing-with-it)
  - [Option 1: manual client configuration](#option-1-manual-client-configuration)
  - [Option 2: turn off AirPrint on the printer](#option-2-turn-off-airprint-on-the-printer)
  - [Option 3: firewall the printer's print ports](#option-3-firewall-the-printers-print-ports)
  - [Option 4: filter at an mDNS reflector](#option-4-filter-at-an-mdns-reflector)
  - [Option 5: full network isolation](#option-5-full-network-isolation)
- [Network topology notes](#network-topology-notes)
- [Step 3: verify](#step-3-verify)
- [Running it](#running-it)
- [Keeping Ghostscript current](#keeping-ghostscript-current)
- [Rolling back](#rolling-back)

## The one thing that goes wrong

`ippfix` publishes a queue that looks like an ordinary AirPrint / IPP
Everywhere printer. So does the printer. Clients discover both.

They usually look nearly identical in the picker, so users choose at random,
and half their jobs still vanish. Worse, a client that already knows the
printer keeps using its cached entry and never sees the proxy at all.

**Deploying `ippfix` without addressing this is the single most common way to
conclude it "doesn't work".** Everything in Step 2 exists to solve it.

A related trap: whatever you do, leave the printer's **scanning** services
alone. Scanning uses eSCL, advertised as `_uscan._tcp` and `_uscans._tcp` on
different ports from printing. It is entirely independent of this problem, and
several of the options below can break it by accident.

## Choosing an approach

| | Effort | Extra software | Users see one printer | Direct printing still possible | Scanning safe |
|---|---|---|---|---|---|
| 1. Manual client config | Lowest | none | No | Yes | Yes |
| 2. Disable AirPrint on printer | Low | none | Yes | No | Verify — see below |
| 3. Firewall print ports | Medium | none | No | No | Yes |
| 4. Filter at mDNS reflector | Medium | a filtering reflector | Yes | No | Yes |
| 5. Full isolation | Highest | reflector + DHCP/firewall | Yes | No | Yes |

Options 1 and 3 combine well. So do 3 and 4.

If you have no segmented network and only a few devices, start with **option
1**, then move to **option 2** once you are satisfied the proxy works.

If the printer already lives on an IoT or guest segment, **option 4** is the
surgical answer and is what the tables below assume by default.

## Step 1: install the daemon

Pick a host that can reach the printer and that clients can reach. A small
always-on machine or container is plenty; conversion is a fraction of a second
per document and the daemon is idle otherwise.

### The packages

Two Debian packages are built from this tree, and they are the recommended way
to install it. `ippfix` is the proxy itself; `ippfix-selfbuild` is optional and
keeps the Python virtual environment rebuilt as the system Python and its
dependencies move, which otherwise has to be remembered by hand across a
release upgrade.

```sh
sudo apt install build-essential debhelper devscripts
git clone https://github.com/gutschke/ippfix.git
cd ippfix
dpkg-buildpackage -b -us -uc
sudo apt install ../ippfix_1.0.0_all.deb ../ippfix-selfbuild_1.0.0_all.deb
```

The package puts the program under `/usr/lib/ippfix`, the units in
`/usr/lib/systemd/system`, the diagnostic scripts in
`/usr/share/ippfix/scripts`, and the manual page where `man 8 ippfix` will find
it. The service accounts `ippfix` and `ippfix-convert` are created from
`sysusers.d`, and `/etc/ippfix`, `/var/lib/ippfix` and `/run/ippfix` from
`tmpfiles.d`.

Configuration is not shipped, so the first step after installing is to create
it:

```sh
sudo install -m0644 /usr/share/doc/ippfix/ippfix.conf.example /etc/ippfix/ippfix.conf
sudoedit /etc/ippfix/ippfix.conf
```

`IPPFIX_ARGS` in that file is the whole command line. It is read by a shell, so
a printer whose name contains a space can be quoted:

```
IPPFIX_ARGS="'Front Desk=ipp://192.0.2.10/ipp/print'"
```

Then start it:

```sh
sudo systemctl start ippfix
systemctl status ippfix
```

Editing that conffile is the supported way to change options; it survives
upgrades, where an edited unit file would not. To change something in the unit
itself — the egress restrictions below, for instance — use a drop-in:

```sh
sudo systemctl edit ippfix
```

To confine the daemon's outbound traffic to the printers and the local network,
put `IPAddressDeny=any` and a matching `IPAddressAllow=` in that drop-in. It
ships commented out because the right values are site specific.

### What installing the package does

Installing does not start a proxy, because the package cannot know which
printers exist. Both the service and its listening socket are conditional on
`/etc/ippfix/ippfix.conf` existing, and the service additionally checks that a
printer is named in it:

- **No configuration** — the units are *skipped*, not started and not failed.
  `systemctl status ippfix` reports `inactive`, the journal says `no printer
  configured; edit /etc/ippfix/ippfix.conf`, and nothing restart-loops. Port
  631 is not taken either, so an unconfigured machine can still run CUPS. (The
  two are alternatives rather than companions once `ippfix` is configured: both
  want port 631.)
- **A printer configured** — the service starts on installation and restarts
  after an upgrade, without anyone having to remember to do it.

So the sequence on a new machine is: install, create the conffile, then
`systemctl start ippfix`. From then on installs and upgrades look after
themselves.

The Python virtual environment is built in the package's `postinst` rather than
shipped, because a virtual environment is tied to the interpreter's minor
version. That step needs the network. If it fails, the package says so and the
service will not start until it exists; `dpkg-reconfigure ippfix` retries, and
`ippfix-selfbuild` retries on its own schedule.

### The fallback: install.sh

Where building a package is not an option, `install.sh` installs the same
software into `/usr/local` (by default `/usr/local/lib/ippfix`; it asks). It
builds the virtual environment, installs the man page, creates the `ippfix` and
`ippfix-convert` accounts, generates the self-signed TLS pair, and enables the
units.

```sh
git clone https://github.com/gutschke/ippfix.git
cd ippfix
sudo ./install.sh
```

There is no conffile in this layout: the printers are named on the `ExecStart`
line of the unit, which `install.sh` symlinks from its installation directory
into `/etc/systemd/system`.

```sh
sudoedit /usr/local/lib/ippfix/ippfix.service
```

```
ExecStart=/usr/local/lib/ippfix/venv/bin/ippfix /usr/local/lib/ippfix/ippfix.py \
    --converter unix:/run/ippfix/convert.sock \
    office=ipp://192.0.2.10/ipp/print
```

```sh
sudo systemctl daemon-reload
sudo systemctl start ippfix
```

Everything else in this document applies to both layouts; only the paths
differ. Where a command below names `/usr/lib/ippfix`, use your installation
directory instead.

### If the queues never appear

Only one mDNS responder may hold UDP port 5353. On a systemd machine that is
usually `systemd-resolved`:

```sh
sudo sed -i 's/^#*MulticastDNS=.*/MulticastDNS=no/' /etc/systemd/resolved.conf
sudo systemctl restart systemd-resolved
sudo systemctl restart ippfix
```

If the host must keep its own responder, run `ippfix` with `--no-advertise`
and publish the queue from that responder instead.

### If the host has several addresses

Autodetection picks one interface, which may not be the one clients use. Pin
it:

```
--advertise 192.0.2.50
```

## Step 2: stop the printer competing with it

### Option 1: manual client configuration

Change nothing on the network. Add the proxy by address on each client and
tell people to use it.

- **macOS:** System Settings → Printers → Add → IP tab → protocol *Internet
  Printing Protocol (IPP)*, queue `ipp/NAME`
- **ChromeOS:** Settings → Printing → Printers → *Add printer manually*,
  protocol IPP, queue `ipp/NAME`
- **Linux/CUPS:** `lpadmin -p office -E -v ipp://HOST/ipp/NAME -m everywhere`

`ippfix --list` prints the queues a running instance serves, which is the
easiest way to get those URIs right. The same listing is served as JSON at
`/queues.json` and as a table at the daemon's HTTP root.

**Pros.** Nothing to change on the network or the printer. Instantly
reversible. Works even where you control nothing but the clients. Good for
proving the fix before committing to anything.

**Cons.** Per-device, so it does not scale. The printer still advertises
itself, so users can still pick it and still lose jobs — and on ChromeOS the
auto-discovered entry often sorts first. Devices that already have the printer
configured keep using it.

**Use when** you have a handful of machines, or you are evaluating.

### Option 2: turn off AirPrint on the printer

Most printers can stop advertising their print services while continuing to
accept jobs from the proxy.

On HP LaserJet models, the embedded web server has **Networking → AirPrint**
with an enable/disable control. Disabling it removes the `_ipp._tcp` and
`_ipps._tcp` advertisements.

**Do not** reach for the global mDNS or Bonjour switch instead. That one is
all-or-nothing: it also stops `_uscan`/`_uscans`, and scanner discovery breaks.
On HP devices you can see which you are looking at — the global switch appears
as `MDNSSupport` in `/DevMgmt/NetAppsDyn.xml`:

```sh
curl -s http://PRINTER/DevMgmt/NetAppsDyn.xml | grep -o 'MDNSSupport>[^<]*'
```

That should still read `enabled` when you are done.

**Verify scanning survived** before walking away — see [Step 3](#step-3-verify).
If your model only offers the global switch, this option is unsafe for anyone
who scans; use option 3 or 4 instead.

**Pros.** Cheapest approach that genuinely removes the duplicate. No extra
software, no network changes, one setting.

**Cons.** Not all models separate AirPrint from mDNS, and some silently take
eSCL down with it. You lose direct printing as a fallback, so if the proxy host
is down nobody can print at all. The setting can come back after a firmware
update, so re-check it after one.

**Use when** the printer shares a network with clients and its firmware
separates the two switches.

### Option 3: firewall the printer's print ports

Block the printer's print ports from the client subnet, permitting only the
proxy host. With nftables on the router:

```
table inet filter {
  chain forward {
    ip saddr $proxy_host ip daddr $printer tcp dport { 631, 9100, 515 } accept
    ip daddr $printer tcp dport { 631, 9100, 515 } drop
  }
}
```

Leave 80, 443 and 8080 open if you want the printer's web interface and eSCL
scanning to keep working — **eSCL runs over those same HTTP ports**, so
blocking them stops scanning.

**Pros.** Enforces the routing no matter what a client discovered or cached.
Survives firmware updates. Does not touch the printer.

**Cons.** Does not stop the printer *advertising*, so clients still show it and
users who pick it get a confusing hang rather than a clear error. Really a
complement to option 1, 2 or 4 rather than a substitute.

**Use when** you need a guarantee that nothing bypasses the proxy.

### Option 4: filter at an mDNS reflector

If the printer is on a separate segment — an IoT or guest VLAN — something is
already bridging discovery between segments. Filter there.

With [mdnsreflect](https://github.com/gutschke/mdnsreflect), exclude the print
service types and leave the scan types alone. Add to `ExecStart`:

```
--exclude _ipp._tcp.local. \
--exclude _ipps._tcp.local. \
--exclude _pdl-datastream._tcp.local.
```

```sh
sudo systemctl daemon-reload
sudo systemctl restart mdnsreflect
mdnsreflect --list-services
```

The printer's `_uscan._tcp` and `_uscans._tcp` entries must still be listed.
Names must be fully qualified with the trailing dot, exactly as shown — a name
that does not match is ignored silently rather than reported.

Note this filters by service *type*, for every device being reflected. That is
usually what you want, since any printer behind the reflector can be given its
own proxy queue. If you need to suppress one device while reflecting another's
print services, filter by name instead.

The proxy itself sits on the client side, so its own advertisements never go
through the reflector at all.

**Pros.** Surgical: print discovery disappears, scan discovery is untouched,
and the printer is not modified so a firmware update cannot undo it. Handles
several printers at once. Reversible by deleting three arguments.

**Cons.** Requires a segmented network and a reflector that can filter. Plain
Avahi reflectors generally cannot do this.

**Use when** the printer is already on its own segment. This is the best
option when it applies.

### Option 5: full network isolation

Put the printer where clients simply cannot reach it, and let only the proxy
across.

[isodhcp](https://github.com/gutschke/isodhcp) is built for this: it hands
untrusted devices `/32` or `/30` leases so they have no layer-2 peers, forcing
everything through the router where firewall policy applies, and populates
nftables sets as leases come and go. Combine it with option 4 for discovery and
option 3 for enforcement.

**Pros.** Strongest guarantee — a misconfigured client cannot reach the printer
even deliberately. Brings the usual benefits of isolating IoT devices.

**Cons.** By far the most work, and disruptive to set up on a live network.
Overkill for this problem alone.

**Use when** you want IoT isolation anyway and this is one beneficiary of it.

## Network topology notes

The common deployment has the printer on an isolated segment and clients on the
main LAN, which means the daemon straddles two networks with different
properties. Three things follow from that, all handled automatically, but worth
knowing about because each fails in a way that looks like something else.

**Mixed address families.** A dual-stack LAN in front of an IPv4-only printer
segment is normal. If a printer's name resolves to both an A and an AAAA record
but its network carries only IPv4, Python's HTTP client would use whichever
address came first and every job would hang until it timed out. `ippfix` tries
each address a name resolves to and remembers the one that worked, so this
costs at most one slow attempt at startup and nothing afterwards. Configuring
printers by literal address avoids the question entirely.

**Multi-homed hosts.** The daemon publishes the address given by `--advertise`
plus the stable global IPv6 addresses **of that same interface**. It does not
publish every address the machine happens to have: on a host with a second
interface facing a network clients cannot route to, advertising those addresses
makes clients stall on an address that will never answer. If autodetection
picks the wrong interface, set `--advertise` explicitly.

**IPv6 addresses that come and go.** Privacy/temporary addresses rotate, and
deprecated or tentative ones are not usable for new connections. Publishing any
of them produces a queue that works today and is unreachable next week.
`ippfix` publishes only stable, globally scoped, non-tentative addresses. Where
IPv6 exists but is not actually routable, `--no-ipv6` publishes IPv4 only.

Link-local addresses are never published: they require a scope identifier that
a DNS-SD record cannot usefully carry.

## Step 3: verify

**1. The queue is visible and answers.**

```sh
avahi-browse -rt _ipp._tcp          # or: mdnsreflect --list-services
ipptool -tv ipp://PROXY/ipp/NAME get-printer-attributes.test
```

`printer-make-and-model` should be the real printer, while `printer-name` and
`printer-uuid` are the proxy's. That combination is the point: clients get the
printer's true capabilities under a distinct identity.

**2. Only one printer is offered.** Check the printer picker on a client that
has never seen either. If two appear, Step 2 is incomplete.

**3. Scanning still works.** The check that matters most, because it is the
one most easily broken by accident:

```sh
avahi-browse -rt _uscan._tcp
curl -s http://PRINTER/eSCL/ScannerCapabilities | head -5
```

Then actually scan something.

**4. Conversion is running.**

```sh
journalctl -u ippfix -f
```

A job should log `outlined … -> … bytes`. If it says `relayed`, conversion
declined — either the job was not a PDF, or `gs` failed. Test the converter
directly:

```sh
/usr/lib/ippfix/defont < sample.pdf > out.pdf
```

**5. The fix actually fixes it.** An affected printer reports success whether
or not it printed, so `job-state` proves nothing. Judge it on the device's own
impression counter, which does not advance for a job that died. The portable
way is the RFC 3805 Printer MIB over SNMP, which `probe-printer.py` reads for
you:

```sh
python3 /usr/share/ippfix/scripts/make-reproducer.py repro.pdf
python3 /usr/share/ippfix/scripts/probe-printer.py ipp://PROXY/ipp/NAME repro.pdf
```

Send the same document straight to the printer for comparison, or run a second,
non-converting instance on another port:

```sh
ippfix --port 6310 --no-advertise --no-convert ipp://PRINTER/ipp/print
```

[DIAGNOSING.md](DIAGNOSING.md) describes what else the printer can be asked,
including the vendor-specific log that identified the defect in the first
place.

## Running it

**Reading the log.** Each job is logged with its queue, the operation, and what
conversion did:

```
upstairs   Print-Job   HTTP 200  [outlined 393836 -> 76789 bytes in 0.2s (font cost 465)]
```

`relayed` in that field means the job went through untouched, with the reason
in brackets: it was not a PDF, the converter failed, or the conversion was
rejected because it would have changed the page. The font cost is recorded for
diagnosis only; nothing is decided by it unless `--convert-threshold` is set,
which it is not by default.

**When a job is lost anyway.** `--alert-mail ADDRESS` follows each job to its
terminal state and mails a report when the printer reports success having
marked nothing — the one failure that is otherwise invisible to everybody. The
report carries the job's state history, the document's structure, and what the
printer said about itself at the time. It needs a local `sendmail`; without one
the report goes to the journal. See the manual page for the rate limit and the
timeout.

**Reading the printer's own page counter.** Every report cross-checks
`job-impressions-completed` against `prtMarkerLifeCount` over SNMP. This is on
by default and needs no configuration; the printer is asked what its counter
counts, and the signal switches itself off, loudly, if the counter misbehaves.
Turn it off with `--no-page-counter`, or per printer with `?page-counter=off`
on its URI.

**Letting clients read the printer's counters.** Optional, and the socket ships
disabled, because opening a UDP port to answer questions about a printer is a
decision about what the network may see:

```
systemctl enable ippfix-snmp.socket
systemctl stop ippfix
systemctl start ippfix-snmp.socket
systemctl start ippfix
```

That order, once. A socket unit cannot hand its descriptor to a service that is
already running; boots after this are automatic.

The relay serves read-only `GET` and `GETNEXT` inside the Printer MIB and
refuses `GETBULK`, `SET`, SNMPv3 and everything outside its subtrees, rate
limited per source. Measured against a real printer the worst answer in the
allowlist is 143 bytes to a 45-byte request, so it is a poor reflector — but it
does put the printer in front of whatever can reach this host, which may be more
than can reach the printer. `--snmp-allow CIDR` narrows that back down and is
worth setting.

**A mail system that queues is not a mail system that delivers.** `sendmail(1)`
returns as soon as the message is queued; a separate agent delivers it. If that
agent is a child of a `Type=oneshot` unit, the default `KillMode` tears it down
the instant the foreground command exits — mid-SMTP, with nothing logged. The
message stays queued, every retry dies the same way, and the report never
arrives. Both `ippfix-alert.service` and a drop-in for `dma.service` set
`KillMode=process` for exactly this reason.

It is worth checking after any change to the mail path, because the symptom is
silence:

```
dma -bp                    # or mailq: anything sitting here is not delivered
journalctl -t dma | grep -c 'delivery successful'
```

The report's `From:` is the address it is addressed to. That address was
configured to receive these, so it is known to route — where `ippfix@` plus
whatever the host calls itself often does not, and a bounce that goes nowhere
is a second thing failing silently.

**Capturing a document that failed.** The failure depends on document content,
so the single most useful thing to have is the document itself.

```
--archive /var/lib/ippfix/archive --archive-max 50
```

Each job is saved exactly as it arrived, before conversion, with a sidecar
noting the queue, job name, format and what conversion did. **This stores the
documents people print.** It is off by default, the directory is 0700 and the
files 0600 owned by the service account, and the daemon logs a warning for as
long as it is on. Archived jobs are aged out after seven days by a `tmpfiles.d`
rule as well as bounded by `--archive-max` and `--archive-max-bytes`, so a
forgotten flag cannot leave documents on disk indefinitely. Treat it as a
diagnostic that gets switched on to answer a question and switched off again.

With `--alert-mail` also set, the archived copy is what makes a report
reproducible: it is attached, alongside the version the proxy actually sent, so
whoever reads the report has both without going to the server. `0` for
`--alert-max-attachment` keeps the reports and drops the documents.

**Not forgetting to switch it off.** Nobody remembers a flag they turned on
three months ago, so schedule the reminder when you turn it on:

```
ippfix-archive-reminder --schedule 2026-11-24
```

That writes a one-shot timer, and the reminder deletes the timer, its unit and
itself once it has run. Turning `--archive` off is then the only thing left to
do by hand, which is the only part that needs judgement. It goes quietly if the
flag is already off by then, and `--cancel` removes it early. The address comes
from `--alert-mail` unless one is given.

**Large jobs.** Outlining inlines a full path at every glyph occurrence, so a
long document can grow past what the printer will accept as a PDF. When that
happens the job is rasterised instead — a modest loss of fidelity, but it prints
rather than being rejected.

Modest is meant literally, and this is worth being accurate about because it is
easy to assume the worst. Rasterising here is 600 dpi contone at the device's
own resolution. The printer's raster interface is 8 bits per channel with no
1-bit mode, so the printer still does its own halftoning and edge processing —
it is not handed pre-screened dots. What is lost is geometry precision: edges
are quantised to the 600 dpi grid before the RIP sees them, and antialiasing
recovers most but not all of that. It costs transfer size and *saves* CPU
(0.18 s/page against 0.49 for outlining). It is not the blurry "print as image"
path — that blur is Chrome encoding the page as JPEG at quality 40.

Measured: outlining costs about **650 KB of PDF per page** on dense body copy
and **390 KB** on an ordinary report page, so a 61 MB device limit arrives at
roughly 90 or 155 pages respectively. That is a long report, not a pathological
document.

**Know the limit of this tier before relying on it.** The raster is 1.5–2.5 MB
per page, which crosses the proxy's own 256 MB ceiling on converted output at
around 175 report pages — only twenty pages past where rasterising starts. Past
that the conversion is abandoned and the original is relayed **unconverted**,
which is the one outcome this proxy exists to avoid. A 200-page report is
already in that band. See
[OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) — splitting the job is the fix, and is
not yet implemented.

The tier is logged whenever it fires:

```sh
journalctl -u ippfix | grep 'rasterising instead'
```

Documents that hit it give up the vector-text advantage the proxy otherwise
preserves — a real cost, if a small one.

What does **not** follow is that long documents are safe to leave unconverted.
The fault is per page: one cover page in a display face, one chart with a symbol
font, one quoted passage in another script, and the job fails no matter how dull
the other ninety-nine pages are. `--convert-threshold` is judged on the worst
page in a document, never on an average, so it does not dilute one costly page
among many cheap ones — but it is still a prediction, and the model behind it
has been falsified twice. That is why it is off by default.

The limit comes from the printer's own
`pdf-k-octets-supported` (the daemon uses 80% of what the device reports, and
logs the figure at startup); `--max-pdf-bytes` supplies a limit only for a
device that declares none. To see what yours declares:

```sh
ipptool -tv ipp://PRINTER/ipp/print get-printer-attributes.test | grep pdf-k-octets
```

The raster format, colour space and resolution are read from the printer's
attributes as well, and travel with each document to the converter, which has
no network of its own. `RASTER_DPI` and `RASTER_COLORSPACE` in the converter's
environment only apply when `defont` is run by hand.

## Keeping Ghostscript current

Ghostscript does the conversion, and it is the most exposed component here: it
parses documents that arrive from the network. It needs to stay patched, which
means it needs to keep coming from the distribution. **Do not build it from
source or carry a private copy** — an unpackaged Ghostscript is one that
`apt upgrade` will never fix, on exactly the component you least want to leave
unfixed.

### The version matters, but nothing needs configuring

Ghostscript before 10.05 discards function-based shadings — how browsers emit
conic and repeating CSS gradients. Later releases keep them:

| Ghostscript | ships in | function-based shadings |
|---|---|---|
| 10.02.1 | Ubuntu 24.04 LTS | discarded |
| 10.05.1 | Debian 13 | preserved |
| 10.06.0 | Ubuntu 26.04 LTS | preserved |

On an affected version nothing prints wrongly, because every conversion is
checked before it is used and one that lost a whole class of drawing construct
is thrown away in favour of the original bytes. Those documents keep their
appearance and simply forgo the font fix. To see which behaviour you have:

```sh
/usr/lib/ippfix/defont --selfcheck
```

### What to do about it

**Nothing, if you are on 24.04 and expect to move to 26.04 eventually.** The
distribution fixes this on its own schedule; the check stops firing by itself
once a newer Ghostscript is installed, and there is no pin to remember to
remove. This is the recommended course.

If those documents matter enough to act sooner, in decreasing order of sanity:

- **Move this host to a release with a newer Ghostscript.** Ubuntu 26.04 LTS or
  Debian 13. Clean, supported, and the whole problem disappears.
- **Run only the converter on a newer base.** The converter is already a
  separate, network-isolated service, so putting it in its own container is a
  small change rather than a redesign. It gains a second thing to keep updated.
- **A PPA.** Works, but you are trusting a third party for security updates on
  your most exposed parser, and PPAs are frequently abandoned.
- **Installing a newer distribution's `.deb` on 24.04.** Do not. Debian 13's
  Ghostscript wants `libjpeg.so.62` and `libpaper.so.2` where 24.04 has `.so.8`
  and `.so.1`, so it drags in a private library stack that apt will not maintain
  — the same problem as building it yourself, with more moving parts.

Whichever you choose, `--selfcheck` tells you where you actually stand, and the
output check means a wrong answer degrades to "not converted" rather than to
"printed wrongly".

## Rolling back

Every option in Step 2 is reversible, and none of them modifies stored jobs or
printer configuration beyond a single toggle:

- **Option 1:** remove the printer from the clients.
- **Option 2:** re-enable AirPrint in the printer's web interface.
- **Option 3:** drop the firewall rules.
- **Option 4:** remove the `--exclude` arguments and restart the reflector.
- **Option 5:** as for 3 and 4; the DHCP topology is independent.

To remove the daemon entirely:

```sh
sudo apt purge ippfix ippfix-selfbuild        # packaged install
sudo /usr/local/lib/ippfix/uninstall.sh       # install.sh install
```

`apt purge` removes the generated TLS credentials and `/var/lib/ippfix`, but
leaves `/etc/ippfix/ippfix.conf`, which is yours rather than the package's.

Clients will rediscover the printer directly once its advertisements return —
though they may need the stale queue removed and re-added, since discovery
results are cached.
