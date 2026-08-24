#!/usr/bin/env python3
"""Find soft masks whose /BC backdrop array does not match its group's colour space.

A luminosity soft mask names a colour space in the group it points at and a
backdrop colour in /BC. When the number of entries in /BC does not equal the
number of components that colour space has, at least one printer accepts the
job, reports `job-state = completed`, and marks nothing at all -- the same
silent loss this repository exists for, but reached without any font involved.

It fails in both directions, too few components and too many; omitting /BC
entirely is safe. Both Ghostscript and poppler render such a file correctly,
which is what makes it dangerous: nothing on a desktop shows a problem, and the
printer reports success, so no layer anywhere reports the loss.

The proxy cannot repair what it cannot see, and it never sees this: the job is
reported successful. Detection therefore has to happen on the way in. This
scans documents for the construct so that an archive can be checked for it.

  python3 scripts/check-softmask.py FILE.pdf...

Exits non-zero if any file contains a mismatch. See DIAGNOSING.md.
"""
import re, sys, zlib

COMPONENTS = {b'DeviceGray': 1, b'CalGray': 1, b'G': 1,
              b'DeviceRGB': 3, b'CalRGB': 3, b'Lab': 3, b'RGB': 3,
              b'DeviceCMYK': 4, b'CMYK': 4}


def expand(d):
    """The raw bytes plus the contents of every object stream."""
    parts = [d]
    for m in re.finditer(rb'/Type\s*/ObjStm.*?stream\r?\n', d, re.S):
        e = d.find(b'endstream', m.end())
        if e < 0:
            continue
        try:
            parts.append(zlib.decompress(d[m.end():e].rstrip(b'\r\n')))
        except Exception:
            pass
    return parts


def objects(blob):
    """num -> body, for objects written plainly."""
    out = {}
    for m in re.finditer(rb'(\d+)\s+\d+\s+obj\b(.{0,4000}?)endobj', blob, re.S):
        out[int(m.group(1))] = m.group(2)
    return out


def cs_components(cs, objs):
    if cs is None:
        return None
    cs = cs.strip()
    m = re.match(rb'/(\w+)', cs)
    if m and m.group(1) in COMPONENTS:
        return COMPONENTS[m.group(1)]
    m = re.match(rb'(\d+)\s+\d+\s+R', cs)
    if m:                                    # indirect: follow it once
        body = objs.get(int(m.group(1)))
        if body:
            if re.search(rb'/DeviceCMYK|/CMYK', body): return 4
            if re.search(rb'/DeviceRGB|/CalRGB|/Lab', body): return 3
            if re.search(rb'/DeviceGray|/CalGray', body): return 1
            m2 = re.search(rb'/N\s+(\d+)', body)         # ICCBased
            if m2:
                return int(m2.group(1))
    if re.search(rb'/ICCBased', cs):
        m2 = re.search(rb'/N\s+(\d+)', cs)
        if m2:
            return int(m2.group(1))
    return None


def check(path):
    d = open(path, 'rb').read()
    blobs = expand(d)
    objs = {}
    for b in blobs:
        objs.update(objects(b))
    findings, masks = [], 0
    for blob in blobs:
        for m in re.finditer(rb'/SMask\s*<<(.{0,400}?)>>', blob, re.S):
            body = m.group(1)
            if b'/Luminosity' not in body and b'/Alpha' not in body:
                continue
            masks += 1
            bc = re.search(rb'/BC\s*\[([^\]]*)\]', body)
            if not bc:
                continue                       # absent is safe
            n_bc = len(bc.group(1).split())
            g = re.search(rb'/G\s+(\d+)\s+\d+\s+R', body)
            grp = None
            if g:
                gb = objs.get(int(g.group(1)), b'')
                gm = re.search(rb'/Group\s*<<(.{0,300}?)>>', gb, re.S)
                if gm:
                    csm = re.search(rb'/CS\s*(/\w+|\d+\s+\d+\s+R|\[[^\]]*\])', gm.group(1))
                    if csm:
                        grp = cs_components(csm.group(1), objs)
            if grp is not None and grp != n_bc:
                findings.append((n_bc, grp))
    return masks, findings


if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        print(__doc__.strip())
        sys.exit(0)
    bad = 0
    for p in sys.argv[1:]:
        try:
            masks, findings = check(p)
        except Exception as e:
            print(f'  {p}: unreadable ({e})')
            continue
        if findings:
            bad += 1
            for n_bc, grp in findings:
                print(f'  MISMATCH {p}: /BC has {n_bc} entr(y|ies), '
                      f'group colour space has {grp} component(s)')
        elif masks:
            print(f'  ok       {p}: {masks} soft mask(s), all consistent')
    sys.exit(1 if bad else 0)
