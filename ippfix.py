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
import email.message
import email.utils
import gzip
import hashlib
import ipaddress
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
import snmpmini as snmp

log = logging.getLogger('ippfix')

OP_PRINT_JOB = 0x0002
OP_CREATE_JOB = 0x0005
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

# The three values `sides` may take on the way to the converter. The client's
# own word is passed through and nothing is ever invented, but it is checked
# against this list first: the value is written into a line the converter parses
# as whitespace-separated fields, so a value carrying a space would let whoever
# sent the job add a field of their own -- a device= naming a Ghostscript device
# with a history of defeating -dSAFER, say. A value that is not one of these is
# dropped and the converter keeps its default, which is what happened before
# this field existed at all.
SIDES_VALUES = ('one-sided', 'two-sided-long-edge', 'two-sided-short-edge')

# The IPP statuses that justify converting again as raster and resending.
#
# Every one of them says the same two things: the printer created no job, and
# the document it was handed is the reason. That is what makes a resend safe --
# there is nothing to double, because nothing was accepted. It is also why this
# list may only ever grow from a status with those two properties. A lost or
# dropped answer has neither: the printer may well be holding the job, and
# sending it again prints it twice.
#
#   0x0408 client-error-request-entity-too-large
#       The document is larger than the printer will take. RFC 8011 8.1.3 has
#       the Printer reject the request, so no Job object exists. This is the
#       measured case: the device declares pdf-k-octets-supported 0..75000 and
#       does not enforce it, so the size at which it will actually answer this
#       cannot be predicted and must be discovered by asking.
#   0x0411 client-error-document-format-error
#       The format was accepted but the bytes could not be parsed. Again no job
#       is created, and the fault is in the document, so handing over the same
#       document again would earn the same answer -- while a raster of it does
#       not go near the interpreter that objected.
#   0x040A client-error-document-format-not-supported
#       The printer will not take this format at all. It is only ever answered
#       about the document just offered, and it is answered before a job is
#       created. Raster is the one other thing this proxy can produce, so it is
#       worth exactly one attempt.
#
# Deliberately absent: everything that describes the printer's state rather than
# the document -- 0x0505 temporary-error, 0x0506 not-accepting-jobs, 0x0507
# busy, 0x0509 multiple-document-jobs-not-supported, 0x050B too-many-jobs.
# Rasterising answers none of them, and several are transient, so a resend would
# be a second attempt at the same job rather than a different one.
RETRY_AS_RASTER = {
    0x0408: 'client-error-request-entity-too-large',
    0x040A: 'client-error-document-format-not-supported',
    0x0411: 'client-error-document-format-error',
}

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
# Against the thirteen jobs it was fitted to, this separated cleanly: everything
# scoring 562 or less printed, everything scoring 586 or more did not. It did
# not survive contact with real browser output -- a page drawing 1264 glyphs
# printed where one drawing 700 failed -- so the score is kept for diagnosis
# and nothing is decided by it unless --convert-threshold is set explicitly.
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

    # Settings that belong to one printer rather than to the daemon travel in
    # the printer's own URI, because that is the only place a per-device
    # setting can be written without inventing a way to say which device a
    # flag applies to. Unknown keys are an error: a mistyped option that is
    # silently ignored is how a printer ends up not doing what its
    # configuration says it does.
    URI_OPTIONS = ('page-counter', 'community', 'snmp-relay',
                   'supply-levels', 'page-geometry')

    def __init__(self, name, uri):
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme not in ('ipp', 'ipps'):
            raise ValueError(f'{name}: expected an ipp:// or ipps:// URI')
        options = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        for key in options:
            if key not in self.URI_OPTIONS:
                raise ValueError(
                    f'{name}: unknown option {key!r} in the printer URI; '
                    f'known options are {", ".join(self.URI_OPTIONS)}')
        self.community = options.get('community', ['public'])[0]
        want = options.get('page-counter', ['on'])[0].lower()
        if want not in ('on', 'off'):
            raise ValueError(f'{name}: page-counter must be on or off, '
                             f'not {want!r}')
        self.want_page_counter = want == 'on'
        # None means "unset", which is not the same as "off": with a single
        # printer an unset relay is served, and with several the administrator
        # has to say which one, because SNMP carries nothing that names a
        # printer. An address says which listener speaks for this printer,
        # which is the only way to serve several of them at once -- one
        # address each, one socket each.
        # Whether to correct a printer that reports a supply level it would
        # itself call empty while warning only that toner is low. See
        # clamp_supply_levels(). "raw" passes the printer's numbers through
        # untouched, for anyone who would rather see the device lie plainly.
        levels = options.get('supply-levels', ['clamped'])[0].lower()
        if levels not in ('clamped', 'raw'):
            raise ValueError(f'{name}: supply-levels must be clamped or raw, '
                             f'not {levels!r}')
        self.clamp_supplies = levels == 'clamped'
        # Whether to put back a page whose sender placed it off the sheet. See
        # repair_placement(). "raw" sends what arrived, which is what to set
        # if a repaired sheet ever comes out worse than an unrepaired one.
        geometry = options.get('page-geometry', ['repair'])[0].lower()
        if geometry not in ('repair', 'raw'):
            raise ValueError(f'{name}: page-geometry must be repair or raw, '
                             f'not {geometry!r}')
        self.repair_placement = geometry == 'repair'
        relay = options.get('snmp-relay', [None])[0]
        if relay is None:
            self.snmp_relay = None
        elif relay.lower() in ('on', 'off'):
            self.snmp_relay = relay.lower() == 'on'
        else:
            try:
                self.snmp_relay = str(ipaddress.ip_address(relay))
            except ValueError:
                raise ValueError(
                    f'{name}: snmp-relay must be on, off, or an address to '
                    f'listen on, not {relay!r}') from None
        self.name = name
        self.slug = slugify(name)
        self.tls = parts.scheme == 'ipps'
        self.host = parts.hostname
        self.port = parts.port or 631
        self.path = parts.path or '/ipp/print'
        self.preferred = None        # address that last connected
        self.supply_note = None      # last supply correction logged, if any
        # Affected printers report multiple-document-jobs-supported=false, so
        # jobs are serialised -- but per printer, not globally, and only around
        # the upstream exchange. Conversion happens outside the lock so one
        # expensive document cannot stall every other queue.
        self.lock = threading.Lock()
        self.raster_format = None      # learned from the printer, see below
        self.raster_device = None
        self.raster_colorspace = None
        self.raster_dpi = None
        # What the printer says it will take, for the log only. Measured not to
        # be enforced: see learn().
        self.declared_pdf_bytes = None
        # What the printer says about itself, for the DNS-SD record. None is
        # "the printer has not said", which is not the same as False and is
        # advertised as neither: see discovery_txt().
        self.formats = []              # document-format-supported, verbatim
        self.colour = None
        self.duplex = None
        self.learned = False
        self.learn_failed_at = None    # monotonic time of the last failure
        self.pages = None              # PageCounter, once Config has built it
        self._warned_formats = False
        if not self.host:
            raise ValueError(f'{name}: no host in {uri!r}')

    def learn(self, timeout=20, retry_after=60):
        """Ask the printer what it can actually take, once.

        Everything the raster fallback needs varies by model: which raster
        format, which colour space (a monochrome device offers no colour one)
        and which resolution. Guessing any of them produces a job the printer
        rejects, which is the failure this proxy exists to remove.

        How large a PDF the printer will accept is read too, and then used for
        nothing but the log. Measured on an M283fdw: it declares
        pdf-k-octets-supported 0..75000, a 76.8 MB working limit, and printed a
        92.5 MB PDF without complaint. A declared cap that the device does not
        enforce cannot be used to decide anything -- deciding from it means
        rasterising documents the printer would have taken whole. What the
        printer will refuse is now discovered by offering it the document; see
        RETRY_AS_RASTER.
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
                 'color-supported', 'sides-supported',
                 'pwg-raster-document-resolution-supported'])
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
        self.formats = formats
        for candidate in RASTER_PREFERENCE:
            if candidate in formats:
                self.raster_format = candidate
                self.raster_device = RASTER_DEVICE[candidate]
                break

        urf = [f.decode('utf-8', 'replace')
               for f in (group.get('urf-supported') or [])]
        colour = group.get('color-supported')
        has_colour = bool(colour and colour[0] not in (b'\x00', b''))
        # Kept apart from has_colour on purpose. Deciding a colour space needs
        # an answer either way, and grey is the safe one to assume; saying so
        # in a DNS-SD record does not, and a printer that never mentioned
        # colour has not said it is monochrome.
        if colour:
            self.colour = has_colour
        sides = [s.decode('utf-8', 'replace')
                 for s in (group.get('sides-supported') or [])]
        if sides:
            self.duplex = any(s.startswith('two-sided') for s in sides)
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
                self.declared_pdf_bytes = upper * 1024

        if self.raster_format:
            log.info('%s: raster fallback %s, %s, %d dpi', self.name,
                     self.raster_format, getattr(self, 'raster_colorname', '?'),
                     self.raster_dpi)
        else:
            log.warning('%s: printer accepts no raster format we can produce; '
                        'oversized jobs will be sent as PDF and may be '
                        'rejected', self.name)
        if self.declared_pdf_bytes:
            # Advisory, and said so in the journal, because a reader who takes
            # this number for a limit will misread every later line about a
            # job that was larger and printed anyway.
            log.info('%s: printer declares PDF up to %.0f MB; treated as '
                     'advisory, since the device this was built for does not '
                     'enforce its own', self.name,
                     self.declared_pdf_bytes / 1e6)

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
        for q in queues:
            q.pages = PageCounter(
                q, community=q.community,
                enabled=q.want_page_counter and not args.no_page_counter)
        self.no_snmp_relay = args.no_snmp_relay
        self.snmp_allow = []
        for cidr in args.snmp_allow or ():
            self.snmp_allow.append(ipaddress.ip_network(cidr, strict=False))
        self.advertise = args.advertise or local_ip()
        if args.also_advertise:
            self.extra_addresses = args.also_advertise
        elif args.no_ipv6:
            self.extra_addresses = []
        else:
            # Only this interface's addresses: see interface_of().
            self.extra_addresses = global_ipv6(interface_of(self.advertise))
        self.advertise_hostname = args.advertise_hostname
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
        self.alert_max_attachment = args.alert_max_attachment * 1024 * 1024
        self.alert_max_watchers = 32
        self.watching = 0
        self.watch_lock = threading.Lock()
        self.alerter = (Alerter(args.alert_mail, args.alert_max_per_hour,
                                spool=args.alert_spool)
                        if args.alert_mail else None)
        self.archive_max = args.archive_max
        self.max_connections = args.max_connections
        self.idle_timeout = args.idle_timeout
        self.require_tls = args.require_tls
        self.fail_closed = args.fail_closed
        self.archive_max_bytes = args.archive_max_bytes * 1024 * 1024
        self.convert_threshold = args.convert_threshold
        self.restrict_formats = not args.all_formats
        # Not a prediction of what the printer will take -- nothing here
        # predicts that any more. It is the size above which the converter is
        # asked to rasterise on its own, which is a last resort for a document
        # this proxy could not hand over whole whatever the printer thinks. 0,
        # the default, means "do not pre-empt": offer the printer the outlined
        # PDF and let it answer for itself. See converter_header().
        self.max_pdf_bytes = args.max_pdf_bytes * 1024 * 1024

    def base_http(self):
        """Where this server re-serves the printer's icons and strings.

        https when plaintext is refused: with --require-tls the daemon answers
        nothing on http, so an http URI here points at a door that does not
        open, and the client silently shows no icon rather than reporting an
        error anybody would see.
        """
        host = (f'[{self.advertise}]' if ':' in self.advertise
                else self.advertise)
        scheme = 'https' if self.require_tls else 'http'
        return f'{scheme}://{host}:{self.port}'

    def dnssd_hostname(self):
        """The name to put in the SRV record clients build their URI from.

        By default this is the advertised IPv4 address, not a .local name. The
        distinction matters after discovery, not during it: a client stores the
        URI it was given and uses it every time it prints from then on. A
        .local name has to be resolved by multicast DNS on each of those
        occasions, and multicast does not cross a VPN, a routed subnet, or a
        wireless network with client isolation -- so the printer is found once
        and then quietly stops working from anywhere else. An address literal
        needs no resolution at all, which is one fewer thing to be somewhere
        it does not work.

        The cost is that the address becomes part of what clients remember, so
        it should be a reserved or static one. That is already true of
        --advertise, which every URI this proxy hands out is built from.

        An IPv6 literal is deliberately not used: clients paste the SRV target
        straight into ipp://HOST:PORT/..., where a bare v6 address needs square
        brackets that they do not add. So a v6-only --advertise falls back to
        the system name, which resolves to both families. AAAA records are
        published either way; this only decides which name clients are handed.
        """
        name = self.advertise_hostname
        if not name:
            name = (self.advertise if '.' in self.advertise
                    else f'{socket.gethostname()}.local')
        elif name == 'auto':
            name = f'{socket.gethostname()}.local'
        return name if name.endswith('.') else name + '.'

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

    Both terms are readable without rendering: how large each font program
    reachable from the page is, and how many distinct glyphs are drawn. What a
    font DECLARES is deliberately not counted; that model was falsified. Fitted
    against measured outcomes on a Color LaserJet Pro MFP M283fdw, where real
    browser jobs scored 434 to 471 and everything at 586 or above failed -- but
    later measurements falsified this model too, so the number is a diagnostic
    rather than a decision. See DIAGNOSING.md.

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


# ---------------------------------------------------------------------------
# Page placement
#
# A print path that imports a page and then fits it to the paper has to make
# two decisions -- where the imported page's own origin is, and where the fit
# puts it on the sheet -- and it is possible to make both and apply both. When
# that happens the document says two incompatible things about itself, and the
# printer believes the wrong one: content lands off the sheet, and a band down
# one edge is never printed at all.
#
# This has been seen from cups-filters' pdftopdf, which wraps an imported page
# in a form XObject using qpdf's idiom -- /BBox is one of the source page's
# boxes and /Matrix translates by that box's negated lower-left -- and then
# prepends a fit-to-printable-area transform to the form's content without
# resetting either. Nothing here keys on that, or on any producer string: the
# captured jobs carry /Producer (PDFium) because pdftopdf preserved the Info
# dictionary of its own input, so the producer names a program that had nothing
# to do with the geometry. The signature that matters is arithmetic.
# ---------------------------------------------------------------------------

# How close two placements have to be before they are called the same. The
# real jobs miss by 1.1pt, because pdftopdf wrapped the page using its TrimBox
# while computing the fit from its CropBox; healthy files that resemble this
# shape miss by 6 to 594pt. So the gap between "is this" and "is not this" is
# wide, and the threshold only has to sit inside it.
PLACEMENT_TOLERANCE = 2.0
# Where a value is meant to be the same number written twice, it is.
EXACT_TOLERANCE = 0.01
# A job with more pages than this is not inspected. The check is cheap, but a
# document is attacker-supplied and a bound that exists is worth more than one
# that is argued to be unnecessary.
MAX_PLACEMENT_PAGES = 512

# Sheets recognised when the job ticket does not name its media. Widths and
# heights in points, portrait; both orientations are matched.
STANDARD_SHEETS = (
    (612.0, 792.0, 'na_letter_8.5x11in'),
    (612.0, 1008.0, 'na_legal_8.5x14in'),
    (595.276, 841.89, 'iso_a4_210x297mm'),
    (419.528, 595.276, 'iso_a5_148x210mm'),
    (841.89, 1190.55, 'iso_a3_297x420mm'),
    (522.0, 756.0, 'na_executive_7.25x10.5in'),
    (792.0, 1224.0, 'na_ledger_11x17in'),
)

_TOKEN = re.compile(rb"""
      (?P<comment>%[^\r\n]*)
    | (?P<name>/[^\s/\[\]<>(){}%]*)
    | (?P<number>[-+]?(?:\d+\.\d*|\.\d+|\d+))
    | (?P<other>[\[\]<>(){}]|[^\s/\[\]<>(){}%]+)
""", re.X)


class NotPlaced(Exception):
    """This document is not the shape the repair knows how to reason about."""


def _tokens(blob, limit=None):
    """Operands and operators, in order. Comments are not tokens.

    Stops at the first delimiter that starts something this cannot read -- a
    string, a dictionary, an inline image. Every use here is a fixed opening
    sequence of operators, so meeting one of those means the answer is "no"
    rather than "look harder".
    """
    out = []
    for m in _TOKEN.finditer(blob):
        kind = m.lastgroup
        if kind == 'comment':
            continue
        text = m.group()
        if kind == 'other' and text in (b'[', b']', b'<', b'>', b'(', b')',
                                        b'{', b'}'):
            break
        out.append((kind, text))
        if limit is not None and len(out) >= limit:
            break
    return out


def _numbers(text):
    """Every number in a PDF array, in order. Returns None if any is not one."""
    body = text.strip()
    if body[:1] == b'[':
        body = body[1:-1] if body[-1:] == b']' else body[1:]
    out = []
    for word in body.split():
        try:
            out.append(float(word))
        except ValueError:
            return None
    return out


def _box(nums):
    """A rectangle as (x0, y0, x1, y1), normalised.

    7.9.5 allows any two diagonally opposite corners, so the numbers as written
    are not necessarily lower-left first, and arithmetic that assumes they are
    is arithmetic on a rectangle the file does not contain.
    """
    if not nums or len(nums) != 4:
        return None
    x0, y0, x1, y1 = nums
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _rect(x, y, w, h):
    """The `re` operator's rectangle. Negative width and height are legal."""
    return (min(x, x + w), min(y, y + h), max(x, x + w), max(y, y + h))


def _size(box):
    return (box[2] - box[0], box[3] - box[1])


def _near(a, b, tol):
    return abs(a - b) <= tol


def _boxes_near(a, b, tol):
    return all(_near(p, q, tol) for p, q in zip(a, b))


def _intersect(a, b):
    out = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return out if out[2] > out[0] and out[3] > out[1] else None


def _contains(outer, inner, tol=EXACT_TOLERANCE):
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _area(box):
    return (box[2] - box[0]) * (box[3] - box[1]) if box else 0.0


# Attributes a page inherits from its ancestors in the page tree
# (7.7.3, inheritance of page attributes).
# Reading MediaBox off the page object alone is wrong whenever it was written
# once on the /Pages node, which is the common way to write it.
_INHERITABLE = (b'Resources', b'MediaBox', b'CropBox', b'Rotate')


def _raw_value(body, key):
    """The bytes of `key`'s value in a dictionary, whatever kind it is."""
    m = re.search(rb'/' + key + rb'(?![A-Za-z0-9])\s*', body)
    if not m:
        return None
    rest = body[m.end():]
    if rest[:1] == b'[':
        depth, i = 0, 0
        while i < len(rest):
            if rest[i:i + 1] == b'[':
                depth += 1
            elif rest[i:i + 1] == b']':
                depth -= 1
                if depth == 0:
                    return rest[:i + 1]
            i += 1
        return None
    if rest[:2] == b'<<':
        return _balanced_dict(rest, 0) or None
    m = re.match(rb'\d+\s+\d+\s+R\b|/[^\s/\[\]<>(){}%]*|[-+]?[\d.]+|\w+', rest)
    return m.group() if m else None


def _pages(data, index):
    """Every page object, in document order, with inherited attributes.

    Yields (objnum, body, inherited) where `inherited` holds the attributes an
    ancestor supplied. Raises NotPlaced rather than guessing at anything it
    cannot follow.
    """
    root = None
    for num, start in index.items():
        end = data.find(b'endobj', start)
        body = data[start:end if end > 0 else start + 4096]
        if re.search(rb'/Type\s*/Catalog\b', body):
            root = _raw_value(body, b'Pages')
            break
    if root is None:
        raise NotPlaced('no catalogue')
    out = []

    def walk(ref, inherited, depth):
        if depth > 32 or len(out) > MAX_PLACEMENT_PAGES:
            raise NotPlaced('page tree too deep or too large')
        m = re.match(rb'(\d+)\s+\d+\s+R\b', ref.strip())
        if not m:
            raise NotPlaced('page tree holds something that is not a reference')
        num = int(m.group(1))
        start = index.get(num)
        if start is None:
            raise NotPlaced(f'object {num} is not in the file')
        end = data.find(b'endobj', start)
        body = data[start:end if end > 0 else start + 4096]
        here = dict(inherited)
        for key in _INHERITABLE:
            value = _raw_value(body, key)
            if value is not None:
                here[key] = value
        if re.search(rb'/Type\s*/Pages\b', body):
            kids = _raw_value(body, b'Kids')
            if kids is None:
                raise NotPlaced('a /Pages node with no /Kids')
            for kid in re.findall(rb'\d+\s+\d+\s+R', kids):
                walk(kid, here, depth + 1)
            return
        if not re.search(rb'/Type\s*/Page(?![a-zA-Z])', body):
            raise NotPlaced('page tree holds something that is not a page')
        out.append((num, body, here))

    walk(root, {}, 0)
    if not out:
        raise NotPlaced('no pages')
    return out


def _single_form_invocation(data, index, body, inherited):
    """The one form a page draws and nothing else, or None.

    The page's whole content has to be `q q [identity cm] /X Do Q Q`. Anything
    else -- a leading `0.1 w`, a watermark drawn afterwards, a second `Do` --
    means the page is not simply somebody else's page re-placed, and the
    reasoning below does not apply to it. A translation in that `cm` is
    refused too: it is a placement decision this cannot see the intent of, and
    the repair would silently drop it.
    """
    contents = _raw_value(body, b'Contents')
    if contents is None:
        return None
    refs = [int(n) for n in re.findall(rb'(\d+)\s+\d+\s+R', contents)]
    if len(refs) != 1:
        return None                      # an array of streams: not this shape
    try:
        stream = _stream_payload(data, index, refs[0])
    except Unreadable:
        return None
    want = [b'q', b'q', b'/X', b'Do', b'Q', b'Q']
    got = _tokens(stream, limit=16)
    words = [t for _, t in got]
    if words[:2] != [b'q', b'q']:
        return None
    rest = words[2:]
    if rest[:7] == [b'1', b'0', b'0', b'1', b'0', b'0', b'cm']:
        rest = rest[7:]
    if len(rest) != 4 or rest[1] != b'Do' or rest[2:] != [b'Q', b'Q']:
        return None
    if not rest[0].startswith(b'/'):
        return None
    name = rest[0]
    resources = inherited.get(b'Resources')
    if resources is None:
        return None
    if resources.strip().endswith(b'R'):
        resources = _resolve(data, index, resources)
        start = resources.find(b'<<')
        resources = _balanced_dict(resources, start) if start >= 0 else b''
    xobjects = _sub_dict(data, index, resources, b'XObject')
    if not xobjects:
        return None
    entries = re.findall(rb'(/[^\s/\[\]<>(){}%]+)\s+(\d+)\s+\d+\s+R', xobjects)
    if len(entries) != 1 or entries[0][0] != name:
        return None
    return int(entries[0][1])


def _leading_fit(stream):
    """The clip rectangle and uniform scale a fit-to-paper transform opens with.

    Returns (clip, scale, tx, ty). The clip is only established by the painting
    operator that follows `W`/`W*` (8.5.4), so that operator has to be there.
    """
    words = [t for _, t in _tokens(stream, limit=24)]
    if words[:1] != [b'q']:
        return None
    try:
        x, y, w, h = (float(v) for v in words[1:5])
    except (ValueError, IndexError):
        return None
    if words[5:6] not in ([b're'],):
        return None
    if words[6:7] not in ([b'W'], [b'W*']):
        return None
    if words[7:8] != [b'n']:
        return None
    try:
        a, b, c, d, tx, ty = (float(v) for v in words[8:14])
    except (ValueError, IndexError):
        return None
    if words[14:15] != [b'cm']:
        return None
    # Uniform, positive, no rotation or skew. A flip or a rotation is a
    # placement this does not know how to undo, so it is not one to touch.
    if b != 0 or c != 0 or a <= 0 or not _near(a, d, 1e-9):
        return None
    return _rect(x, y, w, h), a, tx, ty


_MEDIA_NAME = re.compile(rb'_([0-9.]+)x([0-9.]+)(in|mm)$')


def ticket_sheet(msg):
    """The sheet the client asked for, in points, or None if it did not say.

    `media` is a PWG 5101.1 self-describing name and carries its own
    dimensions, so no table of paper sizes has to be right for this to work.
    `media-col` carries them as hundredths of a millimetre.
    """
    for group in msg.groups:
        col = group.get('media-col') or []
        want = {}
        for i, item in enumerate(col):
            if item in (b'x-dimension', b'y-dimension') and i + 1 < len(col):
                value = col[i + 1]
                if isinstance(value, bytes) and len(value) == 4:
                    want[item] = int.from_bytes(value, 'big', signed=True)
        if len(want) == 2:
            x, y = want[b'x-dimension'], want[b'y-dimension']
            if x > 0 and y > 0:
                return (x * 72.0 / 2540.0, y * 72.0 / 2540.0)
    for group in msg.groups:
        for value in (group.get('media') or []):
            m = _MEDIA_NAME.search(value if isinstance(value, bytes)
                                   else str(value).encode())
            if not m:
                continue
            try:
                w, h = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            per = 72.0 if m.group(3) == b'in' else 72.0 / 25.4
            if w > 0 and h > 0:
                return (w * per, h * per)
    return None


def _matched_sheet(sheet, wanted):
    """The real sheet a derived one is, or None if it is not one.

    The derived numbers come from doubling a margin, so they carry whatever
    rounding the producer wrote; A4 arrives as 595x841 rather than
    595.276x841.89. What goes back into the file is the size paper actually
    comes in, not the arithmetic that recognised it.

    `wanted` is None whenever the job did not say, which is the ordinary case
    rather than an edge one: a client that opens with Create-Job states its
    media there, and the Send-Document carrying the pages repeats nothing --
    all four jobs captured off the wire were like that. This proxy holds no
    per-job state to remember it with, so on that path a sheet is believed only
    if it is a size paper comes in.
    """
    candidates = ([wanted] if wanted is not None
                  else [(w, h) for w, h, _ in STANDARD_SHEETS])
    for w, h in candidates:
        if (_near(sheet[0], w, PLACEMENT_TOLERANCE)
                and _near(sheet[1], h, PLACEMENT_TOLERANCE)):
            return (w, h)
        if (_near(sheet[0], h, PLACEMENT_TOLERANCE)
                and _near(sheet[1], w, PLACEMENT_TOLERANCE)):
            return (h, w)
    return None


def _page_plan(data, index, num, body, inherited, wanted, used_forms):
    """What to rewrite on one page, or a reason not to.

    Returns (plan, None) or (None, reason). `plan` carries every number the
    repair and the check afterwards need, so neither has to parse anything a
    second time and reach a different answer.
    """
    # The shape first, and quietly. Every guard below this reports itself,
    # because past this point the page IS somebody else's page re-placed and a
    # near miss is evidence worth keeping; a page that was never that shape is
    # simply an ordinary page and saying so about each one would bury the
    # reports that matter.
    form_num = _single_form_invocation(data, index, body, inherited)
    if form_num is None:
        return None, None               # not this shape at all: stay quiet
    if form_num in used_forms:
        return None, 'the same form is drawn by more than one page'
    start = index.get(form_num)
    if start is None:
        return None, 'the form is not in the file'
    end = data.find(b'endobj', start)
    form = data[start:end if end > 0 else start + 4096]
    if not re.search(rb'/Subtype\s*/Form\b', form):
        return None, None
    bbox = _box(_numbers(_raw_value(form, b'BBox') or b''))
    matrix = _numbers(_raw_value(form, b'Matrix') or b'')
    if bbox is None or matrix is None or len(matrix) != 6:
        return None, None
    a, b, c, d, mx, my = matrix
    if (a, b, c, d) != (1.0, 0.0, 0.0, 1.0):
        return None, None               # not a pure translation
    if _near(mx, 0, EXACT_TOLERANCE) and _near(my, 0, EXACT_TOLERANCE):
        return None, None               # nothing was normalised away
    # The import idiom: /Matrix undoes /BBox's own origin. From here on the
    # page is one somebody else's page re-placed, and a report is owed whether
    # or not it turns out to be repairable.
    if not (_near(mx, -bbox[0], EXACT_TOLERANCE)
            and _near(my, -bbox[1], EXACT_TOLERANCE)):
        return None, None

    rotate = inherited.get(b'Rotate')
    if rotate is not None and rotate.strip() != b'0':
        # Rotation is applied to the whole composed page, so a repair that set
        # the page to the sheet would deliver the sheet turned on its side.
        return None, 'the page is rotated'
    if _raw_value(body, b'UserUnit') is not None:
        return None, 'the page sets /UserUnit'
    annots = _raw_value(body, b'Annots')
    if annots is not None and annots.strip() not in (b'[]', b'[ ]'):
        # /Rect is in default user space and does not follow the content, so
        # moving the content under an annotation misregisters it (12.5.2).
        return None, 'the page carries annotations'
    media = _box(_numbers(inherited.get(b'MediaBox') or b''))
    if media is None or _size(media)[0] <= 0 or _size(media)[1] <= 0:
        return None, 'no usable /MediaBox'
    crop_box = _box(_numbers(inherited.get(b'CropBox') or b''))
    if crop_box is not None and not _boxes_near(crop_box, media,
                                                EXACT_TOLERANCE):
        # CropBox is what clips (14.11.2, page boundaries). One that differs
        # from MediaBox is a second placement decision, and undoing only the
        # ones understood here would leave the page clipped by the one that
        # is not.
        return None, 'the page has a /CropBox of its own'

    try:
        stream = _stream_payload(data, index, form_num)
    except Unreadable:
        return None, 'the form content could not be read'
    fit = _leading_fit(stream)
    if fit is None:
        return None, 'the form does not open with a fit transform'
    clip, scale, tx, ty = fit

    # The discriminator. If /BBox is in the space the content was in BEFORE the
    # fit, then pushing it through the fit lands it on the fit's own clip --
    # which is exactly the mistake, stated as an equation. A file whose /BBox
    # is in the space it claims to be in misses this by tens to hundreds of
    # points.
    imaged = (scale * bbox[0] + tx, scale * bbox[1] + ty,
              scale * bbox[2] + tx, scale * bbox[3] + ty)
    error = max(abs(p - q) for p, q in zip(imaged, clip))
    if error > PLACEMENT_TOLERANCE:
        return None, (f'the form /BBox is in its own space '
                      f'(off by {error:.1f}pt)')
    if not _boxes_near(_size(media) + _size(media), _size(bbox) + _size(bbox),
                       EXACT_TOLERANCE):
        return None, 'the page is not the size of the form /BBox'
    if clip[0] < -EXACT_TOLERANCE or clip[1] < -EXACT_TOLERANCE:
        return None, 'the fit rectangle starts outside the sheet'
    # The fit rectangle is centred, so the sheet is what it is centred on.
    #
    # Centred in the printable area, strictly, not on the sheet -- which is the
    # same thing only while the hardware margins are symmetric. They are on the
    # printer this was measured against, which advertises the same 4.23mm on
    # all four edges; on a printer with a deeper bottom margin, as many inkjets
    # have, this arrives at a sheet that is not a real one and the job is left
    # alone. That is a job not repaired rather than a job repaired wrongly, so
    # it stays this way until there is a printer here to measure.
    sheet = (2 * clip[0] + (clip[2] - clip[0]),
             2 * clip[1] + (clip[3] - clip[1]))
    matched = _matched_sheet(sheet, wanted)
    if matched is None:
        return None, (f'the implied sheet {sheet[0]:.1f}x{sheet[1]:.1f}pt is '
                      f'not the media asked for')
    sheet = matched
    if _boxes_near(_size(media) + _size(media), sheet + sheet,
                   PLACEMENT_TOLERANCE):
        return None, None               # already the right size: nothing wrong
    return {'page': num, 'form': form_num, 'media': media, 'crop': crop_box,
            'bbox': bbox, 'matrix': (mx, my), 'clip': clip, 'scale': scale,
            'sheet': sheet, 'error': error}, None


def plan_placement(data, msg):
    """Every page that needs re-placing, or why none does.

    Returns (plans, notes). `notes` holds a line for each page that was the
    import idiom and was still not repaired -- the near misses are the evidence
    worth having, so they are reported rather than dropped.
    """
    if re.search(rb'/Type\s*/ObjStm\b', data) or re.search(rb'/Type\s*/XRef\b',
                                                           data):
        # Objects inside an object stream are not byte-addressable, so the
        # rewrite below could not reach them and would silently do nothing.
        raise NotPlaced('the file uses object or cross-reference streams')
    index = _object_index(data)
    if not index:
        raise NotPlaced('no objects could be indexed')
    wanted = ticket_sheet(msg) if msg is not None else None
    pages = _pages(data, index)
    plans, notes, used = [], [], set()
    for num, body, inherited in pages:
        plan, why = _page_plan(data, index, num, body, inherited, wanted, used)
        if plan is not None:
            used.add(plan['form'])
            plans.append(plan)
        elif why:
            notes.append(f'page object {num}: {why}')
    if not plans:
        return [], notes
    if len(plans) != len(pages):
        # All or nothing. A file where only some pages match is a file this
        # does not understand, and half a repair is the worst of both.
        notes.append(f'{len(plans)} of {len(pages)} pages matched; '
                     f'a mixed document is left alone')
        return [], notes
    sheets = {(round(p['sheet'][0], 1), round(p['sheet'][1], 1)) for p in plans}
    if len(sheets) != 1:
        notes.append('the pages imply different sheets')
        return [], notes
    return plans, notes


def _trailer(data):
    i = data.rfind(b'trailer')
    if i < 0:
        raise NotPlaced('no trailer: not a plain cross-reference file')
    j = data.find(b'<<', i)
    out = _balanced_dict(data, j) if j >= 0 else b''
    if not out:
        raise NotPlaced('unterminated trailer')
    return out


def _incremental_update(data, replacements):
    """Append new copies of whole objects, leaving every original byte alone.

    An object is the smallest thing a cross-reference table can address, so a
    replacement is the whole object; and appending means the file we hand on
    still contains, byte for byte, the file we were given.
    """
    old = _trailer(data)
    m = re.search(rb'startxref\s+(\d+)\s*%%EOF\s*$', data)
    if not m:
        raise NotPlaced('no startxref at the end of the file')
    root = _raw_value(old, b'Root')
    size = _raw_value(old, b'Size')
    if root is None or size is None:
        raise NotPlaced('the trailer names no /Root or /Size')
    out = bytearray(data)
    if not out.endswith(b'\n'):
        out += b'\n'
    offsets = {}
    for num in sorted(replacements):
        offsets[num] = len(out)
        out += b'%d 0 obj\n' % num + replacements[num].strip() + b'\nendobj\n'
    at = len(out)
    out += b'xref\n'
    nums = sorted(offsets)
    runs, run = [], [nums[0]]
    for n in nums[1:]:
        if n == run[-1] + 1:
            run.append(n)
        else:
            runs.append(run)
            run = [n]
    runs.append(run)
    for run in runs:
        out += b'%d %d\n' % (run[0], len(run))
        for n in run:
            out += b'%010d 00000 n \n' % offsets[n]
    parts = [b'/Size ' + size, b'/Root ' + root, b'/Prev %d' % int(m.group(1))]
    for key in (b'Info', b'ID', b'Encrypt'):
        value = _raw_value(old, key)
        if value is not None:
            parts.append(b'/' + key + b' ' + value)
    out += (b'trailer\n<< ' + b' '.join(parts) + b' >>\nstartxref\n%d\n%%%%EOF\n'
            % at)
    return bytes(out)


def _fmt(value):
    """A number written the way PDF wants it: no exponent, no trailing zeros."""
    text = f'{value:.5f}'.rstrip('0').rstrip('.')
    return (text or '0').encode()


def _write_box(body, key, box, add_if_missing):
    """Set one rectangle in a dictionary body, adding the key if asked."""
    written = (b'/' + key + b' [ ' + b' '.join(_fmt(v) for v in box) + b' ]')
    m = re.search(rb'/' + key + rb'(?![A-Za-z0-9])\s*\[[^\]]*\]', body)
    if m:
        return body[:m.start()] + written + body[m.end():]
    if not add_if_missing:
        return body
    at = body.find(b'<<')
    if at < 0:
        raise NotPlaced(f'no dictionary to write /{key.decode()} into')
    return body[:at + 2] + b' ' + written + body[at + 2:]


def _visible(bbox, clip, page, offset):
    """The part of the form's own space that reaches paper.

    Everything that decides this is a rectangle: the form's /BBox and the clip
    the content opens with, both in form space, and the page box, which is in
    user space and so arrives here shifted by the form's /Matrix. Intersecting
    them is exact, which is worth more than sampling it with a renderer -- and
    this daemon has no renderer to sample it with, conversion being another
    unit's job entirely.
    """
    shifted = (page[0] - offset[0], page[1] - offset[1],
               page[2] - offset[0], page[3] - offset[1])
    both = _intersect(bbox, clip)
    return _intersect(both, shifted) if both else None


def repair_placement(data, msg):
    """Put every re-placed page back where its own producer meant to put it.

    Returns (document, note, notes). `document` is the original unless every
    check below passed, because a job that prints the old way beats one that
    prints a new way nobody has looked at.
    """
    plans, notes = plan_placement(data, msg)
    if not plans:
        return data, None, notes
    sheet = plans[0]['sheet']
    box = (0.0, 0.0, sheet[0], sheet[1])
    replacements, index = {}, _object_index(data)
    for plan in plans:
        start = index[plan['page']]
        end = data.find(b'endobj', start)
        body = data[start:end]
        body = _write_box(body, b'MediaBox', box, add_if_missing=True)
        if plan['crop'] is not None:
            body = _write_box(body, b'CropBox', box, add_if_missing=False)
        replacements[plan['page']] = body
        start = index[plan['form']]
        end = data.find(b'endobj', start)
        form = data[start:end]
        # /BBox becomes the producer's own clip rather than the whole sheet.
        # The two render identically, because the clip is inside the sheet --
        # but this way nothing the form can paint escapes the region its
        # producer already chose, whatever the rest of the stream does with
        # q and Q. The no-reveal property stops being an argument about the
        # content and becomes a property of the file.
        form = _write_box(form, b'BBox', plan['clip'], add_if_missing=False)
        # Any residue between /Matrix and /BBox's corner was a placement
        # somebody meant; it is kept rather than rounded away.
        residual = (plan['matrix'][0] + plan['bbox'][0],
                    plan['matrix'][1] + plan['bbox'][1])
        written = (b'/Matrix [ 1 0 0 1 ' + _fmt(residual[0]) + b' '
                   + _fmt(residual[1]) + b' ]')
        m = re.search(rb'/Matrix(?![A-Za-z0-9])\s*\[[^\]]*\]', form)
        if not m:
            raise NotPlaced('the form lost its /Matrix')
        replacements[plan['form']] = form[:m.start()] + written + form[m.end():]
        plan['residual'] = residual

    out = _incremental_update(data, replacements)

    # Everything from here is the check that this did not make the job worse.
    # It runs on the bytes about to be sent, not on the intention behind them.
    if not out.startswith(data):
        return data, None, notes + ['the rewrite did not leave the original '
                                    'bytes intact']
    for plan in plans:
        before = _visible(plan['bbox'], plan['clip'], plan['media'],
                          plan['matrix'])
        after = _visible(plan['clip'], plan['clip'], box, plan['residual'])
        if after is None:
            return data, None, notes + ['the repaired page would show nothing']
        if before is not None and not _contains(after, before):
            return data, None, notes + ['the repaired page would lose part of '
                                        'what prints today']
        if _area(after) <= _area(before) + 1.0:
            return data, None, notes + ['the repair would recover nothing']
        if not _contains(box, plan['clip']):
            return data, None, notes + ['the repaired content would still run '
                                        'off the sheet']
    try:
        again, _ = plan_placement(out, msg)
    except NotPlaced as exc:
        return data, None, notes + [f'the repaired document no longer parses '
                                    f'({exc})']
    if again:
        return data, None, notes + ['the repair did not settle']

    first = plans[0]
    note = (f'{len(plans)} page(s) placed off the sheet by their own producer; '
            f'page box {_size(first["media"])[0]:.0f}x'
            f'{_size(first["media"])[1]:.0f}pt restored to the '
            f'{sheet[0]:.0f}x{sheet[1]:.0f}pt sheet the fit was computed for, '
            f'moving content {abs(first["matrix"][0]):.0f}pt right and '
            f'{abs(first["matrix"][1]):.0f}pt up (/BBox off by '
            f'{first["error"]:.2f}pt)')
    return out, note, notes


def report_placement(cfg, queue, msg, data, fmt, note, notes):
    """Mail what was found, whether or not anything was changed.

    A document that reaches this code at all is one whose producer made two
    placement decisions and applied both. That is rare enough to be worth a
    message every time -- both when it was repaired, so the repair can be
    checked against the sheet that comes out, and when it was not, because a
    near miss is a sample of a shape nobody has seen yet.
    """
    if not cfg.alerter:
        return
    name = queue.name if queue else 'a queue'
    group = msg.operation() if msg is not None else None
    job = (group.get_str('job-name') if group else None) or '(unnamed)'
    lines = [f'A job on {name} was placed on the sheet by its sender in a way',
             'that needed looking at.',
             '',
             f'  job          {job}',
             f'  format       {fmt or "unknown"}',
             f'  bytes        {len(data)}',
             f'  media asked  {ticket_sheet(msg) or "not stated by the client"}',
             '']
    if note:
        lines += ['  REPAIRED', f'    {note}', '',
                  '  The page went to the printer re-placed. Compare the sheet',
                  '  with what the client previewed: they should now agree. If',
                  '  they do not, set page-geometry=raw on the printer URI and',
                  '  keep the attached document.']
    else:
        lines += ['  LEFT ALONE',
                  '    The document has the shape a misplaced page has, and',
                  '    one of the checks said no. It went to the printer',
                  '    exactly as it arrived.']
    for line in notes:
        lines.append(f'    - {line}')
    lines += ['', 'What this is:', '',
              '  A print path that imports a page and then fits it to the',
              '  paper can end up applying both placements at once. The page',
              '  then declares one size while its content was arranged for',
              '  another, and the printer believes the declaration.',
              '  See ippfix(8), page-geometry, and DIAGNOSING.md.', '']
    lines += ['The document itself:', '']
    try:
        lines += ['  ' + line for line in describe_document(data, fmt)]
    except Exception:                        # a report must not fail on this
        lines += ['  (could not be described)']
    parts = []
    if cfg.archive:
        parts.append((f'{queue.slug if queue else "job"}-placement.pdf',
                      'application/pdf', data))
    else:
        lines += ['', '  --archive is off, so the document could not be',
                  '  attached. Turning it on keeps the next one.']
    verdict = 'repaired' if note else 'left alone'
    cfg.alerter.send(f'ippfix: page placement {verdict} on {name}',
                     '\n'.join(lines) + '\n', parts)


def place_pages(cfg, queue, msg, data, fmt):
    """Put a misplaced page back, or hand the document on untouched.

    Returns (document, note). The note is None whenever nothing was changed,
    which is every job but the rare one -- and the rare one is reported.
    """
    if queue is not None and not queue.repair_placement:
        return data, None
    payload = normalise_pdf(data)
    if payload is None:
        return data, None
    try:
        out, note, notes = repair_placement(payload, msg)
    except NotPlaced:
        # Not a file this can reason about -- an object-stream PDF, or one with
        # no plain trailer. Silent on purpose: that describes a great many
        # ordinary documents, and a report about each would bury the ones that
        # mean something.
        return data, None
    except Exception:
        # The document came from somebody else. A bug in reading it must cost
        # the job nothing.
        log.exception('page placement could not be checked; job relayed as is')
        return data, None
    if note is None and not notes:
        return data, None
    name = queue.name if queue else 'job'
    if note:
        log.info('%s: %s', name, note)
    for line in notes:
        log.info('%s: page placement left alone: %s', name, line)
    report_placement(cfg, queue, msg, data, fmt, note, notes)
    return (out, note) if note else (data, None)


def archive_document(cfg, queue, job_name, fmt, data, note):
    """Keep a copy of a job as it arrived, for diagnosing a failure.

    This writes users' documents to disk, so it is off by default and the
    directory is created private to the service account. It exists because
    the failure being worked around is silent and content-dependent: without
    the document that provoked it there is very little to go on.

    Turn it off again once the question is answered.

    Returns the path written, so that a job which then fails to print can be
    reported with the document that provoked it attached.
    """
    if not cfg.archive:
        return None
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
        return path
    except OSError as exc:
        log.warning('could not archive job: %s', exc)
        return None


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

    def __init__(self, address, max_per_hour, sender=None, spool=None):
        host = socket.getfqdn()
        self.address = address if '@' in address else f'{address}@{host}'
        self.max_per_hour = max_per_hour
        self.spool = spool
        # From: is the recipient. An address that was configured to receive
        # these is known to route and known to be deliverable, which is more
        # than can be said for ippfix@ plus whatever this host calls itself --
        # a name that is frequently internal, and which turns a bounce or a
        # reply into a second thing that goes missing silently.
        self.sender = sender or self.address
        self.sent = []                      # monotonic timestamps
        self.suppressed = 0
        self.lock = threading.Lock()

    def _allow(self):
        """How many were suppressed before this one, or None to stay silent."""
        now = time.monotonic()
        with self.lock:
            self.sent = [t for t in self.sent if now - t < 3600]
            if len(self.sent) >= self.max_per_hour:
                self.suppressed += 1
                return None
            self.sent.append(now)
            held, self.suppressed = self.suppressed, 0
        # Returned rather than stashed on self: several watcher threads can be
        # in here at once, and a count attached to the wrong message is a number
        # somebody would believe.
        return held

    def send(self, subject, body, attachments=()):
        held = self._allow()
        if held is None:
            log.warning('alert suppressed (%d in the last hour already): %s',
                        self.max_per_hour, subject)
            return
        if held:
            body += (f'\n{held} further alert(s) were suppressed by the rate '
                     f'limit before this one. Raise --alert-max-per-hour, or '
                     f'treat the rate itself as the finding.\n')
        host = socket.getfqdn()
        to = self.address
        message = email.message.EmailMessage()
        message['From'] = f'ippfix <{self.sender}>'
        message['To'] = to
        message['Subject'] = subject
        message['Date'] = email.utils.formatdate(localtime=True)
        message['Message-ID'] = email.utils.make_msgid(domain=host)
        message['Auto-Submitted'] = 'auto-generated'
        message.set_content(body)
        for name, mimetype, payload in attachments:
            maintype, _, subtype = mimetype.partition('/')
            message.add_attachment(payload, maintype=maintype,
                                   subtype=subtype or 'octet-stream',
                                   filename=name)
        raw = message.as_bytes()

        # Handing the message to a spool directory rather than to sendmail(1).
        # This daemon is the most exposed thing here, and delivery needs the
        # mail group -- which would also grant it the mail system's credential
        # file. A separate unit picks messages up, exactly as document
        # conversion is a separate unit for the same reason.
        if self.spool:
            try:
                os.makedirs(self.spool, mode=0o700, exist_ok=True)
                path = os.path.join(
                    self.spool, f'{int(time.time())}-{os.getpid()}-{id(raw):x}.mail')
                fd = os.open(path + '.part',
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as fh:
                    fh.write(raw)
                os.rename(path + '.part', path)   # appears complete or not at all
                log.info('alert queued for %s: %s', to, subject)
                return
            except OSError as exc:
                log.error('could not queue the alert (%s)', exc)

        # No spool: running by hand, or an install that has no helper. Try to
        # deliver directly, which works wherever this is not confined.
        try:
            proc = subprocess.run(
                ['/usr/sbin/sendmail', '-f', self.sender, '-t', '-i'],
                input=raw, capture_output=True, timeout=60)
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


# RFC 8011 printer-state. Three numbers nobody remembers, in a report meant to
# be read once, quickly, by somebody who has just been told printing is broken.
PRINTER_STATES = {3: 'idle', 4: 'processing', 5: 'stopped'}

PRINTER_FACTS = (
    'printer-make-and-model', 'printer-firmware-string-version',
    'printer-state', 'printer-state-reasons', 'printer-state-message',
    'printer-up-time', 'printer-alert', 'printer-alert-description',
    'marker-names', 'marker-levels', 'marker-types',
)


def clamp_supply_levels(group):
    """Stop a printer that says "almost empty" from also saying "empty".

    A supply level below the printer's own `marker-low-levels` mark is the
    printer calling that cartridge empty. Some firmware reports exactly that
    while, in the same message, warning only of *low* toner, staying idle and
    continuing to accept jobs. The M283fdw this proxy was built for has read 0%
    on all three colour supplies for months while printing perfectly well.

    That contradiction is not academic. A client that reads the reason keeps
    working; a client that believes the number decides three cartridges are
    empty and refuses to submit anything at all. It is why the same printer
    prints from ChromeOS and returns "printer error" on Android, with the
    proxy never seeing so much as a Validate-Job.

    So where the printer contradicts itself, believe the half it is acting on.
    A level it would call empty is reported as the lowest level it would not,
    which is its own low-water mark. Nothing here is invented: the client still
    sees a low supply, still sees the warning, and the number it sees is still
    the printer's. The moment the printer says a supply is genuinely empty it
    has stopped contradicting itself, and the real levels go through untouched.

    Returns a description of what was corrected, or None if nothing was.
    """
    levels = group.get('marker-levels')
    lows = group.get('marker-low-levels')
    if not levels or not lows or len(levels) != len(lows):
        return None
    # An explicit empty condition is the printer agreeing with its own gauge.
    # Believe it. RFC 8011 spells these `...-empty-warning`/`-error`, so the
    # stem is what matters, not the severity.
    reasons = [r.decode('utf-8', 'replace').lower()
               for r in (group.get('printer-state-reasons') or [])]
    if any('empty' in r for r in reasons):
        return None
    try:
        have = [int.from_bytes(v, 'big', signed=True) for v in levels]
        marks = [int.from_bytes(v, 'big', signed=True) for v in lows]
    except (TypeError, ValueError):
        return None
    names = [n.decode('utf-8', 'replace')
             for n in (group.get('marker-names') or [])]

    fixed, corrected = [], []
    for i, (level, mark) in enumerate(zip(have, marks)):
        # A negative level is "unknown" (RFC 8011 uses -1, -2 and -3). A
        # printer admitting it does not know is not contradicting itself, and
        # a mark outside 1..100 is not a threshold worth trusting either.
        if 0 <= level < mark <= 100:
            fixed.append(mark)
            corrected.append(names[i] if i < len(names) else f'supply {i + 1}')
        else:
            fixed.append(level)
    if not corrected:
        return None
    group.replace('marker-levels', ipp.TAG_INTEGER, [ipp.i32(v) for v in fixed])
    return (f"{', '.join(corrected)} reported below the printer's own low mark "
            f"while it warns only of low toner; reported at that mark instead, "
            f"so clients do not read the supply as empty")


def printer_snapshot(queue, timeout=20):
    """What the printer says about itself, right after a job went wrong.

    Worth having in the report because several of these are alternative
    explanations for a page that came out blank -- a cartridge at zero prints
    nothing and blames nobody -- and because firmware version is the first
    thing anyone asks when a fault is model specific.
    """
    try:
        req = ipp.new_request(0x000B, 3, queue.upstream_uri())
        req.operation().replace('requested-attributes', ipp.TAG_KEYWORD,
                                list(PRINTER_FACTS))
        status, raw = upstream_ipp(queue, ipp.serialize(req), timeout)
        if status != 200:
            raise OSError(f'HTTP {status}')
        group = ipp.parse(raw).group(ipp.PRINTER_ATTRS)
        if group is None:
            raise ValueError('no printer attributes')
    except Exception as exc:
        return [f'(the printer did not answer: {exc})']

    out = []
    for name in PRINTER_FACTS:
        values = group.get(name)
        if not values:
            continue
        shown = []
        for v in values:
            if len(v) == 4 and name.endswith(('-state', '-levels', '-time')):
                n = struct.unpack('>i', v)[0]
                shown.append(f'{n} ({PRINTER_STATES[n]})'
                             if name == 'printer-state' and n in PRINTER_STATES
                             else str(n))
            else:
                # Collapse whitespace. Printers put newlines in status strings
                # -- an HP M430 answers its console text as "Bereitschafts-\n
                # modus ein" -- and this goes into a mailed report where a line
                # break would forge a line of it.
                shown.append(' '.join(
                    v.decode('utf-8', 'replace').replace('\x00', '').split()
                )[:120])
        text = ', '.join(x for x in shown if x)
        if text:
            out.append(f'{name}: {text}')
    return out or ['(the printer answered, but said nothing useful)']


def gather_evidence(cfg, archived, data, note, budget):
    """Attach what somebody would need to reproduce this, within a size limit.

    Two documents matter and they are not the same one: what the client sent,
    and what this proxy handed the printer. A fault that survives conversion is
    a different bug from one that conversion introduced, and only having both
    tells them apart. The original is only available when --archive is on;
    without it the report can describe the document but not hand it over.

    Attachments go out raw so they can be fed straight back to the tools the
    report names. Compression is a fallback used to make something fit, not a
    default: a gzipped PDF is one more step between a report and an answer.
    """
    def attach(name, payload):
        nonlocal budget
        if len(payload) <= budget:
            budget -= len(payload)
            kind = 'application/pdf' if payload.startswith(b'%PDF-') \
                else 'application/octet-stream'
            return (name, kind, payload), f'{name} ({len(payload):,} bytes)'
        squeezed = gzip.compress(payload, 6)
        if len(squeezed) <= budget:
            budget -= len(squeezed)
            return ((name + '.gz', 'application/gzip', squeezed),
                    f'{name}.gz ({len(squeezed):,} bytes, '
                    f'{len(payload):,} uncompressed)')
        return None, (f'{name} NOT attached: {len(payload):,} bytes exceeds '
                      f'--alert-max-attachment')

    original = None
    if archived:
        try:
            with open(archived, 'rb') as fh:
                original = fh.read()
        except OSError as exc:
            log.warning('could not read %s back for the report: %s',
                        archived, exc)

    stem = os.path.basename(archived) if archived else 'document'
    stem = re.sub(r'\.(pdf|bin)$', '', stem)
    parts, lines = [], []
    if original is None:
        lines.append('  nothing from the client: '
                     + ('--archive is off, so the document as it arrived was '
                        'not kept' if not cfg.archive else
                        'the archived copy could not be read back'))
    elif original == data:
        part, said = attach(f'{stem}.pdf' if original.startswith(b'%PDF-')
                            else f'{stem}.bin', original)
        lines.append('  ' + said + '  -- sent to the printer unchanged')
        if part:
            parts.append(part)
    else:
        part, said = attach(f'{stem}-as-sent-by-client.pdf'
                            if original.startswith(b'%PDF-')
                            else f'{stem}-as-sent-by-client.bin', original)
        lines.append('  ' + said + '  -- what the client sent')
        if part:
            parts.append(part)

    if original is None or original != data:
        ext = 'pdf' if data.startswith(b'%PDF-') else 'bin'
        part, said = attach(f'{stem}-as-given-to-printer.{ext}', data)
        lines.append('  ' + said + '  -- what this proxy handed the printer'
                     + (f' ({note})' if note else ''))
        if part:
            parts.append(part)
    return parts, lines


# ---------------------------------------------------------------------------
# The printer's own page counter.
#
# job-impressions-completed comes from the same firmware that just claimed to
# have printed a job it did not print. The RFC 3805 page counter comes from the
# marking engine, which is a different subsystem and much harder to lie with:
# it is the number the meter reads and the number a service contract bills on.
# When the two disagree, the page counter is the one to believe.
#
# It is not free of doubt either. Some printers do not answer SNMP; some answer
# this OID with something else entirely; some reset it when a cartridge is
# replaced. So it is checked before it is trusted, and it is checked again on
# every job -- see PageCounter, which can revoke its own trust.
# ---------------------------------------------------------------------------
OID_PAGE_COUNT = '1.3.6.1.2.1.43.10.2.1.4.1.1'      # prtMarkerLifeCount
OID_COUNTER_UNIT = '1.3.6.1.2.1.43.10.2.1.3.1.1'    # prtMarkerCounterUnit

# prtMarkerCounterUnit, RFC 3805. Only two of the legal values describe
# something comparable with job-impressions-completed. The rest -- characters,
# lines, micrometres, feet -- are correct answers to this OID and useless ones
# here, and asking is much better than assuming: the printer says outright
# whether its counter can be read as a page count.
COUNTER_UNITS = {7: 'impressions', 8: 'sheets'}

COUNTER32 = 1 << 32
# A single job that moves the counter by more than this is not counting pages.
# Real jobs are bounded by the paper in the tray; a byte counter, a timer or an
# uptime is not. Deliberately generous: a busy shared printer can advance a
# long way between two readings through nobody's fault, because this proxy is
# not the only way to print. That is why a large jump is treated as evidence
# about the OID rather than about the job.
MAX_PLAUSIBLE_JUMP = 20000
# One job whose impressions never reached the page counter is the finding this
# signal exists to surface. Three of them means the instrument is broken, not
# the printer.
FROZEN_LIMIT = 3
# Consecutive unanswered reads before giving up on a printer that answered once.
MISS_LIMIT = 3
# Let the engine finish marking before reading the counter again. A job reaches
# job-state=completed slightly before the last sheet is counted.
SETTLE = 8


# ---------------------------------------------------------------------------
# Relaying SNMP to the printer.
#
# The printer often sits where clients cannot reach it -- that is why this
# proxy exists -- which also puts its status information out of reach. The
# supply levels and state that a print client actually uses already travel over
# IPP and are relayed with everything else; this covers the rest, for a person
# with snmpget who wants the page counter or the console text.
#
# What it will not do is the point of it:
#
#   * GET and GETNEXT only. GETBULK is refused, because max-repetitions is the
#     knob that turns a small request into a large reply, and that is what
#     makes an open SNMP responder useful for reflection. GET and GETNEXT
#     answer one varbind each, so the reply is about the size of the request.
#   * SET is refused. The Printer MIB has writable objects, including a reset.
#   * Only inside a small list of subtrees, checked on the way out as well as
#     on the way in: a GETNEXT at the end of a subtree walks into the next one,
#     and the answer is dropped rather than relayed.
#   * Rate limited per source and overall, and over the limit it answers
#     nothing at all. Answering is the amplification; refusing costs a packet.
#
# None of that defends against somebody on the path, and none of it needs to:
# a printer that answers SNMP on a LAN is already in that position. What this
# does avoid is putting the printer in front of a *wider* audience than the
# administrator chose, which is the one thing the proxy changes.
# ---------------------------------------------------------------------------
RELAY_SUBTREES = (
    '1.3.6.1.2.1.1.',          # system: sysDescr, sysName, sysUpTime
    '1.3.6.1.2.1.25.3.2.',     # hrDeviceTable
    '1.3.6.1.2.1.25.3.5.',     # hrPrinterTable: status and detected errors
    '1.3.6.1.2.1.43.',         # the Printer MIB, RFC 3805
)
RELAY_PDUS = (snmp.GET, snmp.GETNEXT)
RELAY_MAX_VARBINDS = 8         # snmpget takes several; nothing legitimate needs more
RELAY_TIMEOUT = 2              # the forward blocks this loop; keep it short


def client_ip(peer):
    """The source address of a datagram, as somebody would write it.

    A dual-stack socket reports an IPv4 peer as ::ffff:10.0.0.1, which is the
    same host by a name that does not match an IPv4 network in --snmp-allow and
    does not match what an administrator typed. Unmap it once, here, so both
    the filter and the log see the address the operator is thinking of.
    """
    ip = peer[0]
    try:
        # Only IPv6Address carries ipv4_mapped; an address that is already v4
        # has nothing to unmap.
        mapped = getattr(ipaddress.ip_address(ip), 'ipv4_mapped', None)
    except ValueError:
        return ip
    return str(mapped) if mapped else ip


def in_allowlist(oid):
    return any(oid.startswith(t) or oid == t.rstrip('.')
               for t in RELAY_SUBTREES)


class TokenBucket:
    """Rate limit with a burst allowance, in the smallest form that works."""

    def __init__(self, rate, burst):
        self.rate = float(rate)
        self.burst = float(burst)
        self.tokens = float(burst)
        self.when = time.monotonic()

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.when) * self.rate)
        self.when = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True


class SnmpRelay:
    """A read-only SNMP window onto one printer."""

    PER_SOURCE_RATE, PER_SOURCE_BURST = 5, 10
    OVERALL_RATE, OVERALL_BURST = 20, 40
    MAX_SOURCES = 512          # bounded: one packet per spoofed source otherwise
    SOURCE_IDLE = 300

    def __init__(self, queue, allow=()):
        self.queue = queue
        self.allow = list(allow)
        self.overall = TokenBucket(self.OVERALL_RATE, self.OVERALL_BURST)
        self.sources = {}
        self.lock = threading.Lock()
        self.refused = 0
        self.relayed = 0

    def permitted_source(self, ip):
        if not self.allow:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.allow)

    def allowed_rate(self, ip):
        """Both buckets, and neither is allowed to grow without bound."""
        with self.lock:
            if not self.overall.take():
                return False
            now = time.monotonic()
            if len(self.sources) > self.MAX_SOURCES:
                for key, (bucket, seen) in list(self.sources.items()):
                    if now - seen > self.SOURCE_IDLE:
                        del self.sources[key]
                if len(self.sources) > self.MAX_SOURCES:
                    # Still full: somebody is walking source addresses. Serve
                    # only the sources already known rather than evicting one
                    # of them to make room for the flood.
                    if ip not in self.sources:
                        return False
            bucket, _seen = self.sources.get(
                ip, (TokenBucket(self.PER_SOURCE_RATE, self.PER_SOURCE_BURST), 0))
            self.sources[ip] = (bucket, now)
            return bucket.take()

    def acceptable(self, msg):
        """Whether this request may be put to the printer at all."""
        if msg.version not in (snmp.V1, snmp.V2C):
            return 'version %s' % msg.version
        if msg.pdu_type not in RELAY_PDUS:
            return snmp.PDU_NAMES.get(msg.pdu_type, hex(msg.pdu_type))
        if not msg.varbinds or len(msg.varbinds) > RELAY_MAX_VARBINDS:
            return '%d varbinds' % len(msg.varbinds)
        for oid in msg.oids:
            if not in_allowlist(oid):
                return 'oid %s' % oid
        return None

    def forward(self, packet):
        """Put the client's datagram to the printer, verbatim.

        Verbatim on purpose. Re-encoding would mean writing an encoder whose
        bugs are reachable from the network, to gain nothing: the bytes are
        already a valid request for exactly what was asked, and the policy
        decision was made from the parse, not from the re-encoding.
        """
        sock = socket.socket(socket.AF_INET6 if ':' in self.queue.host
                             else socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(RELAY_TIMEOUT)
        try:
            sock.connect((self.queue.host, 161))
            sock.send(packet)
            return sock.recv(snmp.MAX_DATAGRAM)
        except OSError:
            return None
        finally:
            sock.close()

    def handle(self, packet, peer):
        """One datagram in, at most one datagram out."""
        ip = client_ip(peer)
        if not self.permitted_source(ip):
            return None
        try:
            msg = snmp.parse(packet)
        except snmp.SnmpError as exc:
            log.debug('snmp relay: unparseable request from %s (%s)', ip, exc)
            return None
        refusal = self.acceptable(msg)
        if refusal:
            self.refused += 1
            log.info('snmp relay: refused %s from %s', refusal, ip)
            return None
        if not self.allowed_rate(ip):
            self.refused += 1
            return None

        reply = self.forward(packet)
        if reply is None:
            return None
        try:
            parsed = snmp.parse(reply)
        except snmp.SnmpError:
            return None
        if parsed.request_id != msg.request_id:
            return None
        # A GETNEXT at the end of a subtree answers with the next object in the
        # printer's MIB, which is outside what this agreed to serve. Drop it:
        # the walk stops at the boundary, which is what a boundary is for.
        for oid in parsed.oids:
            if not in_allowlist(oid):
                log.debug('snmp relay: not relaying %s, outside the allowlist',
                          oid)
                return None
        self.relayed += 1
        return reply

    def serve(self, sock):
        """Read datagrams until the socket goes away."""
        log.info('snmp relay: answering for %s on behalf of %s',
                 self.queue.name, self.queue.host)
        while True:
            try:
                packet, peer = sock.recvfrom(snmp.MAX_DATAGRAM)
            except OSError as exc:
                log.warning('snmp relay: stopped (%s)', exc)
                return
            try:
                reply = self.handle(packet, peer)
                if reply is not None:
                    sock.sendto(reply, peer)
            except Exception as exc:
                # One bad datagram must never take the relay down with it.
                log.error('snmp relay: while answering %s: %s', peer[0], exc)


WILDCARD_BINDS = ('::', '0.0.0.0', '')


def bound_address(sock):
    """The address a socket is listening on, or None if it is the wildcard.

    This is how a listener is matched to a printer. Asking the socket beats any
    naming convention: whatever the administrator called the unit, the address
    it actually bound is the thing that has to agree with the configuration.
    """
    try:
        addr = sock.getsockname()[0]
    except OSError:
        return None
    if addr in WILDCARD_BINDS:
        return None
    return client_ip((addr,))


def choose_relay_queue(queues, address=None):
    """Which printer a listener speaks for, or None with a reason.

    SNMP carries nothing that names a printer -- no virtual hosts, no
    equivalent of an HTTP Host header -- so one listener can only ever speak
    for one of them, and a monitoring system silently reading the wrong
    printer's page counter is worse than one that reads nothing.

    Which leaves addresses. A host with an alias per printer can run a listener
    per address, and then the address does the naming that the protocol will
    not. That is what `?snmp-relay=ADDRESS` configures, and it is the only way
    to serve several printers at once.
    """
    if address is not None:
        named = [q for q in queues if q.snmp_relay == address]
        if len(named) == 1:
            return named[0], None
        if named:
            return None, (f'{len(named)} printers claim the listener on '
                          f'{address}')
        return None, (f'nothing is listening for a printer on {address}; add '
                      f'"?snmp-relay={address}" to the URI of the printer it '
                      f'should answer for')

    # The wildcard listener. Printers that named an address are served by their
    # own listener and must not also be served by this one.
    unbound = [q for q in queues if not isinstance(q.snmp_relay, str)]
    marked = [q for q in unbound if q.snmp_relay is True]
    if len(marked) > 1:
        return None, 'several printers are marked "?snmp-relay=on"'
    if marked:
        return marked[0], None
    eligible = [q for q in unbound if q.snmp_relay is None]
    if len(eligible) == 1:
        return eligible[0], None
    if not eligible:
        return None, 'no printer is left for it'
    return None, (f'{len(eligible)} printers share this listener and SNMP '
                  f'cannot say which one a request is for. Mark one with '
                  f'"?snmp-relay=on", or give each its own address with '
                  f'"?snmp-relay=ADDRESS" and a socket unit bound to it')


def start_snmp_relays(cfg, socks):
    """Serve SNMP on each listener systemd passed, for the printer it names."""
    if not socks:
        return []
    if cfg.no_snmp_relay:
        log.info('snmp relay: disabled by --no-snmp-relay; the %d socket(s) '
                 'systemd passed will not be answered', len(socks))
        return []
    queues = list(cfg.queues.values())
    started = []
    for sock in socks:
        address = bound_address(sock)
        where = address or 'every address'
        queue, why = choose_relay_queue(queues, address)
        if queue is None:
            # Loud: the socket is open, so somebody meant this to work, and a
            # relay that is listening but answering nothing looks identical
            # from outside to one that is simply slow.
            log.error('snmp relay: NOT answering on %s -- %s', where, why)
            continue
        # A printer that does not answer SNMP has nothing to relay, and
        # standing up a responder for it would add a reflector to the network
        # in exchange for errors.
        if snmp.get(queue.host, OID_PAGE_COUNT, queue.community,
                    timeout=3) is None:
            log.info('snmp relay: not answering on %s -- %s does not answer '
                     'SNMP itself', where, queue.name)
            continue
        relay = SnmpRelay(queue, cfg.snmp_allow)
        threading.Thread(target=relay.serve, args=(sock,), daemon=True,
                         name=f'snmp-relay-{queue.slug}').start()
        started.append(relay)

    # A printer configured for an address nobody is listening on is a setting
    # that looks applied and is not.
    listening = {bound_address(s) for s in socks}
    for queue in queues:
        if isinstance(queue.snmp_relay, str) and queue.snmp_relay not in listening:
            log.error('snmp relay: %s expects a listener on %s and none was '
                      'passed. Add a socket unit with '
                      '"ListenDatagram=%s:161".',
                      queue.name, queue.snmp_relay, queue.snmp_relay)
    return started


def plural(n, word):
    return f'{n} {word}' if n == 1 else f'{n} {word}s'


class PageCounter:
    """The page counter for one printer, and a running opinion of it.

    Two levels of confidence, because they carry different consequences:

      trusted -- the printer answers, and says its counter counts impressions
                 or sheets. Enough to print the numbers in a report.
      proven  -- the counter has been seen to move in step with a job that
                 printed. Enough to let it contradict the printer's own job
                 accounting and raise an alert of its own.

    Nothing is persisted. Trust is established from what the printer states
    about itself, which is available immediately and does not need a history,
    and is revoked from behaviour, which does. That way this is useful on the
    first job after a restart without keeping a state file to go stale.
    """

    def __init__(self, queue, community='public', enabled=True):
        self.queue = queue
        self.community = community
        self.enabled = enabled
        self.unit = None
        self.probed = False
        self.moved = False           # seen to advance in step with a job
        self.misses = 0
        self.frozen = 0
        self.backwards = 0
        self.reason = None           # why it was switched off, if it was
        self.lock = threading.Lock()

    # -- state ------------------------------------------------------------
    @property
    def trusted(self):
        return self.enabled and self.probed and self.unit in COUNTER_UNITS

    @property
    def proven(self):
        return self.trusted and self.moved

    def disable(self, reason):
        """Stop using the counter, and say so where somebody will see it.

        Loudly, and once. A signal that silently degrades to nothing is worse
        than one that was never there: reports keep arriving, one of their
        arguments quietly stops meaning anything, and nobody knows which.
        """
        if not self.enabled:
            return
        self.enabled = False
        self.reason = reason
        log.error('%s: DISABLING the SNMP page-counter cross-check -- %s. '
                  'Reports will no longer corroborate impressions against the '
                  'printer\'s own counter. Silence this with '
                  '"?page-counter=off" on the printer URI once you have '
                  'decided it is expected.', self.queue.name, reason)

    # -- reading ----------------------------------------------------------
    def read(self, timeout=5):
        if not self.enabled:
            return None
        return snmp.get(self.queue.host, OID_PAGE_COUNT, self.community,
                        timeout=timeout)

    def probe(self, timeout=5):
        """Ask once whether this printer has a counter worth reading."""
        with self.lock:
            if self.probed or not self.enabled:
                return
            value = self.read(timeout)
            if value is None:
                self.enabled = False
                self.reason = 'the printer does not answer SNMP'
                log.info('%s: no SNMP page counter (%s); reports will rely on '
                         'the printer\'s own job accounting alone',
                         self.queue.name, self.reason)
                return
            if not isinstance(value, int):
                self.probed = True
                self.disable(f'the page-counter OID returned {value!r}, '
                             f'which is not a number')
                return
            unit = snmp.get(self.queue.host, OID_COUNTER_UNIT, self.community,
                            timeout=timeout)
            self.unit = unit if isinstance(unit, int) else None
            self.probed = True
            if self.unit not in COUNTER_UNITS:
                self.disable(f'the printer counts in units {self.unit!r}, '
                             f'which cannot be compared with impressions')
                return
            log.info('%s: SNMP page counter at %d %s', self.queue.name,
                     value, COUNTER_UNITS[self.unit])

    # -- judging ----------------------------------------------------------
    def delta(self, before, after):
        """How far the counter moved, or None if the move makes no sense."""
        if before is None or after is None:
            return None
        step = after - before
        if step >= 0:
            return step
        # Counter32 wrapping is legitimate and looks like a huge drop. A small
        # drop is not a wrap: it is a reset, which some printers do when a
        # cartridge is replaced, and which invalidates this reading but not
        # necessarily the counter.
        if before > COUNTER32 - MAX_PLAUSIBLE_JUMP:
            return step + COUNTER32
        return None

    def assess(self, before, after, impressions):
        """Judge one finished job, and the counter along with it.

        Called for every job that is followed, not only for failing ones: the
        counter can only earn or lose trust from jobs that worked.

        Returns (lines, contradicted) -- report text, and whether the counter
        says this job did not print despite the printer saying it did.
        """
        with self.lock:
            return self._assess(before, after, impressions)

    def _assess(self, before, after, impressions):
        # Snapshot before anything below can revoke it. Otherwise a job that
        # both contradicts the counter and exhausts its credibility reports
        # "not yet corroborated" about a counter that was corroborated right
        # up until this line.
        was_proven = self.proven
        if not self.enabled:
            why = self.reason or 'switched off'
            return [f'page counter: not used ({why})'], False
        if before is None or after is None:
            self.misses += 1
            if self.misses >= MISS_LIMIT:
                self.disable('the printer stopped answering SNMP')
            return ['page counter: could not be read for this job'], False
        self.misses = 0

        moved = self.delta(before, after)
        if moved is None:
            self.backwards += 1
            if self.backwards >= 2:
                self.disable(f'the counter went backwards more than once '
                             f'({before} then {after})')
            else:
                log.warning('%s: the page counter went backwards, %d then %d. '
                            'A cartridge change or a service reset does this; '
                            'once is not enough to stop believing it.',
                            self.queue.name, before, after)
            return [f'page counter: {before} then {after} -- went backwards, '
                    f'so this job cannot be judged by it'], False

        if moved > MAX_PLAUSIBLE_JUMP:
            self.disable(f'the counter jumped by {moved:,} during one job, '
                         f'which is not a page count')
            return [f'page counter: jumped {moved:,} ({before} to {after})'],\
                False

        unit = COUNTER_UNITS.get(self.unit, 'units')
        line = f'page counter: {before} to {after} ({moved:+d} {unit})'
        contradicted = False
        if impressions:
            if moved:
                self.moved = True       # the instrument works; now it counts
                self.frozen = 0
            else:
                self.frozen += 1
                if was_proven:
                    contradicted = True
                    line += (f'  -- the job reported {plural(impressions, "impression")}'
                             ' and the counter did not move at all')
                if self.frozen >= FROZEN_LIMIT:
                    # Having decided the instrument is broken, do not also act
                    # on its last reading.
                    contradicted = False
                    self.disable(
                        f'{self.frozen} jobs reported impressions without '
                        f'moving the counter, so it is not tracking this '
                        f'printer\'s output. ANY EARLIER REPORT THAT BLAMED '
                        f'THIS COUNTER WAS PROBABLY WRONG')
        elif moved:
            line += '  -- something was marked, though not necessarily this job'
        else:
            line += '  -- nothing was marked, by the printer\'s own count'
        if not was_proven:
            line += ('\n  (not yet corroborated: the counter has not been seen '
                     'to move for a job that printed)')
        return [line], contradicted


def watch_job(cfg, queue, job_id, jobname, fmt, data, note,
              archived=None):
    """Follow one job to its end and report if the printer marked nothing."""
    # Read the page counter before following the job. This happens just after
    # the client was answered, so the printer may in principle have started
    # marking already -- but the failure this matters most for marks nothing at
    # all, and no race can turn "never moved" into "moved".
    queue.pages.probe()
    before = queue.pages.read()

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
        # Keep what was learned. A reply that carries no job attributes -- the
        # printer has forgotten the job, or answered something unhelpful --
        # used to reset all three, so a job watched processing for ten minutes
        # was reported as NO ANSWER because the last poll before the deadline
        # came back empty. Only a genuine answer may change the verdict.
        if new_state is None and new_imp is None and not new_reasons:
            continue
        entry = (ALERT_TERMINAL.get(new_state, new_state), new_imp,
                 ','.join(new_reasons))
        if not history or history[-1] != entry:
            history.append(entry)
        if new_state is not None:
            state = new_state
        if new_imp is not None:
            impressions = new_imp
        if new_reasons:
            reasons = ','.join(new_reasons)
        if new_state in ALERT_TERMINAL:
            break

    if queue.pages.enabled and before is not None:
        time.sleep(SETTLE)          # the last sheet is counted after the job
    after = queue.pages.read()
    page_lines, contradicted = queue.pages.assess(before, after, impressions)

    # Judge. Only complain about things that are actually wrong: a job that
    # completed having marked pages is the ordinary case and says nothing --
    # unless the counter that is tied to the marking engine says otherwise,
    # which is the one case where the job accounting alone would have missed
    # the failure entirely.
    if state == 9 and impressions and not contradicted:
        return
    if contradicted:
        verdict = 'COUNTED BUT NOT PRINTED'
        detail = (f'the printer reported the job completed and claimed '
                  f'{impressions} impressions, but its own page counter did '
                  f'not move. Those two numbers come from different parts of '
                  f'the printer, and the page counter is the one tied to the '
                  f'marking engine -- so the job most likely did not print. '
                  f'Nothing above this proxy would ever have noticed.')
    elif state is None:
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
             f'impressions:  {impressions}']
    lines += [f'{x}' for x in page_lines]
    lines += ['', 'What the printer said, in order:']
    lines += [f'  {st} impressions={im} reasons={rs or "none"}'
              for st, im, rs in history]
    lines += ['', 'The document this proxy gave the printer, structurally:']
    lines += [f'  {x}' for x in describe_document(data, fmt)]
    lines += ['', 'The printer, asked just now:']
    lines += [f'  {x}' for x in printer_snapshot(queue)]

    parts, said = gather_evidence(cfg, archived, data, note,
                                  cfg.alert_max_attachment)
    lines += ['', 'Attached:'] + said
    lines += ['', 'To investigate:', '']
    if verdict in ('LOST SILENTLY', 'COUNTED BUT NOT PRINTED'):
        lines += [
            '  A job that the printer says succeeded but did not print is the',
            '  whole point of this proxy, so this is worth chasing.',
            '',
            '  1. Save the attachment. It is the document that provoked this,',
            '     and it is the one thing here that cannot be reconstructed',
            '     afterwards; everything else can be derived from it.',
            '  2. Check it for a malformed soft mask, a known cause that',
            '     conversion cannot repair:',
            '         python3 scripts/check-softmask.py FILE.pdf',
            '  3. Re-send it directly to the printer and through this proxy:',
            '         python3 scripts/probe-printer.py ipp://PRINTER/ipp/print FILE.pdf',
            '     If it fails both ways, conversion is not the answer for it.',
            '  4. See OPEN-QUESTIONS.md for the faults already known.']
        if archived:
            lines += ['',
                      f'  The same copy is on the server at {archived}, next to',
                      '  a .txt file recording the queue, job name and format.']
    else:
        lines += ['  See DIAGNOSING.md, and keep the document if you can.']
    if not cfg.archive:
        lines += ['',
                  '  --archive is off, so the document the client sent could',
                  '  not be attached. Turning it on captures the next one --',
                  '  but it stores what people print, so turn it off again',
                  '  afterwards.']
    cfg.alerter.send(f'ippfix: job {verdict.lower()} on {queue.name}',
                     '\n'.join(lines) + '\n', parts)


def maybe_watch(cfg, queue, reply, msg, fmt, data, note,
                archived=None):
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
            watch_job(cfg, queue, job_id, jobname, fmt, data, note,
                      archived)
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


def ipp_error(wfile, msg, code, text):
    """Answer an IPP request with an IPP status.

    The client asked in IPP, so it is told in IPP. Answering an IPP request
    with an HTTP status and a line of English means the print system sees a
    transport failure rather than a printer condition, and reports to the user
    accordingly -- or reports nothing, which is the silence this proxy exists
    to remove. The HTTP layer stays 200: in IPP the status lives in the body.
    """
    reply = ipp.Message(code=code, request_id=getattr(msg, 'request_id', 1))
    reply.groups.append(ipp.Group(ipp.OPERATION_ATTRS, [
        (ipp.TAG_CHARSET, b'attributes-charset', b'utf-8'),
        (ipp.TAG_LANGUAGE, b'attributes-natural-language', b'en-us'),
        (ipp.TAG_TEXT, b'status-message', text),
    ]))
    respond(wfile, '200 OK', 'application/ipp', ipp.serialize(reply))


def unreachable(wfile, queue, msg, opname, exc):
    """Answer a client when the printer cannot be reached."""
    log.warning('%s: %s failed, printer unreachable (%s)',
                queue.name, opname, exc)
    # 0x0502 is server-error-service-unavailable: the right answer for an
    # upstream that is not there, as distinct from one that refused the job.
    ipp_error(wfile, msg, 0x0502, b'the printer is not responding')


def converter_header(queue, cfg, sides=None, force_raster=False):
    """Tell the converter what this particular printer will accept.

    The converter runs with no network at all, deliberately, so it cannot ask
    the printer anything. Everything model-specific therefore travels with the
    document: which raster format and colour space to fall back to, at what
    resolution, and how large a PDF to hand over before rasterising instead. A
    converter that receives no header keeps its built-in defaults.

    `sides` is the client's own word, relayed so that the converter can put the
    right duplex byte in a raster stream. Measured on paper: for a URF document
    that byte decides, and the IPP `sides` attribute beside it does not -- a job
    sent as two-sided-long-edge with a stream declaring one-sided came back
    successful, reported two impressions, and produced two simplex sheets. So
    the converter has to be told, and nothing here may invent it: no `sides` in
    the job means no `sides` in the header.

    `force_raster` is how a document the printer has already refused is asked
    for again. It sends `raster=only`, which tells the converter to rasterise
    the input and not to outline it first.

    That is a field of its own rather than `maxpdf=0`, which looks like the
    same request and is not. The converter compares against `maxpdf` only after
    outlining, and only after three fail-safes that each hand back the ORIGINAL
    document and report success -- so a document that lost a shading on the way
    through would come back as the very PDF the printer has just refused, and
    the retry would have nothing to send. Those fail-safes are right for
    outlining, which can lose content; the raster is taken from the original
    input, so there is nothing there for them to protect. A converter too old
    to know the field ignores it and outlines as it always did, which this end
    detects and declines to resend -- see rasterise_after_refusal.
    """
    queue.learn()          # a no-op once it has succeeded; retries if it has not
    if not queue.raster_format:
        # Nothing to ask for: this printer takes no raster format we can make.
        # force_raster cannot be honoured either, and saying both `raster=none`
        # and `raster=only` would be a contradiction rather than a request.
        # rasterise_after_refusal refuses to get here for exactly that reason.
        fields = ['raster=none']
    else:
        fields = [f'device={queue.raster_device}',
                  f'colorspace={queue.raster_colorspace}',
                  f'dpi={queue.raster_dpi}']
        if force_raster:
            fields.append('raster=only')
    # What the printer declares it accepts is not used here; it was measured not
    # to be what the printer enforces (see Queue.learn), so a job is offered
    # whole and rasterised only if the printer actually refuses it -- so no
    # number is sent at all unless an administrator supplied one.
    #
    # This used to send MAX_CONVERTED as "the only honest number left". It was
    # not honest, it was inert: the converter would have rasterised a document
    # whose outlined form passed 256 MB, and raster runs about 3.8 times the
    # size of outlined output, so the result was another 970 MB that this proxy
    # would refuse in turn. A branch that can only fire where its own output is
    # unusable is not a fallback.
    if cfg.max_pdf_bytes:
        fields.append(f'maxpdf={cfg.max_pdf_bytes}')
    if sides is not None:
        if sides in SIDES_VALUES:
            fields.append(f'sides={sides}')
        else:
            # Neither substituted nor guessed at: the converter is simply not
            # told, and keeps whatever it would have done without this field.
            log.info('%s: not relaying sides=%r to the converter; it is not '
                     'one of %s', queue.name, sides[:40],
                     ', '.join(SIDES_VALUES))
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


def convert(cfg, data, fmt, queue=None, sides=None, force_raster=False):
    """Outline the text of a PDF. Anything else is relayed untouched.

    Fails safe: on any doubt the original is forwarded, because a job that
    might not print beats one that prints something wrong.

    `sides` is the client's own value, handed on so the converter can set the
    duplex byte of a raster stream; `force_raster` asks for raster rather than
    an outlined PDF, and is only ever set after a printer has refused the PDF.
    Both travel in the converter's header line -- see converter_header().
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
        header = (converter_header(queue, cfg, sides=sides,
                                   force_raster=force_raster)
                  if queue else b'')
        if cfg.converter_socket:
            out = convert_over_socket(cfg.converter_socket, header + payload,
                                      cfg.timeout)
        else:
            # start_new_session so a timeout can kill the whole group:
            # terminating the helper leaves Ghostscript itself running.
            payload = header + payload
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
    what = 'rasterised' if force_raster else 'outlined'
    return out, (f'{what} {len(data)} -> {len(out)} bytes in '
                 f'{time.time() - started:.1f}s (font cost {cost})')


def refused_document(status, raw):
    """Read a reply as "no job was created, and the document is why".

    Returns the printer's own status when it is one of RETRY_AS_RASTER, and
    None for everything else -- including a reply that cannot be parsed at all.
    Silence and nonsense are not evidence that no job exists, and only that
    evidence makes sending the document again safe.
    """
    if status != 200:
        return None
    try:
        code = ipp.parse(raw).code
    except Exception:
        return None
    return code if code in RETRY_AS_RASTER else None


def rasterise_after_refusal(cfg, queue, msg, original, fmt, sides, refusal):
    """Convert again as raster, for a document the printer has just refused.

    Returns (payload, note) to send instead, or (None, None) when there is
    nothing better to offer -- in which case the printer's refusal reaches the
    client exactly as it did before this existed.

    This is the whole reason the proxy no longer guesses at a size limit. The
    printer declares one it does not enforce, so the only reliable way to learn
    that a document is too large is to offer it and be told. That is safe here
    and nowhere else: every status in RETRY_AS_RASTER means no job was created,
    so this cannot be the second copy of anything.
    """
    if not cfg.convert or not queue.raster_format:
        return None, None          # there is nothing else this proxy can make
    if not looks_like_pdf(msg.data):
        # Already raster, or never a PDF to begin with. Offering the same bytes
        # again would earn the same refusal.
        return None, None
    why = RETRY_AS_RASTER[refusal]
    try:
        data, note = convert(cfg, original, fmt, queue, sides=sides,
                             force_raster=True)
    except ConversionFailed as exc:
        log.warning('%s: the printer refused the document (%s) and it could '
                    'not be rasterised (%s)', queue.name, why, exc)
        return None, None
    produced = sniff_format(data)
    if data is original or produced is None or produced == fmt \
            or produced == 'application/pdf':
        log.warning('%s: the printer refused the document (%s) and the '
                    'converter had no raster to offer instead', queue.name, why)
        return None, None
    msg.data = data
    group = msg.operation()
    if group is not None:
        group.replace('document-format', ipp.TAG_MIMETYPE, [produced])
    log.warning('%s: the printer refused the document (%s); converting again '
                'and sending it as %s. The refused attempt created no job, '
                'which is what that status means, so nothing prints twice',
                queue.name, why, produced)
    return ipp.serialize(msg), f'{note}; refused {why}, sent as {produced}'


def job_sides(msg):
    """The `sides` a client asked for, from the job group of its own request.

    That group and no other. The value is relayed verbatim or not at all, so
    there is nowhere else it could honestly come from.
    """
    group = msg.group(ipp.JOB_ATTRS)
    return group.get_str('sides') if group else None


def warn_sides_without_media(queue, msg):
    """Say so when a duplex request is about to be dropped by the printer.

    Measured on an M283fdw: `sides=two-sided-long-edge` sent on its own comes
    back 0x0001 successful-ok-ignored-or-substituted-attributes with `sides`
    among the unsupported attributes, and the job prints one-sided. Adding
    `media=na_letter_8.5x11in` to the same job makes it answer 0x0000 and
    genuinely duplex. The device publishes `job-constraints-supported:
    duplex-unsupported-media` and resolves that constraint by discarding
    `sides` instead of applying `media-default`, which RFC 8011 5.2 requires.

    Nothing is added to the job to paper over it. `media-supported` on this
    device also lists `custom_min_3x5in` and `custom_max_8.5x14in`, which are
    range descriptors and are refused when actually requested -- so the
    advertised list is not a list of things a proxy may pick from. And no value
    is neutral: an absent `media` is a request to auto-select, not a gap, so
    supplying one would choose the user's paper for them. All this does is turn
    a silent surprise into one line in the journal.
    """
    group = msg.group(ipp.JOB_ATTRS)
    if group is None:
        return
    sides = group.get_str('sides')
    if not sides or not sides.startswith('two-sided'):
        return
    # media-col names a size too, so a client that sent one has chosen paper
    # and the printer's constraint has something to resolve against.
    if group.index_of('media') >= 0 or group.index_of('media-col') >= 0:
        return
    log.warning('%s: %s was asked for with no media in the same job. This '
                'printer discards sides rather than applying media-default, '
                'so the job will print one-sided and say it succeeded. No '
                'media is added here: there is no neutral value, and choosing '
                "one picks the user's paper for them", queue.name, sides[:40])


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
# Operations where job-uri is how the client names the job it means, rather
# than something the printer would go and fetch. RFC 8011 section 4.3 lets a
# client target a job either by job-uri alone or by (printer-uri, job-id); a
# client that chose the first form used to have its only identifier stripped
# and got back a not-found it could do nothing about.
JOB_TARGETED_OPS = frozenset({
    0x0008,   # Cancel-Job
    0x0009,   # Get-Job-Attributes
})


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

    # A job-uri that names a job is translated into the (printer-uri, job-id)
    # form rather than relayed. Relaying it would mean handing the printer a URI
    # the sender composed, and rewriting it would mean guessing the printer's
    # own job-uri spelling -- this one zero-pads the id, others do not. The
    # numeric form is the one already known to work, and printer-uri above has
    # already been pointed at the real device.
    if msg.code in JOB_TARGETED_OPS and group.index_of('job-uri') >= 0:
        if group.index_of('job-id') < 0:
            wanted = group.get('job-uri')[0].decode('utf-8', 'replace')
            tail = wanted.rstrip('/').rsplit('/', 1)[-1]
            if tail.isdigit():
                group.replace('job-id', ipp.TAG_INTEGER,
                              [ipp.i32(int(tail))])
            else:
                log.warning('%s: cannot read a job id out of %r; the request '
                            'will not name a job', queue.name, wanted[:120])

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
            # Status is otherwise passed through verbatim; this is the one
            # exception, and only where the printer disagrees with itself.
            if queue.clamp_supplies:
                note = clamp_supply_levels(group)
                if note and note != queue.supply_note:
                    log.info('%s: %s', queue.name, note)
                queue.supply_note = note
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
class BodyTooLarge(Exception):
    """The client's document is larger than this proxy will read.

    Kept apart from BadRequest because the two deserve different answers. A
    malformed request cannot be answered reliably -- the framing is what has
    gone wrong, so there may be no request to reply to. A body that is merely
    too large arrived on a connection that is working perfectly, and closing it
    without a word is the silence this whole program exists to remove: the
    client sees a dropped connection and reports a network problem, when what
    happened is that a limit here was reached.
    """


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
                raise BodyTooLarge('chunked body too large')
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
        raise BodyTooLarge('body too large')
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
        except BodyTooLarge as exc:
            # Say so, then close. The request cannot be served, but a client
            # that is told 413 reports a document too large for the server;
            # one that is told nothing reports the network.
            log.warning('%s from %s: refusing a document larger than %d bytes',
                        exc, self.client_address[0], MAX_BODY)
            try:
                respond(wfile, '413 Content Too Large', 'text/plain',
                        b'the document is larger than this proxy will relay\n')
            except OSError:
                pass
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
            # 0x0501 is server-error-operation-not-supported. The request
            # parsed and was understood; it is this proxy that will not relay
            # it, which is a different thing from a malformed request.
            ipp_error(wfile, msg, 0x0501,
                      b'this operation is not relayed')
            return
        note = ''
        fmt = None
        archived = None

        # Every operation that can carry a client's job attributes, which is
        # not the same set as the ones that carry a document: a Create-Job
        # names the sides and the Send-Document after it carries the pages.
        if msg.code in (OP_PRINT_JOB, OP_CREATE_JOB, OP_SEND_DOCUMENT):
            warn_sides_without_media(queue, msg)

        if msg.code in (OP_PRINT_JOB, OP_SEND_DOCUMENT) and msg.data:
            group = msg.operation()
            fmt = group.get_str('document-format') if group else None
            # What the client asked for, relayed to the converter untouched. A
            # raster stream carries its own duplex byte and that byte is what
            # the printer obeys, so the converter has to be told; see
            # converter_header(). The job goes upstream unchanged either way.
            #
            # NOTE, pinned as it is: a client that uses Create-Job puts its
            # sides on the Create-Job, which carries no document, and the
            # Send-Document that carries the pages usually repeats nothing. So
            # on that path the converter is not told. Remembering it would mean
            # holding per-job state in a proxy that deliberately holds none,
            # and inventing it is exactly what must not happen -- so the
            # converter keeps its default, as it did before this field existed.
            sides = job_sides(msg)
            # One job at a time: these printers report
            # multiple-document-jobs-supported = false, and a second job
            # arriving mid-transfer confuses them.
            original = msg.data
            # Before conversion, because the converter flattens the form
            # XObject the repair works on: afterwards there is nothing left to
            # put back. The archive still keeps the document as it arrived.
            msg.data, placed = place_pages(cfg, queue, msg, msg.data, fmt)
            submitted = msg.data
            try:
                msg.data, note = convert(cfg, msg.data, fmt, queue, sides=sides)
                if placed:
                    note = f'{placed}; {note}'
                # Conversion may legitimately change the format: a document
                # the converter would not hand over whole comes back as raster
                # instead. Say so, rather than mislabelling it.
                if msg.data is not submitted:
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
            archived = archive_document(
                cfg, queue, group.get_str('job-name') if group else None,
                fmt, original, note)
            rewrite_request(queue, msg)
            payload = ipp.serialize(msg)
            # Two attempts at most, and the second only after an answer that
            # says the printer created no job. The bound is the loop itself:
            # there is no path round it a third time.
            for attempt in (1, 2):
                if not queue.lock.acquire(timeout=cfg.timeout):
                    log.warning('%s: busy, refusing job', queue.name)
                    # 0x0507 is server-error-busy, which a print system
                    # understands as "try again" -- where an HTTP 503 with a
                    # line of English reads as the server being broken.
                    ipp_error(wfile, msg, 0x0507,
                              b'the printer is busy with another job')
                    return
                failure = None
                try:
                    status, raw = upstream_ipp(queue, payload, cfg.timeout)
                except UPSTREAM_ERRORS as exc:
                    failure = exc
                finally:
                    # Release before answering. Writing to a client can block
                    # for as long as that client cares to take, and holding the
                    # queue lock across it would let one slow reader stall
                    # every other job.
                    queue.lock.release()
                if failure is not None:
                    # No answer at all. The printer may be holding the job it
                    # just read, so this is precisely the case that must never
                    # be sent again -- doing so prints it twice.
                    unreachable(wfile, queue, msg, name, failure)
                    return
                refusal = (refused_document(status, raw) if attempt == 1
                           else None)
                if refusal is None:
                    break
                retry, retry_note = rasterise_after_refusal(
                    cfg, queue, msg, original, fmt, sides, refusal)
                if retry is None:
                    break            # nothing better to offer; relay the refusal
                payload, note = retry, retry_note
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
                            msg.data or b'', note, archived)
            except Exception as exc:
                log.error('could not start following the job: %s', exc)


SD_LISTEN_FDS_START = 3


def inherited_sockets():
    """The listening sockets systemd passed us, by name.

    With socket activation systemd opens the ports itself and hands over the
    descriptors, so the service never needs the privilege to bind a port below
    1024 -- it can run with no capabilities at all. Returns {name: socket};
    empty when started by hand, in which case the caller binds for itself.

    Names come from FileDescriptorName= in each socket unit. There is more than
    one socket now, and taking "the first" would work right up until systemd
    passed them in a different order.
    """
    if os.environ.get('LISTEN_PID') != str(os.getpid()):
        return {}
    try:
        count = int(os.environ.get('LISTEN_FDS', '0'))
    except ValueError:
        return {}
    names = os.environ.get('LISTEN_FDNAMES', '').split(':')
    out = []
    for i in range(count):
        fd = SD_LISTEN_FDS_START + i
        name = names[i] if i < len(names) and names[i] else str(fd)
        sock = socket.socket(fileno=fd)
        sock.setblocking(True)
        os.set_inheritable(fd, False)
        out.append((name, sock))
    for name in ('LISTEN_PID', 'LISTEN_FDS', 'LISTEN_FDNAMES'):
        os.environ.pop(name, None)
    # A list, not a dict: an administrator serving several printers runs one
    # socket unit per address, and nothing stops two of them carrying the same
    # FileDescriptorName. Keyed by name, the second would replace the first and
    # one printer would silently go unanswered.
    return out


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
# CUPS reads the DNS-SD printer-type as a bitfield, and two of its bits are
# claims about what this device can do rather than about how it is reached:
# 0x8 colour and 0x10 duplex. Both are set only from what the printer said.
# The base keeps 0x4, black and white, which is the one capability no printer
# lacks, alongside the bits that describe the queue itself -- remote, copies,
# a small and a variable media size, and the commands bit.
#
# The value this replaced was the constant 0x809056, which claimed duplex for
# every device while the words beside it claimed colour, and the two disagreed:
# 0x809056 has 0x4 set and 0x8 clear, i.e. monochrome.
PRINTER_TYPE_BASE = 0x809046
PRINTER_TYPE_COLOUR = 0x8
PRINTER_TYPE_DUPLEX = 0x10


def discovery_txt(cfg, queue, scheme):
    """The DNS-SD TXT record for one queue, from what the printer has said.

    Everything about the device itself is derived from Queue.learn(), and a key
    it could not answer for is left out entirely rather than filled in. This is
    the one place a client reads a capability *before* any IPP exchange can
    correct it: a client that finds no `Color` key asks the printer, while one
    told `Color=T` about a monochrome device believes it and offers colour that
    will never arrive. An absent key is a question; a wrong key is an answer.

    The formats are filtered exactly as rewrite_response filters them, because
    discovery and the printer's own attribute list are read by the same client
    and disagreeing would be worse than either answer alone.
    """
    props = {
        'txtvers': '1', 'qtotal': '1',
        'rp': queue.local_path.lstrip('/'),
        'ty': queue.name,
        'note': 'ippfix',
        'adminurl': cfg.base_http() + '/',
        'priority': '10',
        'UUID': cfg.our_uuid(queue).replace('urn:uuid:', ''),
        'TLS': '1.2' if scheme == 'ipps' else '',
    }
    formats = queue.formats
    if cfg.restrict_formats:
        formats = [f for f in formats
                   if f in SAFE_FORMATS or f.lower() in SAFE_FORMATS]
    # One TXT entry, key included, may be 255 bytes. A printer listing a great
    # many formats is trimmed from the end of its own preference order rather
    # than allowed to break the whole record; what is dropped is said in the
    # journal, because a format missing from discovery is a format clients will
    # not offer.
    kept = []
    for fmt in formats:
        if len('pdl=' + ','.join(kept + [fmt])) > 255:
            log.info('%s: not advertising %s over DNS-SD; the TXT entry is '
                     'full', queue.name, ', '.join(formats[len(kept):]))
            break
        kept.append(fmt)
    if kept:
        props['pdl'] = ','.join(kept)
    ptype = PRINTER_TYPE_BASE
    if queue.colour is not None:
        props['Color'] = 'T' if queue.colour else 'F'
        ptype |= PRINTER_TYPE_COLOUR if queue.colour else 0
    if queue.duplex is not None:
        props['Duplex'] = 'T' if queue.duplex else 'F'
        ptype |= PRINTER_TYPE_DUPLEX if queue.duplex else 0
    props['printer-type'] = hex(ptype)
    return {k: v for k, v in props.items() if v != ''}


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
            props = discovery_txt(cfg, queue, scheme)
            info = ServiceInfo(
                service,
                f'{queue.name}.{service}',
                addresses=None,
                port=cfg.port,
                properties=props,
                server=cfg.dnssd_hostname(),
                parsed_addresses=cfg.published_addresses(),
            )
            zc.register_service(info)
            registered.append(info)
            log.info('advertising %s as %s (%s)', queue.name, service,
                     ', '.join(f'{k}={v}' for k, v in sorted(props.items())
                               if k in ('pdl', 'Color', 'Duplex'))
                     or 'no capabilities: the printer has not answered yet, '
                        'so none are claimed')

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
    parser.add_argument('--advertise-hostname', default=None, metavar='NAME',
                        help='the host name to publish in the DNS-SD SRV '
                             'record, which is what clients build the URI they '
                             'remember out of. Defaults to the --advertise '
                             'address itself, so that printing does not depend '
                             'on multicast DNS still reaching this host. Pass a '
                             'name to use one instead, or "auto" for this '
                             'system\'s .local name')
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
    parser.add_argument('--alert-spool', metavar='DIR',
                        default='/var/lib/ippfix/alerts',
                        help='hand alerts to this directory instead of running '
                             'sendmail, so the daemon needs no mail privileges; '
                             'ippfix-alert.path delivers them. Empty string to '
                             'send directly.')
    parser.add_argument('--alert-timeout', type=int, default=600, metavar='SEC',
                        help='how long to follow a job before giving up on it '
                             '(default 600)')
    parser.add_argument('--no-page-counter', action='store_true',
                        help='do not read the printer\'s RFC 3805 page counter '
                             'over SNMP when judging a job. It is used by '
                             'default because it comes from the marking engine '
                             'rather than from the job accounting that just '
                             'reported success; see ippfix(8). Per printer, put '
                             '"?page-counter=off" on its URI instead.')
    parser.add_argument('--no-snmp-relay', action='store_true',
                        help='do not answer SNMP on the printer\'s behalf, even '
                             'if a socket is passed for it. The relay serves '
                             'read-only GET and GETNEXT inside the Printer MIB '
                             'and refuses everything else; see ippfix(8).')
    parser.add_argument('--snmp-allow', action='append', metavar='CIDR',
                        help='only answer SNMP from this network; repeatable. '
                             'By default any source is answered, subject to the '
                             'rate limit.')
    parser.add_argument('--alert-max-attachment', type=int, default=8,
                        metavar='MB',
                        help='attach at most MB megabytes of documents to a '
                             'report (default: 8). Attachments are the job as '
                             'the client sent it, which requires --archive, and '
                             'the job as this proxy handed it to the printer. 0 '
                             'attaches nothing.')
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
    parser.add_argument('--max-pdf-bytes', type=int, default=0,
                        metavar='MB',
                        help='have the converter rasterise an outlined PDF '
                             'larger than this instead of sending it. The '
                             'default of 0 sends it and lets the printer '
                             'answer for itself, because the size a printer '
                             'declares it accepts was measured not to be the '
                             'size it enforces; a job the printer does refuse '
                             'is converted again as raster and resent. Set a '
                             'number only for a device measured to refuse '
                             'oversized jobs in some way this cannot detect')
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
    passed = inherited_sockets()
    # 'ipp' is the name the socket unit gives it; a hand-started daemon and an
    # older unit without FileDescriptorName= fall back to the first stream
    # socket. Datagram sockets are the SNMP listeners, matched to printers by
    # the address they bound rather than by what they were called.
    streams = [s for n, s in passed if s.type == socket.SOCK_STREAM]
    listen_fd = next((s for n, s in passed if n == 'ipp'),
                     streams[0] if streams else None)
    if listen_fd is not None:
        log.info('  socket activated: using the descriptor systemd passed, '
                 'so no capabilities are required')
    start_snmp_relays(cfg, [s for n, s in passed
                            if s.type == socket.SOCK_DGRAM])
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
