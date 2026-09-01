#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for WalrusCommand base and response dataclasses."""

import httpx
import pytest

from pytusk.commands.walrus_command import (
    BlobData,
    BlobReceipt,
    BlobSlice,
    QuiltPatch,
    QuiltReceipt,
    StorageNodeEnvelopeError,
    WalrusCommand,
    http_failure_message,
    unwrap_storage_node_envelope,
)


class TestWalrusCommandAbstract:
    def test_cannot_instantiate_directly(self) -> None:
        with pytest.raises(TypeError):
            WalrusCommand()  # type: ignore[abstract]

    def test_query_params_default(self) -> None:
        class _Cmd(WalrusCommand):
            def http_method(self) -> str:
                return "GET"

            def url_path(self, base_url: str) -> str:
                return base_url

            def parse_response(self, response):  # type: ignore[override]
                return None

        cmd = _Cmd()
        assert cmd.query_params() == {}

    def test_request_body_default(self) -> None:
        class _Cmd(WalrusCommand):
            def http_method(self) -> str:
                return "GET"

            def url_path(self, base_url: str) -> str:
                return base_url

            def parse_response(self, response):  # type: ignore[override]
                return None

        cmd = _Cmd()
        assert cmd.request_body() is None

    def test_form_files_default(self) -> None:
        class _Cmd(WalrusCommand):
            def http_method(self) -> str:
                return "GET"

            def url_path(self, base_url: str) -> str:
                return base_url

            def parse_response(self, response):  # type: ignore[override]
                return None

        cmd = _Cmd()
        assert cmd.form_files() is None


class TestBlobData:
    def test_default_content(self) -> None:
        bd = BlobData()
        assert bd.content == b""

    def test_with_content(self) -> None:
        bd = BlobData(content=b"hello world")
        assert bd.content == b"hello world"

    def test_equality(self) -> None:
        assert BlobData(content=b"x") == BlobData(content=b"x")
        assert BlobData(content=b"x") != BlobData(content=b"y")


class TestBlobSlice:
    def test_default_content(self) -> None:
        bs = BlobSlice()
        assert bs.content == b""

    def test_with_content(self) -> None:
        bs = BlobSlice(content=b"slice data")
        assert bs.content == b"slice data"

    def test_equality(self) -> None:
        assert BlobSlice(content=b"a") == BlobSlice(content=b"a")


class TestQuiltPatch:
    def test_default_content(self) -> None:
        qp = QuiltPatch()
        assert qp.content == b""

    def test_with_content(self) -> None:
        qp = QuiltPatch(content=b"patch bytes")
        assert qp.content == b"patch bytes"

    def test_equality(self) -> None:
        assert QuiltPatch(content=b"p") == QuiltPatch(content=b"p")


class TestBlobReceipt:
    def test_defaults(self) -> None:
        br = BlobReceipt()
        assert br.blob_id == ""
        assert br.cost == 0
        assert br.expiry_epoch == 0
        assert br.deletable is False

    def test_with_values(self) -> None:
        br = BlobReceipt(blob_id="abc123", cost=500, expiry_epoch=42, deletable=True)
        assert br.blob_id == "abc123"
        assert br.cost == 500
        assert br.expiry_epoch == 42
        assert br.deletable is True

    def test_equality(self) -> None:
        a = BlobReceipt(blob_id="x", cost=1, expiry_epoch=2, deletable=False)
        b = BlobReceipt(blob_id="x", cost=1, expiry_epoch=2, deletable=False)
        assert a == b


class TestQuiltReceipt:
    def test_defaults(self) -> None:
        qr = QuiltReceipt()
        assert qr.quilt_id == ""
        assert qr.patch_keys == []
        assert qr.cost == 0
        assert qr.expiry_epoch == 0

    def test_with_values(self) -> None:
        qr = QuiltReceipt(
            quilt_id="quilt-1",
            patch_keys=["file_a", "file_b"],
            cost=1000,
            expiry_epoch=99,
        )
        assert qr.quilt_id == "quilt-1"
        assert qr.patch_keys == ["file_a", "file_b"]
        assert qr.cost == 1000
        assert qr.expiry_epoch == 99

    def test_equality(self) -> None:
        a = QuiltReceipt(quilt_id="q", patch_keys=["k"], cost=10, expiry_epoch=5)
        b = QuiltReceipt(quilt_id="q", patch_keys=["k"], cost=10, expiry_epoch=5)
        assert a == b


class TestResponseBodyForLog:
    """A logged error body is written by an UNTRUSTED server.

    It reaches a terminal verbatim, so control characters must not survive:
    ANSI escape sequences would let a hostile server clear the screen and
    forge output around the diagnostic line.

    A bare ``httpx.Response`` is enough here -- ``http_failure_message``
    already tolerates an unset ``request`` and a body that is not JSON, so
    no response double is needed.
    """

    def test_ansi_escapes_are_neutralised(self) -> None:
        message = http_failure_message(
            response=httpx.Response(500, text="\x1b[2Jforged output"),
            context="blob_id=abc",
        )
        assert "\x1b" not in message
        assert "forged output" in message

    def test_newlines_and_tabs_are_kept(self) -> None:
        """Both carry real structure in a JSON or HTML error body."""
        message = http_failure_message(
            response=httpx.Response(500, text="line1\n\tline2"),
            context="blob_id=abc",
        )
        assert "line1\n\tline2" in message


# Captured verbatim from four testnet storage nodes on 2026-09-01, which all
# returned byte-identical bodies. Not an invented shape: the camelCase outer
# keys alongside the snake_case children of `deletableCounts` are real, and a
# hand-written fixture would very likely have normalised that away.
_LIVE_PERMANENT_STATUS_BODY = {
    "success": {
        "code": 200,
        "data": {
            "permanent": {
                "endEpoch": 508,
                "isCertified": True,
                "statusEvent": {
                    "txDigest": "DaRmjvMBZoU6cU1iZUM3qMdakFYa5jJcmWFyaxW2aReN",
                    "eventSeq": "0",
                },
                "deletableCounts": {
                    "count_deletable_total": 0,
                    "count_deletable_certified": 0,
                },
                "initialCertifiedEpoch": 507,
            }
        },
    }
}


class TestUnwrapStorageNodeEnvelope:
    """The shared storage-node success envelope.

    Storage nodes wrap every 2xx payload as
    ``{"success": {"code": ..., "data": ...}}``. This is deliberately NOT the
    publisher envelope (``newlyCreated``/``alreadyCertified``), which has no
    ``success`` wrapper and must never be passed to this helper.
    """

    def test_object_data_is_returned(self) -> None:
        """A real permanent blob-status body unwraps to its inner object."""
        data = unwrap_storage_node_envelope(
            response=httpx.Response(200, json=_LIVE_PERMANENT_STATUS_BODY),
            context="blob_id=abc",
        )
        assert isinstance(data, dict)
        assert "permanent" in data

    def test_bare_string_data_is_returned(self) -> None:
        """``data`` is a BARE STRING for an externally tagged unit variant.

        The blob-status ``nonexistent`` variant is the live example. A caller
        assuming an object here breaks on a perfectly valid response -- which
        is precisely why the return type is a union.
        """
        body = {"success": {"code": 200, "data": "nonexistent"}}
        data = unwrap_storage_node_envelope(
            response=httpx.Response(200, json=body), context="blob_id=abc"
        )
        assert data == "nonexistent"

    def test_missing_success_raises(self) -> None:
        body = {"code": 200, "data": {}}
        with pytest.raises(StorageNodeEnvelopeError):
            unwrap_storage_node_envelope(
                response=httpx.Response(200, json=body), context="blob_id=abc"
            )

    def test_missing_data_raises(self) -> None:
        body = {"success": {"code": 200}}
        with pytest.raises(StorageNodeEnvelopeError):
            unwrap_storage_node_envelope(
                response=httpx.Response(200, json=body), context="blob_id=abc"
            )

    def test_non_json_body_raises(self) -> None:
        with pytest.raises(StorageNodeEnvelopeError):
            unwrap_storage_node_envelope(
                response=httpx.Response(200, text="not json at all"),
                context="blob_id=abc",
            )

    def test_non_object_body_raises(self) -> None:
        with pytest.raises(StorageNodeEnvelopeError):
            unwrap_storage_node_envelope(
                response=httpx.Response(200, json=[1, 2, 3]),
                context="blob_id=abc",
            )

    def test_unusable_data_member_raises(self) -> None:
        """``data`` that is neither an object nor a string is not a variant."""
        body = {"success": {"code": 200, "data": 7}}
        with pytest.raises(StorageNodeEnvelopeError):
            unwrap_storage_node_envelope(
                response=httpx.Response(200, json=body), context="blob_id=abc"
            )

    def test_context_is_carried_into_the_message(self) -> None:
        """Diagnostics must name WHICH request failed, as elsewhere."""
        body = {"nope": True}
        with pytest.raises(StorageNodeEnvelopeError, match="blob_id=xyz"):
            unwrap_storage_node_envelope(
                response=httpx.Response(200, json=body), context="blob_id=xyz"
            )
