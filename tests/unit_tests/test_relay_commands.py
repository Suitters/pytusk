#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for Walrus upload relay HTTP commands."""

import tempfile
from typing import Any

import httpx
import pytest

from pytusk.commands.relay_commands import (
    ReadTipConfig,
    RelayUploadAck,
    WriteRelayBlob,
    _parse_tip_kind,
    _tip_address,
)
from pytusk.core.types import (
    ConstTip,
    LinearTip,
    RelayUploadOutcome,
)

_RELAY_ADDRESS = "0x" + "ab" * 32
"""Tip destination used by the fixtures.

A real Sui address, not a placeholder: ``_tip_address`` rejects malformed
input, so a stand-in like ``"0xr"`` fails address validation before a test
can reach the tip-kind assertion it was written for.
"""


class TestReadTipConfig:
    """Tip config request shape and payload parsing."""

    def test_request_shape(self) -> None:
        cmd = ReadTipConfig()
        assert cmd.endpoint_role == "relay"
        assert cmd.http_method() == "GET"
        assert cmd.url_path("https://r.example.com") == (
            "https://r.example.com/v1/tip-config"
        )

    def test_parses_no_tip_bare_string(self) -> None:
        response = httpx.Response(200, json="no_tip")
        result = ReadTipConfig().parse_response(response)
        assert result.is_ok()
        assert result.result_data.address is None
        assert result.result_data.kind is None
        assert result.result_data.requires_payment is False

    def test_parses_const_tip(self) -> None:
        response = httpx.Response(
            200, json={"send_tip": {"address": _RELAY_ADDRESS, "kind": {"const": 31415}}}
        )
        result = ReadTipConfig().parse_response(response)
        assert result.is_ok()
        assert result.result_data.address == _RELAY_ADDRESS
        assert result.result_data.kind == ConstTip(amount=31415)
        assert result.result_data.requires_payment is True

    def test_parses_linear_tip(self) -> None:
        response = httpx.Response(
            200,
            json={
                "send_tip": {
                    "address": _RELAY_ADDRESS,
                    "kind": {"linear": {"base": 101, "encoded_size_mul_per_kib": 42}},
                }
            },
        )
        result = ReadTipConfig().parse_response(response)
        assert result.is_ok()
        assert result.result_data.kind == LinearTip(
            base=101, encoded_size_mul_per_kib=42
        )

    def test_unknown_kind_is_error(self) -> None:
        response = httpx.Response(
            200, json={"send_tip": {"address": _RELAY_ADDRESS, "kind": {"quadratic": 3}}}
        )
        result = ReadTipConfig().parse_response(response)
        assert result.is_err()
        assert "Unknown tip kind" in result.result_string

    def test_missing_linear_field_is_error(self) -> None:
        response = httpx.Response(
            200, json={"send_tip": {"address": _RELAY_ADDRESS, "kind": {"linear": {"base": 1}}}}
        )
        result = ReadTipConfig().parse_response(response)
        assert result.is_err()
        assert "missing field" in result.result_string

    def test_unrecognised_payload_is_error(self) -> None:
        response = httpx.Response(200, json={"nope": {}})
        result = ReadTipConfig().parse_response(response)
        assert result.is_err()
        assert "Unrecognised tip config payload" in result.result_string

    def test_http_error_is_error(self) -> None:
        response = httpx.Response(503, text="down")
        result = ReadTipConfig().parse_response(response)
        assert result.is_err()
        assert "503" in result.result_string


class TestWriteRelayBlob:
    """Upload request shape and response parsing."""

    def test_request_shape(self) -> None:
        cmd = WriteRelayBlob(blob_id="bid", data=b"payload")
        assert cmd.endpoint_role == "relay"
        assert cmd.http_method() == "POST"
        assert cmd.url_path("https://r.example.com") == (
            "https://r.example.com/v1/blob-upload-relay"
        )
        assert cmd.request_body() == b"payload"

    def test_untipped_sends_only_blob_id(self) -> None:
        cmd = WriteRelayBlob(blob_id="bid", data=b"x")
        assert cmd.query_params() == {"blob_id": "bid"}

    def test_tipped_sends_tokens(self) -> None:
        cmd = WriteRelayBlob(
            blob_id="bid", data=b"x", register_tip_tx_digest="0xd", nonce="nn"
        )
        assert cmd.query_params() == {
            "blob_id": "bid",
            "tx_id": "0xd",
            "nonce": "nn",
        }

    def test_deletable_included_only_when_set(self) -> None:
        permanent = WriteRelayBlob(blob_id="bid", data=b"x")
        assert "deletable_blob_object" not in permanent.query_params()
        deletable = WriteRelayBlob(
            blob_id="bid", data=b"x", deletable_blob_object="0xobj"
        )
        assert deletable.query_params()["deletable_blob_object"] == "0xobj"

    def test_encoding_type_included_only_when_set(self) -> None:
        default = WriteRelayBlob(blob_id="bid", data=b"x")
        assert "encoding_type" not in default.query_params()
        explicit = WriteRelayBlob(blob_id="bid", data=b"x", encoding_type=1)
        assert explicit.query_params()["encoding_type"] == 1

    def test_success_carries_uploaded_ack(self) -> None:
        response = httpx.Response(
            200,
            json={"blob_id": "bid", "confirmation_certificate": {"signers": [1, 2]}},
        )
        result = WriteRelayBlob(blob_id="bid", data=b"x").parse_response(response)
        assert result.is_ok()
        ack = result.result_data
        assert ack.outcome is RelayUploadOutcome.UPLOADED
        assert ack.confirmation_certificate == {"signers": [1, 2]}
        assert ack.relay_status == 200

    def test_refusal_carries_status_and_body(self) -> None:
        response = httpx.Response(402, text="tip too small")
        result = WriteRelayBlob(blob_id="bid", data=b"x").parse_response(response)
        assert result.is_err()
        ack = result.result_data
        assert isinstance(ack, RelayUploadAck)
        assert ack.outcome is RelayUploadOutcome.REFUSED
        assert ack.relay_status == 402
        assert ack.relay_message == "tip too small"

    def test_malformed_success_body_is_refused_not_retryable(self) -> None:
        response = httpx.Response(200, json={"blob_id": "bid"})
        result = WriteRelayBlob(blob_id="bid", data=b"x").parse_response(response)
        assert result.is_err()
        ack = result.result_data
        assert ack.outcome is RelayUploadOutcome.REFUSED
        assert ack.relay_status == 200

    def test_result_data_type_is_stable_across_outcomes(self) -> None:
        ok = WriteRelayBlob(blob_id="bid", data=b"x").parse_response(
            httpx.Response(
                200, json={"blob_id": "bid", "confirmation_certificate": {}}
            )
        )
        bad = WriteRelayBlob(blob_id="bid", data=b"x").parse_response(
            httpx.Response(400, text="nope")
        )
        assert isinstance(ok.result_data, RelayUploadAck)
        assert isinstance(bad.result_data, RelayUploadAck)


class TestRelayRoleDispatch:
    """A relay command must never silently fall through to the aggregator."""

    @pytest.fixture
    def client(self) -> Any:
        # Imported lazily so a missing pysui/pytusk config dependency
        # surfaces as a clear test failure at fixture time, not collection.
        from pytusk.client.walrus_client import WalrusClient
        from pytusk.config.tusk_config import PytuskConfiguration

        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = PytuskConfiguration(from_cfg_path=tmp_dir)
            yield WalrusClient(pytusk_config=cfg)

    async def test_relay_role_requires_explicit_base_url(self, client: Any) -> None:
        with pytest.raises(ValueError, match="requires an explicit base_url"):
            await client._dispatch_walrus(
                ReadTipConfig(), timeout=None, headers=None, base_url=None
            )

    async def test_upload_relay_role_requires_explicit_base_url(
        self, client: Any
    ) -> None:
        with pytest.raises(ValueError, match="requires an explicit base_url"):
            await client._dispatch_walrus(
                WriteRelayBlob(blob_id="bid", data=b"x"),
                timeout=None,
                headers=None,
                base_url=None,
            )


class TestWriteRelayBlobNonJsonBody:
    """A 2xx body that is not JSON at all is refused, never raised."""

    def test_non_json_body_is_refused_not_raised(self) -> None:
        """This branch runs POST-SPEND, so it must not raise.

        By the time the relay answers, the tip is paid and the blob is
        registered. An HTML error page from a proxy or CDN carrying a 2xx
        must come back as a REFUSED ack; raising here would destroy the
        nonce the caller needs in order to resume.
        """
        body = "<html>bad gateway</html>"
        response = httpx.Response(200, text=body)
        result = WriteRelayBlob(blob_id="bid", data=b"x").parse_response(response)
        assert not result.is_ok()
        ack = result.result_data
        assert isinstance(ack, RelayUploadAck)
        assert ack.outcome is RelayUploadOutcome.REFUSED
        assert ack.relay_status == 200
        assert ack.relay_message == body
        assert ack.confirmation_certificate is None


class TestTipAmountValidation:
    """A tip amount is a wire ``u64``; bool and negative are not tips.

    Neither tip dataclass validates its own fields, so a bad amount that
    gets past the parser is a bad amount the caller is asked to pay.
    """

    def test_bool_const_amount_rejected(self) -> None:
        """``bool`` subclasses ``int``, so JSON ``true`` would tip 1 MIST."""
        with pytest.raises(TypeError, match="const tip amount must be an integer"):
            _parse_tip_kind(payload={"const": True})

    def test_negative_const_amount_rejected(self) -> None:
        with pytest.raises(
            ValueError, match="const tip amount must not be negative"
        ):
            _parse_tip_kind(payload={"const": -1})

    def test_zero_const_amount_is_valid(self) -> None:
        assert _parse_tip_kind(payload={"const": 0}) == ConstTip(amount=0)

    def test_bool_linear_base_rejected(self) -> None:
        with pytest.raises(TypeError, match="linear tip base must be an integer"):
            _parse_tip_kind(
                payload={"linear": {"base": False, "encoded_size_mul_per_kib": 1}}
            )

    def test_non_integer_linear_base_rejected(self) -> None:
        with pytest.raises(TypeError, match="linear tip base must be an integer"):
            _parse_tip_kind(
                payload={"linear": {"base": "10", "encoded_size_mul_per_kib": 1}}
            )

    def test_negative_linear_multiplier_rejected(self) -> None:
        with pytest.raises(
            ValueError,
            match="linear tip encoded_size_mul_per_kib must not be negative",
        ):
            _parse_tip_kind(
                payload={"linear": {"base": 1, "encoded_size_mul_per_kib": -5}}
            )


class TestTipAddressValidation:
    """The tip destination is validated the way the amount is.

    A malformed address costs nothing directly -- the relay names its own
    payee -- but it must fail as a bad tip config, pre-spend, rather than
    as an opaque error from inside PTB composition.
    """

    def test_non_string_rejected(self) -> None:
        with pytest.raises(TypeError, match="must be a string"):
            _tip_address(value=12345)

    def test_missing_prefix_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be 0x-prefixed"):
            _tip_address(value="ab" * 32)

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValueError, match="is not a Sui address"):
            _tip_address(value="0x")

    def test_over_long_rejected(self) -> None:
        with pytest.raises(ValueError, match="is not a Sui address"):
            _tip_address(value="0x" + "ab" * 33)

    def test_non_hex_rejected(self) -> None:
        with pytest.raises(ValueError, match="is not valid hex"):
            _tip_address(value="0x" + "zz" * 32)

    def test_canonical_address_accepted(self) -> None:
        address = "0x" + "ab" * 32
        assert _tip_address(value=address) == address

    def test_shortened_address_accepted(self) -> None:
        """Leading zeroes may be omitted, so short forms are still valid."""
        assert _tip_address(value="0x2") == "0x2"
