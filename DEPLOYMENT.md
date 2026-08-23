# Deploying ippfix

Installing the daemon is the easy part. The part that causes confusion is
making clients actually *use* it, because the printer is perfectly happy to
keep advertising itself and users will pick whichever queue appears first.

This document covers that, from the cheapest approach to the most thorough.
Most networks want option 2 or 4.

## Contents

- [The one thing that goes wrong](#the-one-thing-that-goes-wrong)
- [Choosing an approach](#choosing-an-approach)
- [Step 1: install the daemon](#step-1-install-the-daemon)
- [Step 2: stop the printer competing with it](#step-2-stop-the-printer-competing-with-it)
  - [Option 1: manual client configuration](#option-1-manual-client-configuration)
  - [Option 2: turn off AirPrint on the printer](#option-2-turn-off-airprint-on-the-printer)
  - [Option 3: firewall the printer's print ports](#option-3-firewall-the-printers-print-ports)
  - [Option 4: filter at an mDNS reflector](#option-4-filter-at-an-mdns-reflector)
  - [Option 5: full network isolation](#option-5-full-network-isolation)
- [Step 3: verify](#step-3-verify)
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

```sh
git clone https://github.com/gutschke/ippfix.git
cd ippfix
sudo ./install.sh
```

Then set the printers in `ExecStart`:

```sh
sudoedit /usr/local/lib/ippfix/ippfix.service
```

```
ExecStart=/usr/local/lib/ippfix/venv/bin/ippfix /usr/local/lib/ippfix/ippfix.py \
    office=ipp://192.0.2.10/ipp/print
```

```sh
sudo systemctl daemon-reload
sudo systemctl start ippfix
systemctl status ippfix
```

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
/usr/local/lib/ippfix/defont < sample.pdf > out.pdf
```

**5. The fix actually fixes it.** An affected printer reports success whether
or not it printed, so `job-state` proves nothing. Use the printer's own
impression counter, which does not advance for a job that died:

```sh
curl -s http://PRINTER/DevMgmt/ProductUsageDyn.xml | grep -o 'TotalImpressions>[0-9]*'
```

Read it before and after. On HP devices the interpreter's own assertion log is
also readable:

```sh
curl -s http://PRINTER/DevMgmt/ProductLogsDyn.xml
```

To A/B it, run a second non-converting instance on another port and send the
same document to both:

```sh
ippfix --port 6310 --no-advertise --no-convert ipp://PRINTER/ipp/print
```

## Rolling back

Every option is reversible, and none of them modifies stored jobs or printer
configuration beyond a single toggle:

- **Option 1:** remove the printer from the clients.
- **Option 2:** re-enable AirPrint in the printer's web interface.
- **Option 3:** drop the firewall rules.
- **Option 4:** remove the `--exclude` arguments and restart the reflector.
- **Option 5:** as for 3 and 4; the DHCP topology is independent.

To remove the daemon entirely:

```sh
sudo /usr/local/lib/ippfix/uninstall.sh
```

Clients will rediscover the printer directly once its advertisements return —
though they may need the stale queue removed and re-added, since discovery
results are cached.
