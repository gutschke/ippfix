#!/usr/bin/env python3
"""Compare a PDF against its converted form, pixel by pixel.

Removing fonts is worth nothing if the page comes out different, and a silently
wrong page is worse than one that fails to print because nobody checks. So the
conversion is measured rather than assumed.

Both files are rasterised with poppler, which had no part in the conversion. A
fault in Ghostscript therefore cannot hide behind a matching fault in the
rasteriser -- and that is not hypothetical: the release that dropped conic
gradients when writing PDFs dropped them when rendering them too, so comparing
two Ghostscript renders would have shown perfect agreement and proved nothing.

Text is expected to differ slightly, because outlining removes hinting and stems
then land on the pixel grid differently. Geometry must not move and effects must
not disappear.

  python3 scripts/fidelity-check.py ORIGINAL.pdf CONVERTED.pdf [-r DPI]

Needs poppler-utils, numpy and pillow; scipy, if present, locates the changes.
"""
import argparse
import os
import subprocess
import sys

try:
    import numpy as np
    from PIL import Image
except ImportError:
    sys.exit('needs numpy and pillow: apt install python3-numpy python3-pil')


def render(pdf, dpi, prefix):
    try:
        subprocess.run(['pdftoppm', '-r', str(dpi), '-png', '-cropbox', pdf, prefix],
                       check=True, capture_output=True)
    except FileNotFoundError:
        sys.exit('needs pdftoppm: apt install poppler-utils')
    except subprocess.CalledProcessError as e:
        sys.exit(f'pdftoppm failed on {pdf}: {e.stderr.decode(errors="replace")[:200]}')
    d = os.path.dirname(prefix) or '.'
    base = os.path.basename(prefix)
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.startswith(base + '-') and f.endswith('.png'))


def load(path):
    return np.asarray(Image.open(path).convert('RGB')).astype(np.int16)


def clusters(mask, min_area):
    """Bounding boxes of connected runs of changed pixels.

    A dropped gradient or a shifted box is one large solid cluster. Text that
    merely rasterises differently is a scatter of thin ones.
    """
    try:
        from scipy import ndimage
    except ImportError:
        return None
    lab, _ = ndimage.label(mask)
    out = []
    for sl in ndimage.find_objects(lab):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if h * w >= min_area:
            out.append((sl[1].start, sl[0].start, w, h))
    return sorted(out, key=lambda b: -b[2] * b[3])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('original')
    ap.add_argument('converted')
    ap.add_argument('-r', '--dpi', type=int, default=200)
    ap.add_argument('-o', '--outdir', default='/tmp/fidelity')
    ap.add_argument('--threshold', type=int, default=32,
                    help='per-channel difference counted as a real change')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    for f in os.listdir(args.outdir):
        if f.endswith('.png'):
            os.remove(os.path.join(args.outdir, f))
    A = render(args.original, args.dpi, os.path.join(args.outdir, 'orig'))
    B = render(args.converted, args.dpi, os.path.join(args.outdir, 'conv'))

    if len(A) != len(B):
        print(f'PAGE COUNT CHANGED: {len(A)} -> {len(B)}')
        return 2

    print(f'{len(A)} page(s) at {args.dpi} dpi\n')
    bad = False
    for i, (pa, pb) in enumerate(zip(A, B), 1):
        a, b = load(pa), load(pb)
        if a.shape != b.shape:
            print(f'  page {i}: PAGE SIZE CHANGED {a.shape} -> {b.shape}')
            bad = True
            continue
        d = np.abs(a - b).max(axis=2)
        changed = d >= args.threshold
        # Ink coverage is the blunt check: if marks went missing, it falls.
        ink_a = (a.mean(axis=2) < 200).mean() * 100
        ink_b = (b.mean(axis=2) < 200).mean() * 100
        print(f'  page {i}: {a.shape[1]}x{a.shape[0]}  mean={d.mean():5.2f}  '
              f'changed={changed.mean()*100:5.2f}%  '
              f'ink {ink_a:5.2f}% -> {ink_b:5.2f}%')
        if abs(ink_a - ink_b) > 0.25:
            print(f'      INK COVERAGE MOVED by {ink_b - ink_a:+.2f} points -- '
                  f'marks were probably lost or gained')
            bad = True

        vis = np.zeros(a.shape, dtype=np.uint8)
        vis[..., 0] = np.clip(d * 3, 0, 255)
        grey = (255 - a.mean(axis=2) * .82).astype(np.uint8)
        vis[..., 1] = vis[..., 2] = np.minimum(grey, 255 - vis[..., 0])
        Image.fromarray(vis).save(os.path.join(args.outdir, f'diff-{i}.png'))

        boxes = clusters(changed, (args.dpi // 6) ** 2)
        if boxes:
            print(f'      {len(boxes)} cluster(s) of change; largest:')
            for x, y, w, h in boxes[:5]:
                print(f'        {w/args.dpi*25.4:5.1f} x {h/args.dpi*25.4:5.1f} mm '
                      f'at ({x/args.dpi*25.4:5.1f}, {y/args.dpi*25.4:5.1f}) mm')
        elif boxes is None:
            print('      (install python3-scipy to locate the changes)')

    print(f'\ndiff maps: {args.outdir}/diff-N.png   red marks what changed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
