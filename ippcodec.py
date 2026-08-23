#!/usr/bin/env python3
"""Byte-exact IPP message codec.

The proxy rewrites a handful of attributes and relays everything else
untouched, so the codec must round-trip anything the printer emits -- including
collections and multi-value attributes it does not understand semantically.

The trick is to keep each attribute group as an ORDERED list of raw
(tag, name, value) triples. An additional value of a multi-valued attribute is
just a triple with an empty name, exactly as it appears on the wire; a
collection is a begCollection/memberAttrName/.../endCollection run of ordinary
triples. Preserving order and rawness makes round-tripping exact without the
codec needing to model any of it.
"""
import struct

# delimiter tags
OPERATION_ATTRS = 0x01
JOB_ATTRS = 0x02
END_OF_ATTRS = 0x03
PRINTER_ATTRS = 0x04
UNSUPPORTED_ATTRS = 0x05
DELIMS = {0x01: 'operation', 0x02: 'job', 0x04: 'printer',
          0x05: 'unsupported', 0x06: 'subscription', 0x07: 'event'}

# value tags we construct
TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_TEXT = 0x41
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_CHARSET = 0x47
TAG_LANGUAGE = 0x48
TAG_MIMETYPE = 0x49


class Group:
    __slots__ = ('tag', 'items')

    def __init__(self, tag, items=None):
        self.tag = tag
        self.items = items if items is not None else []   # [(tag, name, value)]

    def index_of(self, name):
        """Position of the attribute's first triple, or -1."""
        want = name if isinstance(name, bytes) else name.encode()
        for i, (_t, n, _v) in enumerate(self.items):
            if n == want:
                return i
        return -1

    def run_length(self, start):
        """How many triples belong to the attribute starting at `start`
        (itself plus any empty-named additional values)."""
        n = 1
        while start + n < len(self.items) and self.items[start + n][1] == b'':
            n += 1
        return n

    def get(self, name):
        i = self.index_of(name)
        if i < 0:
            return None
        return [v for _t, _n, v in self.items[i:i + self.run_length(i)]]

    def get_str(self, name):
        vals = self.get(name)
        return vals[0].decode('utf-8', 'replace') if vals else None

    def get_int(self, name):
        vals = self.get(name)
        return struct.unpack('>i', vals[0])[0] if vals and len(vals[0]) == 4 else None

    def replace(self, name, tag, values):
        """Replace an attribute in place, preserving its position.

        Appends it if absent. `values` may be bytes or str.
        """
        want = name if isinstance(name, bytes) else name.encode()
        vals = [v if isinstance(v, bytes) else str(v).encode() for v in values]
        new = [(tag, want, vals[0])] + [(tag, b'', v) for v in vals[1:]]
        i = self.index_of(want)
        if i < 0:
            self.items.extend(new)
        else:
            self.items[i:i + self.run_length(i)] = new

    def remove(self, name):
        i = self.index_of(name)
        if i >= 0:
            del self.items[i:i + self.run_length(i)]

    def names(self):
        return [n.decode('utf-8', 'replace') for _t, n, _v in self.items if n]


class Message:
    __slots__ = ('version', 'code', 'request_id', 'groups', 'data')

    def __init__(self, version=(2, 0), code=0, request_id=1,
                 groups=None, data=b''):
        self.version = version
        self.code = code            # operation-id in a request, status in a reply
        self.request_id = request_id
        self.groups = groups if groups is not None else []
        self.data = data            # document payload following end-of-attributes

    def group(self, tag):
        for g in self.groups:
            if g.tag == tag:
                return g
        return None

    def operation(self):
        return self.group(OPERATION_ATTRS)

    def ensure_group(self, tag):
        g = self.group(tag)
        if g is None:
            g = Group(tag)
            # keep end-of-attributes semantics: groups are emitted in order
            self.groups.append(g)
        return g


def parse(buf):
    """Decode a full IPP message. Trailing bytes become Message.data."""
    if len(buf) < 8:
        raise ValueError('short IPP message')
    major, minor = buf[0], buf[1]
    code, request_id = struct.unpack_from('>HI', buf, 2)
    i = 8
    groups = []
    cur = None
    while i < len(buf):
        tag = buf[i]
        if tag == END_OF_ATTRS:
            i += 1
            break
        if tag < 0x10:                      # delimiter
            cur = Group(tag)
            groups.append(cur)
            i += 1
            continue
        if i + 3 > len(buf):
            raise ValueError('truncated attribute header')
        nlen = struct.unpack_from('>H', buf, i + 1)[0]
        name = buf[i + 3:i + 3 + nlen]
        j = i + 3 + nlen
        vlen = struct.unpack_from('>H', buf, j)[0]
        value = buf[j + 2:j + 2 + vlen]
        i = j + 2 + vlen
        if cur is None:                     # malformed but be forgiving
            cur = Group(OPERATION_ATTRS)
            groups.append(cur)
        cur.items.append((tag, bytes(name), bytes(value)))
    return Message((major, minor), code, request_id, groups, bytes(buf[i:]))


def serialize(msg):
    out = bytearray()
    out += bytes([msg.version[0], msg.version[1]])
    out += struct.pack('>HI', msg.code, msg.request_id)
    for g in msg.groups:
        out.append(g.tag)
        for tag, name, value in g.items:
            out.append(tag)
            out += struct.pack('>H', len(name)) + name
            out += struct.pack('>H', len(value)) + value
    out.append(END_OF_ATTRS)
    out += msg.data
    return bytes(out)


def new_request(op, request_id, printer_uri, version=(2, 0)):
    m = Message(version, op, request_id)
    g = Group(OPERATION_ATTRS, [
        (TAG_CHARSET, b'attributes-charset', b'utf-8'),
        (TAG_LANGUAGE, b'attributes-natural-language', b'en-us'),
        (TAG_URI, b'printer-uri', printer_uri.encode()),
    ])
    m.groups.append(g)
    return m


def i32(n):
    return struct.pack('>i', n)
