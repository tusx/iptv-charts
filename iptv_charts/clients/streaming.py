"""Shared streaming primitives for the playlist clients.

Both the Xtream JSON client and the M3U client face the same problem: a
provider response can be hundreds of megabytes, and it must be consumed
without ever materialising it in memory.

The approach is the same in both cases, and lives here so it is written once:

1. **Spool** the response body to a file on disk, counting bytes as they
   arrive. This gives an exact denominator for a progress bar.
2. **Read** from the file incrementally, counting bytes consumed.

Why not parse straight off the socket? Because ``requests`` decompresses
transparently, so the bytes it hands back are *decompressed* while
``Content-Length`` counts *compressed* bytes. Counting one against the other
makes a progress bar reach 100% once gzip finishes shrinking the payload --
roughly a tenth of the way through a real download. Spooling ``response.raw``
counts real transfer bytes instead.

The spool goes to the XDG cache directory and never ``/tmp``: inside a
container ``/tmp`` is often tmpfs-backed, so a 200 MB spool would quietly
become 200 MB of RAM.
"""

from __future__ import annotations

import gzip
import os
import tempfile
import time
from pathlib import Path
from typing import Iterator

from .. import config
from ..errors import SyncCancelled

#: How often progress callbacks fire, in seconds. Without throttling, a
#: 200k-item response would take a lock 200k times for no visible benefit.
PROGRESS_INTERVAL = 0.05

#: Read size for the spool drain. Large blocks because this path is a pure
#: copy to disk; there is nothing to be gained from small reads.
SPOOL_CHUNK = 262144


class CountingReader:
    """Counts bytes consumed from a file while a parser reads through it.

    Placed *underneath* the decompressor so it counts compressed bytes, which
    is what the file holds and therefore what the progress denominator refers
    to.
    """

    def __init__(self, handle, total: int | None = None, on_progress=None,
                 cancel=None) -> None:
        self._handle = handle
        self.total = total
        self.bytes_read = 0
        self._on_progress = on_progress
        self._cancel = cancel
        self._last_publish = 0.0

    def read(self, size: int = -1) -> bytes:
        if self._cancel is not None and self._cancel.is_set():
            raise SyncCancelled()

        data = self._handle.read(size)
        self.bytes_read += len(data)

        if self._on_progress:
            now = time.monotonic()
            if now - self._last_publish >= PROGRESS_INTERVAL:
                self._last_publish = now
                self._on_progress(self.bytes_read, self.total)
        return data

    def publish_final(self) -> None:
        """Emit a last progress update, bypassing the throttle.

        Necessary because the throttle can otherwise suppress *every* update
        but the first: parsing a moderate catalog takes single-digit
        milliseconds, so there is never a second 50 ms window to fire in.
        Without this the bar would sit at whatever the first read happened to
        be and then jump straight to complete.
        """
        if self._on_progress:
            self._last_publish = time.monotonic()
            self._on_progress(self.bytes_read, self.total)

    def seekable(self) -> bool:
        # GzipFile probes this. We must not claim to support seeking: the
        # underlying stream is consumed linearly and cannot be rewound.
        return False

    def close(self) -> None:
        self._handle.close()


def spool_response(response, on_progress=None, cancel=None,
                   prefix: str = "spool-", suffix: str = ".body") -> tuple[Path, str | None, int]:
    """Drain a response body to a temporary file.

    Returns ``(path, content_encoding, bytes_written)``. The caller owns the
    file and must delete it.
    """
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower() or None
    total = _to_int(response.headers.get("Content-Length"))

    handle, name = tempfile.mkstemp(dir=config.spool_dir(), prefix=prefix, suffix=suffix)
    path = Path(name)
    written = 0
    last_publish = 0.0

    try:
        with os.fdopen(handle, "wb") as out:
            # decode_content=False keeps these the raw transferred bytes, so
            # `written` is directly comparable to Content-Length.
            for chunk in response.raw.stream(SPOOL_CHUNK, decode_content=False):
                if cancel is not None and cancel.is_set():
                    raise SyncCancelled()
                out.write(chunk)
                written += len(chunk)
                if on_progress:
                    now = time.monotonic()
                    if now - last_publish >= PROGRESS_INTERVAL:
                        last_publish = now
                        on_progress(written, total)
        if on_progress:
            on_progress(written, total)
    except BaseException:
        path.unlink(missing_ok=True)
        raise

    return path, encoding, written


def open_maybe_gzip(path: Path, encoding: str | None, counted: CountingReader):
    """Wrap ``counted`` in a decompressor when the response was gzipped.

    The decompressor reads *through* the counting reader, so the counter still
    sees compressed bytes and progress stays honest.
    """
    if encoding and "gzip" in encoding:
        return gzip.GzipFile(fileobj=counted)
    return counted


def peek_looks_like_object(path: Path, encoding: str | None) -> str | None:
    """Return a snippet when the payload is a JSON object rather than an array.

    Guards a genuinely nasty failure: if a panel returns ``{"status":"error"}``
    where an array was expected, an ijson ``'item'`` prefix matches nothing and
    yields *zero items with no error at all*. The sync would then report
    success having imported nothing.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None

    if encoding and "gzip" in encoding:
        try:
            head = gzip.decompress(head)
        except (OSError, EOFError):
            return None  # truncated block; the parse itself will complain

    if head.lstrip()[:1] != b"{":
        return None
    return head[:200].decode("utf-8", errors="replace").strip()


def iter_text_lines(path: Path, encoding: str | None, on_progress=None,
                    cancel=None) -> Iterator[str]:
    """Yield decoded lines from a spooled text file, one at a time.

    Memory stays at O(longest line), so a 100k-line playlist costs a few
    megabytes rather than the size of the file.

    Decoding is tolerant on purpose: real playlists contain BOMs and stray
    Latin-1 bytes, and failing an entire sync over one bad byte would be a
    poor trade for a cosmetic problem.
    """
    size = path.stat().st_size
    counted = CountingReader(open(path, "rb"), total=size,
                             on_progress=on_progress, cancel=cancel)

    stream = open_maybe_gzip(path, encoding, counted)
    try:
        yield from _iter_decoded_lines(stream, cancel)
    finally:
        counted.publish_final()
        counted.close()


def _iter_decoded_lines(stream, cancel) -> Iterator[str]:
    """Split a binary stream into decoded, newline-stripped lines.

    Written by hand rather than by iterating the stream, because on the
    uncompressed path the stream is a CountingReader, which implements only
    ``read()`` -- deliberately, since it is a counter, not a file object.
    Iterating it raises TypeError.
    """
    buffer = b""
    while True:
        if cancel is not None and cancel.is_set():
            raise SyncCancelled()

        chunk = stream.read(65536)
        if not chunk:
            break

        parts = (buffer + chunk).split(b"\n")
        buffer = parts.pop()          # trailing partial line, carried forward
        for part in parts:
            yield _decode_line(part)

    if buffer:
        yield _decode_line(buffer)


def _decode_line(raw: bytes) -> str:
    # Strip a BOM wherever it landed. It only ever legitimately appears at the
    # very start of the file, but a playlist served through a broken proxy can
    # carry one per line, and a leading "﻿#EXTM3U" would fail the header
    # check and get a perfectly good playlist rejected.
    return raw.rstrip(b"\r").decode("utf-8", errors="replace").lstrip("﻿")


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


__all__ = [
    "CountingReader",
    "PROGRESS_INTERVAL",
    "iter_text_lines",
    "open_maybe_gzip",
    "peek_looks_like_object",
    "spool_response",
]
