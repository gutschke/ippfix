#!/usr/bin/env python3
"""Refuse to let real network detail into the tree.

This exists because a scrub once undid itself. A captured printer reply was
sanitised carefully -- every identifier replaced by a same-length placeholder
-- and then the header comment listed each substitution as "original -> new",
which handed the reader back exactly the values the substitution had removed.
The scrub was undone by the note explaining it.

So the rule is mechanical rather than remembered: an address, a MAC, or an
address-bearing name may appear in this tree only if it is on the list below.
Anything else fails, whether it is real, invented, or the "before" half of a
substitution. Adding to the list is a deliberate act, which is the point.

    ./scripts/scrub-check.py [PATH...]

with no arguments, every file git tracks. Exits non-zero, naming file, line
and text, on the first thing it cannot account for.
"""
import os
import re
import subprocess
import sys

# Addresses that may appear. RFC 5737 (192.0.2.0/24, 198.51.100.0/24,
# 203.0.113.0/24) and RFC 3849 (2001:db8::/32) exist for documentation and are
# guaranteed never to be anybody's real machine; use them for anything new.
# The rest are wildcards and loopback, which name no host at all.
ALLOWED_ADDRESSES = {
    '0.0.0.0', '127.0.0.1', '255.255.255.255',
    '192.0.2.0', '192.0.2.1', '192.0.2.10', '192.0.2.11', '192.0.2.50',
    '192.0.2.99', '192.0.2.255',
    '198.51.100.0', '198.51.100.9', '198.51.100.11', '198.51.100.12',
    # Kept because the tests that use them are about CIDR arithmetic on a
    # private range, and rewriting them into documentation space would make
    # the assertions harder to read than the risk justifies. They are the
    # only private addresses the tree is allowed to contain.
    '10.0.0.0', '10.0.0.1', '10.0.0.2', '10.0.0.3', '10.1.2.3',
}
ALLOWED_ADDRESSES |= {'::', '::1', '::ffff:0:0', '2001:db8::1', 'fe80::1'}

# The maintainer address the packaging carries. GitHub issues this in place of
# a real mailbox, which is why it is the only literal one permitted; anything
# else has to live in the domains RFC 2606 set aside for examples.
ALLOWED_EMAILS = {'gutschke@users.noreply.github.com'}
EXAMPLE_DOMAINS = ('example', 'example.com', 'example.org', 'example.net',
                   'invalid', 'test', 'localhost')

# Nothing binary, and nothing that is a record of what was already published.
SKIP_SUFFIXES = ('.png', '.jpg', '.pdf', '.gz', '.deb')
SKIP_PATHS = {'scripts/scrub-check.py'}

# A dotted quad, but not four groups lifted out of the middle of an SNMP OID:
# the lookaround insists the run of dot-separated numbers stops here.
IPV4 = re.compile(r'(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])')
# Conservative on purpose: a colon-separated address has to look unmistakably
# like one -- a "::" run, or five separators -- before this complains, so that
# clock times and OID fragments do not.
IPV6 = re.compile(r'(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){5,7}[0-9A-Fa-f]{1,4}'
                  r'|(?<![\w:])[0-9A-Fa-f]{0,4}::[0-9A-Fa-f:.]{1,29}')
MAC = re.compile(r'(?<![\w:-])[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}(?![\w:-])')
# HP builds both printer-name and printer-dns-sd-name out of the last three
# octets of the MAC, so these leak a device identifier without looking like it.
HP_NAME = re.compile(r'\bNPI(?!0{6}\b)[0-9A-Fa-f]{6}\b')
EMAIL = re.compile(r'\b[\w.%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')


def quad(text):
    """True if this is a syntactically valid IPv4 address."""
    return all(part and len(part) < 4 and int(part) < 256
               for part in text.split('.'))


def complaints(line):
    """Everything in one line that this tree may not contain."""
    for found in IPV4.findall(line):
        if quad(found) and found not in ALLOWED_ADDRESSES:
            yield 'address', found
    for found in IPV6.findall(line):
        # ::ffff:A.B.C.D is the same host as A.B.C.D -- a dual-stack socket
        # writes peers that way -- so it stands or falls with the v4 address.
        mapped = found.lower().partition('::ffff:')[2]
        if mapped and quad(mapped):
            if mapped not in ALLOWED_ADDRESSES:
                yield 'address', found
        elif found.rstrip(':') not in ALLOWED_ADDRESSES:
            yield 'address', found
    for found in MAC.findall(line):
        yield 'MAC address', found
    for found in HP_NAME.findall(line):
        yield 'MAC-derived printer name', found
    for found in EMAIL.findall(line):
        domain = found.rpartition('@')[2].lower()
        reserved = any(domain == d or domain.endswith('.' + d)
                       for d in EXAMPLE_DOMAINS)
        if found in ALLOWED_EMAILS or reserved:
            continue
        yield 'e-mail address', found


def tracked():
    """Every file git knows about, or every file here if git does not."""
    try:
        out = subprocess.run(['git', 'ls-files', '-z'], check=True,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL).stdout
        return [p.decode() for p in out.split(b'\0') if p]
    except (OSError, subprocess.CalledProcessError):
        found = []
        for root, dirs, files in os.walk('.'):
            dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__')]
            found += [os.path.relpath(os.path.join(root, f)) for f in files]
        return found


def main(argv):
    paths = argv[1:] or tracked()
    bad = 0
    for path in paths:
        if path in SKIP_PATHS or path.endswith(SKIP_SUFFIXES):
            continue
        try:
            with open(path, encoding='utf-8', errors='replace') as handle:
                lines = handle.read().splitlines()
        except (IsADirectoryError, FileNotFoundError, PermissionError):
            continue
        for number, line in enumerate(lines, 1):
            for kind, text in complaints(line):
                print(f'{path}:{number}: {kind} {text!r}', file=sys.stderr)
                bad += 1
    if bad:
        print(f'{bad} thing(s) this tree may not contain. Either use a '
              f'documentation address (RFC 5737 / RFC 3849 / RFC 2606) or, '
              f'if it really '
              f'belongs here, add it to ALLOWED_* in {sys.argv[0]}.',
              file=sys.stderr)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
