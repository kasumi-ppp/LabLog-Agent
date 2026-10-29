"""Read native conversation logs, including compressed Codex archives."""

from __future__ import annotations

import io
from contextlib import contextmanager
from pathlib import Path


class _ForwardDecompressor(io.RawIOBase):
    """Expose a zstandard stream as a forward-seekable raw binary stream."""

    def __init__(self, stream):
        super().__init__()
        self._stream = stream
        self._position = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._position + offset
        elif whence == io.SEEK_END:
            raise OSError("compressed logs only support forward seeking")
        else:
            raise ValueError("invalid whence")
        if target < self._position:
            raise OSError("compressed logs only support forward seeking")
        self._discard(target - self._position)
        return self._position

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("I/O operation on closed file")
        try:
            data = self._stream.read(len(buffer))
        except Exception as exc:
            raise OSError("could not decompress native conversation log") from exc
        if not data:
            return 0
        buffer[:len(data)] = data
        self._position += len(data)
        return len(data)

    def _discard(self, count):
        while count:
            try:
                chunk = self._stream.read(min(count, 64 * 1024))
            except Exception as exc:
                raise OSError("could not seek in compressed native conversation log") from exc
            if not chunk:
                raise OSError("seek beyond end of compressed native conversation log")
            self._position += len(chunk)
            count -= len(chunk)

    def close(self):
        if not self.closed:
            try:
                self._stream.close()
            finally:
                super().close()


@contextmanager
def open_log(path):
    """Open a native session log for read-only binary access."""
    path = Path(path)
    source = path.open("rb")
    reader = source
    try:
        if path.suffix.lower() == ".zst":
            try:
                import zstandard
                stream = zstandard.ZstdDecompressor().stream_reader(source)
                reader = io.BufferedReader(_ForwardDecompressor(stream))
            except Exception as exc:
                raise OSError("could not open compressed native conversation log") from exc
        yield reader
    finally:
        if reader is not source:
            try:
                reader.close()
            except Exception:
                pass
        if not source.closed:
            source.close()
