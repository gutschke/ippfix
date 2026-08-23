#!/usr/bin/env python3
"""ippfix: an IPP proxy that repairs print jobs for HP LaserJet Pro printers.

The problem
-----------
HP LaserJet Pro printers run a combined PostScript/PDF interpreter with a
fixed per-page budget for embedded fonts and glyphs. Exceed it and the
interpreter aborts: the engine has already warmed up, the job reports
``completed`` over IPP, and nothing is marked. No error reaches the client and
none appears on the panel. Where a device records it at all, it shows up in
``/DevMgmt/ProductLogsDyn.xml`` as::

    ASSERT FAILED
    Task: POSTSCRIPT
    File: fontcache.c  Line: 2494

The budget covers both the glyphs drawn and the embedded font programs they
come from, and the two trade against each other. Measured on a Color LaserJet
Pro MFP M283fdw: with one fully embedded font, 527 distinct glyphs render and
534 do not; add a second fully embedded font and the page fails at 300. A
font's cost scales with how many glyphs its embedded program declares rather
than being a flat per-font constant, so a heavily subsetted face is far cheaper
than a complete one.

Those figures come from probes embedding complete, unsubsetted fonts. Jobs from
a browser subset aggressively and sit well below them: sampled from real Chrome
output, two subsets declaring 93 and 668 glyphs, with 451 distinct glyphs drawn
between them, printed without trouble. So no fixed threshold is safe to design
against -- whether a document crosses the line depends on its typefaces, how
they were subsetted, and how many distinct characters appear on the page.

The defect has been present across firmware builds years apart, so waiting for
a fix is not a strategy. Client-side changes affect how often it is reached:
Chrome 130 through 144 emitted a separate embedded font program per *strike* of
a typeface, including one per text colour, which multiplied the cost of an
ordinary page considerably. Chrome 145 removed the colour component and
normalises text size out of the key, so current versions embed far fewer font
programs than that era did.

What this does
--------------
Jobs are converted so that no font program reaches the printer at all: text
becomes vector outlines. That removes the dependency rather than trying to stay
under a limit which cannot be measured from outside. Text stays vector, so the
printer's own rasteriser still renders it at full device precision and edge
enhancement still applies -- unlike rasterising the page, which commits its
geometry to the device grid before the printer ever sees it, and which also
inflates a small job into tens of megabytes.

Everything else is a faithful relay. Printer capabilities and status are
mirrored from the real device on every request, so duplex, trays, media sizes,
colour modes and live supply and error state are always exactly right and
there is nothing to maintain by hand as firmware changes.
"""
import argparse
import hashlib
import http.client
import json
import logging
import os
import re
import socket
import socketserver
import ssl
import signal
import struct
import subprocess
import sys
import zlib
import threading
import time
import urllib.parse
import urllib.request
import uuid

import ippcodec as ipp

log = logging.getLogger('ippfix')

OP_PRINT_JOB = 0x0002
OP_SEND_DOCUMENT = 0x0006
OP_NAMES = {0x0002: 'Print-Job', 0x0004: 'Validate-Job', 0x0005: 'Create-Job',
            0x0006: 'Send-Document', 0x0008: 'Cancel-Job',
            0x0009: 'Get-Job-Attributes', 0x000A: 'Get-Jobs',
            0x000B: 'Get-Printer-Attributes', 0x000D: 'Resume-Printer',
            0x003B: 'Close-Job', 0x003C: 'Identify-Printer'}

DEFAULT_QUEUE = 'print'

# Only these are relayed. The printer may sit on a segment that clients cannot
# reach, which makes this proxy the sole path to it; forwarding whatever
# arrives would hand every LAN host administrative operations such as
# Set-Printer-Attributes, Purge-Jobs, or Print-URI with a URL of their
# choosing. A print client needs none of those.
ALLOWED_OPS = frozenset({
    0x0002,   # Print-Job
    0x0004,   # Validate-Job
    0x0005,   # Create-Job
    0x0006,   # Send-Document
    0x0008,   # Cancel-Job
    0x0009,   # Get-Job-Attributes
    0x000A,   # Get-Jobs
    0x000B,   # Get-Printer-Attributes
    0x003B,   # Close-Job
    0x003C,   # Identify-Printer
})

# Attributes naming a resource the printer would fetch itself. Never relayed:
# they turn the printer into a fetcher for whoever sent the job.
FORBIDDEN_ATTRS = ('document-uri', 'job-uri', 'job-authorization-uri',
                   'notify-recipient-uri', 'job-mandatory-attributes')

MAX_BODY = 64 * 1024 * 1024        # a print job larger than this is not real
MAX_HEADERS = 100
MAX_KEEPALIVE = 100


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def slugify(name):
    """Turn a display name into something short and safe to type in a URL.

    Users see the display name in their printer list; they type the slug. So
    "Apartment Color Printer" is shown as written and addressed as
    /ipp/apartment-color-printer.
    """
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug or 'printer'


class Queue:
    """One proxied printer.

    `name` is what people see; `slug` is what they type. Keeping them separate
    means a queue can be called "Multifunction Laserjet" without putting spaces
    or capitals into an address anyone has to read aloud.
    """

    def __init__(self, name, uri):
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme not in ('ipp', 'ipps'):
            raise ValueError(f'{name}: expected an ipp:// or ipps:// URI')
        self.name = name
        self.slug = slugify(name)
        self.tls = parts.scheme == 'ipps'
        self.host = parts.hostname
        self.port = parts.port or 631
        self.path = parts.path or '/ipp/print'
        self.preferred = None        # address that last connected
        # Affected printers report multiple-document-jobs-supported=false, so
        # jobs are serialised -- but per printer, not globally, and only around
        # the upstream exchange. Conversion happens outside the lock so one
        # expensive document cannot stall every other queue.
        self.lock = threading.Lock()
        if not self.host:
            raise ValueError(f'{name}: no host in {uri!r}')

    @property
    def local_path(self):
        return f'/ipp/{self.slug}'

    def upstream_uri(self):
        host = f'[{self.host}]' if ':' in self.host else self.host
        port = '' if self.port == 631 else f':{self.port}'
        return f'ipp://{host}{port}{self.path}'

    def __str__(self):
        return (f'{self.name} -> {"ipps" if self.tls else "ipp"}://'
                f'{self.host}:{self.port}{self.path}')


class Config:
    def __init__(self, args, queues):
        self.port = args.port
        self.queues = {q.local_path: q for q in queues}
        self.advertise = args.advertise or local_ip()
        if args.also_advertise:
            self.extra_addresses = args.also_advertise
        elif args.no_ipv6:
            self.extra_addresses = []
        else:
            # Only this interface's addresses: see interface_of().
            self.extra_addresses = global_ipv6(interface_of(self.advertise))
        self.cert = args.cert
        self.key = args.key
        self.convert = not args.no_convert
        self.converter = args.converter
        # unix:/path means the separately confined conversion service.
        self.converter_socket = (args.converter[len('unix:'):]
                                 if args.converter.startswith('unix:') else None)
        self.timeout = args.timeout
        self.archive = args.archive
        self.archive_max = args.archive_max
        self.max_connections = args.max_connections
        self.idle_timeout = args.idle_timeout
        self.require_tls = args.require_tls
        self.fail_closed = args.fail_closed
        self.archive_max_bytes = args.archive_max_bytes * 1024 * 1024
        self.convert_threshold = args.convert_threshold

    def base_http(self):
        host = (f'[{self.advertise}]' if ':' in self.advertise
                else self.advertise)
        return f'http://{host}:{self.port}'

    def published_addresses(self):
        """Addresses to put in the DNS-SD records.

        The URIs name a single host, but the records can carry several, so
        dual-stack clients are offered IPv6 as well without extra
        configuration.
        """
        addrs = [self.advertise]
        if not self.extra_addresses:
            return addrs
        return addrs + [a for a in self.extra_addresses if a not in addrs]

    def our_uri(self, queue, scheme='ipp'):
        """The address a user types. Kept as short as correctness allows.

        631 is the default for both ipp and ipps, so naming it adds nothing but
        four characters to read out loud.
        """
        host = f'[{self.advertise}]' if ':' in self.advertise else self.advertise
        port = '' if self.port == 631 else f':{self.port}'
        return f'{scheme}://{host}{port}{queue.local_path}'

    def our_uuid(self, queue):
        """Stable, and deliberately different from the printer's own: a client
        that sees one printer-uuid on two queues collapses them into one."""
        seed = f'ippfix:{self.advertise}:{queue.slug}:{queue.host}'
        h = hashlib.sha1(seed.encode()).digest()
        return 'urn:uuid:' + str(uuid.UUID(bytes=h[:16]))


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('192.0.2.1', 1))       # TEST-NET-1; no packet is sent
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


# /proc/net/if_inet6 flag bits we must not publish.
IFA_F_SECONDARY = 0x01      # also IFA_F_TEMPORARY: privacy address, rotates
IFA_F_DEPRECATED = 0x20     # still valid for existing flows, not for new ones
IFA_F_TENTATIVE = 0x40      # duplicate address detection has not finished


def interface_of(address):
    """Name of the interface holding an IPv4 address, or None.

    Used to keep the published address set to a single interface. On a
    multi-homed host, publishing every address the machine happens to have
    invites clients to try one they cannot route to, which shows up as a long
    stall rather than a clear failure.
    """
    import fcntl
    try:
        for _idx, name in socket.if_nameindex():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                packed = fcntl.ioctl(s.fileno(), 0x8915,   # SIOCGIFADDR
                                     struct.pack('256s', name[:15].encode()))
                if socket.inet_ntoa(packed[20:24]) == address:
                    return name
            except OSError:
                continue
            finally:
                s.close()
    except (OSError, ImportError):
        pass
    return None


def global_ipv6(device=None):
    """Stable, globally scoped IPv6 addresses, optionally on one interface.

    Published alongside the IPv4 address so dual-stack clients can reach the
    queues over either family. Deliberately excluded:

      * link-local, which needs a scope id that a DNS-SD record cannot carry;
      * tentative and deprecated addresses, which are not usable for new
        connections;
      * privacy/temporary addresses, which rotate, so a client that caches one
        finds the queue unreachable days later.
    """
    out = []
    try:
        with open('/proc/net/if_inet6', encoding='ascii') as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 6:
                    continue
                raw, _idx, _plen, scope, flags, dev = parts[:6]
                if scope != '00' or dev == 'lo':          # 00 == global
                    continue
                if device is not None and dev != device:
                    continue
                bits = int(flags, 16)
                if bits & (IFA_F_SECONDARY | IFA_F_DEPRECATED | IFA_F_TENTATIVE):
                    continue
                out.append(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(raw)))
    except OSError:
        return []
    return out


# ---------------------------------------------------------------------------
# document conversion
# ---------------------------------------------------------------------------
def estimate_font_cost(data):
    """Estimate what this PDF will cost the printer's font cache.

    The measured budget has two components, both readable without rendering:
    the glyph count each embedded font program declares, and the distinct
    glyphs actually drawn. Calibration against known outcomes on a Color
    LaserJet Pro MFP M283fdw:

        printed  : real browser jobs        1212, 1298, 1827
        failed   : one full font, 528 drawn        4056
        failed   : one full font, 908 drawn        7161
        failed   : three full fonts                19367

    Returns None when the file cannot be read confidently, which the caller
    must treat as "convert", never as "safe".
    """
    try:
        declared = 0
        for m in re.finditer(rb'/FontFile2\s+(\d+)\s+0\s+R', data):
            num = int(m.group(1))
            om = re.search(rb'[^0-9]%d\s+0\s+obj(.{0,600}?)stream[\r\n]+'
                           % num, data, re.S)
            if not om:
                return None
            start = om.end()
            end = data.find(b'endstream', start)
            if end < 0:
                return None
            raw = data[start:end]
            if b'/FlateDecode' in om.group(1):
                raw = zlib.decompress(raw)
            count = struct.unpack_from('>H', raw, 4)[0]
            for i in range(count):
                off = 12 + i * 16
                if raw[off:off + 4] == b'maxp':
                    table = struct.unpack_from('>I', raw, off + 8)[0]
                    declared += struct.unpack_from('>H', raw, table + 4)[0]
                    break

        drawn = set()
        for m in re.finditer(rb'stream[\r\n]+', data):
            start = m.end()
            end = data.find(b'endstream', start)
            if end < 0 or end - start > 8 * 1024 * 1024:
                continue
            blob = data[start:end]
            try:
                blob = zlib.decompress(blob)
            except zlib.error:
                pass
            if b'Tj' not in blob and b'TJ' not in blob:
                continue
            for hm in re.finditer(rb'<([0-9A-Fa-f]{4,})>', blob):
                h = hm.group(1)
                if len(h) % 4 == 0:
                    for i in range(0, len(h), 4):
                        drawn.add(h[i:i + 4])
        return declared + len(drawn)
    except Exception:
        return None


def normalise_pdf(data):
    """Return the document positioned so Ghostscript must read it as PDF.

    Ghostscript decides which language to interpret by looking for %PDF- at the
    start of a *line* near the beginning of the file, and treats anything else
    as PostScript. A file beginning "%!PS" with a %PDF- line later on, or one
    with %PDF- appearing mid-line, therefore looks like a PDF to a naive check
    while making gs run its full PostScript interpreter -- which is where the
    historical -dSAFER escapes live. For real PDF, gs 10.x uses a hardened C
    interpreter instead.

    So the choice of interpreter must not be left to the sender. Returning the
    data trimmed to start exactly at %PDF- forces the PDF path; returning None
    means this is not a PDF we should hand to gs at all, and the caller relays
    it untouched.
    """
    if data[:2] == b'%!':                       # declares itself PostScript
        return None
    window = data[:1024]
    i = window.find(b'%PDF-')
    while i >= 0:
        if i == 0 or window[i - 1] in (0x0a, 0x0d):
            return data[i:] if i else data
        i = window.find(b'%PDF-', i + 1)
    return None


def looks_like_pdf(data):
    return normalise_pdf(data) is not None


def archive_document(cfg, queue, job_name, fmt, data, note):
    """Keep a copy of a job as it arrived, for diagnosing a failure.

    This writes users' documents to disk, so it is off by default and the
    directory is created private to the service account. It exists because
    the failure being worked around is silent and content-dependent: without
    the document that provoked it there is very little to go on.

    Turn it off again once the question is answered.
    """
    if not cfg.archive:
        return
    try:
        os.makedirs(cfg.archive, mode=0o700, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', (job_name or 'job'))[:48]
        ext = 'pdf' if looks_like_pdf(data) else 'bin'
        base = os.path.join(cfg.archive, f'{stamp}-{queue.name}-{safe}')
        path = f'{base}.{ext}'
        n = 1
        while os.path.exists(path):
            path = f'{base}.{n}.{ext}'
            n += 1
        os.chmod(cfg.archive, 0o700)      # exist_ok= does not fix an old mode
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        fd = os.open(f'{path}.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            # job-name is attacker controlled; repr() keeps a newline in it
            # from forging further lines in this file.
            handle.write(f'queue: {queue.name}\nprinter: {queue.host}\n'
                         f'job-name: {job_name!r}\ndocument-format: {fmt!r}\n'
                         f'bytes: {len(data)}\nconversion: {note}\n')
        prune_archive(cfg)
        log.debug('archived %s', path)
    except OSError as exc:
        log.warning('could not archive job: %s', exc)


def prune_archive(cfg):
    """Keep the archive bounded so a forgotten flag cannot fill the disk.

    Bounded by total size as well as by count: fifty files of unbounded size is
    not a bound, and the documents arriving here are chosen by whoever is
    printing.
    """
    def drop(path):
        for victim in (path, path + '.txt'):
            try:
                os.remove(victim)
            except OSError:
                pass

    try:
        entries = [os.path.join(cfg.archive, name)
                   for name in os.listdir(cfg.archive)
                   if not name.endswith('.txt')]
        entries.sort(key=os.path.getmtime)
        for path in entries[:max(0, len(entries) - cfg.archive_max)]:
            drop(path)
            entries = entries[1:]

        total = 0
        for path in reversed(entries):          # newest first
            try:
                total += os.path.getsize(path)
            except OSError:
                continue
            if total > cfg.archive_max_bytes:
                drop(path)
    except OSError:
        pass


MAX_CONVERTED = 256 * 1024 * 1024   # outlining inflates; bound it anyway


def convert_over_socket(path, data, timeout):
    """Hand the document to the conversion service and read the result back.

    The converter runs as a separate, unprivileged service with no network
    access at all, so a flaw in the document parser cannot reach the network,
    the certificates, or the archive. Each connection is its own short-lived
    instance, so one hostile document cannot affect the next job.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        total = 0
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONVERTED:
                raise OSError('converted document too large')
            chunks.append(chunk)
        return b''.join(chunks)
    finally:
        sock.close()


class ConversionFailed(Exception):
    """Raised instead of relaying, when the caller asked to fail closed."""


def _failed(cfg, data, why):
    """Decide what a conversion failure means.

    Relaying the original is the safe choice for printing -- a job that might
    not print beats one that prints something wrong -- but it is the unsafe
    choice for the property this proxy exists to guarantee, since whoever sent
    the document also decides whether conversion fails. --fail-closed inverts
    that trade for sites that would rather lose the job than the guarantee.
    """
    if cfg.fail_closed:
        raise ConversionFailed(why)
    return data, f'relayed ({why})'


def convert(cfg, data, fmt):
    """Outline the text of a PDF. Anything else is relayed untouched.

    Fails safe: on any doubt the original is forwarded, because a job that
    might not print beats one that prints something wrong.
    """
    if not cfg.convert or not data:
        return data, 'relayed'
    payload = normalise_pdf(data)
    if payload is None:
        return data, f'relayed ({fmt or "not PDF"})'

    # Outlining is expensive: it replaces every drawn glyph with an inline
    # path, and Ghostscript emits no reusable form for them, so a fifty-page
    # document grows from half a megabyte to thirty-three and takes half a
    # second per page. Most jobs are nowhere near the printer's limit, so
    # estimate first and leave those alone entirely -- that is both free and
    # perfectly faithful. An unreadable estimate means convert, never skip.
    if cfg.convert_threshold:
        cost = estimate_font_cost(payload)
        if cost is not None and cost <= cfg.convert_threshold:
            return data, f'relayed (font cost {cost}, under threshold)'

    started = time.time()
    try:
        if cfg.converter_socket:
            out = convert_over_socket(cfg.converter_socket, payload,
                                      cfg.timeout)
        else:
            # start_new_session so a timeout can kill the whole group:
            # terminating the helper leaves Ghostscript itself running.
            proc = subprocess.Popen(
                [cfg.converter], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
            try:
                out, err = proc.communicate(payload, timeout=cfg.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.communicate()
                raise
            if len(out) > MAX_CONVERTED:
                log.error('converted document too large; relaying original')
                return data, 'relayed (too large)'
            if proc.returncode != 0:
                log.error('converter exited %s: %s', proc.returncode,
                          err[:300].decode('utf-8', 'replace').strip())
                return data, 'relayed (converter error)'
    except (OSError, socket.timeout, subprocess.TimeoutExpired) as exc:
        log.error('converter failed: %s', exc)
        return _failed(cfg, data, 'converter failed')

    if not out:
        log.error('converter produced nothing')
        return _failed(cfg, data, 'converter error')
    if b'/FontFile' in out:
        log.warning('font programs survived conversion')
        return _failed(cfg, data, 'fonts survived')
    return out, (f'outlined {len(data)} -> {len(out)} bytes in '
                 f'{time.time() - started:.1f}s')


# ---------------------------------------------------------------------------
# upstream
# ---------------------------------------------------------------------------
def connect_upstream(queue, port, tls, timeout):
    """Open a connection to the printer, trying every address it resolves to.

    Python's HTTP client uses only the first address a name resolves to. If a
    printer's name has both A and AAAA records but its network carries only one
    family -- an IPv4-only isolated segment behind a dual-stack LAN being the
    common case -- that first address may be the unusable one, and every job
    stalls until it times out. Trying each in turn removes the failure, and the
    address that worked is remembered so the cost is paid at most once.
    """
    try:
        targets = socket.getaddrinfo(queue.host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError(f'cannot resolve {queue.host}: {exc}') from exc

    if queue.preferred:                      # retry what worked last time first
        targets.sort(key=lambda t: t[4][0] != queue.preferred)

    last = None
    for family, _stype, _proto, _canon, sockaddr in targets:
        literal = sockaddr[0]
        header = f'[{literal}]:{port}' if family == socket.AF_INET6 \
            else f'{literal}:{port}'
        try:
            if tls:
                ctx = ssl._create_unverified_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                conn = http.client.HTTPSConnection(literal, port,
                                                   timeout=timeout, context=ctx)
            else:
                conn = http.client.HTTPConnection(literal, port, timeout=timeout)
            conn.connect()
        except OSError as exc:
            last = exc
            log.debug('%s: %s unreachable (%s)', queue.name, literal, exc)
            continue
        if queue.preferred != literal:
            queue.preferred = literal
            log.debug('%s: using %s', queue.name, literal)
        return conn, header
    raise OSError(f'no reachable address for {queue.host}: {last}')


def upstream_ipp(queue, payload, timeout):
    conn, header = connect_upstream(queue, queue.port, queue.tls, timeout)
    try:
        conn.request('POST', queue.path, body=payload,
                     headers={'Content-Type': 'application/ipp',
                              'Content-Length': str(len(payload)),
                              'Host': header})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def upstream_http(queue, path, timeout=30):
    conn, header = connect_upstream(queue, 80, False, timeout)
    try:
        conn.request('GET', path, headers={'Host': header})
        resp = conn.getresponse()
        ctype = resp.getheader('Content-Type', 'application/octet-stream')
        return resp.status, ctype, resp.read()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# attribute rewriting
# ---------------------------------------------------------------------------
def rewrite_request(queue, msg):
    """Address the request to the real printer, and strip what must not pass.

    Only the operation is validated elsewhere; here we make sure no attribute
    can point the printer at a resource of the sender's choosing.
    """
    group = msg.operation()
    if group is None:
        return
    for attr in ('printer-uri', 'job-printer-uri'):
        if group.index_of(attr) >= 0:
            group.replace(attr, ipp.TAG_URI, [queue.upstream_uri()])
    for attr in FORBIDDEN_ATTRS:
        group.remove(attr)


def rewrite_response(cfg, queue, msg):
    """Make the reply describe this server rather than the printer.

    Only identity and URIs are touched. Capabilities and status are left
    exactly as the printer reported them, which is what gives feature parity
    and live status for free.
    """
    base = cfg.base_http()
    for group in msg.groups:
        if group.tag == ipp.PRINTER_ATTRS:
            group.replace('printer-uri-supported', ipp.TAG_URI,
                          [cfg.our_uri(queue, 'ipp'),
                           cfg.our_uri(queue, 'ipps')])
            group.replace('uri-security-supported', ipp.TAG_KEYWORD,
                          ['none', 'tls'])
            group.replace('uri-authentication-supported', ipp.TAG_KEYWORD,
                          ['requesting-user-name', 'requesting-user-name'])
            group.replace('printer-uuid', ipp.TAG_URI, [cfg.our_uuid(queue)])
            group.replace('printer-name', ipp.TAG_NAME, [queue.name])
            group.replace('printer-dns-sd-name', ipp.TAG_NAME, [queue.name])
            group.replace('printer-more-info', ipp.TAG_URI, [base + '/'])
            # Icons and localised strings live on the printer, which clients
            # may have no route to; re-serve them from this server instead.
            if group.index_of('printer-icons') >= 0:
                group.replace('printer-icons', ipp.TAG_URI,
                              [f'{base}{queue.local_path}/icon-small.png',
                               f'{base}{queue.local_path}/icon-large.png'])
            if group.index_of('printer-strings-uri') >= 0:
                group.replace('printer-strings-uri', ipp.TAG_URI,
                              [f'{base}{queue.local_path}/strings'])
            group.remove('printer-supply-info-uri')
        elif group.tag in (ipp.JOB_ATTRS, ipp.UNSUPPORTED_ATTRS):
            if group.index_of('job-printer-uri') >= 0:
                group.replace('job-printer-uri', ipp.TAG_URI,
                              [cfg.our_uri(queue)])
            if group.index_of('job-uri') >= 0:
                old = group.get('job-uri')[0].decode('utf-8', 'replace')
                job_id = old.rstrip('/').rsplit('/', 1)[-1]
                group.replace('job-uri', ipp.TAG_URI,
                              [f'{cfg.our_uri(queue)}/{job_id}'])


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class BadRequest(Exception):
    pass


def read_headers(rfile):
    """Parse request headers strictly.

    Leniency here is not kindness: a header this parser reads differently from
    an intermediary in front of it is a request-smuggling primitive. Folded
    continuation lines and repeated Content-Length are therefore refused rather
    than guessed at.
    """
    headers = {}
    count = 0
    while True:
        line = rfile.readline(8192)
        if not line:
            raise BadRequest('truncated headers')
        if line in (b'\r\n', b'\n'):
            break
        if len(line) >= 8192:
            raise BadRequest('header too long')
        count += 1
        if count > MAX_HEADERS:
            raise BadRequest('too many headers')
        if line[:1] in (b' ', b'\t'):
            raise BadRequest('obsolete line folding')
        if b':' not in line:
            raise BadRequest('malformed header')
        key, value = line.split(b':', 1)
        name = key.strip().lower().decode('latin-1')
        if name in headers and name in ('content-length', 'transfer-encoding'):
            raise BadRequest(f'duplicate {name}')
        headers[name] = value.strip().decode('latin-1')
    if 'content-length' in headers and 'transfer-encoding' in headers:
        raise BadRequest('both content-length and transfer-encoding')
    return headers


def read_body(rfile, headers):
    if 'chunked' in headers.get('transfer-encoding', '').lower():
        body = bytearray()
        while True:
            line = rfile.readline(8192).strip()
            if not line:
                break
            try:
                size = int(line.split(b';')[0], 16)
            except ValueError:
                raise BadRequest('bad chunk size')
            if size < 0:
                raise BadRequest('negative chunk size')
            if size == 0:
                rfile.readline(8192)
                break
            if len(body) + size > MAX_BODY:
                raise BadRequest('body too large')
            while size > 0:
                chunk = rfile.read(min(size, 65536))
                if not chunk:
                    raise BadRequest('truncated chunk')
                body += chunk
                size -= len(chunk)
            rfile.readline(8192)
        return bytes(body)

    try:
        remaining = int(headers.get('content-length', 0) or 0)
    except ValueError:
        raise BadRequest('bad content-length')
    if remaining < 0:
        raise BadRequest('negative content-length')
    if remaining > MAX_BODY:
        raise BadRequest('body too large')
    body = bytearray()
    while len(body) < remaining:
        chunk = rfile.read(min(remaining - len(body), 65536))
        if not chunk:
            raise BadRequest('truncated body')
        body += chunk
    return bytes(body)


def respond(wfile, status, ctype, body):
    wfile.write(f'HTTP/1.1 {status}\r\n'
                f'Content-Type: {ctype}\r\n'
                f'Content-Length: {len(body)}\r\n'
                f'Connection: keep-alive\r\n\r\n'.encode('latin-1'))
    wfile.write(body)
    wfile.flush()


STATUS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ippfix</title></head>
<body>
<h1>ippfix</h1>
<p>IPP proxy. Print jobs have their text converted to vector outlines before
being forwarded, so that no embedded font program reaches the printer.</p>
<h2>Queues</h2>
<p>Add any of these by address if you would rather not rely on discovery.
The same list is available as <a href="/queues.json">/queues.json</a>.</p>
<table border="1" cellpadding="6" cellspacing="0">
<tr><th>Name</th><th>IPP</th><th>IPPS</th><th>Printer</th></tr>
{queues}
</table>
</body></html>
"""


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        cfg = self.server.cfg
        # A connection that never speaks must not hold a thread. Without this
        # a handful of silent connections exhaust the task limit and printing
        # stops until the service is restarted by hand.
        if not self.server.slots.acquire(blocking=False):
            log.warning('connection limit reached; dropping %s',
                        self.client_address[0])
            return
        try:
            self.serve(cfg)
        finally:
            self.server.slots.release()

    def serve(self, cfg):
        sock = self.request
        sock.settimeout(cfg.idle_timeout)
        try:
            first = sock.recv(1, socket.MSG_PEEK)
        except (OSError, socket.timeout):
            return
        if not first:
            return
        if first[0] == 0x16:                       # TLS ClientHello
            try:
                sock = self.server.tls_context.wrap_socket(sock,
                                                           server_side=True)
            except (ssl.SSLError, OSError) as exc:
                log.debug('TLS handshake failed: %s', exc)
                return
        elif cfg.require_tls:
            log.debug('refused plaintext from %s', self.client_address[0])
            return

        rfile = sock.makefile('rb')
        wfile = sock.makefile('wb')
        try:
            served = 0
            while self.one_request(cfg, rfile, wfile):
                served += 1
                if served >= MAX_KEEPALIVE:
                    break
        except (BadRequest, OSError, ssl.SSLError, socket.timeout):
            pass
        finally:
            for handle in (rfile, wfile, sock):
                try:
                    handle.close()
                except OSError:
                    pass

    def one_request(self, cfg, rfile, wfile):
        line = rfile.readline(8192)
        if not line:
            return False
        parts = line.split()
        if len(parts) < 2:
            return False
        method = parts[0].decode('latin-1')
        path = parts[1].decode('latin-1')
        version = parts[2].decode('latin-1') if len(parts) > 2 else 'HTTP/1.0'
        headers = read_headers(rfile)
        keep = (version.endswith('1.1') and
                'close' not in headers.get('connection', '').lower())

        # CUPS-derived clients send this routinely and stall without it.
        if headers.get('expect', '').lower() == '100-continue':
            wfile.write(b'HTTP/1.1 100 Continue\r\n\r\n')
            wfile.flush()

        body = read_body(rfile, headers)

        if method == 'POST' and 'ipp' in headers.get('content-type', ''):
            self.handle_ipp(cfg, wfile, path, body)
        elif method in ('GET', 'HEAD'):
            self.handle_get(cfg, wfile, path)
        else:
            respond(wfile, '405 Method Not Allowed', 'text/plain',
                    b'method not allowed\n')
            return False
        return keep

    def resolve(self, cfg, path):
        """Map a request path to a queue.

        Deliberately forgiving: /ipp/name and /name both work, case is ignored,
        and a trailing job id is tolerated. These are addresses people type
        from memory or read off a sticker, so the cost of being strict is a
        support call and the cost of being lax is nothing.
        """
        base = '/' + path.lstrip('/').split('?')[0].rstrip('/')
        folded = base.lower()
        for local_path, queue in cfg.queues.items():
            lowered = local_path.lower()
            if folded in (lowered, lowered.replace('/ipp/', '/', 1)):
                return queue
        for local_path, queue in cfg.queues.items():
            lowered = local_path.lower()
            if folded.startswith(lowered + '/') or \
               folded.startswith(lowered.replace('/ipp/', '/', 1) + '/'):
                return queue
        if len(cfg.queues) == 1:
            return next(iter(cfg.queues.values()))
        return None

    def handle_get(self, cfg, wfile, path):
        # Machine-readable queue listing, so a site that prefers to hard-code
        # printers rather than rely on mDNS has somewhere authoritative to look.
        if path.split('?')[0] in ('/queues.json', '/queues'):
            body = json.dumps(
                {'queues': [
                    {'name': q.name,
                     'slug': q.slug,
                     'ipp': cfg.our_uri(q, 'ipp'),
                     'ipps': cfg.our_uri(q, 'ipps'),
                     'resource': q.local_path,
                     'printer': f'{q.host}:{q.port}{q.path}',
                     'uuid': cfg.our_uuid(q)}
                    for q in cfg.queues.values()],
                 'port': cfg.port,
                 'addresses': cfg.published_addresses()},
                indent=2).encode()
            respond(wfile, '200 OK', 'application/json; charset=utf-8', body)
            return

        match = re.match(r'^(/ipp/[^/]+)/(icon-small\.png|icon-large\.png|strings)$',
                         path)
        if match:
            queue = cfg.queues.get(match.group(1))
            if queue is None:
                respond(wfile, '404 Not Found', 'text/plain', b'no such queue\n')
                return
            upstream = {'icon-small.png': '/ipp/images/printer.png',
                        'icon-large.png': '/ipp/images/printer-large.png',
                        'strings': '/ipp/strings/en-us'}[match.group(2)]
            try:
                status, ctype, data = upstream_http(queue, upstream)
                # getheader() preserves folded continuations verbatim, so a
                # hostile printer could otherwise inject header lines here.
                ctype = re.sub(r'[^\x20-\x7e]', ' ', ctype)[:128]
                respond(wfile, '200 OK' if status == 200 else f'{status} Error',
                        ctype, data)
            except OSError:
                respond(wfile, '502 Bad Gateway', 'text/plain',
                        b'printer unreachable\n')
            return

        items = ''.join(
            f'<tr><td>{q.name}</td>'
            f'<td><code>{cfg.our_uri(q, "ipp")}</code></td>'
            f'<td><code>{cfg.our_uri(q, "ipps")}</code></td>'
            f'<td>{q.host}:{q.port}{q.path}</td></tr>'
            for q in cfg.queues.values())
        respond(wfile, '200 OK', 'text/html; charset=utf-8',
                STATUS_PAGE.format(queues=items).encode('utf-8'))

    def handle_ipp(self, cfg, wfile, path, body):
        queue = self.resolve(cfg, path)
        if queue is None:
            respond(wfile, '404 Not Found', 'text/plain', b'no such queue\n')
            return
        try:
            msg = ipp.parse(body)
        except Exception as exc:
            log.warning('unparseable request: %s', exc)
            respond(wfile, '400 Bad Request', 'text/plain', b'bad request\n')
            return

        name = OP_NAMES.get(msg.code, f'0x{msg.code:04x}')
        if msg.code not in ALLOWED_OPS:
            log.warning('refused operation %s from %s', name,
                        self.client_address[0])
            respond(wfile, '400 Bad Request', 'text/plain',
                    b'operation not permitted\n')
            return
        note = ''

        if msg.code in (OP_PRINT_JOB, OP_SEND_DOCUMENT) and msg.data:
            group = msg.operation()
            fmt = group.get_str('document-format') if group else None
            # One job at a time: these printers report
            # multiple-document-jobs-supported = false, and a second job
            # arriving mid-transfer confuses them.
            original = msg.data
            try:
                msg.data, note = convert(cfg, msg.data, fmt)
            except ConversionFailed as exc:
                log.warning('%s: refusing job (%s)', queue.name, exc)
                respond(wfile, '400 Bad Request', 'text/plain',
                        b'document could not be converted\n')
                return
            archive_document(cfg, queue,
                             group.get_str('job-name') if group else None,
                             fmt, original, note)
            rewrite_request(queue, msg)
            payload = ipp.serialize(msg)
            if not queue.lock.acquire(timeout=cfg.timeout):
                log.warning('%s: busy, refusing job', queue.name)
                respond(wfile, '503 Service Unavailable', 'text/plain',
                        b'printer busy\n')
                return
            try:
                status, raw = upstream_ipp(queue, payload, cfg.timeout)
            finally:
                queue.lock.release()
        else:
            rewrite_request(queue, msg)
            status, raw = upstream_ipp(queue, ipp.serialize(msg), cfg.timeout)

        try:
            reply = ipp.parse(raw)
            rewrite_response(cfg, queue, reply)
            out = ipp.serialize(reply)
        except Exception as exc:
            log.warning('unparseable reply from %s (%s); relaying verbatim',
                        queue.host, exc)
            out = raw

        log.info('%-14s %-22s HTTP %s%s', queue.name, name, status,
                 f'  [{note}]' if note else '')
        respond(wfile, '200 OK' if status == 200 else f'{status} Error',
                'application/ipp', out)


SD_LISTEN_FDS_START = 3


def inherited_socket():
    """The listening socket systemd passed us, if it did.

    With socket activation systemd opens the port itself and hands over the
    descriptor, so the service never needs the privilege to bind a port below
    1024 -- it can run with no capabilities at all. Falls back to binding
    directly when started by hand.
    """
    if os.environ.get('LISTEN_PID') != str(os.getpid()):
        return None
    try:
        count = int(os.environ.get('LISTEN_FDS', '0'))
    except ValueError:
        return None
    if count < 1:
        return None
    if count > 1:
        log.warning('systemd passed %d sockets; using the first', count)
    sock = socket.socket(fileno=SD_LISTEN_FDS_START)
    sock.setblocking(True)
    os.set_inheritable(sock.fileno(), False)
    for name in ('LISTEN_PID', 'LISTEN_FDS', 'LISTEN_FDNAMES'):
        os.environ.pop(name, None)
    return sock


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32
    address_family = socket.AF_INET6

    def __init__(self, addr, handler, cfg, listen_fd=None):
        self.cfg = cfg
        self.slots = threading.Semaphore(cfg.max_connections)
        # Built once: a fresh context per connection means a disk read and a
        # key parse for every TCP connect, which is its own amplifier.
        self.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.tls_context.load_cert_chain(cfg.cert, cfg.key)
        self._inherited = listen_fd
        if listen_fd is not None:
            self.address_family = listen_fd.family
        super().__init__(addr, handler, bind_and_activate=listen_fd is None)
        if listen_fd is not None:
            self.socket.close()
            self.socket = listen_fd
            self.server_address = listen_fd.getsockname()

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def advertise(cfg):
    """Publish each queue over DNS-SD, on IPv4 and IPv6.

    Returns a callable that withdraws the registrations, or None if the
    zeroconf module is unavailable.
    """
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError:
        log.warning('zeroconf not installed; queues will not be discoverable')
        return None

    zc = Zeroconf(ip_version=IPVersion.All)
    registered = []
    for queue in cfg.queues.values():
        for service, scheme in (('_ipp._tcp.local.', 'ipp'),
                                ('_ipps._tcp.local.', 'ipps')):
            props = {
                'txtvers': '1', 'qtotal': '1',
                'rp': queue.local_path.lstrip('/'),
                'ty': queue.name,
                'note': 'ippfix',
                'pdl': 'application/pdf,image/urf,image/jpeg',
                'adminurl': cfg.base_http() + '/',
                'priority': '10',
                'UUID': cfg.our_uuid(queue).replace('urn:uuid:', ''),
                'TLS': '1.2' if scheme == 'ipps' else '',
                'Color': 'T', 'Duplex': 'T',
                'printer-type': '0x809056',
            }
            props = {k: v for k, v in props.items() if v != ''}
            info = ServiceInfo(
                service,
                f'{queue.name}.{service}',
                addresses=None,
                port=cfg.port,
                properties=props,
                server=f'{socket.gethostname()}.local.',
                parsed_addresses=cfg.published_addresses(),
            )
            zc.register_service(info)
            registered.append(info)
            log.info('advertising %s as %s', queue.name, service)

    def withdraw():
        for info in registered:
            try:
                zc.unregister_service(info)
            except Exception:
                pass
        zc.close()

    return withdraw


# ---------------------------------------------------------------------------
def parse_queue(spec):
    """NAME=ipp://HOST[:PORT][/PATH], or a bare URI for the default queue."""
    if '=' in spec and '://' in spec and spec.index('=') < spec.index('://'):
        name, uri = spec.split('=', 1)
    else:
        name, uri = DEFAULT_QUEUE, spec
    name = name.strip()
    if not name or not slugify(name):
        raise ValueError(f'invalid queue name {name!r}')
    return Queue(name, uri.strip())


def list_queues(url):
    """Print the queues a running instance serves.

    Discovery over mDNS is not always available or trusted, so the daemon also
    answers a plain HTTP request that says exactly what to configure by hand.
    """
    if '://' not in url:
        url = 'http://' + url
    if urllib.parse.urlsplit(url).path in ('', '/'):
        url = url.rstrip('/') + '/queues.json'
    try:
        with urllib.request.urlopen(url, timeout=15) as handle:
            data = json.load(handle)
    except Exception as exc:
        print(f'could not read {url}: {exc}', file=sys.stderr)
        return 1

    queues = data.get('queues', [])
    if not queues:
        print('no queues configured')
        return 0
    print(f"listening on port {data.get('port')}, "
          f"published addresses: {', '.join(data.get('addresses', []))}\n")
    width = max(len(q['name']) for q in queues)
    for q in queues:
        print(f"{q['name']:<{width}}  {q['ipp']}")
        print(f"{'':<{width}}  {q['ipps']}")
        print(f"{'':<{width}}  -> {q['printer']}")
        print(f"{'':<{width}}  uuid {q['uuid']}")
        print()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog='ippfix',
        description='IPP proxy that outlines text so no font program reaches '
                    'the printer')
    parser.add_argument('printers', nargs='*', metavar='[NAME=]URI',
                        help='printer to proxy, e.g. '
                             'upstairs=ipp://printer.example/ipp/print')
    parser.add_argument('-p', '--port', type=int, default=631,
                        help='port to listen on (default: 631)')
    parser.add_argument('-a', '--advertise', default=None,
                        help='address clients should use in URIs '
                             '(default: autodetect)')
    parser.add_argument('--also-advertise', action='append', metavar='ADDRESS',
                        help='additional address to publish in the DNS-SD '
                             'records, repeatable. Defaults to the stable '
                             'global IPv6 addresses of the same interface, so '
                             'dual-stack clients are offered IPv6 without '
                             'extra configuration')
    parser.add_argument('--no-ipv6', action='store_true',
                        help='publish only the IPv4 address, for networks '
                             'where IPv6 is present but not routable')
    parser.add_argument('--cert', default='/etc/ippfix/ippfix.crt',
                        help='TLS certificate')
    parser.add_argument('--key', default='/etc/ippfix/ippfix.key',
                        help='TLS private key')
    parser.add_argument('--converter', default='/usr/local/lib/ippfix/defont',
                        help='PDF conversion helper: either an executable that '
                             'filters stdin to stdout, or unix:PATH to reach '
                             'the separately sandboxed conversion service')
    parser.add_argument('--timeout', type=int, default=300,
                        help='seconds allowed per conversion and per upstream '
                             'request (default: 300)')
    parser.add_argument('--no-convert', action='store_true',
                        help='relay jobs untouched, for comparison')
    parser.add_argument('--archive', metavar='DIR', default=None,
                        help='DIAGNOSTIC ONLY: keep a copy of every job as it '
                             'arrived, before conversion. This stores users\' '
                             'documents on disk; see the manual page before '
                             'enabling it, and turn it off afterwards')
    parser.add_argument('--archive-max', type=int, default=50, metavar='N',
                        help='keep at most N archived jobs (default: 50)')
    parser.add_argument('--max-connections', type=int, default=64, metavar='N',
                        help='refuse connections beyond N, so that idle ones '
                             'cannot exhaust the task limit (default: 64)')
    parser.add_argument('--idle-timeout', type=int, default=30, metavar='SEC',
                        help='drop a connection that stops speaking for this '
                             'long (default: 30)')
    parser.add_argument('--require-tls', action='store_true',
                        help='refuse plaintext IPP and accept only ipps')
    parser.add_argument('--convert-threshold', type=int, default=2500,
                        metavar='N',
                        help='leave a job untouched when its estimated font '
                             'cost is at or below N, since outlining is '
                             'expensive and most jobs are nowhere near the '
                             'printer limit. 0 converts everything (default: '
                             '2500)')
    parser.add_argument('--fail-closed', action='store_true',
                        help='reject a PDF that cannot be converted instead of '
                             'forwarding it unchanged. Safer, because the '
                             'sender otherwise chooses whether conversion '
                             'happens, but it loses jobs the printer might '
                             'have managed')
    parser.add_argument('--archive-max-bytes', type=int, default=512,
                        metavar='MB',
                        help='total size cap for the archive (default: 512)')
    parser.add_argument('--no-advertise', action='store_true',
                        help='do not publish over DNS-SD')
    parser.add_argument('--list', nargs='?', const='http://localhost:631/',
                        metavar='URL',
                        help='print the queues a running instance serves, for '
                             'configuring clients by address instead of by '
                             'discovery. Defaults to the local instance')
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        return list_queues(args.list)
    if not args.printers:
        parser.error('at least one printer is required')

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S')

    try:
        queues = [parse_queue(spec) for spec in args.printers]
    except ValueError as exc:
        parser.error(str(exc))

    names = [q.name for q in queues]
    if len(set(names)) != len(names):
        parser.error('duplicate queue names')

    cfg = Config(args, queues)
    if cfg.convert and not cfg.converter_socket \
            and not os.access(cfg.converter, os.X_OK):
        parser.error(f'converter not executable: {cfg.converter}')

    log.info('listening on [::]:%d (IPv4 and IPv6)', cfg.port)
    log.info('  published addresses: %s', ', '.join(cfg.published_addresses()))
    for queue in queues:
        log.info('  %s', queue)
        log.info('    published as %s', cfg.our_uri(queue))
    log.info('  conversion: %s',
             ('outline text via sandboxed service at '
              f'{cfg.converter_socket}' if cfg.converter_socket
              else f'outline text via {cfg.converter}') if cfg.convert
             else 'DISABLED')
    if cfg.archive:
        log.warning('  ARCHIVING every job to %s (keeping %d) -- this stores '
                    'users\' documents; disable when done diagnosing',
                    cfg.archive, cfg.archive_max)

    withdraw = None if args.no_advertise else advertise(cfg)
    listen_fd = inherited_socket()
    if listen_fd is not None:
        log.info('  socket activated: using the descriptor systemd passed, '
                 'so no capabilities are required')
    server = Server(('::', cfg.port), Handler, cfg, listen_fd)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if withdraw:
            withdraw()
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
