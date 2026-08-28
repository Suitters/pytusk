#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for upload relay tip quoting and authentication packages."""

import base64
import hashlib

import pytest
from pysui import SuiRpcResult

from pytusk.core.encoding import encoded_blob_length
from pytusk.core.relay_upload.common import AuthPackage
from pytusk.core.relay_upload.tip import (
    build_auth_package,
    compute_tip,
    fetch_tip_config,
    quote_tip,
)
from pytusk.core.types import ConstTip, LinearTip, TipConfig, TipConfigError

_SHA256_EMPTY = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class TestAuthPackage:
    """BCS layout and nonce encoding."""

    def test_bcs_layout_is_72_bytes(self) -> None:
        pkg = AuthPackage(
            nonce=bytes(range(32)),
            blob_digest=bytes([0xAA]) * 32,
            nonce_digest=bytes([0xBB]) * 32,
            unencoded_length=1,
        )
        encoded = pkg.bcs
        assert len(encoded) == 72
        assert encoded[:32] == bytes([0xAA]) * 32
        assert encoded[32:64] == bytes([0xBB]) * 32
        assert encoded[64:] == bytes([1, 0, 0, 0, 0, 0, 0, 0])

    def test_bcs_length_is_little_endian(self) -> None:
        pkg = AuthPackage(
            nonce=bytes(32),
            blob_digest=bytes(32),
            nonce_digest=bytes(32),
            unencoded_length=258,
        )
        assert pkg.bcs[64:] == bytes([2, 1, 0, 0, 0, 0, 0, 0])

    def test_nonce_base64url_is_unpadded_and_url_safe(self) -> None:
        pkg = build_auth_package(data=b"payload")
        text = pkg.nonce_base64url
        assert "=" not in text
        assert "+" not in text
        assert "/" not in text

    def test_nonce_base64url_round_trips(self) -> None:
        pkg = build_auth_package(data=b"payload")
        text = pkg.nonce_base64url
        padded = text + "=" * (-len(text) % 4)
        assert base64.urlsafe_b64decode(padded) == pkg.nonce


class TestBuildAuthPackage:
    """Digest and nonce construction."""

    def test_empty_blob_golden_digest(self) -> None:
        pkg = build_auth_package(data=b"")
        assert pkg.blob_digest.hex() == _SHA256_EMPTY
        assert pkg.unencoded_length == 0
        assert len(pkg.bcs) == 72

    def test_nonce_digest_matches_nonce(self) -> None:
        pkg = build_auth_package(data=b"payload")
        assert len(pkg.nonce) == 32
        assert pkg.nonce_digest == hashlib.sha256(pkg.nonce).digest()

    def test_unencoded_length_tracks_data(self) -> None:
        pkg = build_auth_package(data=b"x" * 4096)
        assert pkg.unencoded_length == 4096

    def test_nonce_is_fresh_per_call(self) -> None:
        first = build_auth_package(data=b"payload")
        second = build_auth_package(data=b"payload")
        assert first.nonce != second.nonce
        assert first.blob_digest == second.blob_digest
        assert first.bcs != second.bcs


class TestComputeTip:
    """Const and Linear tip arithmetic."""

    def test_const_ignores_blob_size(self) -> None:
        kind = ConstTip(amount=31415)
        assert compute_tip(kind=kind, unencoded_length=0, n_shards=1000) == 31415
        assert compute_tip(kind=kind, unencoded_length=10**9, n_shards=1000) == 31415

    def test_linear_uses_encoded_length(self) -> None:
        kind = LinearTip(base=101, encoded_size_mul_per_kib=42)
        encoded = encoded_blob_length(unencoded_length=1_048_576, n_shards=1000)
        expected = 101 + ((encoded + 1023) // 1024) * 42
        assert (
            compute_tip(kind=kind, unencoded_length=1_048_576, n_shards=1000)
            == expected
        )

    def test_linear_is_not_computed_from_unencoded_length(self) -> None:
        kind = LinearTip(base=0, encoded_size_mul_per_kib=1)
        unencoded = 1_048_576
        encoded = encoded_blob_length(unencoded_length=unencoded, n_shards=1000)
        assert encoded != unencoded
        tip = compute_tip(kind=kind, unencoded_length=unencoded, n_shards=1000)
        assert tip == (encoded + 1023) // 1024
        assert tip != (unencoded + 1023) // 1024

    def test_linear_rounds_up_on_partial_kib(self) -> None:
        kind = LinearTip(base=0, encoded_size_mul_per_kib=1)
        length = next(
            n
            for n in range(1, 8192)
            if encoded_blob_length(unencoded_length=n, n_shards=1000) % 1024
        )
        encoded = encoded_blob_length(unencoded_length=length, n_shards=1000)
        tip = compute_tip(kind=kind, unencoded_length=length, n_shards=1000)
        assert tip == encoded // 1024 + 1

    def test_linear_exact_kib_multiple_does_not_round_up(self) -> None:
        """At an exact 1024 multiple, div_ceil must not add a spurious KiB.

        Both vectors are real encoded lengths, located by scanning
        ``encoded_blob_length`` and pinned here so the boundary is always
        exercised rather than skipped when a scan finds no exact multiple.
        """
        kind = LinearTip(base=0, encoded_size_mul_per_kib=1)
        for n_shards, length, encoded in ((7, 991, 7168), (10, 841, 10240)):
            assert (
                encoded_blob_length(unencoded_length=length, n_shards=n_shards)
                == encoded
            )
            assert encoded % 1024 == 0
            assert (
                compute_tip(kind=kind, unencoded_length=length, n_shards=n_shards)
                == encoded // 1024
            )

    def test_linear_base_added_once(self) -> None:
        kind = LinearTip(base=500, encoded_size_mul_per_kib=0)
        assert compute_tip(kind=kind, unencoded_length=1_048_576, n_shards=1000) == 500

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(TypeError, match="Unsupported tip kind"):
            compute_tip(kind="nope", unencoded_length=1, n_shards=1000)  # type: ignore[arg-type]


class TestTipConfig:
    """requires_payment reflects the no_tip case."""

    def test_no_tip_requires_no_payment(self) -> None:
        assert TipConfig(address=None, kind=None).requires_payment is False

    def test_send_tip_requires_payment(self) -> None:
        config = TipConfig(address="0xrelay", kind=ConstTip(amount=1))
        assert config.requires_payment is True


class _FakeTipClient:
    """Fake ``WalrusClient``-shaped object for tip-config lookups.

    Implements only ``execute(command=..., base_url=...)``, recording each
    dispatch so the resolved relay URL can be asserted.
    """

    def __init__(self, *, response: SuiRpcResult) -> None:
        self.response = response
        self.calls: list[tuple[object, str | None]] = []

    async def execute(
        self, *, command: object, base_url: str | None = None, **kwargs: object
    ) -> SuiRpcResult:
        """Record the dispatch and return the canned response."""
        self.calls.append((command, base_url))
        return self.response


class TestFetchTipConfig:
    """The GET, and its pre-spend failure contract."""

    async def test_returns_parsed_config(self) -> None:
        client = _FakeTipClient(
            response=SuiRpcResult(
                True, "", TipConfig(address="0xr", kind=ConstTip(amount=7))
            )
        )
        config = await fetch_tip_config(client=client, relay_url="https://r")
        assert config.address == "0xr"
        assert config.requires_payment is True

    async def test_dispatches_to_the_named_relay(self) -> None:
        client = _FakeTipClient(
            response=SuiRpcResult(True, "", TipConfig(address=None, kind=None))
        )
        await fetch_tip_config(client=client, relay_url="https://r")
        assert client.calls[0][1] == "https://r"

    async def test_failure_raises_rather_than_returning(self) -> None:
        client = _FakeTipClient(response=SuiRpcResult(False, "down", None))
        with pytest.raises(TipConfigError, match="Cannot fetch tip config"):
            await fetch_tip_config(client=client, relay_url="https://r")


class TestQuoteTip:
    """Resolving a relay's policy against one specific blob."""

    async def test_no_tip_relay_requires_no_payment(self) -> None:
        client = _FakeTipClient(
            response=SuiRpcResult(True, "", TipConfig(address=None, kind=None))
        )
        quote = await quote_tip(
            client=client, relay_url="https://r", unencoded_length=1024, n_shards=1000
        )
        assert quote.requires_payment is False
        assert quote.amount is None
        assert quote.address is None

    async def test_const_quote(self) -> None:
        client = _FakeTipClient(
            response=SuiRpcResult(
                True, "", TipConfig(address="0xr", kind=ConstTip(amount=31415))
            )
        )
        quote = await quote_tip(
            client=client, relay_url="https://r", unencoded_length=1024, n_shards=1000
        )
        assert quote.address == "0xr"
        assert quote.amount == 31415
        assert quote.requires_payment is True

    async def test_linear_quote_matches_compute_tip(self) -> None:
        kind = LinearTip(base=101, encoded_size_mul_per_kib=42)
        client = _FakeTipClient(
            response=SuiRpcResult(True, "", TipConfig(address="0xr", kind=kind))
        )
        quote = await quote_tip(
            client=client,
            relay_url="https://r",
            unencoded_length=1_048_576,
            n_shards=1000,
        )
        assert quote.amount == compute_tip(
            kind=kind, unencoded_length=1_048_576, n_shards=1000
        )
        assert quote.kind == kind
