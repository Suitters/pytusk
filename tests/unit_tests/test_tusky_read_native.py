#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for the tusky ``read_blob_native`` command handler.

Handler orchestration only: where content is written, what reaches stdout
versus a file, how the blob id is converted at the CLI/library boundary, and
how library exceptions become stderr messages and a non-zero exit. Argument
parsing is covered in ``test_tusky_args.py``.
"""

import argparse
import base64

import pytest

from pytusk import BlobDecodeError, NativeReadResult, SliverFetchError
from pytusk.tusky import tusky_cmds_read

_BLOB_ID_BYTES = b"\x01" * 32
_BLOB_ID = base64.urlsafe_b64encode(_BLOB_ID_BYTES).decode().rstrip("=")


class _FakeClient:
    """Stands in for WalrusClient's async context manager."""

    async def __aenter__(self) -> "_FakeClient":
        """Enter the context."""
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        """Leave the context without suppressing anything."""
        return False


def _result(content: bytes = b"reconstructed-blob") -> NativeReadResult:
    """Build a representative successful read result."""
    return NativeReadResult(
        content=content,
        blob_id=_BLOB_ID_BYTES,
        epoch=7,
        axis="primary",
        slivers_used=4,
    )


def _args(**overrides: object) -> argparse.Namespace:
    """Build the namespace the handler expects, with overrides."""
    values: dict[str, object] = {
        "blob_id": _BLOB_ID,
        "file": None,
        "verify": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _install(monkeypatch: pytest.MonkeyPatch, pipeline: object) -> None:
    """Stub out config, client construction and the library pipeline."""
    monkeypatch.setattr(
        tusky_cmds_read, "config_from_args", lambda args: object()
    )
    monkeypatch.setattr(
        tusky_cmds_read, "WalrusClient", lambda **kwargs: _FakeClient()
    )
    monkeypatch.setattr(
        tusky_cmds_read, "_read_blob_native_pipeline", pipeline
    )


class TestReadBlobNativeHandler:
    """The read_blob_native CLI handler."""

    async def test_writes_content_to_stdout_when_no_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        """Without --file the raw bytes go to stdout."""

        async def _pipeline(**kwargs: object) -> NativeReadResult:
            return _result()

        _install(monkeypatch, _pipeline)

        await tusky_cmds_read.read_blob_native(_args())

        assert capsysbinary.readouterr().out == b"reconstructed-blob"

    async def test_writes_content_to_the_file_when_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """With --file the bytes land in the file, byte for byte."""

        async def _pipeline(**kwargs: object) -> NativeReadResult:
            return _result()

        _install(monkeypatch, _pipeline)
        target = tmp_path / "out.bin"

        await tusky_cmds_read.read_blob_native(_args(file=target))

        assert target.read_bytes() == b"reconstructed-blob"

    async def test_no_summary_on_stdout_when_content_goes_to_stdout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        """REGRESSION: stdout carries the blob and nothing else.

        A summary line printed alongside the content would corrupt it for
        any caller piping the output into a file or another process -- the
        blob would silently gain a human-readable trailer.
        """

        async def _pipeline(**kwargs: object) -> NativeReadResult:
            return _result()

        _install(monkeypatch, _pipeline)

        await tusky_cmds_read.read_blob_native(_args())

        assert capsysbinary.readouterr().out == b"reconstructed-blob"

    async def test_summary_is_printed_when_writing_to_a_file(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path,
    ) -> None:
        """Writing to a file reports what was written, since stdout is free."""

        async def _pipeline(**kwargs: object) -> NativeReadResult:
            return _result()

        _install(monkeypatch, _pipeline)
        target = tmp_path / "out.bin"

        await tusky_cmds_read.read_blob_native(_args(file=target))

        out = capsys.readouterr().out
        assert str(target) in out
        assert "18 bytes" in out

    async def test_blob_id_reaches_the_pipeline_as_raw_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION: the CLI holds base64, the library wants 32 bytes.

        ``args.blob_id`` is a URL-safe base64 string. Handing it straight to
        the pipeline would fail its length check, and the conversion has to
        happen exactly once -- converting twice is not idempotent.
        """
        captured: dict[str, object] = {}

        async def _pipeline(
            *, client: object, blob_id: bytes, verify: bool
        ) -> NativeReadResult:
            captured["blob_id"] = blob_id
            captured["verify"] = verify
            return _result()

        _install(monkeypatch, _pipeline)

        await tusky_cmds_read.read_blob_native(_args(file=None))

        assert captured["blob_id"] == _BLOB_ID_BYTES
        assert isinstance(captured["blob_id"], bytes)

    async def test_no_verify_is_forwarded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """verify=False reaches the library rather than being dropped."""
        captured: dict[str, object] = {}

        async def _pipeline(
            *, client: object, blob_id: bytes, verify: bool
        ) -> NativeReadResult:
            captured["verify"] = verify
            return _result()

        _install(monkeypatch, _pipeline)

        await tusky_cmds_read.read_blob_native(_args(verify=False))

        assert captured["verify"] is False

    async def test_read_error_exits_nonzero_and_names_the_stage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A read failure reports its stage on stderr and exits 1."""

        async def _pipeline(**kwargs: object) -> NativeReadResult:
            raise SliverFetchError(
                message="only 2 of 4 slivers", stage="fetch_slivers"
            )

        _install(monkeypatch, _pipeline)

        with pytest.raises(SystemExit) as caught:
            await tusky_cmds_read.read_blob_native(_args())

        assert caught.value.code == 1
        assert "fetch_slivers" in capsys.readouterr().err

    async def test_decode_error_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """REGRESSION: BlobDecodeError has no `stage` attribute.

        It subclasses ValueError, not NativeReadError, so a handler that
        reached for `exc.stage` on every failure would raise AttributeError
        while reporting an error.
        """

        async def _pipeline(**kwargs: object) -> NativeReadResult:
            raise BlobDecodeError("decoding_unsuccessful")

        _install(monkeypatch, _pipeline)

        with pytest.raises(SystemExit) as caught:
            await tusky_cmds_read.read_blob_native(_args())

        assert caught.value.code == 1
        assert "decoding_unsuccessful" in capsys.readouterr().err
