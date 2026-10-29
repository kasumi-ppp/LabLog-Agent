"""Synthetic coverage for read-only native log streams."""

import io
import sys

import pytest
import zstandard

from lablog.native_log import open_log


def test_plain_log_is_opened_as_read_only_binary_file(tmp_path):
    path = tmp_path / "session.jsonl"
    content = "第一行\n第二行\n".encode("utf-8")
    path.write_bytes(content)

    with open_log(path) as stream:
        assert stream.mode == "rb"
        assert stream.readline() == "第一行\n".encode("utf-8")
        assert stream.read() == "第二行\n".encode("utf-8")


def test_zstandard_stream_reads_lines_and_seeks_by_logical_utf8_bytes(tmp_path):
    path = tmp_path / "archived.jsonl.zst"
    lines = ["第一条：中文\n".encode(), "第二条：内容\n".encode()]
    long_line = ("甲" * 10000 + "\n").encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(b"".join(lines) + long_line))

    with open_log(path) as stream:
        assert isinstance(stream, io.BufferedReader)
        assert stream.readline() == lines[0]
        assert stream.tell() == len(lines[0])
        assert stream.seek(len(lines[0]), io.SEEK_SET) == len(lines[0])
        assert stream.readline() == lines[1]
        # This offset is beyond BufferedReader's 8192-byte cache. Offsets count
        # decompressed UTF-8 bytes, so stay on a three-byte character boundary.
        target = len(lines[0]) + len(lines[1]) + 9000
        stream.seek(target)
        assert stream.read(len("甲".encode("utf-8"))) == "甲".encode("utf-8")


def test_corrupt_zstandard_source_raises_oserror_when_read(tmp_path):
    path = tmp_path / "corrupt.jsonl.zst"
    path.write_bytes(b"not a zstandard frame")

    with open_log(path) as stream:
        with pytest.raises(OSError):
            stream.read()


def test_missing_zstandard_dependency_is_wrapped_as_oserror(tmp_path, monkeypatch):
    path = tmp_path / "compressed.jsonl.zst"
    path.write_bytes(b"synthetic")
    monkeypatch.setitem(sys.modules, "zstandard", None)

    with pytest.raises(OSError, match="compressed"):
        with open_log(path):
            pass
