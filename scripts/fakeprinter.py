#!/usr/bin/env python3
"""A mock IPP printer, for tests that must never touch real hardware.

The proxy's relay path -- what it sends upstream, what it does with the answer,
what it does when there is no answer -- can only be pinned against something
that speaks IPP back. The real printers are the thing this project exists to
work around, they are slow, and every experiment on one costs paper, so the
self-test talks to this instead: a listener on loopback that answers the ten
operations `ALLOWED_OPS` permits and can be told to fail in the specific ways
that matter.

Three rules shape it.

**The capabilities are captured, not invented.** Get-Printer-Attributes is
answered from `fixtures/get-printer-attributes.b64`, which is a real reply off
a real M283fdw with the addresses sanitised and nothing else changed. An
attribute set written by hand drifts away from what firmware sends, and a test
built on one then passes while describing a printer that does not exist.

**The clock is the test's, not the wall's.** `Clock` looks enough like the
`time` module to be swapped in for it, and every job here advances only when
somebody advances the clock. `watch_job()` sleeps five seconds a poll and eight
more to let the last sheet land; against a real clock that is a twenty-second
test, and against this one it is instant and deterministic.

**It cleans up after itself.** Job history is bounded, connection threads are
tracked and joined, and it is a context manager -- so a test cannot leave one
listening, and a leaked thread is a failure rather than a slow drift.

There is deliberately no second IPP codec here: everything is built and parsed
with `ippcodec`, so a bug in the codec cannot be hidden by a mock that agrees
with it by accident.

Run it standalone to poke at by hand:

    python3 scripts/fakeprinter.py            # prints the URI it is listening on
"""
import base64
import collections
import contextlib
import os
import socket
import struct
import sys
import threading
import time as _real_time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ippcodec as ipp                                  # noqa: E402


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'fixtures', 'get-printer-attributes.b64')

# IPP operations. Named here rather than imported from ippfix, because a mock
# that shares its constants with the thing it tests cannot catch a change to
# them.
OP_PRINT_JOB = 0x0002
OP_VALIDATE_JOB = 0x0004
OP_CREATE_JOB = 0x0005
OP_SEND_DOCUMENT = 0x0006
OP_CANCEL_JOB = 0x0008
OP_GET_JOB_ATTRIBUTES = 0x0009
OP_GET_JOBS = 0x000A
OP_GET_PRINTER_ATTRIBUTES = 0x000B
OP_CLOSE_JOB = 0x003B
OP_IDENTIFY_PRINTER = 0x003C

# IPP status codes this printer can answer with.
OK = 0x0000
OK_IGNORED = 0x0001            # successful-ok-ignored-or-substituted-attributes
BAD_REQUEST = 0x0400
NOT_FOUND = 0x0406
NOT_POSSIBLE = 0x0404          # what the M283fdw answers; measured, see below
TOO_LARGE = 0x0408             # client-error-request-entity-too-large
MULTIPLE_JOBS_NOT_SUPPORTED = 0x0509

# A URF stream begins UNIRAST\0, then a 4-byte page count, then one 32-byte
# header per page. The first three bytes of that header are bits per pixel,
# colour space, and duplex -- so the duplex byte of the first page is at offset
# 14 of the stream, and that byte is what the printer obeys. Measured on paper:
# a two-page URF declaring one-sided, sent with sides=two-sided-long-edge and
# media=na_letter_8.5x11in, was answered 0x0000 with nothing in
# unsupported-attributes, reported two impressions, completed -- and came out as
# two simplex sheets.
URF_MAGIC = b'UNIRAST\0'
URF_PAGE_HEADER = 32
URF_ONE_SIDED = 1              # the only duplex value confirmed on paper

# Colour space bytes this mock will accept in a URF stream. NOT measured: what
# was measured is that a stream produced without -dcupsColorSpace -- so carrying
# a colour space nobody chose -- came back job-state 8, aborted, with 0
# impressions, and made the printer emit an error page naming a parser fault.
# These five are the values the proxy is able to ask for, being the
# urf-supported names it knows how to translate; anything else stands for a
# colour space the device does not implement. So what the abort below pins is
# the behaviour, not the device's table -- establishing that would cost paper.
URF_COLORSPACES = (0, 1, 18, 19, 20)

# RFC 8011 job-state.
PENDING = 3
PENDING_HELD = 4
PROCESSING = 5
CANCELED = 7
ABORTED = 8
COMPLETED = 9
TERMINAL = (CANCELED, ABORTED, COMPLETED)

# The SNMP OIDs PageCounter reads. A test that wants the cross-check exercised
# points ippfix.snmp.get at FakePrinter.snmp_get.
OID_PAGE_COUNT = '1.3.6.1.2.1.43.10.2.1.4.1.1'      # prtMarkerLifeCount
OID_COUNTER_UNIT = '1.3.6.1.2.1.43.10.2.1.3.1.1'    # prtMarkerCounterUnit
UNIT_IMPRESSIONS = 7


class Clock:
    """A monotonic clock that only moves when a test moves it.

    Substitutable for the `time` module: `monotonic`, `time` and `sleep` are
    the three the proxy uses, and anything else falls through to the real
    module so that swapping this in cannot break an unrelated call. `sleep`
    does not sleep -- it advances the clock, which is exactly what the code
    under test believes happened, and what lets a poll loop written around
    five-second sleeps run in no time at all.
    """

    def __init__(self, start=10000.0):
        self.now = float(start)
        self._listeners = []

    def on_advance(self, callback):
        """Call `callback(seconds)` whenever the clock moves."""
        self._listeners.append(callback)

    def advance(self, seconds):
        self.now += seconds
        for callback in list(self._listeners):
            callback(seconds)

    # -- the parts of the time module the proxy uses -----------------------
    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.advance(seconds)

    def __getattr__(self, name):
        return getattr(_real_time, name)


@contextlib.contextmanager
def controlled_clock(module, clock):
    """Run `module` against `clock` instead of the wall clock.

    Restores the real module afterwards even if the body raises, because a
    test process that leaves a fake clock installed poisons every test after
    it in ways that look like anything but the cause.
    """
    real = module.time
    module.time = clock
    try:
        yield clock
    finally:
        module.time = real


def captured_attributes():
    """The captured Get-Printer-Attributes reply, as raw IPP bytes.

    See the header of the fixture for when and from what it was captured, and
    for the four length-preserving substitutions that were made to it.
    """
    with open(FIXTURE) as handle:
        text = ''.join(line for line in handle if not line.startswith('#'))
    return base64.b64decode(text)


def declared_pdf_cap():
    """The upper end of pdf-k-octets-supported, in bytes, as captured.

    Read from the fixture rather than written down here, so that the number the
    mock declares and the number it can be told to enforce are the same one the
    device published. It declares 0..75000 -- and does not enforce it.
    """
    group = ipp.parse(captured_attributes()).group(ipp.PRINTER_ATTRS)
    value = (group.get('pdf-k-octets-supported') or [b''])[0]
    if len(value) != 8:
        return None
    return struct.unpack_from('>ii', value, 0)[1] * 1024


URFHeader = collections.namedtuple('URFHeader', 'pages bpp colorspace duplex')


def parse_urf(data):
    """Read the first page header of a URF stream.

    Returns (pages, bpp, colorspace, duplex), or None for anything that is not
    a URF stream this printer could begin to read. Only the first page is
    looked at: it is the one the device acts on for the whole job, and it is
    the one a test can put a known byte into.
    """
    if data[:len(URF_MAGIC)] != URF_MAGIC:
        return None
    start = len(URF_MAGIC) + 4
    if len(data) < start + URF_PAGE_HEADER:
        return None
    pages = struct.unpack_from('>I', data, len(URF_MAGIC))[0]
    bpp, colorspace, duplex = data[start], data[start + 1], data[start + 2]
    return URFHeader(pages, bpp, colorspace, duplex)


class Job:
    """One job, and what the printer will say about it."""

    __slots__ = ('id', 'name', 'fmt', 'size', 'state', 'reasons',
                 'impressions', 'pages', 'open', 'sides', 'sides_ignored',
                 'urf_duplex')

    def __init__(self, job_id, name, fmt, size, pages):
        self.id = job_id
        self.name = name
        self.fmt = fmt
        self.size = size
        self.state = PENDING
        self.reasons = ['none']
        self.impressions = 0
        self.pages = pages
        # False between Create-Job and Close-Job: a job still taking documents
        # does not start printing.
        self.open = False
        # What the printer took from the request, which is not always what the
        # request asked for: sides is dropped unless media came with it.
        self.sides = None
        self.sides_ignored = False
        # The duplex byte of the raster stream, when the document was one.
        self.urf_duplex = None

    @property
    def active(self):
        return self.state not in TERMINAL

    @property
    def duplexed(self):
        """Whether this job really duplexed, as far as the paper would show.

        None means unknown, and is returned rather than a guess in the case
        that matters: the URF duplex values for two-sided have not been
        established yet, so only `URF_ONE_SIDED` is read as an answer. For a
        raster job the stream decides and the IPP attribute does not -- that is
        the finding this exists to keep -- and for anything else the attribute
        is all there is.
        """
        if self.urf_duplex is not None:
            return False if self.urf_duplex == URF_ONE_SIDED else None
        if self.sides is None:
            return None
        return self.sides != 'one-sided'


class FakePrinter:
    """An IPP printer that never marks paper.

    Use it as a context manager; `port` is allocated by the kernel, so several
    tests can run at once and none of them needs a fixed port:

        with FakePrinter() as printer:
            queue = ippfix.parse_queue(f't={printer.uri}')
            ...
            printer.clock.advance(5)      # move every job one step on
    """

    # Failure modes. Anything else is a typo, and a typo that silently means
    # "behave normally" turns a test that proves something into one that
    # proves nothing.
    MODES = (None,
             'reject_job',                 # the job is accepted and aborted
             'hold_job',                   # pending, media-empty, forever
             'silent_loss',                # completes having marked nothing
             'unreachable',                # connections are refused
             'enforce_pdf_cap',            # refuses a PDF over its declared cap
             'accept_then_drop_response')  # body read in full, no answer sent

    MAX_JOBS = 16          # bounded history: a test must not grow one without end
    MAX_REQUESTS = 64      # likewise for the recorded transcript
    ACCEPT_POLL = 0.1      # how often the accept loop checks for a mode change

    def __init__(self, host='127.0.0.1', path='/ipp/print', mode=None,
                 pages_per_job=1, page_counter=1000, pdf_cap=None,
                 urf_colorspaces=URF_COLORSPACES):
        self.host = host
        self.path = path
        self.pages_per_job = pages_per_job
        # The cap this printer declares, taken from the captured attributes so
        # that it is the device's own number rather than one invented here. It
        # is enforced only in 'enforce_pdf_cap' mode: the real device declares
        # 75000 KB and printed a 92.5 MB PDF, so accepting is the faithful
        # default. A test may lower it rather than transfer 76 MB to prove a
        # point about a refusal.
        self.pdf_cap = declared_pdf_cap() if pdf_cap is None else pdf_cap
        self.urf_colorspaces = tuple(urf_colorspaces)
        # The lifetime page counter the SNMP cross-check reads. It starts
        # somewhere non-zero because a printer that has never printed is not
        # the case anybody is testing.
        self.page_counter = page_counter
        self.clock = Clock()
        self.clock.on_advance(self._tick)
        self.jobs = []                     # oldest first, at most MAX_JOBS
        self.requests = []                 # [(op, raw request bytes)]
        self.lock = threading.RLock()
        self._next_job_id = 101
        self._attributes = captured_attributes()
        self._mode = None
        self._stop = threading.Event()
        self._threads = []
        # Bind before the mode is set: 'unreachable' closes the listener, which
        # has to exist first, and the port has to be known before it can be
        # rebound later.
        self._listener = self._bind(0)
        self.port = self._listener.getsockname()[1]
        self._accepting = threading.Thread(target=self._serve, daemon=True,
                                           name='fakeprinter')
        self._accepting.start()
        self.mode = mode                   # validated by the property below

    # -- lifecycle ---------------------------------------------------------
    def _bind(self, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, port))
        sock.listen(8)
        sock.settimeout(self.ACCEPT_POLL)
        return sock

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def close(self):
        """Stop listening and join everything started here.

        A leaked thread is raised rather than tolerated: this project's whole
        posture is that state left behind is a bug, and a test harness is not
        exempt from it.
        """
        self._stop.set()
        self._accepting.join(timeout=5)
        for thread in list(self._threads):
            thread.join(timeout=5)
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        alive = [t.name for t in self._threads + [self._accepting]
                 if t.is_alive()]
        if alive:
            raise AssertionError(f'fake printer leaked threads: {alive}')
        self._threads.clear()

    @property
    def uri(self):
        return f'ipp://{self.host}:{self.port}{self.path}'

    # -- failure modes -----------------------------------------------------
    @property
    def mode(self):
        return self._mode

    @mode.setter
    def mode(self, value):
        if value not in self.MODES:
            raise ValueError(f'unknown failure mode {value!r}; '
                             f'known modes are {self.MODES}')
        previous = self._mode
        self._mode = value
        if value == 'unreachable' and previous != 'unreachable':
            # Really refuse connections rather than accepting and hanging up:
            # ECONNREFUSED is what a printer that is switched off produces, and
            # it is a different code path in connect_upstream() from a printer
            # that answers badly.
            if self._listener is not None:
                self._listener.close()
                self._listener = None
                self._wait_until_refused()
        elif previous == 'unreachable' and value != 'unreachable':
            self._listener = self._bind(self.port)

    def _wait_until_refused(self):
        """Block until the port really refuses, not merely until close() returned.

        The accept loop may be inside accept() when the listener is closed, and
        the kernel keeps the listening socket alive until that call returns --
        so for a moment afterwards a connection is still accepted and then
        reset. Both are OSError and the proxy treats them alike, but a test
        that means "the printer is off" should not be racing a 0.1s window.
        """
        deadline = _real_time.monotonic() + 5
        while _real_time.monotonic() < deadline:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(0.5)
            try:
                probe.connect((self.host, self.port))
            except OSError:
                probe.close()
                return
            probe.close()
            _real_time.sleep(0.02)
        raise AssertionError('the fake printer would not stop listening')

    def release(self):
        """Let a held job proceed, as refilling the tray would.

        Only clears 'hold_job'. Going through the attribute would be wrong for
        'unreachable', which owns the listening socket and has to be turned off
        deliberately rather than as a side effect of refilling a tray.
        """
        with self.lock:
            if self._mode == 'hold_job':
                self._mode = None
            for job in self.jobs:
                if job.state in (PENDING, PENDING_HELD):
                    job.reasons = ['none']

    # -- the job engine ----------------------------------------------------
    def _tick(self, _seconds):
        """Advance every unfinished job by one step.

        One step per clock advance, whatever its size: the point is a state
        machine a test can drive, not a simulation of how long a page takes.
        """
        with self.lock:
            for job in self.jobs:
                if not job.active or job.open:
                    continue
                if self._mode == 'hold_job':
                    # media-empty is the conformant way to say the tray is
                    # empty, and it is what a test asking for a held job
                    # expects to see. Measured, the M283fdw does something
                    # else: it holds at job-state 6 with job-state-reasons =
                    # printer-stopped and shows the empty tray only over SNMP
                    # (OPEN-QUESTIONS.md). A test that needs that exact shape
                    # should set job.state and job.reasons itself rather than
                    # have this mode quietly mean two things.
                    job.state = PENDING
                    job.reasons = ['media-empty']
                    continue
                if job.state in (PENDING, PENDING_HELD):
                    job.state = PROCESSING
                    job.reasons = ['job-printing']
                    continue
                if job.state == PROCESSING:
                    if self._mode != 'silent_loss':
                        job.impressions += 1
                        self.page_counter += 1
                    if job.impressions >= job.pages \
                            or self._mode == 'silent_loss':
                        job.state = COMPLETED
                        job.reasons = ['job-completed-successfully']

    def _new_job(self, name, fmt, size):
        job = Job(self._next_job_id, name, fmt, size, self.pages_per_job)
        self._next_job_id += 1
        self.jobs.append(job)
        # Bounded history. Terminal jobs go first, so a test that keeps a
        # printer for a long run still sees whatever is happening now.
        while len(self.jobs) > self.MAX_JOBS:
            drop = next((j for j in self.jobs if not j.active), self.jobs[0])
            self.jobs.remove(drop)
        return job

    def job(self, job_id):
        return next((j for j in self.jobs if j.id == job_id), None)

    def _too_large(self, fmt, size):
        """Refuse a PDF over the declared cap -- only when asked to.

        The real device does not do this: it declares pdf-k-octets-supported
        0..75000, a 76.8 MB working limit, and printed a 92.5 MB PDF. So the
        default is to accept, which is why the proxy no longer decides for
        itself that a document is too big. This mode exists because the
        refuse-then-rasterise path it grew instead cannot be tested against a
        printer that never refuses anything.

        Nothing is created when it refuses. That is the property the whole
        retry rests on: the client may send something else without printing
        anything twice.
        """
        if self._mode != 'enforce_pdf_cap' or not self.pdf_cap:
            return None
        if (fmt or '') not in ('application/pdf', 'application/octet-stream'):
            return None
        return TOO_LARGE if size > self.pdf_cap else None

    def _take_sides(self, job, request):
        """Apply the sides attribute the way this firmware does, or not at all.

        Measured: `sides=two-sided-long-edge` on its own comes back 0x0001 with
        `sides` in unsupported-attributes and the job prints one-sided; the
        same job with `media=na_letter_8.5x11in` beside it comes back 0x0000
        and duplexes. The device publishes `job-constraints-supported:
        duplex-unsupported-media` and resolves the constraint by discarding
        `sides` rather than by applying `media-default`, which RFC 8011 5.2
        says it should.

        Returns the unsupported-attributes groups for the reply, which is empty
        when there was nothing to complain about.
        """
        group = request.group(ipp.JOB_ATTRS)
        sides = group.get_str('sides') if group else None
        if sides is None:
            return []
        has_media = (group.index_of('media') >= 0
                     or group.index_of('media-col') >= 0)
        if has_media:
            job.sides = sides
            return []
        job.sides_ignored = True
        return [ipp.Group(ipp.UNSUPPORTED_ATTRS,
                          [(ipp.TAG_KEYWORD, b'sides', sides.encode())])]

    def _read_document(self, job, fmt, data):
        """Look at a raster document the way the marking engine would.

        Two findings live here. The stream's own duplex byte is what the device
        obeys, whatever `sides` said, so it is recorded and exposed rather than
        interpreted -- only the one-sided value has been confirmed on paper.
        And a stream it cannot parse is aborted rather than dropped: a URF made
        without -dcupsColorSpace came back job-state 8 with 0 impressions.

        The abort also costs a sheet on the real device, which emits an error
        page naming a parser fault. Nothing here can model paper; the point is
        that code which starts producing an unacceptable raster fails a test
        instead of a print.
        """
        if not data or (fmt != 'image/urf'
                        and data[:len(URF_MAGIC)] != URF_MAGIC):
            return
        header = parse_urf(data)
        if header is None or header.colorspace not in self.urf_colorspaces:
            job.state = ABORTED
            job.reasons = ['job-aborted-by-system']
            return
        job.urf_duplex = header.duplex
        if header.pages:
            # The stream says how many pages there are, and the device reports
            # one impression per page of it.
            job.pages = header.pages

    @property
    def active_job(self):
        return next((j for j in self.jobs if j.active), None)

    def snmp_get(self, _host, oid, _community='public', timeout=5):
        """Stand in for snmpmini.get() so the page-counter path can be tested.

        Only the two OIDs `PageCounter` reads are answered; anything else
        returns None, which is what an unanswered request looks like to it.
        """
        del timeout
        if self._mode == 'unreachable':
            return None
        if oid == OID_PAGE_COUNT:
            return self.page_counter
        if oid == OID_COUNTER_UNIT:
            return UNIT_IMPRESSIONS
        return None

    # -- IPP ---------------------------------------------------------------
    def _job_group(self, job):
        return ipp.Group(ipp.JOB_ATTRS, [
            (ipp.TAG_URI, b'job-uri',
             f'{self.uri}/{job.id}'.encode()),
            (ipp.TAG_INTEGER, b'job-id', ipp.i32(job.id)),
            (ipp.TAG_URI, b'job-printer-uri', self.uri.encode()),
            (ipp.TAG_NAME, b'job-name', (job.name or 'untitled').encode()),
            (ipp.TAG_NAME, b'job-originating-user-name', b'tester'),
            (ipp.TAG_ENUM, b'job-state', ipp.i32(job.state)),
        ] + [(ipp.TAG_KEYWORD, b'job-state-reasons' if i == 0 else b'',
              reason.encode())
             for i, reason in enumerate(job.reasons)] + [
            (ipp.TAG_INTEGER, b'job-impressions-completed',
             ipp.i32(job.impressions)),
        ])

    def _reply(self, request, status, groups=()):
        msg = ipp.Message(version=request.version, code=status,
                          request_id=request.request_id)
        msg.groups.append(ipp.Group(ipp.OPERATION_ATTRS, [
            (ipp.TAG_CHARSET, b'attributes-charset', b'utf-8'),
            (ipp.TAG_LANGUAGE, b'attributes-natural-language', b'en'),
        ]))
        msg.groups.extend(groups)
        return ipp.serialize(msg)

    def _int_attr(self, request, name):
        group = request.operation()
        if group is None:
            return None
        values = group.get(name)
        if not values or len(values[0]) != 4:
            return None
        return struct.unpack('>i', values[0])[0]

    def handle_message(self, raw):
        """Answer one IPP request. Returns the raw reply, or None to hang up."""
        request = ipp.parse(raw)
        with self.lock:
            self.requests.append((request.code, raw))
            del self.requests[:-self.MAX_REQUESTS]
            op = request.operation()
            name = op.get_str('job-name') if op else None
            fmt = op.get_str('document-format') if op else None

            if request.code == OP_GET_PRINTER_ATTRIBUTES:
                reply = ipp.parse(self._attributes)
                reply.request_id = request.request_id
                return ipp.serialize(reply)

            if request.code == OP_VALIDATE_JOB:
                return self._reply(request, OK)

            if request.code == OP_IDENTIFY_PRINTER:
                return self._reply(request, OK)

            if request.code in (OP_PRINT_JOB, OP_CREATE_JOB):
                # multiple-document-jobs-supported is false on the printers
                # this proxy exists for, and they mishandle a second job
                # arriving mid-transfer. Refusing it here is what turns a proxy
                # bug that interleaves jobs into a red test rather than into
                # ruined paper.
                if self.active_job is not None:
                    return self._reply(request, MULTIPLE_JOBS_NOT_SUPPORTED)
                # Refused before a job exists, so there is nothing for a client
                # that sends something else instead to print twice.
                refusal = self._too_large(fmt, len(request.data))
                if refusal is not None:
                    return self._reply(request, refusal)
                job = self._new_job(name, fmt, len(request.data))
                unsupported = self._take_sides(job, request)
                self._read_document(job, fmt, request.data)
                if request.code == OP_CREATE_JOB:
                    job.open = True
                elif self._mode == 'reject_job':
                    # Accepted at the protocol level and then abandoned, which
                    # is how these printers refuse a document they have already
                    # taken -- the client is told, unlike a silent loss.
                    job.state = ABORTED
                    job.reasons = ['job-canceled-by-system']
                elif self._mode == 'hold_job':
                    job.reasons = ['media-empty']
                if self._mode == 'accept_then_drop_response':
                    # The body has been read in full and the job exists. Now
                    # vanish. Anything that resubmits after this double-prints.
                    return None
                # 0x0001 successful-ok-ignored-or-substituted-attributes: the
                # job was taken, and something it asked for was not done.
                return self._reply(request, OK_IGNORED if unsupported else OK,
                                   unsupported + [self._job_group(job)])

            if request.code == OP_SEND_DOCUMENT:
                job_id = self._int_attr(request, 'job-id')
                job = self.job(job_id)
                if job is None:
                    return self._reply(request, NOT_FOUND)
                refusal = self._too_large(fmt or job.fmt,
                                          job.size + len(request.data))
                if refusal is not None:
                    # The document is refused; the job it was offered to is
                    # left as it was, having taken nothing.
                    return self._reply(request, refusal)
                job.size += len(request.data)
                if job.fmt is None:
                    job.fmt = fmt
                unsupported = self._take_sides(job, request)
                self._read_document(job, fmt or job.fmt, request.data)
                last = op.get('last-document') if op else None
                if last and last[0] not in (b'\x00', b''):
                    job.open = False
                if self._mode == 'reject_job':
                    job.state = ABORTED
                    job.reasons = ['job-canceled-by-system']
                elif self._mode == 'hold_job':
                    job.reasons = ['media-empty']
                if self._mode == 'accept_then_drop_response':
                    return None
                return self._reply(request, OK_IGNORED if unsupported else OK,
                                   unsupported + [self._job_group(job)])

            if request.code == OP_CLOSE_JOB:
                job = self.job(self._int_attr(request, 'job-id'))
                if job is None:
                    return self._reply(request, NOT_FOUND)
                job.open = False
                return self._reply(request, OK, [self._job_group(job)])

            if request.code == OP_CANCEL_JOB:
                job = self.job(self._int_attr(request, 'job-id'))
                if job is None:
                    return self._reply(request, NOT_FOUND)
                if not job.active:
                    # Measured on the real printer (see OPEN-QUESTIONS.md):
                    # Cancel-Job on a job that has already reached a terminal
                    # state comes back client-error-not-possible, not a
                    # success and not a not-found.
                    return self._reply(request, NOT_POSSIBLE)
                job.state = CANCELED
                job.reasons = ['job-canceled-by-user']
                return self._reply(request, OK)

            if request.code == OP_GET_JOB_ATTRIBUTES:
                job = self.job(self._int_attr(request, 'job-id'))
                if job is None:
                    return self._reply(request, NOT_FOUND)
                return self._reply(request, OK, [self._job_group(job)])

            if request.code == OP_GET_JOBS:
                return self._reply(request, OK,
                                   [self._job_group(j) for j in self.jobs])

            return self._reply(request, BAD_REQUEST)

    # -- HTTP --------------------------------------------------------------
    def _serve(self):
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:                 # unreachable; wait to return
                self._stop.wait(self.ACCEPT_POLL)
                continue
            try:
                conn, _peer = listener.accept()
            except OSError:                      # timeout, or closed under us
                continue
            thread = threading.Thread(target=self._converse, args=(conn,),
                                      daemon=True, name='fakeprinter-conn')
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
            thread.start()

    def _converse(self, conn):
        rfile = None
        try:
            conn.settimeout(10)
            rfile = conn.makefile('rb')
            line = rfile.readline(8192)
            if not line:
                return
            headers = {}
            while True:
                header = rfile.readline(8192)
                if header in (b'', b'\r\n', b'\n'):
                    break
                key, _, value = header.decode('latin-1').partition(':')
                headers[key.strip().lower()] = value.strip()
            if headers.get('expect', '').lower() == '100-continue':
                conn.sendall(b'HTTP/1.1 100 Continue\r\n\r\n')
            # Only Content-Length is handled, because that is all upstream_ipp()
            # ever sends. A chunked body here would mean the proxy changed, and
            # a mock that quietly coped would hide it.
            length = int(headers.get('content-length', '0') or 0)
            body = rfile.read(length) if length else b''
            if len(body) != length:
                return
            try:
                reply = self.handle_message(body)
            except Exception as exc:              # a mock must not die quietly
                reply = None
                print(f'fakeprinter: {exc}', file=sys.stderr)
            if reply is None:
                return                            # drop, having read it all
            conn.sendall(
                b'HTTP/1.1 200 OK\r\n'
                b'Content-Type: application/ipp\r\n'
                b'Content-Length: ' + str(len(reply)).encode() + b'\r\n'
                b'Connection: close\r\n\r\n' + reply)
        except OSError:
            pass
        finally:
            for handle in (rfile, conn):
                try:
                    if handle is not None:
                        handle.close()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# driving the proxy against one of these
# ---------------------------------------------------------------------------
# ippfix is imported lazily below rather than at the top of this file, so that
# the mock itself stays usable -- and debuggable -- without dragging the thing
# it is meant to test into the process.

def proxy_for(printer, name='office', extra=()):
    """A Config and Queue that point the proxy at this fake printer.

    Conversion is off: Ghostscript is exercised elsewhere, it is slow, and its
    output is not byte-stable, so a relay test built on it could not pin a
    transcript.
    """
    import ippfix
    spec = f'{name}=ipp://{printer.host}:{printer.port}{printer.path}'
    args = ippfix.build_parser().parse_args(
        ['--advertise', '192.0.2.10', '--no-ipv6', '--no-convert',
         *extra, spec])
    queue = ippfix.parse_queue(spec)
    return ippfix.Config(args, [queue]), queue


class Answer:
    """What a client would have seen come back."""

    __slots__ = ('status', 'headers', 'body', 'ipp')

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body
        self.ipp = (ipp.parse(body)
                    if 'ipp' in headers.get('content-type', '') else None)


def relay(cfg, msg, path=None, client=('192.0.2.99', 5000)):
    """Push one IPP request through Handler.handle_ipp and read the answer.

    This is the whole do_POST path -- resolve, operation check, rewrite,
    forward, rewrite back -- with only the socket replaced, which is what makes
    it testable without a client and without a printer.
    """
    import io
    import ippfix
    handler = ippfix.Handler.__new__(ippfix.Handler)
    handler.client_address = client
    sink = io.BytesIO()
    handler.handle_ipp(cfg, sink, path or next(iter(cfg.queues)),
                       ipp.serialize(msg))
    head, _, body = sink.getvalue().partition(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(':')
        headers[key.strip().lower()] = value.strip()
    return Answer(lines[0].split(' ', 1)[1], headers, body)


def post(printer, msg):
    """Send one IPP request straight at the mock, with no proxy in between.

    The tests that pin what the mock itself does need to reach it directly: put
    the proxy in the middle and a fault there could hide a fault here.
    """
    import http.client
    body = ipp.serialize(msg)
    conn = http.client.HTTPConnection(printer.host, printer.port, timeout=5)
    try:
        conn.request('POST', printer.path, body=body,
                     headers={'Content-Type': 'application/ipp',
                              'Content-Length': str(len(body))})
        return ipp.parse(conn.getresponse().read())
    finally:
        conn.close()


def job_request(op, request_id, uri, job_id=None, job_attrs=(), **attrs):
    """An IPP request as a client would send it, with attributes in one order.

    Order matters here: these messages are compared byte for byte against what
    the proxy forwarded, and a dict that iterated differently would make that
    comparison meaningless.

    `job_attrs` is a sequence of (name, tag, values) put in the job group,
    which is where a client asks for sides and media -- and where this printer
    insists on seeing both of them together.
    """
    msg = ipp.new_request(op, request_id, uri)
    group = msg.operation()
    if job_id is not None:
        group.items.append((ipp.TAG_INTEGER, b'job-id', ipp.i32(job_id)))
    for name, (tag, values) in attrs.items():
        group.replace(name.replace('_', '-'), tag, values)
    if job_attrs:
        job = ipp.Group(ipp.JOB_ATTRS)
        for name, tag, values in job_attrs:
            job.replace(name, tag, values)
        msg.groups.append(job)
    return msg


def urf_document(pages=1, colorspace=19, duplex=URF_ONE_SIDED, bpp=8):
    """A URF stream with a chosen first-page header, for tests.

    Only the header matters here: nothing rasterises it, and the printer this
    stands in for decides what it will do from these bytes.
    """
    header = bytes([bpp, colorspace, duplex, 0]) + bytes(URF_PAGE_HEADER - 4)
    return URF_MAGIC + struct.pack('>I', pages) + header + bytes(64)


if __name__ == '__main__':
    with FakePrinter() as p:
        print(f'listening on {p.uri}')
        try:
            while True:
                _real_time.sleep(1)
                p.clock.advance(1)
        except KeyboardInterrupt:
            pass
