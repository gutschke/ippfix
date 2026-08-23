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
for bad_spec in ('http://printer.example/x', 'a b=ipp://h/p', 'ipp:///nohost'):
    try:
        parse_queue(bad_spec)
    except ValueError:
        continue
    raise AssertionError(f'should have rejected {bad_spec!r}')
PY

echo 'addressing'
python3 - <<'PY2' && ok 'URLs stay short and paths stay forgiving' || bad 'URL handling'
import sys, argparse
sys.path.insert(0, '.')
import ippfix

def cfg(port=631, queues=('office=ipp://printer.example/ipp/print',)):
    a = argparse.Namespace(port=port, advertise='192.0.2.10', also_advertise=None,
                           no_ipv6=True, cert='c', key='k', no_convert=True,
                           converter='x', timeout=1, archive=None, archive_max=5)
    qs = [ippfix.parse_queue(q) for q in queues]
    return ippfix.Config(a, qs), qs

c, qs = cfg()
# The default port is assumed by every client; naming it is only noise.
assert c.our_uri(qs[0], 'ipp') == 'ipp://192.0.2.10/ipp/office', c.our_uri(qs[0])
assert c.our_uri(qs[0], 'ipps') == 'ipps://192.0.2.10/ipp/office'
c2, qs2 = cfg(port=8631)
assert c2.our_uri(qs2[0]) == 'ipp://192.0.2.10:8631/ipp/office'

# IPv6 literals must be bracketed or the port cannot be told from the address.
a = argparse.Namespace(port=631, advertise='2001:db8::1', also_advertise=[],
                       no_ipv6=True, cert='c', key='k', no_convert=True,
                       converter='x', timeout=1, archive=None, archive_max=5)
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
  check 'test input really embeds a font' "grep -qa '/FontFile' '$work/in.pdf'"
  ./defont < "$work/in.pdf" > "$work/out.pdf" 2>/dev/null
  check 'removes every font program'  "! grep -qa '/FontFile' '$work/out.pdf'"
  check 'output is still a PDF'       "head -c 5 '$work/out.pdf' | grep -qa '%PDF-'"
  printf 'UNIRAST\0not a pdf at all' > "$work/raster.bin"
  ./defont < "$work/raster.bin" > "$work/raster.out" 2>/dev/null
  check 'passes non-PDF through unchanged' "cmp -s '$work/raster.bin' '$work/raster.out'"
  printf '%%PDF-1.4 truncated and broken' > "$work/broken.pdf"
  ./defont < "$work/broken.pdf" > "$work/broken.out" 2>/dev/null || true
  check 'falls back to the original on failure' "[ -s '$work/broken.out' ]"
else
  echo '  skip  defont (ghostscript not installed)'
fi

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
