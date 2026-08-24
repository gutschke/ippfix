#!/usr/bin/env python3
"""Build a PDF that reliably provokes the fault, for testing a printer.

Useful for two things: establishing whether a given printer is affected at all,
and checking that a change actually helps. A job that provokes the fault marks
no paper, so an affected printer costs nothing to test and an unaffected one
costs a single sheet.

The document draws many distinct glyphs from one embedded, hinted font on a
single page. That combination fails consistently on the printer this was
developed against; see DIAGNOSING.md for what is and is not known about why.

  python3 scripts/make-reproducer.py OUT.pdf [FONT.ttf] [glyphs]

FONT must be a real TrueType font with hinting -- DejaVu, Liberation, Noto and
the fonts a browser ships all qualify. A synthetic font with simple outlines
does NOT reproduce the fault, even with 26000 glyphs, so use a real one.
"""
import os
import struct
import sys
import zlib

DEFAULT_FONTS = [
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    '/usr/share/fonts/truetype/croscore/Arimo-Regular.ttf',
    '/usr/share/fonts/TTF/DejaVuSans.ttf',
]


def find_font():
    for path in DEFAULT_FONTS:
        if os.path.exists(path):
            return path
    raise SystemExit('no font found; pass one as the second argument')


def tables(data):
    out = {}
    count = struct.unpack_from('>H', data, 4)[0]
    for i in range(count):
        p = 12 + i * 16
        out[data[p:p + 4]] = struct.unpack_from('>II', data, p + 8)
    return out


def cmap_of(data):
    """Unicode -> glyph id, from whichever subtable covers the most."""
    tabs = tables(data)
    base = tabs[b'cmap'][0]
    n = struct.unpack_from('>H', data, base + 2)[0]
    best, best_rank = None, -1
    for i in range(n):
        pid, eid, off = struct.unpack_from('>HHI', data, base + 4 + i * 8)
        if (pid, eid) not in ((3, 10), (3, 1), (0, 4), (0, 3)):
            continue
        fmt = struct.unpack_from('>H', data, base + off)[0]
        if fmt not in (4, 12):
            continue
        rank = 2 if fmt == 12 else 1
        if rank > best_rank:
            best, best_rank = (base + off, fmt), rank
    sub, fmt = best
    mapping = {}
    if fmt == 4:
        segx2 = struct.unpack_from('>H', data, sub + 6)[0]
        ends, starts = sub + 14, sub + 14 + segx2 + 2
        deltas, ranges = starts + segx2, starts + segx2 * 2
        for i in range(segx2 // 2):
            end = struct.unpack_from('>H', data, ends + i * 2)[0]
            start = struct.unpack_from('>H', data, starts + i * 2)[0]
            delta = struct.unpack_from('>h', data, deltas + i * 2)[0]
            ro = struct.unpack_from('>H', data, ranges + i * 2)[0]
            if start == 0xFFFF:
                continue
            for c in range(start, min(end, 0xFFFE) + 1):
                if ro == 0:
                    g = (c + delta) & 0xFFFF
                else:
                    gp = ranges + i * 2 + ro + (c - start) * 2
                    g = struct.unpack_from('>H', data, gp)[0]
                    if g:
                        g = (g + delta) & 0xFFFF
                if g:
                    mapping[c] = g
    else:
        groups = struct.unpack_from('>I', data, sub + 12)[0]
        for i in range(groups):
            s, e, gs = struct.unpack_from('>III', data, sub + 16 + i * 12)
            for c in range(s, min(e, s + 65535) + 1):
                mapping[c] = gs + (c - s)
    return mapping


class PDF:
    def __init__(self):
        self.objs = []

    def add(self, body, stream=None):
        self.objs.append((len(self.objs) + 1, body, stream))
        return len(self.objs)

    def stream(self, extra, payload):
        packed = zlib.compress(payload, 9)
        return self.add(b'<< /Length %d /Filter /FlateDecode%s >>'
                        % (len(packed), extra), packed)

    def build(self, root):
        out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
        offsets = {}
        for num, body, stream in self.objs:
            offsets[num] = len(out)
            out += b'%d 0 obj\n%s\n' % (num, body)
            if stream is not None:
                out += b'stream\n' + stream + b'\nendstream\n'
            out += b'endobj\n'
        start = len(out)
        out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(self.objs) + 1)
        for i in range(1, len(self.objs) + 1):
            out += b'%010d 00000 n \n' % offsets[i]
        out += (b'trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n'
                % (len(self.objs) + 1, root, start))
        return bytes(out)


def main():
    # Positional, but --help has to work: the documentation tells people to run
    # this, and taking '--help' as an output filename writes a 400 kB file named
    # --help into whatever directory they happened to be in.
    args = sys.argv[1:]
    if args and args[0] in ('-h', '--help'):
        print(__doc__.strip())
        return
    out_path = args[0] if args else 'reproducer.pdf'
    font_path = args[1] if len(args) > 1 else find_font()
    try:
        want = int(args[2]) if len(args) > 2 else 900
    except ValueError:
        raise SystemExit(f'glyph count must be a number, not {args[2]!r}')

    sfnt = open(font_path, 'rb').read()
    mapping = cmap_of(sfnt)
    tabs = tables(sfnt)
    upem = struct.unpack_from('>H', sfnt, tabs[b'head'][0] + 18)[0]
    metrics = struct.unpack_from('>H', sfnt, tabs[b'hhea'][0] + 34)[0]

    def advance(gid):
        off = tabs[b'hmtx'][0]
        i = min(gid, metrics - 1)
        return struct.unpack_from('>H', sfnt, off + i * 4)[0] * 1000 // upem

    codes = [c for c in sorted(mapping) if 0x20 <= c < 0x2E00][:want]
    gids = sorted({mapping[c] for c in codes})

    pdf = PDF()
    ff = pdf.stream(b' /Length1 %d' % len(sfnt), sfnt)
    xmin, ymin, xmax, ymax = struct.unpack_from('>hhhh', sfnt, tabs[b'head'][0] + 36)
    sc = lambda v: v * 1000 // upem                              # noqa: E731
    fd = pdf.add(b'<< /Type /FontDescriptor /FontName /Repro /Flags 4 '
                 b'/FontBBox [%d %d %d %d] /ItalicAngle 0 /Ascent 800 '
                 b'/Descent -200 /CapHeight 700 /StemV 80 /FontFile2 %d 0 R >>'
                 % (sc(xmin), sc(ymin), sc(xmax), sc(ymax), ff))
    widths = b' '.join(b'%d [%d]' % (g, advance(g)) for g in gids)
    desc = pdf.add(b'<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Repro '
                   b'/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) '
                   b'/Supplement 0 >> /FontDescriptor %d 0 R /DW 1000 /W [%s] '
                   b'/CIDToGIDMap /Identity >>' % (fd, widths))
    font = pdf.add(b'<< /Type /Font /Subtype /Type0 /BaseFont /Repro '
                   b'/Encoding /Identity-H /DescendantFonts [%d 0 R] >>' % desc)
    helv = pdf.add(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>')

    body = bytearray(b'BT /F0 11 Tf 40 755 Td '
                     b'(ippfix reproducer: %d distinct glyphs, one page) Tj ET\n'
                     % len(gids))
    y = 730
    for i in range(0, len(codes), 60):
        row = [mapping[c] for c in codes[i:i + 60]]
        body += b'BT /F1 9 Tf 40 %d Td <%s> Tj ET\n' % (
            y, b''.join(b'%04X' % g for g in row))
        y -= 13
        if y < 40:
            break
    content = pdf.stream(b'', bytes(body))
    res = b'<< /Font << /F0 %d 0 R /F1 %d 0 R >> >>' % (helv, font)
    page = pdf.add(b'<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] '
                   b'/Resources %s /Contents %d 0 R >>'
                   % (len(pdf.objs) + 2, res, content))
    pages = pdf.add(b'<< /Type /Pages /Kids [%d 0 R] /Count 1 >>' % page)
    root = pdf.add(b'<< /Type /Catalog /Pages %d 0 R >>' % pages)

    open(out_path, 'wb').write(pdf.build(root))
    print(f'{out_path}: {os.path.getsize(out_path):,} bytes, {len(gids)} distinct '
          f'glyphs from {os.path.basename(font_path)}, one page')
    print('Send it to the printer and compare the page counter before and after.')


if __name__ == '__main__':
    main()
