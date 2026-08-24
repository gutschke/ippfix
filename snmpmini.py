"""Just enough SNMP to read a printer's counters, and to decide whether a
client's request may be forwarded to one.

This is deliberately not a general SNMP library. It speaks v1 and v2c, GET and
GETNEXT, over UDP, and it refuses everything else. That is the whole surface a
print proxy needs, and every feature left out is one that cannot be turned
against the printer sitting behind it.

Two habits worth keeping if this is extended:

  * Every length is checked against the buffer before it is used. This parses
    datagrams that arrive from the network, so a length field is an attacker's
    first tool.
  * Nothing here raises out of a parse. Callers get None or an exception they
    already handle; a malformed packet is a non-event, not an incident.
"""
import os
import socket
import struct

# BER tags. Only the ones that appear in the messages this speaks.
T_INT = 0x02
T_OCTETS = 0x04
T_NULL = 0x05
T_OID = 0x06
T_SEQ = 0x30
T_COUNTER32 = 0x41
T_GAUGE32 = 0x42
T_TIMETICKS = 0x43
T_OPAQUE = 0x44
T_COUNTER64 = 0x46
T_NO_SUCH_OBJECT = 0x80
T_NO_SUCH_INSTANCE = 0x81
T_END_OF_MIB = 0x82

# PDU types.
GET = 0xA0
GETNEXT = 0xA1
RESPONSE = 0xA2
SET = 0xA3
GETBULK = 0xA5
INFORM = 0xA6
TRAP2 = 0xA7
REPORT = 0xA8

PDU_NAMES = {GET: 'get', GETNEXT: 'get-next', RESPONSE: 'response',
             SET: 'set', GETBULK: 'get-bulk', INFORM: 'inform',
             TRAP2: 'trap', REPORT: 'report'}

V1 = 0
V2C = 1
VERSION_NAMES = {V1: 'v1', V2C: 'v2c'}

MAX_DATAGRAM = 8192      # far above any answer to a single-varbind request


class SnmpError(Exception):
    pass


# ---------------------------------------------------------------------------
# BER
# ---------------------------------------------------------------------------
def _tlv(tag, body):
    if len(body) < 0x80:
        return bytes([tag, len(body)]) + body
    length = len(body).to_bytes((len(body).bit_length() + 7) // 8, 'big')
    return bytes([tag, 0x80 | len(length)]) + length + body


def _int(value):
    if value == 0:
        return _tlv(T_INT, b'\x00')
    out = value.to_bytes((value.bit_length() + 8) // 8, 'big')
    return _tlv(T_INT, out.lstrip(b'\x00') or b'\x00')


def encode_oid(text):
    parts = [int(x) for x in text.split('.')]
    if len(parts) < 2 or parts[0] > 6 or parts[1] > 39:
        raise SnmpError(f'not an OID: {text!r}')
    body = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p < 0:
            raise SnmpError(f'not an OID: {text!r}')
        chunk = bytearray([p & 0x7F])
        p >>= 7
        while p:
            chunk.insert(0, (p & 0x7F) | 0x80)
            p >>= 7
        body += bytes(chunk)
    return _tlv(T_OID, body)


def decode_oid(body):
    """BER OID contents to dotted text. Raises rather than guessing."""
    if not body:
        raise SnmpError('empty OID')
    out = [str(body[0] // 40), str(body[0] % 40)]
    value = 0
    for i, byte in enumerate(body[1:], 1):
        value = (value << 7) | (byte & 0x7F)
        if value > 0xFFFFFFFF:
            raise SnmpError('OID arc too large')
        if not byte & 0x80:
            out.append(str(value))
            value = 0
    if value:
        raise SnmpError('truncated OID')
    return '.'.join(out)


def _read(buf, pos):
    """One TLV. Returns (tag, body, next_pos); raises on anything malformed."""
    if pos + 2 > len(buf):
        raise SnmpError('truncated')
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length == 0x80:
        # Indefinite length is legal BER and illegal in SNMP, and supporting it
        # would mean scanning for a terminator inside attacker-supplied bytes.
        raise SnmpError('indefinite length')
    if length & 0x80:
        n = length & 0x7F
        if n > 4 or pos + n > len(buf):
            raise SnmpError('bad length')
        length = int.from_bytes(buf[pos:pos + n], 'big')
        pos += n
    if pos + length > len(buf):
        raise SnmpError('length past end of buffer')
    return tag, buf[pos:pos + length], pos + length


def _value(tag, body):
    """A varbind's value as something Python can compare and print."""
    if tag in (T_INT, T_COUNTER32, T_GAUGE32, T_TIMETICKS, T_COUNTER64):
        return int.from_bytes(body, 'big', signed=(tag == T_INT))
    if tag in (T_NULL, T_NO_SUCH_OBJECT, T_NO_SUCH_INSTANCE, T_END_OF_MIB):
        return None
    if tag == T_OID:
        try:
            return decode_oid(body)
        except SnmpError:
            return None
    return body.decode('utf-8', 'replace').strip()


class Message:
    """A parsed SNMP message: enough to make a policy decision about it."""

    __slots__ = ('version', 'community', 'pdu_type', 'request_id',
                 'error_status', 'varbinds')

    def __init__(self, version, community, pdu_type, request_id,
                 error_status, varbinds):
        self.version = version
        self.community = community
        self.pdu_type = pdu_type
        self.request_id = request_id
        self.error_status = error_status
        self.varbinds = varbinds        # [(oid_text, tag, value)]

    @property
    def oids(self):
        return [oid for oid, _t, _v in self.varbinds]

    def __repr__(self):
        return (f'<snmp {VERSION_NAMES.get(self.version, self.version)} '
                f'{PDU_NAMES.get(self.pdu_type, hex(self.pdu_type))} '
                f'{self.oids}>')


def parse(data):
    """Parse one datagram. Raises SnmpError; never returns something partial."""
    if len(data) > MAX_DATAGRAM:
        raise SnmpError('oversized datagram')
    tag, body, _end = _read(data, 0)
    if tag != T_SEQ:
        raise SnmpError('not a sequence')
    pos = 0
    tag, raw, pos = _read(body, pos)
    if tag != T_INT:
        raise SnmpError('no version')
    version = int.from_bytes(raw, 'big')
    tag, community, pos = _read(body, pos)
    if tag != T_OCTETS:
        raise SnmpError('no community')
    pdu_type, pdu, _pos = _read(body, pos)

    pos = 0
    tag, raw, pos = _read(pdu, pos)
    if tag != T_INT:
        raise SnmpError('no request id')
    request_id = int.from_bytes(raw, 'big')
    tag, raw, pos = _read(pdu, pos)              # error-status / non-repeaters
    error_status = int.from_bytes(raw, 'big')
    tag, _raw, pos = _read(pdu, pos)             # error-index / max-repetitions
    tag, vbs, _pos = _read(pdu, pos)
    if tag != T_SEQ:
        raise SnmpError('no varbind list')

    varbinds = []
    pos = 0
    while pos < len(vbs):
        tag, vb, pos = _read(vbs, pos)
        if tag != T_SEQ:
            raise SnmpError('bad varbind')
        inner = 0
        tag, raw, inner = _read(vb, inner)
        if tag != T_OID:
            raise SnmpError('varbind does not start with an OID')
        oid = decode_oid(raw)
        vtag, vraw, _inner = _read(vb, inner)
        varbinds.append((oid, vtag, _value(vtag, vraw)))
    return Message(version, community.decode('latin1'), pdu_type,
                   request_id, error_status, varbinds)


def encode_request(oid, community='public', pdu_type=GET, request_id=None,
                   version=V2C):
    """One single-varbind GET or GETNEXT."""
    if request_id is None:
        request_id = int.from_bytes(os.urandom(3), 'big') + 1
    varbind = _tlv(T_SEQ, encode_oid(oid) + _tlv(T_NULL, b''))
    pdu = _tlv(pdu_type,
               _int(request_id) + _int(0) + _int(0) + _tlv(T_SEQ, varbind))
    return request_id, _tlv(T_SEQ, _int(version)
                            + _tlv(T_OCTETS, community.encode()) + pdu)


def get(host, oid, community='public', timeout=5, port=161):
    """One GET. Returns the value, or None if it cannot be read.

    The socket is connected, not merely sent on, so the kernel drops replies
    from anybody except the printer; the request id is random and checked for
    the same reason. Neither is a defence against somebody on the path, but
    both remove the trivially spoofable case, and this value is used to judge
    whether a printer is lying.
    """
    try:
        request_id, packet = encode_request(oid, community, GET)
    except SnmpError:
        return None
    sock = socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET,
                         socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.send(packet)
        while True:
            data = sock.recv(MAX_DATAGRAM)
            try:
                reply = parse(data)
            except SnmpError:
                return None
            if reply.request_id != request_id:
                continue            # a late answer to an earlier question
            if reply.pdu_type != RESPONSE or reply.error_status:
                return None
            if len(reply.varbinds) != 1 or reply.varbinds[0][0] != oid:
                return None
            return reply.varbinds[0][2]
    except (OSError, SnmpError):
        return None
    finally:
        sock.close()
