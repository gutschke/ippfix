#!/usr/bin/env python3
"""Send documents to a printer and report what it actually did with them.

The failure this repository exists for is silent: the printer accepts the job,
warms up, reports `job-state = completed`, and marks nothing. Every layer above
it repeats that success, so a probe cannot be judged on what the print system
says. It is judged on the page counter.

Two things this checks that a naive harness does not, both learned the hard way:

  * The printer's state is read BEFORE each job as well as after. A job sent
    while the tray was empty says nothing at all about the document, and one
    such measurement -- recorded as a font failure -- was load-bearing in three
    different models before it was caught. Those runs are reported INCONCLUSIVE
    and are never mistaken for evidence.

  * The page counter comes from the RFC 3805 Printer MIB over SNMP, not from a
    vendor's own web interface, so this works on any printer that answers SNMP.

Nothing here is specific to one manufacturer.

  python3 scripts/probe-printer.py ipp://printer.example/ipp/print doc.pdf
  python3 scripts/probe-printer.py ipp://10.0.0.5/ipp/print probes/*.pdf --continue-on-fail

Testing an affected printer is free: a job that provokes the fault marks no
paper. Only a job that prints costs a sheet, which is why this stops at the
first failure unless told otherwise.
"""
import argparse
import os
import socket
import struct
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ippcodec as ipp                                              # noqa: E402

# RFC 3805 / RFC 2790. Portable across manufacturers.
OID_PAGE_COUNT = '1.3.6.1.2.1.43.10.2.1.4.1.1'      # prtMarkerLifeCount
OID_PRINTER_STATUS = '1.3.6.1.2.1.25.3.5.1.1.1'     # hrPrinterStatus
OID_PANEL_TEXT = '1.3.6.1.2.1.43.16.5.1.2.1.1'      # prtConsoleDisplayBufferText

JOB_STATE = {3: 'pending', 4: 'pending-held', 5: 'processing',
             6: 'processing-stopped', 7: 'canceled', 8: 'aborted',
             9: 'completed'}
TERMINAL = {7, 8, 9}

# Conditions meaning the printer could not print, as distinct from choosing not
# to. Checked before a job (the probe says nothing) and after (the document did
# it). Keep these two readings apart; conflating them is how bad data is made.
NOT_READY = ('printer-stopped', 'paused', 'media-needed', 'media-empty',
             'media-jam', 'media-low', 'door-open', 'cover-open',
             'output-area-full', 'toner-empty', 'marker-supply-empty',
             'shutdown', 'moving-to-paused', 'spool-area-full', 'other-error')


# --------------------------------------------------------------------------
# SNMP v2c get, just enough of BER to ask one question.
# --------------------------------------------------------------------------
def _len(n):
    if n < 0x80:
        return bytes([n])
    out = b''
    while n:
        out = bytes([n & 0xFF]) + out
        n >>= 8
    return bytes([0x80 | len(out)]) + out


def _tlv(tag, payload):
    return bytes([tag]) + _len(len(payload)) + payload


def _int(n):
    if n == 0:
        return _tlv(0x02, b'\x00')
    out = b''
    while n:
        out = bytes([n & 0xFF]) + out
        n >>= 8
    if out[0] & 0x80:
        out = b'\x00' + out
    return _tlv(0x02, out)


def _oid(text):
    parts = [int(x) for x in text.split('.')]
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0x80:
            body += bytes([p])
            continue
        chunk = []
        while p:
            chunk.insert(0, (p & 0x7F) | 0x80)
            p >>= 7
        chunk[-1] &= 0x7F
        body += bytes(chunk)
    return _tlv(0x06, body)


def _read(buf, pos):
    tag = buf[pos]
    ln = buf[pos + 1]
    pos += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(buf[pos:pos + k], 'big')
        pos += k
    return tag, buf[pos:pos + ln], pos + ln


def snmp_get(host, oid, community='public', timeout=5):
    """One SNMP v2c GET. Returns str, int, or None when unreachable."""
    varbind = _tlv(0x30, _oid(oid) + _tlv(0x05, b''))
    pdu = _tlv(0xA0, _int(1) + _int(0) + _int(0) + _tlv(0x30, varbind))
    pkt = _tlv(0x30, _int(1) + _tlv(0x04, community.encode()) + pdu)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (host, 161))
        data, _ = s.recvfrom(4096)
    except OSError:
        return None
    finally:
        s.close()
    try:
        _t, seq, _ = _read(data, 0)
        pos = 0
        _t, _v, pos = _read(seq, pos)
        _t, _c, pos = _read(seq, pos)
        _t, body, _ = _read(seq, pos)
        pos = 0
        _t, _rid, pos = _read(body, pos)
        _t, err, pos = _read(body, pos)
        if int.from_bytes(err, 'big'):
            return None
        _t, _idx, pos = _read(body, pos)
        _t, vbs, _ = _read(body, pos)
        _t, vb, _ = _read(vbs, 0)
        pos = 0
        _t, _o, pos = _read(vb, pos)
        tag, val, _ = _read(vb, pos)
    except (IndexError, ValueError):
        return None
    if tag in (0x02, 0x41, 0x42, 0x43, 0x44, 0x45):
        return int.from_bytes(val, 'big')
    if tag == 0x05:
        return None
    return val.decode('utf-8', 'replace').strip()


# --------------------------------------------------------------------------
# IPP
# --------------------------------------------------------------------------
def ipp_call(uri, msg, timeout=180):
    parts = urllib.parse.urlsplit(uri)
    http = ('https' if parts.scheme in ('ipps', 'https') else 'http')
    port = parts.port or (443 if http == 'https' else 631)
    url = f'{http}://{parts.hostname}:{port}{parts.path or "/"}'
    req = urllib.request.Request(url, data=ipp.serialize(msg),
                                 headers={'Content-Type': 'application/ipp'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ipp.parse(r.read())


def _first(msg, name):
    for g in msg.groups:
        v = g.get(name)
        if v:
            return v[0]
    return None


def _as_int(v):
    if isinstance(v, bytes) and len(v) == 4:
        return struct.unpack('>i', v)[0]
    return v if isinstance(v, int) else None


def _as_str(v):
    if isinstance(v, bytes):
        return v.decode('utf-8', 'replace')
    return '' if v is None else str(v)


def printer_state(uri):
    m = ipp.new_request(0x000B, 3, uri)           # Get-Printer-Attributes
    g = m.operation()
    for want in (b'printer-state', b'printer-state-reasons'):
        g.items.append((ipp.TAG_KEYWORD, b'requested-attributes', want))
    try:
        r = ipp_call(uri, m, timeout=30)
    except Exception:
        return None, ''
    reasons = []
    for gr in r.groups:
        v = gr.get('printer-state-reasons')
        if v:
            reasons = [_as_str(x) for x in v]
    return _as_int(_first(r, 'printer-state')), ','.join(reasons)


def not_ready(state, reasons):
    """True when the printer cannot print, ignoring purely advisory warnings."""
    if state == 5:                                 # stopped
        return True
    low = reasons.lower()
    for r in NOT_READY:
        if r in low:
            # '-warning' and '-report' severities are advisory, not blocking.
            for suffix in ('-warning', '-report'):
                low = low.replace(r + suffix, '')
            if r in low:
                return True
    return False


def submit(uri, data, fmt, name):
    m = ipp.new_request(0x0002, 1, uri)            # Print-Job
    g = m.operation()
    g.items.append((ipp.TAG_NAME, b'requesting-user-name', b'probe-printer'))
    g.items.append((ipp.TAG_NAME, b'job-name', name.encode()[:255]))
    g.items.append((ipp.TAG_MIMETYPE, b'document-format', fmt.encode()))
    m.data = data
    return ipp_call(uri, m)


def job_status(uri, job_id):
    m = ipp.new_request(0x0009, 2, uri)            # Get-Job-Attributes
    g = m.operation()
    g.items.append((ipp.TAG_INTEGER, b'job-id', ipp.i32(job_id)))
    for want in (b'job-state', b'job-state-reasons',
                 b'job-impressions-completed'):
        g.items.append((ipp.TAG_KEYWORD, b'requested-attributes', want))
    r = ipp_call(uri, m, timeout=30)
    reasons = []
    for gr in r.groups:
        v = gr.get('job-state-reasons')
        if v:
            reasons = [_as_str(x) for x in v]
    return (_as_int(_first(r, 'job-state')),
            _as_int(_first(r, 'job-impressions-completed')),
            ','.join(reasons))


# --------------------------------------------------------------------------
def probe(uri, snmp_host, community, path, timeout, settle):
    name = os.path.basename(path)
    data = open(path, 'rb').read()
    print(f'\n{"=" * 70}\n{name}   ({len(data):,} bytes)')

    state, reasons = printer_state(uri)
    pages_before = snmp_get(snmp_host, OID_PAGE_COUNT, community)
    print(f'  before: printer-state={state} ({reasons or "none"})  '
          f'pages={pages_before if pages_before is not None else "?"}')

    if state is None:
        return name, 'UNREACHABLE', 'no reply to Get-Printer-Attributes'
    if not_ready(state, reasons):
        panel = snmp_get(snmp_host, OID_PANEL_TEXT, community)
        return name, 'INCONCLUSIVE', (
            f'printer was not ready before the job ({reasons}'
            + (f'; panel says "{panel}"' if panel else '') +
            '). This says nothing about the document.')
    if pages_before is None:
        return name, 'NO-COUNTER', (
            'the page counter could not be read over SNMP, and it is the only '
            'honest signal. Check the community string and that SNMP is enabled.')

    try:
        r = submit(uri, data, 'application/pdf', name)
    except Exception as e:
        return name, 'SUBMIT-FAILED', str(e)
    if r.code >= 0x0100:
        return name, 'REJECTED', f'status 0x{r.code:04x}'
    job_id = _as_int(_first(r, 'job-id'))
    print(f'  submitted: job {job_id}')

    state_j = imp = None
    jreasons = ''
    last = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(4)
        try:
            state_j, imp, jreasons = job_status(uri, job_id)
        except Exception:
            continue
        cur = (state_j, imp, jreasons)
        if cur != last:
            print(f'    {JOB_STATE.get(state_j, state_j)} '
                  f'impressions={imp} reasons={jreasons or "none"}')
            last = cur
        if state_j in TERMINAL:
            break

    time.sleep(settle)                     # let the engine finish marking
    pages_after = snmp_get(snmp_host, OID_PAGE_COUNT, community)
    state2, reasons2 = printer_state(uri)
    delta = (pages_after - pages_before) if pages_after is not None else None
    print(f'  after:  printer-state={state2} ({reasons2 or "none"})  '
          f'pages={pages_after} (+{delta})')

    # The printer was ready before this job, so if it is not ready now, this
    # document is why. That is a failure, and a louder one than a silent drop.
    if not_ready(state2, reasons2) and not delta:
        return name, 'FAILED-PRINTER-STOPPED', f'document left the printer {reasons2}'
    if state_j == 8:
        return name, 'ABORTED', f'job-state=aborted ({jreasons})'
    if state_j == 7:
        return name, 'CANCELED', jreasons
    if delta is None:
        return name, 'UNKNOWN', 'page counter unreadable after the job'
    if delta <= 0:
        return name, 'SILENT-NO-OUTPUT', (
            f'job reported {JOB_STATE.get(state_j, state_j)} but the page '
            f'counter did not move')
    if state_j not in TERMINAL:
        return name, 'STUCK', f'no terminal state within {timeout}s'
    return name, 'PRINTED', f'{delta} page(s)'


def main():
    ap = argparse.ArgumentParser(
        description='Send documents to a printer and report what it really did.')
    ap.add_argument('printer', help='ipp://HOST/ipp/print')
    ap.add_argument('files', nargs='+')
    ap.add_argument('--snmp-host', help='default: the printer host')
    ap.add_argument('--community', default='public')
    ap.add_argument('--timeout', type=int, default=240)
    ap.add_argument('--settle', type=int, default=12,
                    help='seconds to wait after the job before reading the counter')
    ap.add_argument('--continue-on-fail', action='store_true',
                    help='keep going instead of stopping at the first failure')
    args = ap.parse_args()

    snmp_host = args.snmp_host or urllib.parse.urlsplit(args.printer).hostname
    results = []
    for path in args.files:
        if not os.path.exists(path):
            print(f'  {path}: missing, skipped')
            continue
        r = probe(args.printer, snmp_host, args.community, path,
                  args.timeout, args.settle)
        results.append(r)
        print(f'  VERDICT: {r[1]}  {r[2]}')
        if r[1] == 'INCONCLUSIVE':
            print('\n  Stopping: the printer could not print, so nothing here is\n'
                  '  evidence about the document. Fix the printer and re-run.')
            break
        if r[1] != 'PRINTED' and not args.continue_on_fail:
            print('\n  Stopping at the first failure. Re-run with '
                  '--continue-on-fail to go on.')
            break

    print(f'\n{"=" * 70}\nSUMMARY')
    for name, verdict, detail in results:
        print(f'  {name:<34s} {verdict:<24s} {detail}')
    return 0 if all(v == 'PRINTED' for _, v, _ in results) else 1


if __name__ == '__main__':
    sys.exit(main())
