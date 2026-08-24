#!/usr/bin/env python3
"""Build the HTML pages that provoke the fault from a browser.

The font fault is usually met by printing a PDF, because a browser renders
pages in the system's fonts and those tend to be safe. But a web page may
supply its own font, and a browser embeds that font -- hinting and all -- into
the PDF it sends to the printer. So the browser path can reach the fault too.

This writes two pages that differ in exactly one respect: whether the font they
carry has hinting. Print both from a browser, directly to the printer.

  A-hinted.html     expected to FAIL
  B-unhinted.html   expected to PRINT

If A fails and B prints, this printer is affected and the browser path reaches
it. If both print, this printer is not affected at this glyph count -- raise it
with --glyphs and try again.

  python3 scripts/make-html-reproducer.py [OUTDIR] [--font FONT.ttf] [--glyphs N]

Serve OUTDIR over HTTP and open the pages from the machine you print from; the
fonts are loaded with @font-face and most browsers will not load them from a
file:// URL.

  python3 -m http.server 8080 --directory OUTDIR
"""
import argparse
import os
import struct

DEFAULT_FONTS = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    '/usr/share/fonts/TTF/DejaVuSans.ttf',
]
HINT_TABLES = (b'fpgm', b'prep', b'cvt ', b'gasp')


def find_font():
    for p in DEFAULT_FONTS:
        if os.path.exists(p):
            return p
    raise SystemExit('no font found; pass one with --font')


def read_tables(d):
    n = struct.unpack_from('>H', d, 4)[0]
    out = {}
    for i in range(n):
        p = 12 + i * 16
        tag = d[p:p + 4]
        off, ln = struct.unpack_from('>II', d, p + 8)
        out[tag] = (off, ln)
    return out


def strip_glyph(g):
    """Remove hinting bytecode from one glyph, simple or composite."""
    if len(g) < 10:
        return g
    nc = struct.unpack_from('>h', g, 0)[0]
    if nc >= 0:                                        # simple glyph
        p = 10 + nc * 2
        if p + 2 > len(g):
            return g
        ilen = struct.unpack_from('>H', g, p)[0]
        if not ilen:
            return g
        return g[:p] + b'\x00\x00' + g[p + 2 + ilen:]
    # composite: walk the components, clearing WE_HAVE_INSTRUCTIONS as we go
    out = bytearray(g[:10])
    p, had = 10, False
    while True:
        if p + 4 > len(g):
            return g
        flags, gid = struct.unpack_from('>HH', g, p)
        had = had or bool(flags & 0x0100)
        newflags = flags & ~0x0100
        out += struct.pack('>HH', newflags, gid)
        p += 4
        n = 4 if flags & 0x0001 else 2                 # ARG_1_AND_2_ARE_WORDS
        if flags & 0x0008:                             # WE_HAVE_A_SCALE
            n += 2
        elif flags & 0x0040:                           # X_AND_Y_SCALE
            n += 4
        elif flags & 0x0080:                           # TWO_BY_TWO
            n += 8
        out += g[p:p + n]
        p += n
        if not flags & 0x0020:                         # MORE_COMPONENTS
            break
    return bytes(out) if had else g


def dehint(d):
    """Return the font with every trace of hinting removed."""
    tabs = read_tables(d)
    for t in (b'head', b'loca', b'glyf', b'maxp'):
        if t not in tabs:
            raise SystemExit(f'font has no {t.decode()} table; not a TrueType outline font')

    ilf = struct.unpack_from('>h', d, tabs[b'head'][0] + 50)[0]
    lo, ll = tabs[b'loca']
    count = (ll // (4 if ilf else 2)) - 1
    if ilf:
        locs = list(struct.unpack_from('>%dI' % (count + 1), d, lo))
    else:
        locs = [x * 2 for x in struct.unpack_from('>%dH' % (count + 1), d, lo)]

    gbase = tabs[b'glyf'][0]
    glyf, newloca = bytearray(), [0]
    for i in range(count):
        g = strip_glyph(bytes(d[gbase + locs[i]:gbase + locs[i + 1]]))
        glyf += g
        while len(glyf) % 4:                            # keep offsets aligned
            glyf += b'\x00'
        newloca.append(len(glyf))

    long_loca = newloca[-1] > 0x1FFFF or any(x % 2 for x in newloca)
    if long_loca:
        loca = struct.pack('>%dI' % len(newloca), *newloca)
    else:
        loca = struct.pack('>%dH' % len(newloca), *[x // 2 for x in newloca])

    keep = {}
    for tag, (off, ln) in tabs.items():
        if tag in HINT_TABLES:
            continue
        keep[tag] = bytearray(d[off:off + ln])
    keep[b'glyf'] = bytes(glyf)
    keep[b'loca'] = loca
    struct.pack_into('>h', keep[b'head'], 50, 1 if long_loca else 0)
    struct.pack_into('>I', keep[b'head'], 8, 0)         # checkSumAdjustment

    # maxp: the hinting maxima are now meaningless, and leaving them high
    # tells the consumer to reserve resources for bytecode that is not there.
    if len(keep[b'maxp']) >= 32:
        for field in (24, 26, 28):                      # maxZones, maxTwilight,
            struct.pack_into('>H', keep[b'maxp'], field, 0)  # maxStorage
        struct.pack_into('>H', keep[b'maxp'], 30, 0)    # maxFunctionDefs
        if len(keep[b'maxp']) >= 34:
            struct.pack_into('>H', keep[b'maxp'], 32, 0)  # maxInstructionDefs
        if len(keep[b'maxp']) >= 36:
            struct.pack_into('>H', keep[b'maxp'], 34, 0)  # maxStackElements
        if len(keep[b'maxp']) >= 38:
            struct.pack_into('>H', keep[b'maxp'], 36, 0)  # maxSizeOfInstructions

    tags = sorted(keep)
    n = len(tags)
    sr = 1
    while sr * 2 <= n:
        sr *= 2
    out = bytearray(struct.pack('>IHHHH', 0x00010000, n, sr * 16,
                                (sr).bit_length() - 1, n * 16 - sr * 16))
    off = 12 + n * 16
    dirent, body = bytearray(), bytearray()
    for tag in tags:
        blob = bytes(keep[tag])
        pad = (-len(blob)) % 4
        csum = sum(struct.unpack('>%dI' % ((len(blob) + pad) // 4),
                                 blob + b'\x00' * pad)) & 0xFFFFFFFF
        dirent += tag + struct.pack('>III', csum, off + len(body), len(blob))
        body += blob + b'\x00' * pad
    out += dirent + body

    total = sum(struct.unpack('>%dI' % (len(out) // 4), bytes(out))) & 0xFFFFFFFF
    head_off = 12 + tags.index(b'head') * 16
    head_start = struct.unpack_from('>I', out, head_off + 8)[0]
    struct.pack_into('>I', out, head_start + 8, (0xB1B0AFBA - total) & 0xFFFFFFFF)
    return bytes(out)


def cmap_chars(d, want):
    """Characters this font actually has glyphs for.

    Only glyphs that render cost anything. Characters with no glyph collapse to
    one shared .notdef, so picking an arbitrary Unicode range proves nothing.
    """
    tabs = read_tables(d)
    base = tabs[b'cmap'][0]
    n = struct.unpack_from('>H', d, base + 2)[0]
    best, rank = None, -1
    for i in range(n):
        pid, eid, off = struct.unpack_from('>HHI', d, base + 4 + i * 8)
        if (pid, eid) not in ((3, 10), (3, 1), (0, 4), (0, 3)):
            continue
        fmt = struct.unpack_from('>H', d, base + off)[0]
        if fmt in (4, 12) and (2 if fmt == 12 else 1) > rank:
            best, rank = (base + off, fmt), 2 if fmt == 12 else 1
    if not best:
        raise SystemExit('no usable cmap subtable')
    sub, fmt = best
    m = {}
    if fmt == 4:
        segx2 = struct.unpack_from('>H', d, sub + 6)[0]
        ends = sub + 14
        starts = ends + segx2 + 2
        deltas, ranges = starts + segx2, starts + segx2 * 2
        for i in range(segx2 // 2):
            e = struct.unpack_from('>H', d, ends + i * 2)[0]
            s = struct.unpack_from('>H', d, starts + i * 2)[0]
            dl = struct.unpack_from('>h', d, deltas + i * 2)[0]
            ro = struct.unpack_from('>H', d, ranges + i * 2)[0]
            if s == 0xFFFF:
                continue
            for c in range(s, min(e, 0xFFFE) + 1):
                if ro == 0:
                    g = (c + dl) & 0xFFFF
                else:
                    g = struct.unpack_from('>H', d, ranges + i * 2 + ro + (c - s) * 2)[0]
                    if g:
                        g = (g + dl) & 0xFFFF
                if g:
                    m[c] = g
    else:
        for i in range(struct.unpack_from('>I', d, sub + 12)[0]):
            s, e, gs = struct.unpack_from('>III', d, sub + 16 + i * 12)
            for c in range(s, min(e, s + 65535) + 1):
                m[c] = gs + (c - s)
    good = [c for c, g in sorted(m.items())
            if 0x21 <= c < 0x2E00 and g > 0 and chr(c).isprintable()]
    return ''.join(chr(c) for c in good[:want])


PAGE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ size: letter; margin: 12mm; }}
  body {{ font-family: Arial, Helvetica, sans-serif; font-size: 10pt; }}
  h1 {{ font-size: 13pt; margin: 0 0 2mm 0; }}
  .note {{ font-size: 8pt; color: #444; margin: 0 0 4mm 0; }}
  @font-face {{ font-family: 'Probe'; src: url('{font}') format('truetype'); }}
  .p {{ font-family: 'Probe', serif; font-size: 8pt; line-height: 1.3;
        word-wrap: break-word; margin: 0 0 0.5mm 0; }}
</style></head>
<body>
<h1>{title}</h1>
<p class="note">{note}</p>
{body}
</body></html>
'''


def write_page(path, title, note, chars, font):
    rows = '\n'.join(
        '<p class="p">%s</p>' % chars[i:i + 90].replace('&', '&amp;')
                                              .replace('<', '&lt;')
                                              .replace('>', '&gt;')
        for i in range(0, len(chars), 90))
    with open(path, 'w', encoding='utf-8') as f:
        f.write(PAGE.format(title=title, note=note, body=rows, font=font))


def main():
    ap = argparse.ArgumentParser(
        description='Build HTML pages that provoke the font fault from a browser.')
    ap.add_argument('outdir', nargs='?', default='html-reproducer')
    ap.add_argument('--font', help='TrueType font with hinting')
    ap.add_argument('--glyphs', type=int, default=900,
                    help='distinct glyphs per page (default 900)')
    args = ap.parse_args()

    src = args.font or find_font()
    data = open(src, 'rb').read()
    tabs = read_tables(data)
    hinting = sum(tabs[t][1] for t in HINT_TABLES if t in tabs)
    if not hinting:
        raise SystemExit(f'{src} carries no hinting; the comparison needs a hinted font')

    os.makedirs(args.outdir, exist_ok=True)
    stripped = dehint(data)
    open(os.path.join(args.outdir, 'hinted.ttf'), 'wb').write(data)
    open(os.path.join(args.outdir, 'unhinted.ttf'), 'wb').write(stripped)

    chars = cmap_chars(data, args.glyphs)
    write_page(os.path.join(args.outdir, 'A-hinted.html'),
               f'A - {len(chars)} glyphs, hinted web font',
               'This page carries a hinted font. Expected to FAIL on an '
               'affected printer, marking no paper.',
               chars, 'hinted.ttf')
    write_page(os.path.join(args.outdir, 'B-unhinted.html'),
               f'B - {len(chars)} glyphs, same font without hinting',
               'Identical to A except that the hinting has been removed. '
               'Expected to PRINT. One variable separates the two.',
               chars, 'unhinted.ttf')

    print(f'{args.outdir}/')
    print(f'  hinted.ttf     {len(data):>9,} B  ({hinting:,} B of hinting tables)')
    print(f'  unhinted.ttf   {len(stripped):>9,} B  (hinting removed)')
    print(f'  A-hinted.html / B-unhinted.html   {len(chars)} glyphs each, one page')
    print(f'\nServe it, then print both pages from a browser:')
    print(f'  python3 -m http.server 8080 --directory {args.outdir}')


if __name__ == '__main__':
    main()
