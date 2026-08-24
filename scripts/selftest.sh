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
             'idle_timeout', 'require_tls', 'extra_addresses'):
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
else
  echo '  skip  systemd units (systemd-analyze not available)'
fi

echo 'documentation'
check 'man page renders without warnings' \
      "[ -z \"\$(man --warnings -l ./ippfix.8 2>&1 >/dev/null)\" ]"
check 'man page and --help list the same options' \
      "diff <(python3 ippfix.py --help 2>/dev/null | grep -oE -- '--[a-z0-9-]+' | grep -v '^--help$' | sort -u) \
            <(sed -e 's/\\\\f[BIRP]//g' -e 's/\\\\-/-/g' ippfix.8 | grep -oE -- '--[a-z0-9-]+' | sort -u)"
check 'README references DEPLOYMENT.md' "grep -q 'DEPLOYMENT.md' README.md"
check 'no absolute home paths leaked' \
      "! grep -rqE '/home/[a-z]+/' --exclude-dir=.git --exclude-dir=__pycache__ ."

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
