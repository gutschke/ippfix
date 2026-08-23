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
import logging
import os
import re
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
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


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
class Queue:
    """One proxied printer, reachable at /ipp/<name> on this server."""

    def __init__(self, name, uri):
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme not in ('ipp', 'ipps'):
            raise ValueError(f'{name}: expected an ipp:// or ipps:// URI')
        self.name = name
        self.tls = parts.scheme == 'ipps'
        self.host = parts.hostname
        self.port = parts.port or 631
        self.path = parts.path or '/ipp/print'
        if not self.host:
            raise ValueError(f'{name}: no host in {uri!r}')

    @property
    def local_path(self):
        return f'/ipp/{self.name}'

    def upstream_uri(self):
        return f'ipp://{self.host}{self.path}'

    def __str__(self):
        return (f'{self.name} -> {"ipps" if self.tls else "ipp"}://'
                f'{self.host}:{self.port}{self.path}')


class Config:
    def __init__(self, args, queues):
        self.port = args.port
        self.queues = {q.local_path: q for q in queues}
        self.advertise = args.advertise or local_ip()
        self.cert = args.cert
        self.key = args.key
        self.convert = not args.no_convert
        self.converter = args.converter
        self.timeout = args.timeout
        self.archive = args.archive
        self.archive_max = args.archive_max

    def base_http(self):
        return f'http://{self.advertise}:{self.port}'

    def our_uri(self, queue, scheme='ipp'):
        return f'{scheme}://{self.advertise}:{self.port}{queue.local_path}'

    def our_uuid(self, queue):
        """Stable, and deliberately different from the printer's own: a client
        that sees one printer-uuid on two queues collapses them into one."""
        seed = f'ippfix:{self.advertise}:{queue.name}:{queue.host}'
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


# ---------------------------------------------------------------------------
# document conversion
# ---------------------------------------------------------------------------
def looks_like_pdf(data):
    return data[:1024].find(b'%PDF-') >= 0


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
        with open(path, 'wb') as handle:
            handle.write(data)
        os.chmod(path, 0o600)
        with open(f'{path}.txt', 'w', encoding='utf-8') as handle:
            handle.write(f'queue: {queue.name}\nprinter: {queue.host}\n'
                         f'job-name: {job_name}\ndocument-format: {fmt}\n'
                         f'bytes: {len(data)}\nconversion: {note}\n')
        os.chmod(f'{path}.txt', 0o600)
        prune_archive(cfg)
        log.debug('archived %s', path)
    except OSError as exc:
        log.warning('could not archive job: %s', exc)


def prune_archive(cfg):
    """Keep the archive bounded so a forgotten flag cannot fill the disk."""
    try:
        entries = [os.path.join(cfg.archive, name)
                   for name in os.listdir(cfg.archive)
                   if not name.endswith('.txt')]
        entries.sort(key=os.path.getmtime)
        for path in entries[:max(0, len(entries) - cfg.archive_max)]:
            for victim in (path, path + '.txt'):
                try:
                    os.remove(victim)
                except OSError:
                    pass
    except OSError:
        pass


def convert(cfg, data, fmt):
    """Outline the text of a PDF. Anything else is relayed untouched.

    Fails safe: on any doubt the original is forwarded, because a job that
    might not print beats one that prints something wrong.
    """
    if not cfg.convert or not data:
        return data, 'relayed'
    if not looks_like_pdf(data):
        return data, f'relayed ({fmt or "not PDF"})'

    started = time.time()
    try:
        proc = subprocess.run([cfg.converter], input=data,
                              capture_output=True, timeout=cfg.timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error('converter failed (%s); relaying original', exc)
        return data, 'relayed (converter failed)'

    out = proc.stdout
    if proc.returncode != 0 or not out:
        log.error('converter exited %s: %s', proc.returncode,
                  proc.stderr[:300].decode('utf-8', 'replace').strip())
        return data, 'relayed (converter error)'
    if b'/FontFile' in out:
        log.warning('font programs survived conversion; relaying original')
        return data, 'relayed (fonts survived)'
    return out, (f'outlined {len(data)} -> {len(out)} bytes in '
                 f'{time.time() - started:.1f}s')


# ---------------------------------------------------------------------------
# upstream
# ---------------------------------------------------------------------------
def upstream_ipp(queue, payload, timeout):
    if queue.tls:
        ctx = ssl._create_unverified_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(queue.host, queue.port,
                                           timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(queue.host, queue.port,
                                          timeout=timeout)
    try:
        conn.request('POST', queue.path, body=payload,
                     headers={'Content-Type': 'application/ipp',
                              'Content-Length': str(len(payload)),
                              'Host': f'{queue.host}:{queue.port}'})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def upstream_http(queue, path, timeout=30):
    conn = http.client.HTTPConnection(queue.host, 80, timeout=timeout)
    try:
        conn.request('GET', path, headers={'Host': queue.host})
        resp = conn.getresponse()
        ctype = resp.getheader('Content-Type', 'application/octet-stream')
        return resp.status, ctype, resp.read()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# attribute rewriting
# ---------------------------------------------------------------------------
def rewrite_request(queue, msg):
    """Address the request to the real printer before forwarding."""
    group = msg.operation()
    if group is None:
        return
    for attr in ('printer-uri', 'job-printer-uri'):
        if group.index_of(attr) >= 0:
            group.replace(attr, ipp.TAG_URI, [queue.upstream_uri()])


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
    headers = {}
    while True:
        line = rfile.readline(8192)
        if not line or line in (b'\r\n', b'\n'):
            break
        if b':' in line:
            key, value = line.split(b':', 1)
            headers[key.strip().lower().decode('latin-1')] = \
                value.strip().decode('latin-1')
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
            if size == 0:
                rfile.readline(8192)
                break
            while size > 0:
                chunk = rfile.read(min(size, 65536))
                if not chunk:
                    raise BadRequest('truncated chunk')
                body += chunk
                size -= len(chunk)
            rfile.readline(8192)
        return bytes(body)

    remaining = int(headers.get('content-length', 0) or 0)
    body = bytearray()
    while len(body) < remaining:
        chunk = rfile.read(min(remaining - len(body), 65536))
        if not chunk:
            break
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
<ul>{queues}</ul>
</body></html>
"""


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        cfg = self.server.cfg
        sock = self.request
        try:
            first = sock.recv(1, socket.MSG_PEEK)
        except OSError:
            return
        if not first:
            return
        if first[0] == 0x16:                       # TLS ClientHello
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                context.load_cert_chain(cfg.cert, cfg.key)
                sock = context.wrap_socket(sock, server_side=True)
            except (ssl.SSLError, OSError) as exc:
                log.debug('TLS handshake failed: %s', exc)
                return

        rfile = sock.makefile('rb')
        wfile = sock.makefile('wb')
        try:
            while self.one_request(cfg, rfile, wfile):
                pass
        except (BadRequest, OSError, ssl.SSLError):
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
        headers = read_headers(rfile)

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
        return True

    def resolve(self, cfg, path):
        """Map a request path to a queue, tolerating trailing job ids."""
        base = '/' + path.lstrip('/').split('?')[0]
        if base in cfg.queues:
            return cfg.queues[base]
        for local_path, queue in cfg.queues.items():
            if base.startswith(local_path + '/'):
                return queue
        if len(cfg.queues) == 1:
            return next(iter(cfg.queues.values()))
        return None

    def handle_get(self, cfg, wfile, path):
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
                respond(wfile, '200 OK' if status == 200 else f'{status} Error',
                        ctype, data)
            except OSError:
                respond(wfile, '502 Bad Gateway', 'text/plain',
                        b'printer unreachable\n')
            return

        items = ''.join(
            f'<li><code>{cfg.our_uri(q)}</code> &rarr; {q.host}</li>'
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
        note = ''

        if msg.code in (OP_PRINT_JOB, OP_SEND_DOCUMENT) and msg.data:
            group = msg.operation()
            fmt = group.get_str('document-format') if group else None
            # One job at a time: these printers report
            # multiple-document-jobs-supported = false, and a second job
            # arriving mid-transfer confuses them.
            original = msg.data
            with self.server.job_lock:
                msg.data, note = convert(cfg, msg.data, fmt)
                archive_document(cfg, queue,
                                 group.get_str('job-name') if group else None,
                                 fmt, original, note)
                rewrite_request(queue, msg)
                status, raw = upstream_ipp(queue, ipp.serialize(msg),
                                           cfg.timeout)
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


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 32
    address_family = socket.AF_INET6

    def __init__(self, addr, handler, cfg):
        self.cfg = cfg
        self.job_lock = threading.Lock()
        super().__init__(addr, handler)

    def server_bind(self):
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
                parsed_addresses=[cfg.advertise],
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
    if not re.fullmatch(r'[A-Za-z0-9._-]+', name):
        raise ValueError(f'invalid queue name {name!r}')
    return Queue(name, uri.strip())


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='ippfix',
        description='IPP proxy that outlines text so no font program reaches '
                    'the printer')
    parser.add_argument('printers', nargs='+', metavar='[NAME=]URI',
                        help='printer to proxy, e.g. '
                             'upstairs=ipp://printer.example/ipp/print')
    parser.add_argument('-p', '--port', type=int, default=631,
                        help='port to listen on (default: 631)')
    parser.add_argument('-a', '--advertise', default=None,
                        help='address clients should use (default: autodetect)')
    parser.add_argument('--cert', default='/etc/ippfix/ippfix.crt',
                        help='TLS certificate')
    parser.add_argument('--key', default='/etc/ippfix/ippfix.key',
                        help='TLS private key')
    parser.add_argument('--converter', default='/usr/local/lib/ippfix/defont',
                        help='PDF conversion helper')
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
    parser.add_argument('--no-advertise', action='store_true',
                        help='do not publish over DNS-SD')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args(argv)

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
    if cfg.convert and not os.access(cfg.converter, os.X_OK):
        parser.error(f'converter not executable: {cfg.converter}')

    log.info('listening on [::]:%d (IPv4 and IPv6)', cfg.port)
    for queue in queues:
        log.info('  %s', queue)
        log.info('    published as %s', cfg.our_uri(queue))
    log.info('  conversion: %s',
             f'outline text via {cfg.converter}' if cfg.convert else 'DISABLED')
    if cfg.archive:
        log.warning('  ARCHIVING every job to %s (keeping %d) -- this stores '
                    'users\' documents; disable when done diagnosing',
                    cfg.archive, cfg.archive_max)

    withdraw = None if args.no_advertise else advertise(cfg)
    server = Server(('::', cfg.port), Handler, cfg)
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
