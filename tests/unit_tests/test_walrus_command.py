#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for WalrusCommand base and response dataclasses."""

import pytest

from pytusk.commands.walrus_command import (
    BlobData,
    BlobReceipt,
    BlobSlice,
    QuiltPatch,
    QuiltReceipt,
    WalrusCommand,
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
