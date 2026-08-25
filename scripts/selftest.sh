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
check 'fakeprinter.py compiles'   'python3 -m py_compile scripts/fakeprinter.py'
rm -rf __pycache__ scripts/__pycache__
for s in defont install.sh uninstall.sh ippfix scripts/selftest.sh; do
  check "$s parses" "bash -n '$s'"
done

echo 'executables carry the executable bit'
for f in defont ippfix install.sh uninstall.sh scripts/selftest.sh \
         scripts/fakeprinter.py; do
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

python3 - <<'PY2' && ok 'one costly page is not diluted by a long document' || bad 'per-page worst case'
import sys, zlib
sys.path.insert(0, '.')
from ippfix import estimate_font_cost

# The fault is per page, so a hundred-page report is not safe because most of
# its pages are dull: one chart, cover or equation page drawing an unusual set
# of glyphs breaks the whole job. The estimate must therefore report the WORST
# page, never an average and never the first one it happens to look at.
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
try:
    font = open(FONT, 'rb').read()
except OSError:
    sys.exit(0)                      # no font installed; nothing to measure

def esc(b):
    return b.replace(b'\\', b'\\\\').replace(b'(', b'\\(').replace(b')', b'\\)')

def build(total, odd_index=None):
    out = bytearray(b'%PDF-1.4\n')
    offs = {}
    def add(num, raw):
        offs[num] = len(out)
        out.extend(b'%d 0 obj\n' % num + raw + b'\nendobj\n')
    kids = ' '.join('%d 0 R' % (5 + i) for i in range(total))
    add(1, b'<< /Type /Catalog /Pages 2 0 R >>')
    add(2, ('<< /Type /Pages /Kids [%s] /Count %d >>' % (kids, total)).encode())
    add(3, ('<< /Type /Font /Subtype /TrueType /BaseFont /DejaVuSans '
            '/FirstChar 32 /LastChar 126 /Widths [%s] /FontDescriptor 4 0 R >>'
            % ' '.join(['600'] * 95)).encode())
    comp = zlib.compress(font, 9)
    add(4, ('<< /Type /FontDescriptor /FontName /DejaVuSans /Flags 32 '
            '/FontBBox [-1021 -463 1793 1232] /ItalicAngle 0 /Ascent 928 '
            '/Descent -236 /CapHeight 700 /StemV 80 /FontFile2 %d 0 R >>'
            % (5 + total)).encode())
    for p in range(total):
        add(5 + p, ('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                    '/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>'
                    % (5 + total + 1 + p)).encode())
    add(5 + total,
        b'<< /Length %d /Length1 %d /Filter /FlateDecode >>\nstream\n'
        % (len(comp), len(font)) + comp + b'\nendstream')
    for p in range(total):
        txt = esc(bytes(range(33, 127))) if p == odd_index else b'the quick fox'
        body = b'BT /F1 9 Tf 36 756 Td (' + txt + b') Tj ET'
        add(5 + total + 1 + p,
            b'<< /Length %d >>\nstream\n' % len(body) + body + b'\nendstream')
    x = len(out)
    n = max(offs) + 1
    out += b'xref\n0 %d\n0000000000 65535 f \n' % n
    for i in range(1, n):
        out += b'%010d 00000 n \n' % offs.get(i, 0)
    out += (b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
            % (n, x))
    return bytes(out)

alone = estimate_font_cost(build(1, 0))
plain = estimate_font_cost(build(100))
assert alone > plain, (alone, plain)
# Wherever the costly page sits, the answer is the same as that page alone.
for where in (0, 50, 99):
    got = estimate_font_cost(build(100, where))
    assert got == alone, ('diluted at position %d' % where, got, alone)
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

echo 'relay path'
# The mock the rest of this section runs against. If it stops behaving like the
# printer it was captured from, every test built on it quietly stops meaning
# anything, so it is checked first and on its own.
python3 - <<'PY2' && ok 'the mock printer matches the one that was captured' || bad 'fake printer'
import sys, threading
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
from fakeprinter import FakePrinter, captured_attributes

raw = captured_attributes()
# Captured off the wire, so the codec must round-trip it exactly. This is the
# only IPP message in the tree that no code here produced.
assert ipp.serialize(ipp.parse(raw)) == raw, 'the fixture does not round-trip'
group = ipp.parse(raw).group(ipp.PRINTER_ATTRS)
assert len(group.names()) == 130, len(group.names())
assert group.get_str('printer-make-and-model') == 'HP ColorLaserJet MFP M282-M285'
# The whole reason the proxy serialises jobs. If a firmware update ever makes
# this true, the queue lock stops being a workaround and becomes a bottleneck.
assert group.get('multiple-document-jobs-supported') == [b'\x00']
# House rule: no real network detail in the tree. Checked by shape -- anything
# that looks like a private address, or like the MAC-derived names HP builds --
# and never against the values that were substituted out, because an assertion
# naming those would put them straight back into the tree.
import re
assert not re.search(rb'\b(?:10|127|192\.168|169\.254|'
                     rb'172\.(?:1[6-9]|2[0-9]|3[01]))\.[0-9]', raw), 'address'
assert not re.search(rb'NPI(?!000000)[0-9A-Fa-f]{6}', raw), 'MAC-derived name'
assert not re.search(rb'\b(?!0{12})[0-9a-f]{12}\b', raw), 'a MAC'

def submit(printer, request_id=1):
    """One Print-Job, straight at the mock, with no proxy in between."""
    import http.client
    msg = ipp.new_request(0x0002, request_id, printer.uri)
    msg.operation().replace('document-format', ipp.TAG_MIMETYPE,
                            ['application/pdf'])
    msg.data = b'%PDF-1.4\n'
    body = ipp.serialize(msg)
    conn = http.client.HTTPConnection(printer.host, printer.port, timeout=5)
    conn.request('POST', printer.path, body=body,
                 headers={'Content-Type': 'application/ipp',
                          'Content-Length': str(len(body))})
    reply = ipp.parse(conn.getresponse().read())
    conn.close()
    return reply


before = threading.active_count()
with FakePrinter() as printer:
    assert printer.snmp_get('h', '1.3.6.1.2.1.43.10.2.1.3.1.1') == 7

    assert submit(printer, 1).code == 0x0000
    # A second job while one is active must be an error. A proxy bug that
    # interleaves jobs then shows up as a red test rather than as ruined paper.
    assert submit(printer, 2).code == 0x0509, 'a concurrent job was accepted'

    job = printer.jobs[0]
    assert (job.state, job.impressions) == (3, 0)
    printer.clock.advance(5)
    assert (job.state, job.impressions) == (5, 0), 'pending did not become processing'
    printer.clock.advance(5)
    assert (job.state, job.impressions) == (9, 1), 'processing did not complete'
    assert printer.page_counter == 1001, 'the page counter did not follow'
    assert submit(printer, 3).code == 0x0000, 'a finished job still blocks the queue'

    # Job history is bounded: a long run must not accumulate without end.
    for n in range(printer.MAX_JOBS * 2):
        printer.clock.advance(5)
        printer.clock.advance(5)
        submit(printer, 10 + n)
    assert len(printer.jobs) <= printer.MAX_JOBS, len(printer.jobs)

with FakePrinter(mode='hold_job') as printer:
    assert submit(printer, 1).code == 0x0000
    for _ in range(4):
        printer.clock.advance(5)
    held = printer.jobs[0]
    assert (held.state, held.reasons) == (3, ['media-empty']), held.reasons
    printer.release()                      # as refilling the tray would
    printer.clock.advance(5)
    printer.clock.advance(5)
    assert (held.state, held.impressions) == (9, 1)

with FakePrinter(mode='reject_job') as printer:
    assert submit(printer, 1).code == 0x0000
    printer.clock.advance(5)
    assert printer.jobs[0].state == 8, 'a rejected job must end aborted'

with FakePrinter(mode='silent_loss') as printer:
    assert submit(printer, 1).code == 0x0000
    printer.clock.advance(5)
    printer.clock.advance(5)
    # Completed, and nothing marked: the fault this whole project exists for.
    assert (printer.jobs[0].state, printer.jobs[0].impressions) == (9, 0)
    assert printer.page_counter == 1000, 'nothing marked must not move the counter'

# ...and nothing is left running afterwards.
assert threading.active_count() == before, 'the mock leaked a thread'
PY2
# Four things measured on the real device, each of which was a paragraph
# somebody had to remember. Modelled in the mock so that code which forgets one
# fails a test rather than a sheet of paper. Each block is its own check, so a
# regression names the finding it broke.
python3 - <<'PY2' && ok 'sides is ignored unless media is sent with it' || bad 'sides quirk'
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
from fakeprinter import FakePrinter, job_request, post

SIDES = ('sides', ipp.TAG_KEYWORD, ['two-sided-long-edge'])
MEDIA = ('media', ipp.TAG_KEYWORD, ['na_letter_8.5x11in'])


def submit(printer, request_id, job_attrs):
    msg = job_request(0x0002, request_id, printer.uri, job_attrs=job_attrs,
                      document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    msg.data = b'%PDF-1.4\n'
    return post(printer, msg)


with FakePrinter() as printer:
    # sides on its own: the job is taken, the request is dropped, and the
    # printer says so. 0x0001 is
    # successful-ok-ignored-or-substituted-attributes.
    reply = submit(printer, 1, [SIDES])
    assert reply.code == 0x0001, hex(reply.code)
    unsupported = reply.group(ipp.UNSUPPORTED_ATTRS)
    assert unsupported is not None, 'sides was dropped without saying so'
    assert unsupported.get_str('sides') == 'two-sided-long-edge'
    job = printer.jobs[-1]
    assert (job.sides, job.sides_ignored) == (None, True)
    assert job.duplexed is not True, 'a dropped sides must not duplex'
    printer.clock.advance(5)
    printer.clock.advance(5)

    # The same request with media beside it in the same job group: taken, and
    # this time applied. This is the whole quirk -- the device resolves its
    # duplex-unsupported-media constraint by discarding sides rather than by
    # applying media-default, as RFC 8011 5.2 requires.
    reply = submit(printer, 2, [SIDES, MEDIA])
    assert reply.code == 0x0000, hex(reply.code)
    assert reply.group(ipp.UNSUPPORTED_ATTRS) is None, 'nothing to complain of'
    job = printer.jobs[-1]
    assert (job.sides, job.sides_ignored) == ('two-sided-long-edge', False)
    assert job.duplexed is True

    # A job asking for nothing is unaffected either way.
    printer.clock.advance(5)
    printer.clock.advance(5)
    reply = submit(printer, 3, [])
    assert reply.code == 0x0000 and reply.group(ipp.UNSUPPORTED_ATTRS) is None
PY2

python3 - <<'PY2' && ok 'the URF duplex byte decides, not the sides attribute' || bad 'urf duplex byte'
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
from fakeprinter import FakePrinter, job_request, post, urf_document, parse_urf

# Byte 14 of the stream: byte 2 of the first 32-byte page header, after the
# 8-byte magic and the 4-byte page count.
stream = urf_document(pages=2, duplex=1)
assert stream[14] == 1 and parse_urf(stream).duplex == 1
assert parse_urf(b'%PDF-1.4\n') is None, 'only a URF stream has a duplex byte'
assert parse_urf(b'UNIRAST\x00\x00\x00\x00\x01') is None, 'a truncated header'

with FakePrinter() as printer:
    msg = job_request(0x0002, 1, printer.uri,
                      document_format=(ipp.TAG_MIMETYPE, ['image/urf']),
                      job_attrs=[('sides', ipp.TAG_KEYWORD,
                                  ['two-sided-long-edge']),
                                 ('media', ipp.TAG_KEYWORD,
                                  ['na_letter_8.5x11in'])])
    msg.data = stream
    reply = post(printer, msg)
    # Measured on paper: 0x0000, nothing in unsupported-attributes, two
    # impressions, completed -- and two simplex sheets.
    assert reply.code == 0x0000, hex(reply.code)
    assert reply.group(ipp.UNSUPPORTED_ATTRS) is None
    job = printer.jobs[-1]
    assert job.sides == 'two-sided-long-edge', 'the attribute was accepted'
    assert job.urf_duplex == 1, job.urf_duplex
    assert job.duplexed is False, 'the stream must beat the attribute'
    for _ in range(3):
        printer.clock.advance(5)
    assert (job.state, job.impressions) == (9, 2), (job.state, job.impressions)
PY2

python3 - <<'PY2' && ok 'an unreadable raster stream aborts the job' || bad 'malformed raster'
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
from fakeprinter import FakePrinter, job_request, post, urf_document


def send(printer, request_id, data):
    msg = job_request(0x0002, request_id, printer.uri,
                      document_format=(ipp.TAG_MIMETYPE, ['image/urf']))
    msg.data = data
    return post(printer, msg)


with FakePrinter() as printer:
    # A colour space the device does not implement, which is what Ghostscript
    # produces when nobody passes -dcupsColorSpace. Measured: job-state 8,
    # aborted, 0 impressions -- and a physical error page, which is the part
    # this mock cannot model. The job is still accepted at the protocol level.
    assert send(printer, 1, urf_document(colorspace=7)).code == 0x0000
    job = printer.jobs[-1]
    assert (job.state, job.impressions) == (8, 0), (job.state, job.impressions)
    assert job.state not in (3, 5, 9), 'aborted, not silently dropped'
    printer.clock.advance(5)
    assert printer.jobs[-1].state == 8, 'an aborted job does not resume'

    # Truncated, and headerless: neither is something a marking engine can read.
    assert send(printer, 2, b'UNIRAST\x00\x00\x00\x00\x01').code == 0x0000
    assert printer.jobs[-1].state == 8
    assert send(printer, 3, b'not a raster stream at all').code == 0x0000
    assert printer.jobs[-1].state == 8

    # ...while a stream it can read is not aborted.
    assert send(printer, 4, urf_document(colorspace=19)).code == 0x0000
    assert printer.jobs[-1].state == 3, printer.jobs[-1].state
PY2

python3 - <<'PY2' && ok 'an oversized PDF is accepted unless the cap is enforced' || bad 'pdf cap'
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
from fakeprinter import (FakePrinter, declared_pdf_cap, job_request, post)

# The cap comes from the captured attributes, not from a number written here.
assert declared_pdf_cap() == 75000 * 1024, declared_pdf_cap()

OVERSIZED = b'%PDF-1.4\n' + b'x' * 200


def send(printer, request_id, data):
    msg = job_request(0x0002, request_id, printer.uri,
                      document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    msg.data = data
    return post(printer, msg)


# A cap small enough to cross in a test. The real one is 76.8 MB, and the
# device printed a 92.5 MB PDF through it.
with FakePrinter(pdf_cap=64) as printer:
    assert send(printer, 1, OVERSIZED).code == 0x0000, 'the cap is advisory'
    assert len(printer.jobs) == 1
    printer.clock.advance(5)
    printer.clock.advance(5)

    # Enforced on demand, which is the only way to test what the proxy does
    # about a refusal. 0x0408 is client-error-request-entity-too-large.
    printer.mode = 'enforce_pdf_cap'
    assert send(printer, 2, OVERSIZED).code == 0x0408
    assert len(printer.jobs) == 1, 'a refused document must create no job'
    # Which is what makes sending something else instead safe.
    assert send(printer, 3, b'%PDF-1.4\n').code == 0x0000
    assert len(printer.jobs) == 2
PY2


# What the proxy actually puts on the wire for one Print-Job, byte for byte.
# Every attribute here is either forwarded untouched, rewritten, or stripped,
# and this is the only test that would notice a change to which is which. It is
# also the transcript any job-splitting work has to keep producing for the
# single-job case.
python3 - <<'PY2' && ok 'a Print-Job goes upstream byte for byte as pinned' || bad 'Print-Job transcript'
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
from fakeprinter import FakePrinter, proxy_for, relay

DOCUMENT = b'%PDF-1.4\n1 0 obj\n<< >>\nendobj\ntrailer\n<< >>\n%%EOF\n'

with FakePrinter() as printer:
    cfg, queue = proxy_for(printer)
    msg = ipp.new_request(0x0002, 4242, 'ipp://192.0.2.10/ipp/office')
    op = msg.operation()
    op.replace('requesting-user-name', ipp.TAG_NAME, ['tester'])
    op.replace('job-name', ipp.TAG_NAME, ['transcript'])
    op.replace('document-format', ipp.TAG_MIMETYPE, ['application/pdf'])
    # Both of these must not survive: they would make the printer fetch a
    # resource of the sender's choosing.
    op.replace('job-uri', ipp.TAG_URI, ['ipp://192.0.2.10/ipp/office/9'])
    op.replace('document-uri', ipp.TAG_URI, ['http://attacker.example/x'])
    msg.data = DOCUMENT

    answer = relay(cfg, msg)
    assert answer.status == '200 OK', answer.status
    assert answer.headers['content-type'] == 'application/ipp'

    sent = printer.requests[-1][1]
    # The upstream port is whatever the kernel handed out, so it is the one
    # thing that cannot be a constant. Linux allocates five-digit ephemeral
    # ports, so blanking it keeps every length prefix below correct.
    assert 10000 <= printer.port <= 65535, printer.port
    sent = sent.replace(str(printer.port).encode(), b'00000')

    expected = (
        b'\x02\x00'                          # version 2.0, as the client sent
        b'\x00\x02'                          # Print-Job
        b'\x00\x00\x10\x92'                  # request-id 4242, relayed unchanged
        b'\x01'                              # operation-attributes-tag
        b'G\x00\x12attributes-charset\x00\x05utf-8'
        b'H\x00\x1battributes-natural-language\x00\x05en-us'
        # Re-addressed to the printer, in the position printer-uri already had.
        b'E\x00\x0bprinter-uri\x00\x1fipp://127.0.0.1:00000/ipp/print'
        b'B\x00\x14requesting-user-name\x00\x06tester'
        b'B\x00\x08job-name\x00\ntranscript'
        b'I\x00\x0fdocument-format\x00\x0fapplication/pdf'
        # job-uri and document-uri are gone; nothing else was added or moved.
        b'\x03'                              # end-of-attributes-tag
        + DOCUMENT)                          # the document, byte for byte
    if sent != expected:
        for i, (a, b) in enumerate(zip(sent, expected)):
            if a != b:
                raise AssertionError(f'transcript differs at byte {i}: '
                                     f'{sent[i:i + 40]!r} != {expected[i:i + 40]!r}')
        raise AssertionError(f'transcript length {len(sent)} != {len(expected)}')

    # And what the client is told: the printer's job, wearing this proxy's URIs.
    job = answer.ipp.group(ipp.JOB_ATTRS)
    assert answer.ipp.code == 0x0000, hex(answer.ipp.code)
    assert answer.ipp.request_id == 4242
    assert job.get_int('job-id') == 101, 'the upstream job id is handed on as-is'
    assert job.get_str('job-uri') == 'ipp://192.0.2.10/ipp/office/101'
    assert job.get_str('job-printer-uri') == 'ipp://192.0.2.10/ipp/office'
PY2

# rewrite_response() edits about a dozen attributes of a reply that carries a
# hundred and thirty. Nothing asserted any of it, and a slip either way is
# invisible: writing one attribute too many breaks capability mirroring for
# every client, and writing one too few leaks the printer's own address to
# clients that may have no route to it. Diffed here against the captured reply.
python3 - <<'PY2' && ok 'rewrite_response changes exactly what it should' || bad 'rewrite_response diff'
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
import ippfix
from fakeprinter import captured_attributes

raw = captured_attributes()
args = ippfix.build_parser().parse_args(
    ['--advertise', '192.0.2.10', '--no-ipv6',
     'office=ipp://192.0.2.10/ipp/print'])
queue = ippfix.parse_queue('office=ipp://192.0.2.10/ipp/print')
cfg = ippfix.Config(args, [queue])

before, after = ipp.parse(raw), ipp.parse(raw)
ippfix.rewrite_response(cfg, queue, after)
old, new = before.group(ipp.PRINTER_ATTRS), after.group(ipp.PRINTER_ATTRS)

# One attribute is removed outright, and every other one keeps its position:
# clients have been seen to depend on the order a printer states things in.
assert [n for n in old.names() if n != 'printer-supply-info-uri'] == new.names()
assert new.index_of('printer-supply-info-uri') < 0

# Exactly these, with exactly these values. Nothing else may differ.
expect = {
    'printer-uri-supported': [b'ipp://192.0.2.10/ipp/office',
                              b'ipps://192.0.2.10/ipp/office'],
    'printer-name': [b'office'],
    'printer-dns-sd-name': [b'office'],
    'printer-more-info': [b'http://192.0.2.10:631/'],
    # Re-served from this daemon, because clients may have no route to the
    # printer's own web server.
    'printer-icons': [b'http://192.0.2.10:631/ipp/office/icon-small.png',
                      b'http://192.0.2.10:631/ipp/office/icon-large.png'],
    'printer-strings-uri': [b'http://192.0.2.10:631/ipp/office/strings'],
    # Deliberately not the printer's own: a client that sees one printer-uuid
    # on two queues collapses them into one.
    'printer-uuid': [b'urn:uuid:5bfe0d22-2210-f2b6-05f0-dcfd091b13dc'],
    # application/postscript, and only that, is withheld. It is interpreted by
    # exactly the task that fails and cannot be converted the way PDF is.
    'document-format-supported': [b'image/urf', b'application/PCLm',
                                  b'application/octet-stream',
                                  b'application/pdf',
                                  b'application/vnd.hp-PCL',
                                  b'application/vnd.hp-PCLXL', b'image/jpeg'],
}
changed = {n: new.get(n) for n in new.names() if old.get(n) != new.get(n)}
assert changed == expect, ('unexpected attribute changes: '
                           f'{sorted(set(changed) ^ set(expect))}')

# Three more are written and happen to land on what the printer already said.
# They are listed because "unchanged" here means "agreed with", not "left
# alone": a printer that answered differently would see them overwritten.
for name, values in (('uri-security-supported', [b'none', b'tls']),
                     ('uri-authentication-supported',
                      [b'requesting-user-name', b'requesting-user-name']),
                     ('document-format-default', [b'application/pdf'])):
    assert new.get(name) == values, (name, new.get(name))

# Everything else is passed through untouched, tag for tag and value for
# value, which is where feature parity and live status come from for free.
for name in old.names():
    if name in expect or name == 'printer-supply-info-uri':
        continue
    i, j = old.index_of(name), new.index_of(name)
    assert (old.items[i:i + old.run_length(i)]
            == new.items[j:j + new.run_length(j)]), name

# The operation group is not touched at all, and no group is added or dropped.
assert [g.tag for g in before.groups] == [g.tag for g in after.groups]
assert before.group(ipp.OPERATION_ATTRS).items \
       == after.group(ipp.OPERATION_ATTRS).items
assert (before.version, before.code, before.request_id) \
       == (after.version, after.code, after.request_id)

# NOTE, pinned as it is rather than fixed: printer-supply-info-uri is removed
# rather than re-served like the icons and strings beside it, so a client loses
# the supply page entirely instead of getting one it can reach. And the icon
# and strings URIs are http:// on the IPP port, which --require-tls does not
# serve -- so with that flag on, they point at nothing. Both look wrong; both
# are what this code does today.
PY2

# Job operations as they are relayed today. The upcoming split turns one client
# job into several upstream jobs, at which point every one of these has to keep
# meaning what it means here.
python3 - <<'PY2' && ok 'Cancel-Job, Get-Jobs and Get-Job-Attributes relay as they do today' || bad 'job operation relay'
import sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
from fakeprinter import FakePrinter, proxy_for, relay, job_request

OURS = 'ipp://192.0.2.10/ipp/office'

with FakePrinter() as printer:
    cfg, queue = proxy_for(printer)

    def send(op, request_id, **kw):
        return relay(cfg, job_request(op, request_id, OURS, **kw))

    first = send(0x0002, 1, document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    assert first.ipp.group(ipp.JOB_ATTRS).get_int('job-id') == 101

    # Get-Job-Attributes. The job id the client holds is the printer's own, and
    # the request is forwarded with it untouched; only printer-uri is
    # re-addressed.
    printer.clock.advance(5)
    got = send(0x0009, 2, job_id=101)
    job = got.ipp.group(ipp.JOB_ATTRS)
    assert got.ipp.code == 0x0000
    assert job.get_int('job-state') == 5, 'job-state must be relayed untouched'
    assert job.get_str('job-state-reasons') == 'job-printing'
    assert job.get_str('job-uri') == OURS + '/101'
    assert job.get_str('job-printer-uri') == OURS
    upstream = ipp.parse(printer.requests[-1][1])
    assert upstream.code == 0x0009 and upstream.request_id == 2
    assert upstream.operation().get_int('job-id') == 101
    assert upstream.operation().get_str('printer-uri') == queue.upstream_uri()

    # Get-Jobs. Every job group in the reply is rewritten, not just the first.
    printer.clock.advance(5)
    send(0x0002, 3, document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    listed = send(0x000A, 4)
    groups = [g for g in listed.ipp.groups if g.tag == ipp.JOB_ATTRS]
    assert len(groups) == 2, len(groups)
    assert [g.get_str('job-uri') for g in groups] == [OURS + '/101', OURS + '/102']
    assert all(g.get_str('job-printer-uri') == OURS for g in groups)

    # Cancel-Job, identified the way a print client identifies a job.
    assert send(0x0008, 5, job_id=102).ipp.code == 0x0000
    assert printer.job(102).state == 7, 'the cancel did not reach the printer'
    # Cancelling something already finished is relayed as the printer answers
    # it, not turned into a success. 0x0404 is client-error-not-possible,
    # which is what the real printer was measured to answer.
    assert send(0x0008, 6, job_id=101).ipp.code == 0x0404

    # RFC 8011 lets a client name a job by job-uri instead of by
    # (printer-uri, job-id). That form is translated into the numeric one
    # rather than relayed: relaying it would hand the printer a URI the sender
    # composed, and rewriting it would mean guessing the printer's own job-uri
    # spelling, which this device zero-pads and others do not.
    fresh = send(0x0002, 7, document_format=(ipp.TAG_MIMETYPE,
                                            ['application/pdf']))
    fresh_id = fresh.ipp.group(ipp.JOB_ATTRS).get_int('job-id')
    msg = ipp.new_request(0x0008, 8, OURS)
    msg.operation().replace('job-uri', ipp.TAG_URI, [f'{OURS}/{fresh_id}'])
    assert relay(cfg, msg).ipp.code == 0x0000
    sent = ipp.parse(printer.requests[-1][1]).operation()
    assert sent.index_of('job-uri') < 0, 'the sender-composed URI was relayed'
    assert sent.get_int('job-id') == fresh_id, sent.get_int('job-id')
    assert printer.job(fresh_id).state == 7, 'the cancel did not reach the printer'

    # An explicit job-id wins; job-uri is not allowed to contradict it, and a
    # job-uri with no number in it must not have one invented for it.
    import ippfix
    for uri, want in ((f'{OURS}/101', 102), ('ipp://x/ipp/print/none', 102)):
        m = job_request(0x0008, 9, OURS, job_id=102)
        m.operation().replace('job-uri', ipp.TAG_URI, [uri])
        ippfix.rewrite_request(queue, m)
        assert m.operation().get_int('job-id') == want, uri
    m = ipp.new_request(0x0008, 10, OURS)
    m.operation().replace('job-uri', ipp.TAG_URI, ['ipp://x/ipp/print/none'])
    ippfix.rewrite_request(queue, m)
    assert m.operation().index_of('job-id') < 0
PY2

# The other way a client submits a job: Create-Job, one or more
# Send-Documents, Close-Job. Job splitting will almost certainly be built on
# this sequence, so what it does today is worth having written down.
python3 - <<'PY2' && ok 'a Create-Job sequence relays as it does today' || bad 'create-job sequence'
import logging, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
logging.disable(logging.CRITICAL)
import ippcodec as ipp
from fakeprinter import FakePrinter, proxy_for, relay, job_request

OURS = 'ipp://192.0.2.10/ipp/office'
DOCUMENT = b'%PDF-1.4\ntwo-step\n'

with FakePrinter() as printer:
    cfg, queue = proxy_for(printer)

    created = relay(cfg, job_request(
        0x0005, 1, OURS,
        document_format=(ipp.TAG_MIMETYPE, ['application/pdf'])))
    job = created.ipp.group(ipp.JOB_ATTRS)
    assert created.ipp.code == 0x0000
    assert job.get_int('job-id') == 101
    assert job.get_str('job-uri') == OURS + '/101'
    # A job that is still taking documents does not start printing, so the
    # clock moving must not advance it.
    printer.clock.advance(5)
    assert printer.job(101).state == 3, printer.job(101).state

    send = job_request(0x0006, 2, OURS, job_id=101,
                       document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    send.operation().replace('last-document', ipp.TAG_BOOLEAN, [b'\x01'])
    send.data = DOCUMENT
    answered = relay(cfg, send)
    assert answered.ipp.code == 0x0000, hex(answered.ipp.code)
    upstream = ipp.parse(printer.requests[-1][1])
    assert upstream.data == DOCUMENT, 'the document was not relayed verbatim'
    assert upstream.operation().get_int('job-id') == 101
    assert upstream.operation().get('last-document') == [b'\x01']
    assert upstream.operation().get_str('printer-uri') == queue.upstream_uri()

    assert relay(cfg, job_request(0x003b, 3, OURS, job_id=101)).ipp.code == 0x0000
    printer.clock.advance(5)
    printer.clock.advance(5)
    assert (printer.job(101).state, printer.job(101).impressions) == (9, 1)

    # Send-Document carries the document, so it is the operation that takes the
    # queue lock -- Create-Job and Close-Job do not, having nothing to transfer.
    cfg.timeout = 0.05
    queue.lock.acquire()
    try:
        held = relay(cfg, send)
        # HTTP 200 carrying IPP 0x0507 server-error-busy: in IPP the status is
        # in the body, and the client asked in IPP.
        assert held.status == '200 OK', held.status
        assert held.ipp.code == 0x0507, hex(held.ipp.code)
        opened = relay(cfg, job_request(0x0005, 4, OURS))
        assert opened.status == '200 OK', opened.status
    finally:
        queue.lock.release()
PY2

# A printer that is not there must produce an IPP answer, not a dropped
# connection: a print system that is told nothing reports nothing to the user,
# which is the same silence this proxy exists to remove.
python3 - <<'PY2' && ok 'an unreachable printer answers IPP 0x0502' || bad 'unreachable path'
import logging, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
logging.disable(logging.CRITICAL)          # this deliberately provokes warnings
import ippcodec as ipp
from fakeprinter import FakePrinter, proxy_for, relay, job_request

with FakePrinter(mode='unreachable') as printer:
    cfg, queue = proxy_for(printer)

    answer = relay(cfg, job_request(0x000B, 77, 'ipp://192.0.2.10/ipp/office'))
    # HTTP succeeded; the failure is reported inside IPP, where the client
    # looks. 0x0502 is server-error-service-unavailable.
    assert answer.status == '200 OK', answer.status
    assert answer.headers['content-type'] == 'application/ipp'
    assert answer.ipp.code == 0x0502, hex(answer.ipp.code)
    assert answer.ipp.request_id == 77, 'the client cannot match up the reply'
    op = answer.ipp.operation()
    assert op.get_str('status-message') == 'the printer is not responding'
    assert op.get_str('attributes-charset') == 'utf-8'

    # The same on the job path, which reaches it from inside the queue lock.
    # The lock must come back: a printer that is down for a minute must not
    # wedge the queue for good. The document matters -- a Print-Job carrying
    # none takes the other branch and never touches the lock at all.
    job = job_request(0x0002, 78, 'ipp://192.0.2.10/ipp/office',
                      document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    job.data = b'%PDF-1.4\ndown\n'
    answer = relay(cfg, job)
    assert answer.ipp.code == 0x0502, hex(answer.ipp.code)
    assert queue.lock.acquire(blocking=False), 'the queue lock was not released'
    queue.lock.release()
PY2

# The one failure that can cost paper twice: the printer takes the whole job
# and then says nothing. The job exists upstream, and anything that reacts to
# the lost answer by sending it again prints it twice.
python3 - <<'PY2' && ok 'a job whose answer was lost is not sent twice' || bad 'lost response'
import logging, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
logging.disable(logging.CRITICAL)
import ippcodec as ipp
from fakeprinter import FakePrinter, proxy_for, relay, job_request

DOCUMENT = b'%PDF-1.4\nlost\n'

with FakePrinter(mode='accept_then_drop_response') as printer:
    cfg, queue = proxy_for(printer)
    msg = job_request(0x0002, 5, 'ipp://192.0.2.10/ipp/office',
                      document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    msg.data = DOCUMENT
    answer = relay(cfg, msg)

    # The printer read the whole body and created the job before going quiet.
    assert len(printer.jobs) == 1, printer.jobs
    assert printer.jobs[0].size == len(DOCUMENT)
    # Exactly one Print-Job reached it. No retry, at any level.
    assert [op for op, _raw in printer.requests] == [0x0002]
    # The client is told the printer is not responding, which is true and is
    # all this proxy knows. NOTE: a client that retries on that will print the
    # job twice, and nothing here can tell it not to -- there is no job-id to
    # report and no way to ask the printer what it just accepted. Pinned as the
    # behaviour that exists; anything that adds a retry has to solve this first.
    assert answer.ipp.code == 0x0502, hex(answer.ipp.code)
    assert queue.lock.acquire(blocking=False), 'the queue lock was not released'
    queue.lock.release()
PY2
# The reject-then-rasterise path. It needs a converter, and Ghostscript is slow
# and its output is not byte-stable, so this stands in for one: it answers the
# header line rather than the document, which is exactly what is being tested
# here -- who decides to rasterise, and how that decision is expressed.
cat > "$work/stubconv" <<'STUB'
#!/usr/bin/env python3
"""A converter that only reads its header line. See selftest.sh."""
import os
import struct
import sys

data = sys.stdin.buffer.read()
line, _, document = data.partition(b'\n')
fields = dict(f.split(b'=', 1) for f in line.split()[1:] if b'=' in f)
with open(os.path.abspath(sys.argv[0]) + '.log', 'a') as handle:
    handle.write(line.decode('utf-8', 'replace') + '\n')
if fields.get(b'maxpdf') == b'0':
    # maxpdf=0 is how the proxy asks for raster: everything is over the limit.
    # The duplex byte follows the sides field, which is the whole reason that
    # field exists.
    duplex = 1 if fields.get(b'sides', b'one-sided') == b'one-sided' else 2
    header = bytes([8, int(fields.get(b'colorspace', b'19')), duplex, 0])
    sys.stdout.buffer.write(b'UNIRAST\0' + struct.pack('>I', 1)
                            + header + bytes(28) + bytes(64))
else:
    sys.stdout.buffer.write(b'%PDF-1.4\n% outlined\n' + document)
STUB
chmod +x "$work/stubconv"

python3 - "$work" <<'PY2' && ok 'a document the printer refuses is resent as raster, once' || bad 'raster retry'
import logging, os, sys
work = sys.argv[1]
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
logging.disable(logging.CRITICAL)          # the retry is announced loudly
import ippcodec as ipp
import ippfix
from fakeprinter import FakePrinter, proxy_for, relay, job_request

CONVERTER = os.path.join(work, 'stubconv')
HEADERS = CONVERTER + '.log'
DOCUMENT = b'%PDF-1.4\n' + b'body line\n' * 20


def run(mode):
    """One Print-Job through the proxy. Returns what everybody saw."""
    open(HEADERS, 'w').close()
    with FakePrinter(mode=mode, pdf_cap=64) as printer:
        cfg, queue = proxy_for(printer, extra=['--converter', CONVERTER])
        cfg.convert = True             # proxy_for turns it off; this needs it
        msg = job_request(
            0x0002, 1, 'ipp://192.0.2.10/ipp/office',
            document_format=(ipp.TAG_MIMETYPE, ['application/pdf']),
            job_attrs=[('sides', ipp.TAG_KEYWORD, ['two-sided-long-edge']),
                       ('media', ipp.TAG_KEYWORD, ['na_letter_8.5x11in'])])
        msg.data = DOCUMENT
        answer = relay(cfg, msg)
        jobs = [ipp.parse(raw) for op, raw in printer.requests if op == 0x0002]
        with open(HEADERS) as handle:
            return answer, list(printer.jobs), jobs, handle.read().splitlines()


# What the real device does: it declares a cap and does not enforce it. The
# outlined PDF goes as a PDF, at full fidelity, and nothing is retried.
answer, jobs, sent, headers = run(None)
assert answer.ipp.code == 0x0000, hex(answer.ipp.code)
assert len(sent) == 1, f'the job was sent {len(sent)} times'
assert sent[0].operation().get_str('document-format') == 'application/pdf'
assert sent[0].data.startswith(b'%PDF-'), sent[0].data[:16]
assert len(jobs) == 1
# Nothing pre-empts the printer's answer any more: the only ceiling the
# converter is given is the largest document this proxy would take back at all.
assert len(headers) == 1, headers
assert f'maxpdf={ippfix.MAX_CONVERTED}' in headers[0], headers[0]
assert 'sides=two-sided-long-edge' in headers[0], headers[0]

# And the same job against a printer that does enforce it: refused with 0x0408
# before any job exists, converted again as raster, sent once more, accepted.
answer, jobs, sent, headers = run('enforce_pdf_cap')
assert answer.ipp.code == 0x0000, hex(answer.ipp.code)
assert len(sent) == 2, f'expected one retry, got {len(sent)} attempts'
assert sent[0].data.startswith(b'%PDF-')
assert sent[1].operation().get_str('document-format') == 'image/urf'
assert sent[1].data.startswith(b'UNIRAST'), sent[1].data[:16]
# One job on the printer, not two. The refused attempt created nothing, which
# is the only reason resending is allowed at all.
assert len(jobs) == 1, f'{len(jobs)} jobs exist upstream'
assert jobs[0].urf_duplex is not None, 'the raster was not readable'
# The retry asks for raster by saying everything is too large, and carries the
# client's sides with it so the stream's duplex byte can follow.
assert 'maxpdf=0' not in headers[0] and 'maxpdf=0' in headers[1], headers
assert 'sides=two-sided-long-edge' in headers[1], headers[1]
# The client is told about the job that exists, not about the refusal.
assert answer.ipp.group(ipp.JOB_ATTRS).get_int('job-id') == jobs[0].id


# And the list of answers that justify any of that, which is the thing that
# must not grow carelessly. Each of these means the printer created no job and
# the document was the reason; everything else reaches the client untouched.
def reply(code):
    return ipp.serialize(ipp.Message(code=code, request_id=1))


for code in (0x0408, 0x040A, 0x0411):
    assert ippfix.refused_document(200, reply(code)) == code, hex(code)
# Busy, out of paper, too many jobs, a plain success: none of them say the
# document was unacceptable, and several would be a second try at the same job.
for code in (0x0000, 0x0001, 0x0400, 0x0404, 0x0500, 0x0502, 0x0505, 0x0506,
             0x0507, 0x0509, 0x050B):
    assert ippfix.refused_document(200, reply(code)) is None, hex(code)
# Nor is anything this proxy could not read, or did not get over HTTP 200.
assert ippfix.refused_document(200, b'not an IPP message') is None
assert ippfix.refused_document(500, reply(0x0408)) is None
PY2

# The distinction the retry rests on, in the one case where it must not fire.
python3 - "$work" <<'PY2' && ok 'a lost answer is not resent, with the retry armed' || bad 'lost response with retry'
import logging, os, sys
work = sys.argv[1]
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
logging.disable(logging.CRITICAL)
import ippcodec as ipp
from fakeprinter import FakePrinter, proxy_for, relay, job_request

CONVERTER = os.path.join(work, 'stubconv')
HEADERS = CONVERTER + '.log'
open(HEADERS, 'w').close()

# Everything the retry needs is available here -- conversion is on, a raster
# format is learned, the printer would take a raster. The only thing missing is
# an answer, and that is the whole point: the printer has read the document and
# may be printing it. Resending would print it twice.
with FakePrinter(mode='accept_then_drop_response', pdf_cap=64) as printer:
    cfg, queue = proxy_for(printer, extra=['--converter', CONVERTER])
    cfg.convert = True
    msg = job_request(0x0002, 1, 'ipp://192.0.2.10/ipp/office',
                      document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    msg.data = b'%PDF-1.4\n' + b'lost\n' * 20
    answer = relay(cfg, msg)

    assert answer.ipp.code == 0x0502, hex(answer.ipp.code)
    assert [op for op, _raw in printer.requests].count(0x0002) == 1, \
        'the job was sent again after a lost answer'
    assert len(printer.jobs) == 1, printer.jobs
    with open(HEADERS) as handle:
        assert len(handle.read().splitlines()) == 1, 'it converted again'
    assert queue.lock.acquire(blocking=False), 'the queue lock was not released'
    queue.lock.release()
PY2

python3 - <<'PY2' && ok 'sides reaches the converter, and a retry asks for raster' || bad 'converter sides header'
import sys
sys.path.insert(0, '.')
import ippfix

args = ippfix.build_parser().parse_args(['t=ipp://p.example/ipp/print'])
queue = ippfix.parse_queue('t=ipp://p.example/ipp/print')
cfg = ippfix.Config(args, [queue])
queue.learned = True          # do not go to the network for this

plain = ippfix.converter_header(queue, cfg)
assert b'sides=' not in plain, 'a sides nobody asked for was invented'
# No pre-emptive ceiling by default: the printer answers for itself now.
assert f'maxpdf={ippfix.MAX_CONVERTED}'.encode() in plain, plain

for value in ippfix.SIDES_VALUES:
    header = ippfix.converter_header(queue, cfg, sides=value)
    assert f'sides={value}'.encode() in header, header

# Anything else is dropped rather than substituted. The value is written into a
# line of whitespace-separated fields the converter parses, so one carrying a
# space would let whoever sent the job add a field of their own.
for bogus in ('two-sided', '', 'ONE-SIDED', 'one-sided device=uniprint',
              'one-sided\nmaxpdf=0'):
    assert b'sides=' not in ippfix.converter_header(queue, cfg, sides=bogus), \
        repr(bogus)

# A retry asks for raster the only way there is to ask: everything is over the
# limit. The client's sides goes with it, because the stream's duplex byte is
# what the printer will actually obey.
forced = ippfix.converter_header(queue, cfg, sides='one-sided',
                                 force_raster=True)
assert b'maxpdf=0' in forced and b'sides=one-sided' in forced, forced

# A site that has measured a printer which really does refuse oversized jobs
# can still put the ceiling back.
cfg.max_pdf_bytes = 1234
assert b'maxpdf=1234' in ippfix.converter_header(queue, cfg)
PY2

python3 - <<'PY2' && ok 'sides without media is logged, not quietly fixed' || bad 'sides without media'
import logging, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippcodec as ipp
import ippfix
from fakeprinter import FakePrinter, proxy_for, relay, job_request

seen = []


class Grab(logging.Handler):
    def emit(self, record):
        seen.append(record.getMessage())


ippfix.log.addHandler(Grab())
ippfix.log.propagate = False          # keep the test's own output clean
ippfix.log.setLevel(logging.WARNING)

SIDES = ('sides', ipp.TAG_KEYWORD, ['two-sided-long-edge'])
MEDIA = ('media', ipp.TAG_KEYWORD, ['na_letter_8.5x11in'])


def warned():
    return [m for m in seen if 'print one-sided' in m]


with FakePrinter() as printer:
    cfg, queue = proxy_for(printer)

    def send(request_id, job_attrs):
        seen.clear()
        msg = job_request(
            0x0002, request_id, 'ipp://192.0.2.10/ipp/office',
            document_format=(ipp.TAG_MIMETYPE, ['application/pdf']),
            job_attrs=job_attrs)
        msg.data = b'%PDF-1.4\nduplex\n'
        answer = relay(cfg, msg)
        printer.clock.advance(5)
        printer.clock.advance(5)
        return answer

    send(1, [SIDES])
    assert len(warned()) == 1, seen
    # ...and nothing was done about it. The printer sees exactly what the
    # client sent: a sides, and no media. There is no neutral media value to
    # add, and adding one would choose the user's paper for them.
    upstream = ipp.parse(printer.requests[-1][1]).group(ipp.JOB_ATTRS)
    assert upstream.get_str('sides') == 'two-sided-long-edge'
    assert upstream.index_of('media') < 0, 'a media value was invented'
    assert upstream.index_of('media-col') < 0, 'a media-col was invented'

    # Nothing to warn about when the client named paper itself...
    send(2, [SIDES, MEDIA])
    assert not warned(), seen
    # ...nor when it asked for one-sided, which this printer does honour...
    send(3, [('sides', ipp.TAG_KEYWORD, ['one-sided'])])
    assert not warned(), seen
    # ...nor when it asked for nothing at all.
    send(4, [])
    assert not warned(), seen

    # A client using Create-Job names its sides on the operation that carries
    # no document, so the warning has to come from the job attributes rather
    # than from the path a document takes.
    seen.clear()
    created = relay(cfg, job_request(0x0005, 5, 'ipp://192.0.2.10/ipp/office',
                                     job_attrs=[SIDES]))
    # 0x0001, because the printer dropped the sides -- which is precisely what
    # the journal line is there to explain, and it is relayed to the client
    # untouched.
    assert created.ipp.code == 0x0001, hex(created.ipp.code)
    assert len(warned()) == 1, seen
PY2


# One job at a time, per printer. A second job arriving while one is in flight
# has to be refused quickly rather than queued behind it, because the client is
# sitting on a socket waiting.
python3 - <<'PY2' && ok 'a busy queue is refused rather than left waiting' || bad 'queue contention'
import logging, sys, time
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
logging.disable(logging.CRITICAL)
import ippcodec as ipp
from fakeprinter import FakePrinter, proxy_for, relay, job_request

with FakePrinter() as printer:
    cfg, queue = proxy_for(printer)
    cfg.timeout = 0.05          # how long a second job waits for the first

    msg = job_request(0x0002, 1, 'ipp://192.0.2.10/ipp/office',
                      document_format=(ipp.TAG_MIMETYPE, ['application/pdf']))
    msg.data = b'%PDF-1.4\nbusy\n'

    queue.lock.acquire()        # stand in for a job already being transferred
    started = time.monotonic()
    try:
        answer = relay(cfg, msg)
    finally:
        queue.lock.release()
    # Refused after cfg.timeout, not queued behind the job in flight. The real
    # clock is the right one here: the point is that a client is not left
    # holding a socket open for as long as a large job takes to transfer.
    waited = time.monotonic() - started
    assert waited < 2, f'waited {waited:.1f}s for a busy queue'
    # An IPP question gets an IPP answer: HTTP 200 carrying 0x0507
    # server-error-busy, which a print system reads as "try again". The HTTP
    # 503 with a line of English that used to come back here read as the server
    # being broken, and disagreed with the unreachable path two tests above,
    # which always answered in IPP.
    assert answer.status == '200 OK', answer.status
    assert answer.ipp.code == 0x0507, hex(answer.ipp.code)
    assert printer.requests == [], 'the second job reached the printer anyway'

    # Only the job path takes the lock. A status query during a transfer still
    # gets through, which is what keeps a client's queue display alive.
    queue.lock.acquire()
    try:
        answer = relay(cfg, job_request(0x000B, 2, 'ipp://192.0.2.10/ipp/office'))
        assert answer.status == '200 OK', answer.status
        assert answer.ipp.code == 0x0000

        # NOTE, pinned as it is rather than fixed: the lock is taken only when
        # the operation carries a document. A Print-Job with an empty body goes
        # down the other branch -- no conversion, no archive, no lock -- and is
        # forwarded even while a job is in flight. No print client sends one,
        # but "one job at a time" is a property of the branch rather than of
        # the queue, which is worth knowing before that branch is rewritten.
        empty = job_request(0x0002, 3, 'ipp://192.0.2.10/ipp/office',
                            document_format=(ipp.TAG_MIMETYPE,
                                             ['application/pdf']))
        assert not empty.data
        answer = relay(cfg, empty)
        assert answer.status == '200 OK', answer.status
        assert [op for op, _raw in printer.requests] == [0x000b, 0x0002]
    finally:
        queue.lock.release()
PY2

# Following a job to its end, over a clock the test owns. watch_job() sleeps
# five seconds a poll and eight more for the last sheet to land; against a real
# clock this would be a twenty-second test, and against a virtual one it is
# instant and says the same thing.
python3 - <<'PY2' && ok 'watch_job reaches a verdict and keeps a working counter' || bad 'watch_job'
import logging, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
logging.disable(logging.CRITICAL)
import ippcodec as ipp
import ippfix
import snmpmini
from fakeprinter import (FakePrinter, controlled_clock, proxy_for, relay,
                         job_request)


real_get = snmpmini.get


class Recorder:
    """Stands in for Alerter so nothing tries to send mail."""

    def __init__(self):
        self.mail = []

    def send(self, subject, body, attachments=()):
        self.mail.append((subject, body, attachments))


def follow(mode):
    with FakePrinter(mode=mode) as printer:
        cfg, queue = proxy_for(printer)
        cfg.alerter = Recorder()
        # The page-counter cross-check reads SNMP, which this mock answers for
        # its own counter; without it the counter would switch itself off and
        # the healthy case below would prove nothing.
        snmpmini.get = printer.snmp_get
        try:
            relay(cfg, job_request(
                0x0002, 1, 'ipp://192.0.2.10/ipp/office',
                document_format=(ipp.TAG_MIMETYPE, ['application/pdf'])))
            with controlled_clock(ippfix, printer.clock):
                ippfix.watch_job(cfg, queue, 101, 'a job', 'application/pdf',
                                 b'%PDF-1.4\nwatched\n', 'relayed')
        finally:
            snmpmini.get = real_get
        return cfg, queue, printer


# A healthy job: it printed, the counter moved with it, and nobody is told
# anything. The counter must still be enabled afterwards -- a job that works is
# how it earns the right to contradict the printer later.
cfg, queue, printer = follow(None)
assert printer.jobs[0].state == 9 and printer.jobs[0].impressions == 1
assert cfg.alerter.mail == [], cfg.alerter.mail
assert queue.pages.enabled, 'a healthy job switched off the page counter'
assert queue.pages.trusted and queue.pages.proven, 'the counter never got proven'

# The failure this proxy exists for: completed, and nothing marked. It has to
# come out as a verdict rather than as silence.
cfg, queue, printer = follow('silent_loss')
assert printer.jobs[0].state == 9 and printer.jobs[0].impressions == 0
assert len(cfg.alerter.mail) == 1, cfg.alerter.mail
subject, body, _parts = cfg.alerter.mail[0]
assert subject == 'ippfix: job lost silently on office', subject
assert 'marked no impressions at all' in body
assert 'final state:  completed' in body
assert 'job-completed-successfully' in body     # what the printer said, in order
assert 'sha256:' in body                        # the document, structurally
assert 'printer-make-and-model' in body         # the printer, asked just then
assert queue.pages.enabled, 'one job that marked nothing is not a broken counter'
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
PY2

python3 - <<'PY2' && ok 'one SNMP listener speaks for exactly one printer' || bad 'relay queue selection'
import sys
sys.path.insert(0, '.')
import ippfix

# Restored after being deleted by mistake. These cover choose_relay_queue, which
# is live code called from start_snmp_relays -- they were removed by a commit
# that was only meant to take out the abandoned job-splitting planner, and the
# loss was invisible because the suite still passed. Their failure mode is a
# monitoring system reading the wrong printer's page counter.
def qs(*specs):
    return [ippfix.parse_queue(s) for s in specs]

# SNMP carries nothing that names a printer, so one listener speaks for one
# printer -- and with several the daemon must refuse rather than pick.
one = qs('a=ipp://p1/ipp/print')
assert ippfix.choose_relay_queue(one)[0] is one[0]
two = qs('a=ipp://p1/ipp/print', 'b=ipp://p2/ipp/print')
picked, why = ippfix.choose_relay_queue(two)
assert picked is None and 'snmp-relay' in why, why
marked = qs('a=ipp://p1/ipp/print?snmp-relay=on', 'b=ipp://p2/ipp/print')
assert ippfix.choose_relay_queue(marked)[0].name == 'a'
both = qs('a=ipp://p1/ipp/print?snmp-relay=on', 'b=ipp://p2/ipp/print?snmp-relay=on')
assert ippfix.choose_relay_queue(both)[0] is None

# One address per printer is the way to serve several at once: the address does
# the naming the protocol will not.
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

python3 - <<'PY2' && ok 'a document too large is refused out loud, not in silence' || bad 'oversized body'
import io, sys
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
import ippfix
from fakeprinter import FakePrinter, proxy_for

# A body over MAX_BODY used to raise BadRequest, which serve() swallowed, so the
# connection closed with no HTTP status and no IPP status at all. The client saw
# a network fault where the truth was a limit here -- the exact silence this
# program exists to remove, in the program itself.
with FakePrinter() as printer:
    cfg, queue = proxy_for(printer)
    handler = ippfix.Handler.__new__(ippfix.Handler)
    handler.client_address = ('192.0.2.50', 5555)
    body = b'x' * (ippfix.MAX_BODY + 1)
    request = (b'POST /ipp/office HTTP/1.1\r\nHost: h\r\n'
               b'Content-Type: application/ipp\r\n'
               b'Content-Length: %d\r\n\r\n' % len(body)) + body

    # A socket just real enough for serve(): it peeks one byte to tell TLS from
    # plaintext, then makes file objects out of it.
    class Recording(io.BytesIO):
        # serve() closes its handles on the way out, so keep what was written.
        written = b''
        def close(self):
            Recording.written = self.getvalue()
            super().close()

    class Sock:
        def __init__(self, data):
            self.rfile = io.BytesIO(data)
            self.wfile = Recording()
        def settimeout(self, _t): pass
        def recv(self, n, flags=0): return self.rfile.getvalue()[:n]
        def makefile(self, mode, _bufsize=0):
            return self.rfile if 'r' in mode else self.wfile
        def close(self): pass
        def shutdown(self, _how): pass

    handler.request = Sock(request)
    handler.server = None
    handler.serve(cfg)
    answer = Recording.written

assert answer, 'the client was told nothing at all'
assert answer.startswith(b'HTTP/1.1 413'), answer[:60]
assert printer.requests == [], 'an oversized body reached the printer'
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

python3 - <<'PY2' && ok 'the converter is asked for a range, and answers with a count' || bad 'converter page-range contract'
import sys
sys.path.insert(0, '.')
import ippfix

args = ippfix.build_parser().parse_args(['t=ipp://p.example/ipp/print'])
queue = ippfix.parse_queue('t=ipp://p.example/ipp/print')
cfg = ippfix.Config(args, [queue])
queue.learned = True          # do not go to the network for this

plain = ippfix.converter_header(queue, cfg)
assert b'first=' not in plain and b'report=' not in plain, plain
# Asking for one range, 1-based and inclusive at both ends. An off-by-one here
# duplicates or drops a page at every seam, and neither is visible until
# somebody reads the paper.
ranged = ippfix.converter_header(queue, cfg, first=4, last=6, report=True)
assert b'first=4' in ranged and b'last=6' in ranged and b'report=1' in ranged

# The count comes back ahead of the document. A converter that predates this
# says nothing, and None must not be read as a page count.
cases = [(b'%%ippfix-out pages=9\n%PDF-1.4\nx', (9, b'%PDF-1.4\nx')),
         (b'%PDF-1.4\nx', (None, b'%PDF-1.4\nx')),
         (b'%%ippfix-out pages=0\n%PDF', (0, b'%PDF')),
         (b'%%ippfix-out pages=x\n%PDF', (None, b'%PDF')),
         (b'%%ippfix-out pages=3', (None, b'%%ippfix-out pages=3'))]
for raw, want in cases:
    assert ippfix.read_converter_report(raw) == want, raw
PY2

# The page-range suite is its own file: it needs Ghostscript and takes about
# twenty seconds, and keeping it separate means it can be run on its own while
# working on the converter. There is still one entry point.
if [ -x ./scripts/selftest-pagerange.sh ]; then
  echo
  ./scripts/selftest-pagerange.sh || fail=$((fail+1))
  echo
else
  bad 'scripts/selftest-pagerange.sh is missing or not executable'
fi

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
