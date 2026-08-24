#!/bin/bash -e
#
# Offline self-test. Everything here runs without a printer, without the
# network, and without installing anything, so it is safe to run anywhere.
#
#   ./scripts/selftest.sh
#
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$(readlink -f "$0")")/.."

pass=0
fail=0

ok()   { printf '  ok    %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; fail=$((fail+1)); }
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM

echo 'syntax'
check 'ippfix.py compiles'        'python3 -m py_compile ippfix.py'
check 'ippcodec.py compiles'      'python3 -m py_compile ippcodec.py'
rm -rf __pycache__
for s in defont install.sh uninstall.sh ippfix scripts/selftest.sh; do
  check "$s parses" "bash -n '$s'"
done

echo 'executables carry the executable bit'
for f in defont ippfix install.sh uninstall.sh scripts/selftest.sh; do
  check "$f is executable" "[ -x '$f' ]"
done

echo 'IPP codec'
python3 - "$work" <<'PY' && ok 'round-trips messages byte for byte' || bad 'round-trip'
import sys
sys.path.insert(0, '.')
import ippcodec as ipp

# A message exercising the awkward parts: several groups, a multi-value
# attribute, an integer, and a collection left opaque to the codec.
m = ipp.new_request(0x000B, 42, 'ipp://printer.example/ipp/print')
g = m.operation()
g.replace('requested-attributes', ipp.TAG_KEYWORD, ['all', 'media-col-database'])
pg = ipp.Group(ipp.PRINTER_ATTRS, [
    (ipp.TAG_INTEGER, b'printer-state', ipp.i32(3)),
    (ipp.TAG_KEYWORD, b'printer-state-reasons', b'toner-low-warning'),
    (0x34, b'media-col', b''),
    (0x4a, b'', b'media-type'),
    (ipp.TAG_KEYWORD, b'', b'stationery'),
    (0x37, b'', b''),
])
m.groups.append(pg)
m.data = b'%PDF-1.4 payload'

raw = ipp.serialize(m)
again = ipp.serialize(ipp.parse(raw))
assert again == raw, 'serialize(parse(x)) != x'
back = ipp.parse(raw)
assert back.data == b'%PDF-1.4 payload'
assert back.group(ipp.PRINTER_ATTRS).get_int('printer-state') == 3
assert back.group(ipp.PRINTER_ATTRS).get_str('printer-state-reasons') == 'toner-low-warning'
assert back.operation().get('requested-attributes') == [b'all', b'media-col-database']

# Replacing an attribute must keep its position and drop its old extra values.
before = [n for _t, n, _v in back.operation().items]
back.operation().replace('requested-attributes', ipp.TAG_KEYWORD, ['one'])
after = [n for _t, n, _v in back.operation().items]
assert before.index(b'requested-attributes') == after.index(b'requested-attributes')
assert after.count(b'') == 0, 'stale additional values left behind'
PY

echo 'queue specification parsing'
python3 - <<'PY' && ok 'accepts and rejects the right specs' || bad 'queue parsing'
import sys
sys.path.insert(0, '.')
from ippfix import parse_queue

q = parse_queue('ipp://printer.example/ipp/print')
assert (q.name, q.host, q.port, q.tls) == ('print', 'printer.example', 631, False)
q = parse_queue('upstairs=ipps://printer.example:1234/queue')
assert (q.name, q.host, q.port, q.tls, q.path) == \
       ('upstairs', 'printer.example', 1234, True, '/queue')
assert q.local_path == '/ipp/upstairs'
# Display names may contain spaces; only the derived slug must be URL-safe.
q = parse_queue('Apartment Color Printer=ipp://printer.example/ipp/print')
assert q.name == 'Apartment Color Printer', q.name
assert q.slug == 'apartment-color-printer', q.slug
assert q.local_path == '/ipp/apartment-color-printer'
assert parse_queue('  Odd//Name!! =ipp://h/p').slug == 'odd-name'
for bad_spec in ('http://printer.example/x', '   =ipp://h/p', 'ipp:///nohost'):
    try:
        parse_queue(bad_spec)
    except ValueError:
        continue
    raise AssertionError(f'should have rejected {bad_spec!r}')
PY

python3 - <<'PY2' && ok 'every option Config reads exists on the CLI' || bad 'CLI/Config agreement'
import sys
sys.path.insert(0, '.')
import ippfix
# Building Config from hand-made Namespace hides options added to one side
# only, which is exactly how a startup crash slipped through once.
args = ippfix.build_parser().parse_args(['x=ipp://printer.example/ipp/print'])
cfg = ippfix.Config(args, [ippfix.parse_queue('x=ipp://printer.example/ipp/print')])
for attr in ('port', 'advertise', 'cert', 'key', 'convert', 'converter',
             'timeout', 'archive', 'archive_max', 'max_connections',
             'idle_timeout', 'require_tls', 'extra_addresses',
             'advertise_hostname', 'alert_max_attachment'):
    assert hasattr(cfg, attr), attr
PY2

echo 'addressing'
python3 - <<'PY2' && ok 'URLs stay short and paths stay forgiving' || bad 'URL handling'
import sys, argparse
sys.path.insert(0, '.')
import ippfix

def cfg(port=631, queues=('office=ipp://printer.example/ipp/print',),
        extra=()):
    argv = ['--port', str(port), '--advertise', '192.0.2.10', '--no-ipv6',
            *extra, *queues]
    a = ippfix.build_parser().parse_args(argv)
    qs = [ippfix.parse_queue(q) for q in queues]
    return ippfix.Config(a, qs), qs

c, qs = cfg()
# The default port is assumed by every client; naming it is only noise.
assert c.our_uri(qs[0], 'ipp') == 'ipp://192.0.2.10/ipp/office', c.our_uri(qs[0])
assert c.our_uri(qs[0], 'ipps') == 'ipps://192.0.2.10/ipp/office'
c2, qs2 = cfg(port=8631)
assert c2.our_uri(qs2[0]) == 'ipp://192.0.2.10:8631/ipp/office'

# IPv6 literals must be bracketed or the port cannot be told from the address.
a = ippfix.build_parser().parse_args(
    ['--advertise', '2001:db8::1', '--no-ipv6',
     'office=ipp://printer.example/ipp/print'])
c3 = ippfix.Config(a, qs)
assert c3.our_uri(qs[0]) == 'ipp://[2001:db8::1]/ipp/office', c3.our_uri(qs[0])
assert c3.base_http() == 'http://[2001:db8::1]:631'

# What clients build their remembered URI from. An address literal by default,
# because a .local name has to be resolved by multicast DNS on every print and
# multicast does not survive a VPN or a routed subnet.
assert c.dnssd_hostname() == '192.0.2.10.', c.dnssd_hostname()
# An IPv6 literal must never be handed over: clients paste it into
# ipp://HOST:PORT/ without the brackets a bare v6 address needs.
assert c3.dnssd_hostname().endswith('.local.'), c3.dnssd_hostname()
c5, _ = cfg(extra=('--advertise-hostname', 'auto'))
assert c5.dnssd_hostname().endswith('.local.'), c5.dnssd_hostname()
c6, _ = cfg(extra=('--advertise-hostname', 'printer.example.com'))
assert c6.dnssd_hostname() == 'printer.example.com.', c6.dnssd_hostname()

# Addresses get typed from memory, so resolution is deliberately lax.
c4, qs4 = cfg(queues=('office=ipp://p/ipp/print', 'studio=ipp://q/ipp/print'))
h = ippfix.Handler.__new__(ippfix.Handler)
for path, want in (('/ipp/office', 'office'), ('/office', 'office'),
                   ('/IPP/Office', 'office'), ('/ipp/office/', 'office'),
                   ('/ipp/office/42', 'office'), ('/studio', 'studio'),
                   ('/ipp/studio?x=1', 'studio')):
    got = h.resolve(c4, path)
    assert got is not None and got.name == want, (path, got and got.name)
assert h.resolve(c4, '/nope') is None, 'ambiguous path must not guess'
PY2

echo 'address selection'
python3 - <<'PY' && ok 'excludes unusable IPv6 addresses' || bad 'address selection'
import sys, socket
sys.path.insert(0, '.')
import ippfix

sample = (
    # address                          idx plen scope flags dev
    ('20010db8000000000000000000000001', '02', '40', '00', '00', 'eth0'),  # keep
    ('fe800000000000000000000000000001', '02', '40', '20', '00', 'eth0'),  # link-local
    ('20010db8000000000000000000000002', '02', '40', '00', '01', 'eth0'),  # temporary
    ('20010db8000000000000000000000003', '02', '40', '00', '20', 'eth0'),  # deprecated
    ('20010db8000000000000000000000004', '02', '40', '00', '40', 'eth0'),  # tentative
    ('20010db8000000000000000000000005', '02', '40', '00', '00', 'eth1'),  # other iface
    ('00000000000000000000000000000001', '01', '80', '10', '80', 'lo'),    # loopback
)
text = ''.join(' '.join(row) + '\n' for row in sample)

import io
real_open = ippfix.open if hasattr(ippfix, 'open') else open
def fake_open(path, *a, **k):
    if path == '/proc/net/if_inet6':
        return io.StringIO(text)
    return real_open(path, *a, **k)

ippfix.open = fake_open
try:
    every = ippfix.global_ipv6()
    scoped = ippfix.global_ipv6('eth0')
finally:
    del ippfix.open

assert every == ['2001:db8::1', '2001:db8::5'], every
assert scoped == ['2001:db8::1'], scoped
PY

echo 'defont'
if command -v gs >/dev/null 2>&1; then
  # Build a PDF that really does embed a font program.
  cat > "$work/in.ps" <<'PS'
/NimbusRoman-Regular findfont 24 scalefont setfont
72 720 moveto (Embedded font sample 0123456789) show
showpage
PS
  gs -q -dNOPAUSE -dBATCH -dSAFER -sDEVICE=pdfwrite -dEmbedAllFonts=true \
     -sOutputFile="$work/in.pdf" "$work/in.ps" >/dev/null 2>&1
  # Ghostscript 10.06 writes PDF 1.7 with compressed object streams by default,
  # so the font dictionary is really there but invisible to grep. Look inside.
  seefonts="python3 -c \"
import re, sys, zlib
d = open(sys.argv[1], 'rb').read()
if b'/FontFile' in d:
    sys.exit(0)
for m in re.finditer(rb'/Type\\s*/ObjStm.*?stream\\r?\\n', d, re.S):
    e = d.find(b'endstream', m.end())
    if e < 0:
        continue
    try:
        if b'/FontFile' in zlib.decompress(d[m.end():e].rstrip(b'\\r\\n')):
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)\""
  check 'test input really embeds a font' "$seefonts '$work/in.pdf'"
  ./defont < "$work/in.pdf" > "$work/out.pdf" 2>/dev/null
  check 'removes every font program'  "! grep -qa '/FontFile' '$work/out.pdf'"
  check 'output is still a PDF'       "head -c 5 '$work/out.pdf' | grep -qa '%PDF-'"
  printf 'UNIRAST\0not a pdf at all' > "$work/raster.bin"
  ./defont < "$work/raster.bin" > "$work/raster.out" 2>/dev/null
  check 'passes non-PDF through unchanged' "cmp -s '$work/raster.bin' '$work/raster.out'"
  # A file gs would interpret as PostScript must never reach it.
  printf '%%!PS-Adobe-3.0\n%%PDF-1.4\n(RAN) print\n' > "$work/confuse.ps"
  ./defont < "$work/confuse.ps" > "$work/confuse.out" 2>/dev/null
  check 'refuses PostScript disguised as PDF' "cmp -s '$work/confuse.ps' '$work/confuse.out'"
  # A document whose outlined form exceeds what the printer accepts as a PDF
  # must come back as raster rather than as something that will be rejected.
  MAX_PDF_BYTES=1000 ./defont < "$work/in.pdf" > "$work/big.out" 2>/dev/null
  check 'falls back to raster when the PDF would be too large' "head -c 7 '$work/big.out' | grep -qa UNIRAST"

  # The per-printer header reaches a Ghostscript command line. Ghostscript has
  # devices that have been used to defeat -dSAFER, so only the ones this tool
  # emits may be named.
  { printf '%%%%ippfix device=uniprint dpi=600 maxpdf=1000\n'; cat "$work/in.pdf"; } \
    > "$work/inject.pdf"
  ./defont < "$work/inject.pdf" > "$work/inject.out" 2>"$work/inject.err"
  check 'refuses an unknown raster device' "grep -q 'refusing unknown raster device' '$work/inject.err'"
  check 'still produces output after refusing one' "[ -s '$work/inject.out' ]"

  printf '%%PDF-1.4 truncated and broken' > "$work/broken.pdf"
  ./defont < "$work/broken.pdf" > "$work/broken.out" 2>/dev/null || true
  check 'falls back to the original on failure' "[ -s '$work/broken.out' ]"
else
  echo '  skip  defont (ghostscript not installed)'
fi

echo 'hardening'
python3 - <<'PY2' && ok 'refuses hostile IPP and oversized input' || bad 'hardening'
import sys, struct
sys.path.insert(0, '.')
import ippcodec as ipp
import ippfix

# A run of delimiter bytes used to allocate one object per byte: 2MB of input
# became ~250MB of heap and OOM-killed the daemon.
try:
    ipp.parse(bytes([2, 0, 0, 0x0b, 0, 0, 0, 1]) + b'\x00' * 2_000_000)
    raise AssertionError('unbounded group allocation still accepted')
except ValueError:
    pass

# Tags that are not valid delimiters must be refused outright.
try:
    ipp.parse(bytes([2, 0, 0, 0x0b, 0, 0, 0, 1]) + b'\x09')
    raise AssertionError('invalid delimiter accepted')
except ValueError:
    pass

# Operations a print client never needs must not reach the printer. The
# printer may be reachable only through this proxy, so relaying these would
# hand every LAN host administrative control of it.
for op in (0x0013, 0x0003, 0x0007, 0x0012, 0x000D):
    assert op not in ippfix.ALLOWED_OPS, hex(op)
for op in (0x0002, 0x0004, 0x0005, 0x0006, 0x0009, 0x000B):
    assert op in ippfix.ALLOWED_OPS, hex(op)

# Attributes naming a resource the printer would fetch must be stripped.
q = ippfix.parse_queue('t=ipp://printer.example/ipp/print')
m = ipp.new_request(0x0002, 1, 'ipp://attacker/x')
g = m.operation()
g.replace('document-uri', ipp.TAG_URI, ['http://attacker.example/payload'])
g.replace('job-name', ipp.TAG_NAME, ['x'])
ippfix.rewrite_request(q, m)
assert g.index_of('document-uri') < 0, 'document-uri survived'
assert g.get_str('printer-uri') == 'ipp://printer.example/ipp/print'
assert g.index_of('job-name') >= 0, 'stripped too much'
PY2

python3 - <<'PY2' && ok 'never lets the sender pick the interpreter' || bad 'interpreter selection'
import sys
sys.path.insert(0, '.')
from ippfix import normalise_pdf

# Ghostscript reads %PDF- at a line start as PDF and anything else as
# PostScript, where the historical -dSAFER escapes live. A document must never
# be able to choose the PostScript path by looking like a PDF to us.
assert normalise_pdf(b'%PDF-1.4\n1 0 obj') is not None
assert normalise_pdf(b'junk\n%PDF-1.4\n1 0 obj') is not None
assert normalise_pdf(b'%!PS-Adobe-3.0\n%PDF-1.4\n') is None
assert normalise_pdf(b'%%Title: %PDF-1.4 report\n') is None
assert normalise_pdf(b'%!PS\n(x) print\n') is None
assert normalise_pdf(b'no marker here') is None
# Whatever comes back must start exactly at the header.
assert normalise_pdf(b'junk\n%PDF-1.4\nx').startswith(b'%PDF-')
PY2

python3 - <<'PY2' && ok 'font-cost estimate ranks known outcomes correctly' || bad 'cost estimate'
import struct
import sys
sys.path.insert(0, '.')
from ippfix import estimate_font_cost


def sfnt(padding):
    """A font program of a chosen size. Its declared glyph count is set high
    deliberately: measurement showed that number does not affect cost, and a
    test that rewarded it would re-introduce the model it disproved."""
    maxp = struct.pack('>IH', 0x00005000, 65535)
    off = 12 + 32
    head = (struct.pack('>IHHHH', 0x00010000, 2, 0, 0, 0)
            + b'maxp' + struct.pack('>III', 0, off, len(maxp))
            + b'glyf' + struct.pack('>III', 0, off + len(maxp), padding))
    return head + maxp + bytes(padding)


def pdf(font_bytes, drawn):
    prog = sfnt(font_bytes)
    text = b'BT ' + b' '.join(b'<%04X> Tj' % g for g in range(1, drawn + 1)) + b' ET'
    out = bytearray(b'%PDF-1.4\n')
    out += b'1 0 obj\n<< /Length1 %d /Length %d >>\nstream\n' % (len(prog), len(prog))
    out += prog + b'\nendstream\nendobj\n'
    out += b'2 0 obj\n<< /Type /FontDescriptor /FontFile2 1 0 R >>\nendobj\n'
    out += b'3 0 obj\n<< /Type /Font /Subtype /Type0 /DescendantFonts [4 0 R] >>\nendobj\n'
    out += b'4 0 obj\n<< /Type /Font /Subtype /CIDFontType2 /FontDescriptor 2 0 R >>\nendobj\n'
    out += b'5 0 obj\n<< /Length %d >>\nstream\n' % len(text) + text + b'\nendstream\nendobj\n'
    out += (b'6 0 obj\n<< /Type /Page /Resources << /Font << /F1 3 0 R >> >> '
            b'/Contents 5 0 R >>\nendobj\n')
    return bytes(out)


# A huge declared count with few glyphs drawn must stay cheap: a font declaring
# 65535 glyphs and drawing 27 printed on real hardware.
assert estimate_font_cost(pdf(4096, 20)) < 100

# Glyphs drawn dominate.
few, many = estimate_font_cost(pdf(4096, 20)), estimate_font_cost(pdf(4096, 700))
assert many - few > 600, (few, many)

# The font program itself costs something too: two large fonts failed at 300
# glyphs where one small font survived 523.
small, large = estimate_font_cost(pdf(4096, 100)), estimate_font_cost(pdf(2_000_000, 100))
assert large > small + 400, (small, large)

# Unreadable input must never be reported as cheap.
assert estimate_font_cost(b'%PDF-1.4\n/FontFile2 7 0 R\n') is None
PY2

python3 - <<'PY2' && ok 'recognises what conversion handed back' || bad 'format sniffing'
import sys
sys.path.insert(0, '.')
from ippfix import sniff_format
assert sniff_format(b'%PDF-1.4\\n') == 'application/pdf'
assert sniff_format(b'UNIRAST\\x00\\x00') == 'image/urf'
assert sniff_format(b'RaS2rest') == 'image/pwg-raster'
assert sniff_format(b'PCLmrest') == 'application/PCLm'
assert sniff_format(b'nonsense') is None
PY2

python3 - <<'PY2' && ok 'unreadable input is never scored as cheap' || bad 'estimator safety'
import sys, zlib
sys.path.insert(0, '.')
from ippfix import estimate_font_cost

page = b'%PDF-1.4\n1 0 obj\n<< /Type /Page /Contents 2 0 R >>\nendobj\n'

# A stream that expands enormously must not be inflated, and must not then be
# scored as "this page draws nothing".
bomb = zlib.compress(bytes(64 * 1024 * 1024), 9)
pdf = (page + b'2 0 obj\n<< /Filter /FlateDecode /Length '
       + str(len(bomb)).encode() + b' >>\nstream\n' + bomb
       + b'\nendstream\nendobj\n')
assert estimate_font_cost(pdf) is None, 'decompression bomb scored as readable'

# Contents we cannot resolve likewise means "convert", not "safe".
assert estimate_font_cost(page) is None

# But a page that genuinely has no fonts is cheap, and must still say so.
plain = (page + b'2 0 obj\n<< /Length 18 >>\nstream\n0 0 9 9 re f\n'
         b'endstream\nendobj\n')
cost = estimate_font_cost(plain)
assert cost is not None and cost < 100, cost

# The cost is per page, so it must not grow simply because a document is long.
many = page * 3 + b'2 0 obj\n<< /Length 18 >>\nstream\n0 0 9 9 re f\nendstream\nendobj\n'
assert estimate_font_cost(many) == cost, 'estimate grew with page count'
PY2

python3 - <<'PY2' && ok 'offers only formats it can stand behind' || bad 'format policy'
import sys
sys.path.insert(0, '.')
from ippfix import SAFE_FORMATS

# PostScript is handled by the interpreter that fails and cannot be converted
# the way PDF is, so it must never be offered.
assert 'application/postscript' not in SAFE_FORMATS
# PCL uses a different interpreter and is a legitimate choice; withholding it
# would remove a working path for no reason.
assert 'application/vnd.hp-PCL' in SAFE_FORMATS
assert 'application/vnd.hp-PCLXL' in SAFE_FORMATS
assert 'application/pdf' in SAFE_FORMATS
PY2

echo 'systemd units'
if command -v systemd-analyze >/dev/null 2>&1; then
  for u in ippfix.service ippfix.socket ippfix-convert.socket 'ippfix-convert@.service'; do
    check "$u is valid" "systemd-analyze verify './$u' 2>&1 | grep -qvE 'Unit .* not found' || true"
  done
  # The converter is where hostile documents are parsed; it must stay the more
  # confined of the two, and must never be given network access.
  check 'converter has no network'      "grep -q '^PrivateNetwork=true' 'ippfix-convert@.service'"
  check 'converter has no capabilities' "grep -q '^CapabilityBoundingSet=\$' 'ippfix-convert@.service'"
  check 'proxy has no capabilities'     "grep -q '^CapabilityBoundingSet=\$' ippfix.service"
  check 'proxy never runs the converter itself' \
        "! grep -qE '^ExecStart=.*defont' ippfix.service"
  # Settings placed in the wrong section are ignored with a log line nobody
  # reads, so the unit quietly does not do what its own comment says. This has
  # happened twice: Condition* in [Service], then StartLimit* in [Service],
  # which left the proxy with the default 5-starts-in-10s limit while claiming
  # to retry forever.
  python3 - <<'PY2' && ok 'unit settings are in the section systemd reads' \
    || bad 'unit section placement'
import glob, sys
UNIT_ONLY = ('Condition', 'Assert', 'StartLimit', 'Description=',
             'Documentation=', 'Requires=', 'Wants=', 'After=', 'Before=')
bad = []
for path in glob.glob('*.service') + glob.glob('*.socket') \
        + glob.glob('debian/pkg/*.service') + glob.glob('debian/pkg/*.socket') \
        + glob.glob('debian/pkg/*.path') + glob.glob('debian/pkg/selfbuild/*'):
    section = None
    for n, line in enumerate(open(path), 1):
        line = line.strip()
        if line.startswith('['):
            section = line
        elif section and section != '[Unit]' \
                and any(line.startswith(k) for k in UNIT_ONLY):
            bad.append(f'{path}:{n}: {line} in {section}')
assert not bad, '\n'.join(bad)
PY2
  # sendmail(1) queues and forks; the delivery agent outlives ExecStart. With
  # the default KillMode systemd kills it mid-SMTP and the mail is lost with
  # nothing logged -- the exact failure these reports exist to make visible.
  check 'the unit that mails reports lets delivery outlive it' \
        "grep -q '^KillMode=process\$' debian/pkg/ippfix-alert.service"
else
  echo '  skip  systemd units (systemd-analyze not available)'
fi

echo 'reporting'
python3 - <<'PY2' && ok 'a report carries the documents needed to reproduce it' || bad 'report attachments'
import os, sys, tempfile
sys.path.insert(0, '.')
import ippfix

d = tempfile.mkdtemp()
cfg = object.__new__(ippfix.Config)
cfg.archive = d
arrived = b'%PDF-1.4\n' + b'A' * 4000
sent = b'%PDF-1.4\n' + b'B' * 6000
path = os.path.join(d, '20260824-120000-q-doc.pdf')
open(path, 'wb').write(arrived)

# Both documents matter and they are not the same one: a fault that survives
# conversion is a different bug from one conversion introduced.
parts, lines = ippfix.gather_evidence(cfg, path, sent, 'outlined', 1 << 20)
assert len(parts) == 2, parts
assert parts[0][2] == arrived and parts[1][2] == sent
assert all(t == 'application/pdf' for _n, t, _b in parts), parts

# Unchanged jobs must not be attached twice under two names.
parts, _ = ippfix.gather_evidence(cfg, path, arrived, '', 1 << 20)
assert len(parts) == 1, parts

# Too large to send: compressed to fit rather than dropped...
parts, lines = ippfix.gather_evidence(cfg, path, sent, '', 3000)
assert len(parts) == 2 and all(n.endswith('.gz') for n, _t, _b in parts), parts
# ...and named rather than silently missing when even that will not fit.
parts, lines = ippfix.gather_evidence(cfg, path, sent, '', 0)
assert parts == [], parts
assert any('NOT attached' in x for x in lines), lines

# Without an archive there is no document as the client sent it, and the
# report has to say so rather than implying the attachment is the original.
cfg2 = object.__new__(ippfix.Config)
cfg2.archive = None
parts, lines = ippfix.gather_evidence(cfg2, None, sent, '', 1 << 20)
assert len(parts) == 1 and 'given-to-printer' in parts[0][0], parts
assert any('--archive is off' in x for x in lines), lines
PY2

python3 - <<'PY2' && ok 'alerts are well-formed mail with attachments' || bad 'alert MIME'
import email, email.policy, sys, subprocess
sys.path.insert(0, '.')
import ippfix

captured = []
class Done:
    returncode = 0
    stderr = b''
subprocess.run = lambda cmd, input=None, **kw: (captured.append((cmd, input)), Done())[1]

a = ippfix.Alerter('someone@example.com', 6)
a.send('subject', 'body\n', [('doc.pdf', 'application/pdf', b'%PDF-1.4\nx')])
cmd, raw = captured[0]
# Envelope sender must match the header, or strict receivers refuse it, and
# both are the recipient: that address is known to route, where ippfix@ plus
# whatever this host calls itself frequently does not.
assert cmd[:3] == ['/usr/sbin/sendmail', '-f', 'someone@example.com'], cmd
msg = email.message_from_bytes(raw, policy=email.policy.default)
assert msg.is_multipart(), 'attachments must not collapse the message'
kids = list(msg.iter_parts())
assert kids[0].get_content().startswith('body'), kids[0].get_content()
assert kids[1].get_filename() == 'doc.pdf'
assert kids[1].get_content() == b'%PDF-1.4\nx'
assert msg['From'] == 'ippfix <someone@example.com>', msg['From']
assert msg['To'] == 'someone@example.com', msg['To']
PY2

echo 'page counter'
python3 - <<'PY2' && ok 'the page counter is checked before it is believed' || bad 'page counter trust'
import logging, sys
sys.path.insert(0, '.')
logging.disable(logging.CRITICAL)      # this deliberately provokes error logs
import ippfix

def counter(unit=7):
    q = ippfix.parse_queue('t=ipp://printer.example/ipp/print')
    pc = ippfix.PageCounter(q)
    pc.probed, pc.unit = True, unit
    return pc

# The printer says what its counter counts. Anything but impressions or sheets
# is a correct answer to that OID and a useless one here.
assert counter(7).trusted and counter(8).trusted
assert not counter(5).trusted, 'characters is not a page count'
assert not counter(None).trusted

# Nothing is believed enough to raise an alert until it has been seen to work.
pc = counter()
_lines, contradicted = pc.assess(100, 100, 3)
assert not contradicted, 'an uncorroborated counter must not accuse a printer'
_lines, contradicted = pc.assess(100, 103, 3)      # now it has been seen to move
assert not contradicted and pc.proven
_lines, contradicted = pc.assess(103, 103, 3)
assert contradicted, 'a proven counter that does not move is the finding'

# ...and three of those means the instrument is broken, not the printer. The
# last one must not also be reported: we have just decided not to believe it.
pc = counter(); pc.assess(0, 1, 1)
verdicts = [pc.assess(1, 1, 1)[1] for _ in range(3)]
assert verdicts == [True, True, False], verdicts
assert not pc.enabled and 'PROBABLY WRONG' in pc.reason

# Backwards once is a cartridge change; twice is not.
pc = counter()
assert pc.delta(500, 400) is None
pc.assess(500, 400, 1); assert pc.enabled
pc.assess(400, 300, 1); assert not pc.enabled
# ...but a Counter32 wrap is not backwards at all.
assert counter().delta((1 << 32) - 5, 3) == 8

# A jump too large to be paper says the OID is not a page count. A merely large
# one does not: this proxy is not the only way to reach a printer.
pc = counter(); pc.assess(0, ippfix.MAX_PLAUSIBLE_JUMP - 1, 1)
assert pc.enabled
pc = counter(); pc.assess(0, ippfix.MAX_PLAUSIBLE_JUMP + 1, 1)
assert not pc.enabled

# Silence is also a verdict.
pc = counter()
for _ in range(ippfix.MISS_LIMIT):
    pc.assess(None, None, 1)
assert not pc.enabled
PY2

python3 - <<'PY2' && ok 'per-printer settings ride on the printer URI' || bad 'printer URI options'
import sys
sys.path.insert(0, '.')
import ippfix

q = ippfix.parse_queue('t=ipp://p.example/ipp/print')
assert q.want_page_counter and q.community == 'public'
assert q.snmp_relay is None, 'unset is not the same as off'
q = ippfix.parse_queue('t=ipp://p.example/ipp/print?page-counter=off&community=x')
assert not q.want_page_counter and q.community == 'x'
assert q.path == '/ipp/print', 'options must not leak into the upstream path'
# A mistyped option that is silently ignored is how a printer ends up not doing
# what its configuration says.
for bad_uri in ('t=ipp://p/ipp/print?pagecounter=off',
                't=ipp://p/ipp/print?page-counter=maybe'):
    try:
        ippfix.parse_queue(bad_uri)
    except ValueError:
        continue
    raise AssertionError(bad_uri)

# SNMP carries nothing that names a printer, so one listener speaks for one
# printer -- and with several printers the daemon must refuse rather than pick.
def qs(*specs):
    return [ippfix.parse_queue(s) for s in specs]
one = qs('a=ipp://p1/ipp/print')
assert ippfix.choose_relay_queue(one)[0] is one[0]
two = qs('a=ipp://p1/ipp/print', 'b=ipp://p2/ipp/print')
picked, why = ippfix.choose_relay_queue(two)
assert picked is None and 'snmp-relay' in why, why
marked = qs('a=ipp://p1/ipp/print?snmp-relay=on', 'b=ipp://p2/ipp/print')
assert ippfix.choose_relay_queue(marked)[0].name == 'a'
both = qs('a=ipp://p1/ipp/print?snmp-relay=on', 'b=ipp://p2/ipp/print?snmp-relay=on')
assert ippfix.choose_relay_queue(both)[0] is None

# One address per printer is the way to serve several at once: the address
# does the naming the protocol will not.
pair = qs('a=ipp://p1/ipp/print?snmp-relay=10.0.0.1',
          'b=ipp://p2/ipp/print?snmp-relay=10.0.0.2')
assert ippfix.choose_relay_queue(pair, '10.0.0.1')[0].name == 'a'
assert ippfix.choose_relay_queue(pair, '10.0.0.2')[0].name == 'b'
assert ippfix.choose_relay_queue(pair, '10.0.0.3')[0] is None
# A printer with its own listener must not also be answered by the wildcard.
assert ippfix.choose_relay_queue(pair, None)[0] is None
mixed = qs('a=ipp://p1/ipp/print?snmp-relay=10.0.0.1', 'c=ipp://p3/ipp/print')
assert ippfix.choose_relay_queue(mixed, None)[0].name == 'c'
# Two printers claiming one address is a configuration error, not a coin toss.
clash = qs('a=ipp://p1/ipp/print?snmp-relay=10.0.0.1',
           'b=ipp://p2/ipp/print?snmp-relay=10.0.0.1')
assert ippfix.choose_relay_queue(clash, '10.0.0.1')[0] is None
try:
    ippfix.parse_queue('a=ipp://p/ipp/print?snmp-relay=nonsense')
except ValueError:
    pass
else:
    raise AssertionError('an unparseable listen address must not be accepted')
PY2

echo 'snmp relay'
python3 - <<'PY2' && ok 'the relay answers only what it promised to' || bad 'snmp relay policy'
import ipaddress, logging, sys
sys.path.insert(0, '.')
logging.disable(logging.CRITICAL)
import ippfix, snmpmini as snmp

q = ippfix.parse_queue('t=ipp://198.51.100.9/ipp/print')
relay = ippfix.SnmpRelay(q)

def req(oid, pdu=snmp.GET, version=snmp.V2C):
    return snmp.encode_request(oid, 'public', pdu, version=version)[1]

# Refused before the printer is ever contacted, so acceptable() is the test:
# handle() would try to forward and time out.
assert relay.acceptable(snmp.parse(req('1.3.6.1.2.1.43.10.2.1.4.1.1'))) is None
assert relay.acceptable(snmp.parse(req('1.3.6.1.2.1.1.1.0'))) is None
assert relay.acceptable(snmp.parse(req('1.3.6.1.2.1.43.10', snmp.GETNEXT))) is None
# GETBULK is the amplifier; SET can reset the printer; the rest is not ours.
assert relay.acceptable(snmp.parse(req('1.3.6.1.2.1.43.10', snmp.GETBULK)))
assert relay.acceptable(snmp.parse(req('1.3.6.1.2.1.43.5.1.1.3.1', snmp.SET)))
assert relay.acceptable(snmp.parse(req('1.3.6.1.2.1.2.2.1.6.1')))
assert relay.acceptable(snmp.parse(req('1.3.6.1.4.1.11.2.3.9.1')))
assert relay.acceptable(snmp.parse(req('1.3.6.1.2.1.1.1.0', version=3)))

# Malformed input is a non-event, not an incident.
for junk in (b'', b'\x30', b'\x30\x05\x02\x01', b'\x30\x82\xff\xff junk',
             b'\x30\x03\x02\x01\x00', bytes(9000)):
    assert relay.handle(junk, ('192.0.2.1', 1)) is None

# Rate limited per source, and over the limit nothing is sent: answering is
# the amplification.
r2 = ippfix.SnmpRelay(q)
allowed = sum(1 for _ in range(50) if r2.allowed_rate('192.0.2.1'))
assert allowed == r2.PER_SOURCE_BURST, allowed
# The per-source table must not grow without bound on spoofed sources.
r3 = ippfix.SnmpRelay(q)
for i in range(r3.MAX_SOURCES + 200):
    r3.allowed_rate('192.0.2.%d' % (i % 250))
assert len(r3.sources) <= r3.MAX_SOURCES + 1, len(r3.sources)

r4 = ippfix.SnmpRelay(q, [ipaddress.ip_network('10.0.0.0/8')])
assert r4.permitted_source('10.1.2.3') and not r4.permitted_source('192.0.2.1')
assert not r4.permitted_source('not-an-address')
# A dual-stack socket reports an IPv4 peer as ::ffff:10.1.2.3, which matches no
# IPv4 network an administrator would type -- so --snmp-allow would have
# blocked everyone while looking correct.
assert ippfix.client_ip(('::ffff:10.1.2.3', 161)) == '10.1.2.3'
assert ippfix.client_ip(('2001:db8::1', 161)) == '2001:db8::1'
assert r4.permitted_source(ippfix.client_ip(('::ffff:10.1.2.3', 161)))
PY2

python3 - <<'PY2' && ok 'SNMP parsing survives what arrives from a network' || bad 'snmp codec'
import sys
sys.path.insert(0, '.')
import snmpmini as snmp

rid, packet = snmp.encode_request('1.3.6.1.2.1.43.10.2.1.4.1.1', 'secret')
msg = snmp.parse(packet)
assert msg.community == 'secret' and msg.pdu_type == snmp.GET
assert msg.oids == ['1.3.6.1.2.1.43.10.2.1.4.1.1'] and msg.request_id == rid
assert snmp.decode_oid(snmp.encode_request('1.3.6.1.4.1.99999.1')[1][-13:-2]) or True

# Round-tripping an OID with a multi-byte arc, which is where hand-rolled BER
# usually breaks.
for text in ('1.3.6.1.2.1.43.10.2.1.4.1.1', '1.3.6.1.4.1.11.2.3.9.1',
             '1.3.6.1.4.1.2147483647.1', '0.0'):
    body = snmp.encode_oid(text)
    assert snmp.decode_oid(body[2:]) == text, text

# Printer firmware pads strings with NUL and puts newlines in them: an HP M553
# answers prtMarkerSuppliesDescription NUL-terminated, and an M430 answers its
# console text with an embedded line break. Neither belongs in a report.
assert snmp._value(snmp.T_OCTETS, b'Black Cartridge\x00') == 'Black Cartridge'
assert '\x00' not in snmp._value(snmp.T_OCTETS, b'a\x00b')

# Every length is checked against the buffer before it is used.
for junk in (b'', b'\x30', b'\x30\x84\xff\xff\xff\xff', b'\x30\x80\x00\x00',
             b'\x30\x02\x02\x7f', b'\x02\x01\x00'):
    try:
        snmp.parse(junk)
    except snmp.SnmpError:
        continue
    raise AssertionError(repr(junk))
try:
    snmp.parse(bytes(snmp.MAX_DATAGRAM + 1))
except snmp.SnmpError:
    pass
else:
    raise AssertionError('oversized datagram accepted')
PY2

echo 'documentation'
check 'man page renders without warnings' \
      "[ -z \"\$(man --warnings -l ./ippfix.8 2>&1 >/dev/null)\" ]"
# Compare the options the manual declares with the options the program accepts.
# Only the .B/.BR header lines inside .SH OPTIONS count: scanning the whole page
# meant the manual could not mention any other program's long option -- naming
# 'defont --selfcheck' in prose was reported as a missing option.
check 'man page and --help list the same options' \
      "diff <(python3 ippfix.py --help 2>/dev/null | grep -oE -- '--[a-z0-9-]+' | grep -v '^--help$' | sort -u) \
            <(sed -e 's/\\\\f[BIRP]//g' -e 's/\\\\-/-/g' ippfix.8 \
              | awk '/^\\.SH OPTIONS/{f=1;next} /^\\.SH /{f=0} f' \
              | grep -E '^\\.BR? ' | grep -oE -- '--[a-z0-9-]+' | sort -u)"
check 'README references DEPLOYMENT.md' "grep -q 'DEPLOYMENT.md' README.md"
check 'no absolute home paths leaked' \
      "! grep -rqE '/home/[a-z]+/' --exclude-dir=.git --exclude-dir=__pycache__ ."

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
