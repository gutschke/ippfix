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

What makes a page too expensive is not reliably predictable from outside.
Four models were fitted and each falsified by measurement: the glyph count a
font declares (a font declaring 65535 while drawing 27 printed), the glyphs a
page draws (1264 printed where 700 failed), the size of the embedded font
program, and the outline complexity of the glyphs used (519 glyphs in 47 kB
failed where 519 in 50 kB printed). Whatever the firmware counts is not
apparent in the file.

So this proxy does not try to predict. It converts every PDF, which removes
every font program and therefore the whole failure mode, at a cost of about a
third of a second and roughly double the file size on a real job. A cost
estimate is still computed and logged, because it is useful when investigating,
and --convert-threshold can act on it for a site that has measured its own
workload -- but nothing depends on it being right.

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
import email.utils
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

# Formats a client may be offered. PDF is converted; raster and JPEG carry no
# font programs at all; octet-stream is sniffed and handled as whatever it turns
# out to be. PCL and PCL-XL are relayed untouched and are listed deliberately:
# the fault is in the device's PostScript task, which also interprets PDF, while
# PCL is a separate interpreter -- so PCL is if anything a safer choice here,
# and withholding it would remove a working path for no reason.
#
# application/postscript is the one omission. It is handled by exactly the
# interpreter that crashes, and it cannot be converted the way PDF is: feeding
# PostScript to Ghostscript means running its PostScript interpreter, which is
# where the sandbox-escape history lives and which normalise_pdf() exists to
# avoid. Offering it would advertise a fixed queue that silently is not one.
SAFE_FORMATS = ('application/pdf', 'image/urf', 'application/PCLm',
                'image/pwg-raster', 'image/jpeg', 'application/octet-stream',
                'application/vnd.hp-PCL', 'application/vnd.hp-PCLXL',
                'application/vnd.hp-PCLXL'.lower())

# Raster formats worth falling back to, best first. Chosen from what the
# printer says it accepts, never assumed.
RASTER_PREFERENCE = ('image/urf', 'application/PCLm', 'image/pwg-raster')

# Ghostscript device per raster format, and the cupsColorSpace numbers behind
# the names printers use in urf-supported. Nothing here is assumed of a
# printer: the choice is made from what it actually advertises.
RASTER_DEVICE = {'image/urf': 'appleraster',
                 'application/PCLm': 'pclm',
                 'image/pwg-raster': 'pwgraster'}
URF_COLORSPACE = {'SRGB24': 19, 'ADOBERGB24': 20, 'DEVRGB24': 1,
                  'W8': 18, 'DEVW8': 0}
# Colour first where the printer has colour, grey otherwise. Grey also halves
# the size and avoids composite-black fringing on text.
COLOUR_ORDER = ('SRGB24', 'ADOBERGB24', 'DEVRGB24')
GREY_ORDER = ('W8', 'DEVW8')

MAX_BODY = 64 * 1024 * 1024        # a print job larger than this is not real
MAX_PAGES_INSPECTED = 2000         # beyond this, decline to estimate

# Cost of a page = glyphs drawn + (embedded font bytes / FONT_BYTE_UNIT).
#
# Derived by fitting every measured outcome, not assumed. What a font DECLARES
# turns out to be irrelevant: a font declaring 65535 glyphs while drawing 27
# printed without trouble, which falsified an earlier model built on that
# number. Drawn glyphs dominate, and the font program itself carries a cost
# too -- two large fonts fail at 300 drawn glyphs where one small font survives
# 523.
#
# Against thirteen observed jobs this separates cleanly: everything scoring 562
# or less printed, everything scoring 586 or more did not.
FONT_BYTE_UNIT = 4096
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
        self.raster_format = None      # learned from the printer, see below
        self.raster_device = None
        self.raster_colorspace = None
        self.raster_dpi = None
        self.max_pdf_bytes = None
        self.learned = False
        self.learn_failed_at = None    # monotonic time of the last failure
        self._warned_formats = False
        if not self.host:
            raise ValueError(f'{name}: no host in {uri!r}')

    def learn(self, timeout=20, retry_after=60):
        """Ask the printer what it can actually take, once.

        Everything the raster fallback needs varies by model: which raster
        format, which colour space (a monochrome device offers no colour one),
        which resolution, and how large a PDF it will accept. Guessing any of
        them produces a job the printer rejects, which is the failure this
        proxy exists to remove.
        """
        if self.learned:
            return
        # A printer that was down when this daemon started must not stay
        # degraded for the daemon's lifetime: marking the attempt done before
        # making it meant one unlucky boot disabled the raster fallback until
        # the next restart. Only success is final. Failure is retried, but not
        # before retry_after has passed, so an unreachable printer costs one
        # stalled request per minute rather than one per job.
        now = time.monotonic()
        if self.learn_failed_at is not None \
                and now - self.learn_failed_at < retry_after:
            return
        try:
            request = ipp.new_request(0x000B, 1, self.upstream_uri())
            request.operation().replace(
                'requested-attributes', ipp.TAG_KEYWORD,
                ['document-format-supported', 'urf-supported',
                 'printer-resolution-supported', 'pdf-k-octets-supported',
                 'color-supported', 'pwg-raster-document-resolution-supported'])
            status, raw = upstream_ipp(self, ipp.serialize(request), timeout)
            if status != 200:
                raise OSError(f'HTTP {status}')
            group = ipp.parse(raw).group(ipp.PRINTER_ATTRS)
            if group is None:
                raise ValueError('no printer attributes')
        except Exception as exc:
            self.learn_failed_at = now
            log.warning('%s: could not read capabilities (%s); the raster '
                        'fallback is unavailable until the printer answers',
                        self.name, exc)
            return
        self.learned = True
        self.learn_failed_at = None

        formats = [f.decode('utf-8', 'replace')
                   for f in (group.get('document-format-supported') or [])]
        for candidate in RASTER_PREFERENCE:
            if candidate in formats:
                self.raster_format = candidate
                self.raster_device = RASTER_DEVICE[candidate]
                break

        urf = [f.decode('utf-8', 'replace')
               for f in (group.get('urf-supported') or [])]
        colour = group.get('color-supported')
        has_colour = bool(colour and colour[0] not in (b'\x00', b''))
        order = (COLOUR_ORDER + GREY_ORDER) if has_colour else GREY_ORDER
        for name in order:
            if name in urf:
                self.raster_colorspace = URF_COLORSPACE[name]
                self.raster_colorname = name
                break
        if self.raster_colorspace is None and self.raster_format:
            # No usable token; grey is the safest thing any printer renders.
            self.raster_colorspace = URF_COLORSPACE['W8']
            self.raster_colorname = 'W8 (assumed)'

        for token in urf:                       # e.g. RS600 or RS300-600
            if token.startswith('RS'):
                try:
                    self.raster_dpi = max(int(x) for x in
                                          token[2:].split('-') if x.isdigit())
                except ValueError:
                    pass
                break
        if not self.raster_dpi:
            res = group.get('printer-resolution-supported')
            if res and len(res[0]) >= 9:
                self.raster_dpi = struct.unpack_from('>i', res[0], 0)[0]
        self.raster_dpi = self.raster_dpi or 600

        koctets = group.get('pdf-k-octets-supported')
        if koctets and len(koctets[0]) == 8:
            upper = struct.unpack_from('>ii', koctets[0], 0)[1]
            if upper > 0:
                # Stay clear of the limit rather than sitting on it.
                self.max_pdf_bytes = int(upper * 1024 * 0.8)

        if self.raster_format:
            log.info('%s: raster fallback %s, %s, %d dpi', self.name,
                     self.raster_format, getattr(self, 'raster_colorname', '?'),
                     self.raster_dpi)
        else:
            log.warning('%s: printer accepts no raster format we can produce; '
                        'oversized jobs will be sent as PDF and may be '
                        'rejected', self.name)
        if self.max_pdf_bytes:
            log.info('%s: printer accepts PDF up to %.0f MB', self.name,
                     self.max_pdf_bytes / 1e6)

    def note_formats(self, offered, kept):
        """Remember which raster format this printer will actually take.

        Never assumed: a monochrome or older device may support none of them,
        in which case there is no raster tier for it and that has to be known
        rather than discovered when a job fails.
        """
        if not self._warned_formats:
            self._warned_formats = True
            dropped = [f for f in offered if f not in kept]
            if dropped:
                log.info('%s: not offering %s to clients (handled by the same '
                         'interpreter that fails, and cannot be converted '
                         'safely)', self.name, ', '.join(dropped))

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
        # Following jobs to see whether they printed. Off unless an address is
        # given: on a printer that does not report impressions honestly this
        # would report every job as lost.
        self.alert_timeout = args.alert_timeout
        self.alert_max_watchers = 32
        self.watching = 0
        self.watch_lock = threading.Lock()
        self.alerter = (Alerter(args.alert_mail, args.alert_max_per_hour)
                        if args.alert_mail else None)
        self.archive_max = args.archive_max
        self.max_connections = args.max_connections
        self.idle_timeout = args.idle_timeout
        self.require_tls = args.require_tls
        self.fail_closed = args.fail_closed
        self.archive_max_bytes = args.archive_max_bytes * 1024 * 1024
        self.convert_threshold = args.convert_threshold
        self.restrict_formats = not args.all_formats
        self.max_pdf_bytes = args.max_pdf_bytes * 1024 * 1024

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
def _object_index(data):
    """Byte offset of every ``N 0 obj`` in the file.

    Deliberately simple. A file using cross-reference or object streams will
    not index fully, and the caller then gets None and converts -- which is the
    right answer, because an estimate we cannot make is not an estimate we
    should trust.
    """
    index = {}
    for m in re.finditer(rb'(?:^|[\r\n>\s])(\d+)\s+(\d+)\s+obj\b', data):
        index[int(m.group(1))] = m.end()
    return index


def _resolve(data, index, ref, depth=0):
    """Body of an indirect object, following one reference at a time."""
    if depth > 8:
        return b''
    m = re.match(rb'\s*(\d+)\s+\d+\s+R\b', ref)
    if not m:
        return ref
    start = index.get(int(m.group(1)))
    if start is None:
        return b''
    end = data.find(b'endobj', start)
    return _resolve(data, index, data[start:end if end > 0 else start + 4096],
                    depth + 1)


def _font_program_size(data, index, font_body):
    """Size in bytes of the font program behind a /Font object, or 0.

    Not the declared glyph count. A font declaring 65535 glyphs while drawing
    27 printed without trouble, so what a font DECLARES turns out not to matter;
    what it costs is the program itself plus the glyphs actually rendered.
    """
    body = font_body
    m = re.search(rb'/DescendantFonts\s*\[?\s*(\d+\s+\d+\s+R)', body)
    if m:
        body = _resolve(data, index, m.group(1))
    m = re.search(rb'/FontDescriptor\s+(\d+\s+\d+\s+R)', body)
    if not m:
        return 0
    descriptor = _resolve(data, index, m.group(1))
    m = re.search(rb'/FontFile2\s+(\d+)\s+0\s+R', descriptor)
    if not m:
        return 0
    start = index.get(int(m.group(1)))
    if start is None:
        return 0
    head_end = data.find(b'stream', start)
    if head_end < 0:
        return 0
    head = data[start:head_end]
    body_start = head_end + len(b'stream')
    while data[body_start:body_start + 1] in (b'\r', b'\n'):
        body_start += 1
    body_end = data.find(b'endstream', body_start)
    raw = data[body_start:body_end]
    if b'/FlateDecode' in head:
        raw = _inflate(raw)
    return len(raw)


MAX_INFLATE = 32 * 1024 * 1024      # enough for any real content stream
MAX_XOBJECT_DEPTH = 8               # form XObjects nest; cycles must not loop


def _inflate(raw):
    """Decompress, refusing to be a decompression bomb."""
    obj = zlib.decompressobj()
    out = obj.decompress(raw, MAX_INFLATE)
    if obj.unconsumed_tail:
        raise ValueError('stream expands beyond the inspection limit')
    return out


def _balanced_dict(data, start):
    """The << ... >> beginning at `start`, respecting nesting.

    A non-greedy regex stops at the first '>>', which is wrong the moment a
    dictionary contains another -- and resource dictionaries always do.
    """
    if data[start:start + 2] != b'<<':
        return b''
    depth, i = 0, start
    while i < len(data) - 1:
        pair = data[i:i + 2]
        if pair == b'<<':
            depth += 1
            i += 2
            continue
        if pair == b'>>':
            depth -= 1
            i += 2
            if depth == 0:
                return data[start:i]
            continue
        i += 1
    return b''


def _sub_dict(data, index, body, key):
    """Value of `key` in `body`, whether written inline or as a reference."""
    m = re.search(rb'/' + key + rb'\s*', body)
    if not m:
        return b''
    rest = body[m.end():]
    if rest[:2] == b'<<':
        return _balanced_dict(body, m.end())
    rm = re.match(rb'(\d+)\s+\d+\s+R', rest)
    if not rm:
        return b''
    resolved = _resolve(data, index, rm.group(0))
    start = resolved.find(b'<<')
    return _balanced_dict(resolved, start) if start >= 0 else resolved


class Unreadable(Exception):
    """A stream we could not inspect. Never treat that as "contains nothing"."""


def _stream_payload(data, index, ref):
    start = index.get(ref)
    if start is None:
        raise Unreadable('content stream not found')
    head_end = data.find(b'stream', start)
    if head_end < 0:
        raise Unreadable('content stream has no body')
    head = data[start:head_end]
    body_start = head_end + len(b'stream')
    while data[body_start:body_start + 1] in (b'\r', b'\n'):
        body_start += 1
    body_end = data.find(b'endstream', body_start)
    raw = data[body_start:body_end]
    if b'/FlateDecode' in head:
        try:
            raw = _inflate(raw)
        except (zlib.error, ValueError) as exc:
            # Could be corruption, could be a decompression bomb. Either way we
            # have not seen this page's glyphs and must not pretend otherwise.
            raise Unreadable(f'content stream: {exc}')
    return raw


def _walk_resources(data, index, resources, cache, seen, seen_fonts,
                    depth=0):
    """Fonts declared and glyphs drawn by a resource dictionary.

    Follows form XObjects. This is not a nicety: a PDF printed through a
    viewer typically arrives with the whole page wrapped in one, so the page's
    own resources name no fonts at all and everything that matters lives one
    level down. Missing that scores such a job as free, which is exactly
    backwards -- those are the jobs that fail.
    """
    font_bytes = 0
    drawn = set()
    if depth > MAX_XOBJECT_DEPTH:
        raise Unreadable('form XObjects nested too deeply')

    fonts = _sub_dict(data, index, resources, b'Font')
    for rm in re.finditer(rb'/[^\s/<>\[\]]+\s+(\d+)\s+\d+\s+R', fonts):
        num = int(rm.group(1))
        if num in seen_fonts:
            continue
        seen_fonts.add(num)
        if num not in cache:
            cache[num] = _font_program_size(data, index,
                                            _resolve(data, index, b'%d 0 R' % num))
        font_bytes += cache[num]

    xobjects = _sub_dict(data, index, resources, b'XObject')
    for rm in re.finditer(rb'/[^\s/<>\[\]]+\s+(\d+)\s+\d+\s+R', xobjects):
        num = int(rm.group(1))
        if num in seen:
            continue
        seen.add(num)
        body = _resolve(data, index, b'%d 0 R' % num)
        if b'/Subtype' in body and b'/Form' not in body:
            continue                       # an image carries no fonts
        drawn |= _glyphs_in(_stream_payload(data, index, num))
        inner = _sub_dict(data, index, body, b'Resources')
        if inner:
            sub_bytes, sub_drawn = _walk_resources(data, index, inner, cache,
                                                   seen, seen_fonts, depth + 1)
            font_bytes += sub_bytes
            drawn |= sub_drawn
    return font_bytes, drawn


def _glyphs_in(blob):
    """Distinct glyphs a content stream draws."""
    drawn = set()
    for hm in re.finditer(rb'<([0-9A-Fa-f]{4,})>', blob):
        h = hm.group(1)
        if len(h) % 4 == 0:
            for i in range(0, len(h), 4):
                drawn.add(h[i:i + 4])
    # Simple fonts show text as bytes rather than glyph ids. Counting distinct
    # bytes is coarse, but stops such a page being scored as drawing nothing.
    for sm in re.finditer(rb'\((?:\\.|[^\\()])*\)\s*Tj', blob):
        drawn.update(bytes([b]) for b in sm.group(0))
    return drawn


def estimate_font_cost(data):
    """Estimate the worst page's cost to the printer's font cache.

    The budget is measured PER PAGE, so the estimate is too. Summing a whole
    document over-states long ones badly, and over-stating is not harmless: it
    sends jobs down the conversion path, which inflates them, which is how a
    long document ends up too large for the printer to accept at all.

    Both terms are readable without rendering: what each font program reachable
    from the page declares, and how many distinct glyphs are drawn. Calibrated
    against known outcomes on a Color LaserJet Pro MFP M283fdw -- browser jobs
    at 1205, 1291 and 1820 printed; bisection put the limit between 3558 and
    3697.

    Returns None when the file cannot be read confidently. The caller must
    treat that as "convert", never as "safe".
    """
    try:
        index = _object_index(data)
        if not index:
            return None

        pages = [m.start() for m in re.finditer(rb'/Type\s*/Page[^s]', data)]
        if not pages:
            return None
        if len(pages) > MAX_PAGES_INSPECTED:
            return None

        cache = {}
        worst = 0
        for pos in pages:
            obj_start = data.rfind(b'obj', 0, pos)
            end = data.find(b'endobj', pos)
            page = data[obj_start:end if end > 0 else pos + 8192]

            dict_start = page.find(b'<<')
            page_dict = (_balanced_dict(page, dict_start) if dict_start >= 0
                         else page)
            resources = _sub_dict(data, index, page_dict, b'Resources')

            seen, seen_fonts = set(), set()
            font_bytes, drawn = _walk_resources(data, index, resources, cache,
                                                seen, seen_fonts)

            refs = []
            cm = re.search(rb'/Contents\s+(?:(\d+)\s+\d+\s+R|\[([^\]]*)\])',
                           page_dict)
            if cm:
                refs = ([int(cm.group(1))] if cm.group(1) else
                        [int(x) for x in re.findall(rb'(\d+)\s+\d+\s+R',
                                                    cm.group(2) or b'')])
            if not refs:
                raise Unreadable('page contents not resolvable')
            for ref in refs:
                drawn |= _glyphs_in(_stream_payload(data, index, ref))

            worst = max(worst, len(drawn) + font_bytes // FONT_BYTE_UNIT)

        return worst
    except Unreadable as exc:
        log.debug('cannot estimate font cost (%s); will convert', exc)
        return None
    except Exception:
        return None


def sniff_format(data):
    """Identify what the converter handed back.

    Conversion normally returns a PDF, but a document whose outlined form would
    be too large for the printer to accept comes back as raster instead. The
    job's document-format has to follow, or the printer is told to read a
    bitmap as a PDF.
    """
    if data[:5] == b'%PDF-':
        return 'application/pdf'
    if data[:7] == b'UNIRAST':
        return 'image/urf'
    if data[:4] in (b'RaS2', b'RaS3'):
        return 'image/pwg-raster'
    if data[:4] == b'PCLm':
        return 'application/PCLm'
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


# ---------------------------------------------------------------------------
# Watching what the printer actually did with a job.
#
# The failures this proxy exists for are silent: the printer accepts the job,
# reports it completed, and marks nothing. Everything above it repeats that
# success, so nobody finds out. Outlining prevents one such fault, but not the
# others -- and a fault that reports success is invisible to a fallback that
# triggers on rejection.
#
# So the job is followed to its terminal state and judged on what the printer
# says it marked, not on whether the request succeeded. IPP reports this
# itself: a job that completes having marked nothing gives
# job-impressions-completed = 0, which is exactly what a lost job looks like.
# No SNMP and no extra protocol is needed.
#
# This is off unless an address is configured, because on a printer that does
# not report impressions honestly it would cry wolf on every job.
# ---------------------------------------------------------------------------

ALERT_TERMINAL = {7: 'canceled', 8: 'aborted', 9: 'completed'}


class Alerter:
    """Rate-limited delivery of 'that job did not print' reports."""

    def __init__(self, address, max_per_hour, sender=None):
        self.address = address
        self.max_per_hour = max_per_hour
        self.sender = sender or f'ippfix@{socket.getfqdn()}'
        self.sent = []                      # monotonic timestamps
        self.suppressed = 0
        self.lock = threading.Lock()

    def _allow(self):
        """True if we may send now. Keeps a bounded window, not a counter."""
        now = time.monotonic()
        with self.lock:
            self.sent = [t for t in self.sent if now - t < 3600]
            if len(self.sent) >= self.max_per_hour:
                self.suppressed += 1
                return False
            self.sent.append(now)
            held, self.suppressed = self.suppressed, 0
        self.held = held
        return True

    def send(self, subject, body):
        if not self._allow():
            log.warning('alert suppressed (%d in the last hour already): %s',
                        self.max_per_hour, subject)
            return
        held = getattr(self, 'held', 0)
        if held:
            body += (f'\n{held} further alert(s) were suppressed by the rate '
                     f'limit before this one. Raise --alert-max-per-hour, or '
                     f'treat the rate itself as the finding.\n')
        host = socket.getfqdn()
        to = self.address if '@' in self.address else f'{self.address}@{host}'
        message = (
            f'From: ippfix <{self.sender}>\n'
            f'To: {to}\n'
            f'Subject: {subject}\n'
            f'Date: {email.utils.formatdate(localtime=True)}\n'
            f'Message-ID: {email.utils.make_msgid(domain=host)}\n'
            f'Auto-Submitted: auto-generated\n'
            f'\n{body}')
        try:
            proc = subprocess.run(
                ['/usr/sbin/sendmail', '-f', self.sender, '-t', '-i'],
                input=message.encode('utf-8', 'replace'),
                capture_output=True, timeout=60)
            if proc.returncode == 0:
                log.info('alert sent to %s: %s', to, subject)
                return
            log.error('sendmail refused the alert (%s); logging it instead',
                      proc.stderr.decode('utf-8', 'replace').strip()[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            log.error('could not send the alert (%s); logging it instead', exc)
        # Losing the report is worse than the noise of printing it.
        for line in body.splitlines():
            log.error('  %s', line)


def describe_document(data, fmt):
    """Structural facts about a document, for a report.

    Deliberately no text and no images: this goes in an email, and the point is
    to make a fault reproducible, not to copy what somebody printed.
    """
    out = [f'format: {fmt}', f'size: {len(data):,} bytes',
           f'sha256: {hashlib.sha256(data).hexdigest()[:32]}']
    if not data.startswith(b'%PDF-'):
        return out
    out.append(f'pdf version: {data[:8].decode("latin1", "replace")}')
    blobs = [data]
    for m in re.finditer(rb'/Type\s*/ObjStm.*?stream\r?\n', data, re.S):
        e = data.find(b'endstream', m.end())
        if e < 0:
            continue
        try:
            blobs.append(zlib.decompress(data[m.end():e].rstrip(b'\r\n')))
        except Exception:
            pass
    joined = b'\n'.join(blobs)

    prod = re.search(rb'/Producer\s*\(([^)]{0,120})\)', joined)
    if prod:
        out.append('producer: '
                   + prod.group(1).decode('latin1', 'replace'))
    fonts = {}
    for tag in (b'FontFile', b'FontFile2', b'FontFile3'):
        n = len(re.findall(b'/' + tag + rb'[^\d]', joined))
        if n:
            fonts[tag.decode()] = n
    out.append('embedded font programs: '
               + (', '.join(f'{k}={v}' for k, v in fonts.items()) or 'none'))
    for label, pat in (('shading types', rb'/ShadingType\s*(\d+)'),
                       ('pattern types', rb'/PatternType\s*(\d+)'),
                       ('function types', rb'/FunctionType\s*(\d+)')):
        found = sorted({int(x) for x in re.findall(pat, joined)})
        if found:
            out.append(f'{label}: {found}')
    for label, pat in (('transparency groups', rb'/S\s*/Transparency'),
                       ('soft masks', rb'/SMask\s*<<'),
                       ('images', rb'/Subtype\s*/Image')):
        n = len(re.findall(pat, joined))
        if n:
            out.append(f'{label}: {n}')
    return out


def watch_job(cfg, queue, job_id, jobname, fmt, data, note):
    """Follow one job to its end and report if the printer marked nothing."""
    deadline = time.monotonic() + cfg.alert_timeout
    state = impressions = None
    reasons = ''
    history = []
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            req = ipp.new_request(0x0009, 2, queue.upstream_uri())
            g = req.operation()
            g.items.append((ipp.TAG_INTEGER, b'job-id', ipp.i32(job_id)))
            for want in (b'job-state', b'job-state-reasons',
                         b'job-impressions-completed'):
                g.items.append((ipp.TAG_KEYWORD, b'requested-attributes', want))
            _st, raw = upstream_ipp(queue, ipp.serialize(req), 30)
            reply = ipp.parse(raw)
        except Exception:
            continue
        new_state = new_imp = None
        new_reasons = []
        for gr in reply.groups:
            v = gr.get('job-state')
            if v:
                new_state = struct.unpack('>i', v[0])[0] if len(v[0]) == 4 else v[0]
            v = gr.get('job-impressions-completed')
            if v:
                new_imp = struct.unpack('>i', v[0])[0] if len(v[0]) == 4 else v[0]
            v = gr.get('job-state-reasons')
            if v:
                new_reasons = [x.decode('utf-8', 'replace') if isinstance(x, bytes)
                               else str(x) for x in v]
        entry = (ALERT_TERMINAL.get(new_state, new_state), new_imp,
                 ','.join(new_reasons))
        if not history or history[-1] != entry:
            history.append(entry)
        state, impressions, reasons = new_state, new_imp, ','.join(new_reasons)
        if new_state in ALERT_TERMINAL:
            break

    # Judge. Only complain about things that are actually wrong: a job that
    # completed having marked pages is the ordinary case and says nothing.
    if state == 9 and impressions:
        return
    if state is None:
        verdict = 'NO ANSWER'
        detail = (f'the printer stopped answering questions about this job '
                  f'within {cfg.alert_timeout}s')
    elif state == 9 and not impressions:
        verdict = 'LOST SILENTLY'
        detail = ('the printer reported the job completed successfully and '
                  'marked no impressions at all. This is the failure this '
                  'proxy exists for, and it means something got through it.')
    elif state == 8:
        verdict = 'REJECTED'
        detail = ('the printer aborted the job. Unlike a silent loss the '
                  'client was told, so the user may already know.')
    elif state == 7:
        verdict = 'CANCELED'
        detail = 'the job was canceled. This may simply have been the user.'
    else:
        verdict = 'DID NOT FINISH'
        detail = (f'the job never reached a terminal state within '
                  f'{cfg.alert_timeout}s')

    lines = [detail, '',
             f'queue:        {queue.name}',
             f'printer:      {queue.upstream_uri()}',
             f'job id:       {job_id}',
             f'job name:     {jobname or "(none)"}',
             f'conversion:   {note or "relayed unconverted"}',
             f'final state:  {ALERT_TERMINAL.get(state, state)}'
             + (f' ({reasons})' if reasons else ''),
             f'impressions:  {impressions}',
             '', 'What the printer said, in order:']
    lines += [f'  {st} impressions={im} reasons={rs or "none"}'
              for st, im, rs in history]
    lines += ['', 'The document, structurally:']
    lines += [f'  {x}' for x in describe_document(data, fmt)]
    lines += ['', 'To investigate:', '']
    if verdict == 'LOST SILENTLY':
        lines += [
            '  A job that the printer says succeeded but did not print is the',
            '  whole point of this proxy, so this is worth chasing.',
            '',
            '  1. If --archive was on, the document is in the archive directory',
            '     under this job name. That copy is the single most useful',
            '     thing to keep; everything else can be derived from it.',
            '  2. Check it for a malformed soft mask, a known cause that',
            '     conversion cannot repair:',
            '         python3 scripts/check-softmask.py FILE.pdf',
            '  3. Re-send it directly to the printer and through this proxy:',
            '         python3 scripts/probe-printer.py ipp://PRINTER/ipp/print FILE.pdf',
            '     If it fails both ways, conversion is not the answer for it.',
            '  4. See OPEN-QUESTIONS.md for the faults already known.']
    else:
        lines += ['  See DIAGNOSING.md, and keep the document if you can.']
    if not cfg.archive:
        lines += ['',
                  '  --archive is off, so no copy of this document was kept.',
                  '  Turning it on captures the next one -- but it stores what',
                  '  people print, so turn it off again afterwards.']
    cfg.alerter.send(f'ippfix: job {verdict.lower()} on {queue.name}',
                     '\n'.join(lines) + '\n')


def maybe_watch(cfg, queue, reply, msg, fmt, data, note):
    """Start following a print job, if alerting is configured."""
    if not cfg.alerter:
        return
    job_id = None
    for gr in reply.groups:
        v = gr.get('job-id')
        if v:
            job_id = struct.unpack('>i', v[0])[0] if len(v[0]) == 4 else v[0]
            break
    if not job_id:
        return
    op = msg.operation()
    jobname = op.get_str('job-name') if op else None
    with cfg.watch_lock:
        if cfg.watching >= cfg.alert_max_watchers:
            log.warning('not following job %s: already watching %d',
                        job_id, cfg.watching)
            return
        cfg.watching += 1

    def run():
        try:
            watch_job(cfg, queue, job_id, jobname, fmt, data, note)
        except Exception as exc:
            log.error('while following job %s: %s', job_id, exc)
        finally:
            with cfg.watch_lock:
                cfg.watching -= 1

    threading.Thread(target=run, daemon=True,
                     name=f'watch-{job_id}').start()


# What a failure to reach the printer actually looks like. socket errors are
# OSError, but a printer that answers with a malformed HTTP response raises
# http.client.HTTPException instead -- BadStatusLine, IncompleteRead and
# LineTooLong are not OSError subclasses, and letting one of those escape drops
# the client's connection with no explanation at all.
UPSTREAM_ERRORS = (OSError, http.client.HTTPException)


def unreachable(wfile, queue, msg, opname, exc):
    """Answer a client when the printer cannot be reached.

    Dropping the connection tells the client nothing, and a print system that
    is told nothing reports nothing to the user -- the same silence this proxy
    exists to remove. IPP has a status for exactly this, so say it plainly and
    let the client retry or surface it.
    """
    log.warning('%s: %s failed, printer unreachable (%s)',
                queue.name, opname, exc)
    # 0x0502 is server-error-service-unavailable: the right answer for an
    # upstream that is not there, as distinct from one that refused the job.
    reply = ipp.Message(code=0x0502,
                        request_id=getattr(msg, 'request_id', 1))
    reply.groups.append(ipp.Group(ipp.OPERATION_ATTRS, [
        (ipp.TAG_CHARSET, b'attributes-charset', b'utf-8'),
        (ipp.TAG_LANGUAGE, b'attributes-natural-language', b'en-us'),
        (ipp.TAG_TEXT, b'status-message',
         b'the printer is not responding'),
    ]))
    respond(wfile, '200 OK', 'application/ipp', ipp.serialize(reply))


def converter_header(queue, cfg):
    """Tell the converter what this particular printer will accept.

    The converter runs with no network at all, deliberately, so it cannot ask
    the printer anything. Everything model-specific therefore travels with the
    document: which raster format and colour space to fall back to, at what
    resolution, and how large a PDF the printer will take. A converter that
    receives no header keeps its built-in defaults.
    """
    queue.learn()          # a no-op once it has succeeded; retries if it has not
    if not queue.raster_format:
        fields = ['raster=none']
    else:
        fields = [f'device={queue.raster_device}',
                  f'colorspace={queue.raster_colorspace}',
                  f'dpi={queue.raster_dpi}']
    limit = queue.max_pdf_bytes or cfg.max_pdf_bytes
    fields.append(f'maxpdf={limit}')
    return ('%%ippfix ' + ' '.join(fields) + '\n').encode()


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


def convert(cfg, data, fmt, queue=None):
    """Outline the text of a PDF. Anything else is relayed untouched.

    Fails safe: on any doubt the original is forwarded, because a job that
    might not print beats one that prints something wrong.
    """
    if not cfg.convert or not data:
        return data, 'relayed'
    payload = normalise_pdf(data)
    if payload is None:
        return data, f'relayed ({fmt or "not PDF"})'

    # Skipping conversion is an optimisation, and it is off by default.
    #
    # Predicting which jobs a printer will refuse turned out to be unreliable.
    # A model fitted to thirteen measured outcomes was falsified twice by real
    # browser output: a font declaring 65535 glyphs printed where the model said
    # it would fail, and a page drawing 1200 glyphs printed where the model said
    # the same. Documents constructed here fail as predicted; documents a
    # browser produces do not follow the same rule, and the difference is not
    # understood.
    #
    # Converting unconditionally costs about a third of a second on a real job
    # and roughly doubles a hundred-kilobyte file, which is not worth trading
    # for a prediction that has been wrong twice. The estimate is still computed
    # and logged, because it is useful for diagnosis, and a site that has
    # measured its own workload can act on it via --convert-threshold.
    cost = estimate_font_cost(payload)
    if cfg.convert_threshold:
        if cost is not None and cost <= cfg.convert_threshold:
            return data, f'relayed (font cost {cost}, under threshold)'

    started = time.time()
    try:
        if cfg.converter_socket:
            out = convert_over_socket(cfg.converter_socket,
                                      converter_header(queue, cfg) + payload,
                                      cfg.timeout)
        else:
            # start_new_session so a timeout can kill the whole group:
            # terminating the helper leaves Ghostscript itself running.
            payload = converter_header(queue, cfg) + payload if queue \
                else payload
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
                 f'{time.time() - started:.1f}s (font cost {cost})')


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
            # Narrow what clients may choose to what we can actually protect.
            if cfg.restrict_formats:
                offered = [f.decode('utf-8', 'replace')
                           for f in (group.get('document-format-supported') or [])]
                kept = [f for f in offered
                        if f in SAFE_FORMATS or f.lower() in SAFE_FORMATS]
                if kept:
                    group.replace('document-format-supported',
                                  ipp.TAG_MIMETYPE, kept)
                    queue.note_formats(offered, kept)
                default = group.get_str('document-format-default')
                if default and default not in kept and kept:
                    group.replace('document-format-default',
                                  ipp.TAG_MIMETYPE,
                                  ['application/pdf' if 'application/pdf' in kept
                                   else kept[0]])
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
        fmt = None

        if msg.code in (OP_PRINT_JOB, OP_SEND_DOCUMENT) and msg.data:
            group = msg.operation()
            fmt = group.get_str('document-format') if group else None
            # One job at a time: these printers report
            # multiple-document-jobs-supported = false, and a second job
            # arriving mid-transfer confuses them.
            original = msg.data
            try:
                msg.data, note = convert(cfg, msg.data, fmt, queue)
                # Conversion may legitimately change the format: an outlined
                # document too large for the printer to accept as a PDF comes
                # back as raster. Say so, rather than mislabelling it.
                if msg.data is not original:
                    produced = sniff_format(msg.data)
                    if produced and produced != fmt and group is not None:
                        group.replace('document-format', ipp.TAG_MIMETYPE,
                                      [produced])
                        note += f'; sent as {produced}'

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
            failure = None
            try:
                status, raw = upstream_ipp(queue, payload, cfg.timeout)
            except UPSTREAM_ERRORS as exc:
                failure = exc
            finally:
                # Release before answering. Writing to a client can block for as
                # long as that client cares to take, and holding the queue lock
                # across it would let one slow reader stall every other job.
                queue.lock.release()
            if failure is not None:
                unreachable(wfile, queue, msg, name, failure)
                return
        else:
            rewrite_request(queue, msg)
            try:
                status, raw = upstream_ipp(queue, ipp.serialize(msg), cfg.timeout)
            except UPSTREAM_ERRORS as exc:
                unreachable(wfile, queue, msg, name, exc)
                return

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

        # The client has its answer; now find out whether the printer really
        # prints it. This happens after responding, so following a job never
        # delays one.
        if msg.code in (0x0002, 0x0006) and status == 200 and cfg.alerter:
            try:
                maybe_watch(cfg, queue, ipp.parse(raw), msg, fmt,
                            msg.data or b'', note)
            except Exception as exc:
                log.error('could not start following the job: %s', exc)


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


DEFAULT_CONVERTER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'defont')


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
    # Alongside this file, wherever it was installed. Hard-coding a path meant
    # the default was wrong for whichever of the two install layouts was not
    # chosen when it was written.
    parser.add_argument('--converter', default=DEFAULT_CONVERTER,
                        help='PDF conversion helper: either an executable that '
                             'filters stdin to stdout, or unix:PATH to reach '
                             'the separately sandboxed conversion service '
                             '(default: defont, alongside this program)')
    parser.add_argument('--timeout', type=int, default=300,
                        help='seconds allowed per conversion and per upstream '
                             'request (default: 300)')
    parser.add_argument('--alert-mail', metavar='ADDRESS',
                        help='email this address when a job does not print. '
                             'The printer reports success even when it marks '
                             'nothing, so without this such a loss is invisible '
                             'to everyone. Off unless set: a printer that does '
                             'not report impressions honestly would cry wolf on '
                             'every job.')
    parser.add_argument('--alert-max-per-hour', type=int, default=6,
                        metavar='N',
                        help='never send more than N alerts an hour (default '
                             '6). Suppressed ones are logged and counted in the '
                             'next message, so a flood is reported as a flood '
                             'rather than becoming one.')
    parser.add_argument('--alert-timeout', type=int, default=600, metavar='SEC',
                        help='how long to follow a job before giving up on it '
                             '(default 600)')
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
    parser.add_argument('--convert-threshold', type=int, default=0,
                        metavar='N',
                        help='leave a job untouched when its estimated font '
                             'cost is at or below N. The default of 0 converts '
                             'every PDF, because the cost model predicts '
                             'synthetic documents well and real browser output '
                             'poorly, and converting is cheap. Set a threshold '
                             'only if you have measured your own workload')
    parser.add_argument('--max-pdf-bytes', type=int, default=60,
                        metavar='MB',
                        help='rasterise rather than send an outlined PDF larger '
                             'than this. Overridden by the printer\'s own '
                             'pdf-k-octets-supported when it reports one '
                             '(default: 60)')
    parser.add_argument('--all-formats', action='store_true',
                        help='offer clients every format the printer '
                             'supports, including PostScript. PostScript is '
                             'handled by the interpreter that fails and cannot '
                             'be converted safely, so it is withheld by '
                             'default; PCL is offered either way')
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

    for queue in queues:
        queue.learn()

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
