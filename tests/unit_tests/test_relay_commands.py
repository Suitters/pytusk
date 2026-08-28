#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for Walrus upload relay HTTP commands."""

import tempfile
from typing import Any

import httpx
import pytest

from pytusk.commands.relay_commands import (
    GetTipConfig,
    RelayUploadAck,
    UploadRelayBlob,
)
from pytusk.core.types import (
    ConstTip,
    LinearTip,
    RelayUploadOutcome,
)


class TestGetTipConfig:
    """Tip config request shape and payload parsing."""

    def test_request_shape(self) -> None:
        cmd = GetTipConfig()
        assert cmd.endpoint_role == "relay"
        assert cmd.http_method() == "GET"
        assert cmd.url_path("https://r.example.com") == (
            "https://r.example.com/v1/tip-config"
        )

    def test_parses_no_tip_bare_string(self) -> None:
        response = httpx.Response(200, json="no_tip")
        result = GetTipConfig().parse_response(response)
        assert result.is_ok()
        assert result.result_data.address is None
        assert result.result_data.kind is None
        assert result.result_data.requires_payment is False

    def test_parses_const_tip(self) -> None:
        response = httpx.Response(
            200, json={"send_tip": {"address": "0xr", "kind": {"const": 31415}}}
        )
        result = GetTipConfig().parse_response(response)
        assert result.is_ok()
        assert result.result_data.address == "0xr"
        assert result.result_data.kind == ConstTip(amount=31415)
        assert result.result_data.requires_payment is True

    def test_parses_linear_tip(self) -> None:
        response = httpx.Response(
            200,
            json={
                "send_tip": {
                    "address": "0xr",
                    "kind": {"linear": {"base": 101, "encoded_size_mul_per_kib": 42}},
                }
            },
        )
        result = GetTipConfig().parse_response(response)
        assert result.is_ok()
        assert result.result_data.kind == LinearTip(
            base=101, encoded_size_mul_per_kib=42
        )

    def test_unknown_kind_is_error(self) -> None:
        response = httpx.Response(
            200, json={"send_tip": {"address": "0xr", "kind": {"quadratic": 3}}}
        )
        result = GetTipConfig().parse_response(response)
        assert result.is_err()
        assert "Unknown tip kind" in result.result_string

    def test_missing_linear_field_is_error(self) -> None:
        response = httpx.Response(
            200, json={"send_tip": {"address": "0xr", "kind": {"linear": {"base": 1}}}}
        )
        result = GetTipConfig().parse_response(response)
        assert result.is_err()
        assert "missing field" in result.result_string

    def test_unrecognised_payload_is_error(self) -> None:
        response = httpx.Response(200, json={"nope": {}})
        result = GetTipConfig().parse_response(response)
        assert result.is_err()
        assert "Unrecognised tip config payload" in result.result_string

    def test_http_error_is_error(self) -> None:
        response = httpx.Response(503, text="down")
        result = GetTipConfig().parse_response(response)
        assert result.is_err()
        assert "503" in result.result_string


class TestUploadRelayBlob:
    """Upload request shape and response parsing."""

    def test_request_shape(self) -> None:
        cmd = UploadRelayBlob(blob_id="bid", data=b"payload")
        assert cmd.endpoint_role == "relay"
        assert cmd.http_method() == "POST"
        assert cmd.url_path("https://r.example.com") == (
            "https://r.example.com/v1/blob-upload-relay"
        )
        assert cmd.request_body() == b"payload"

    def test_untipped_sends_only_blob_id(self) -> None:
        cmd = UploadRelayBlob(blob_id="bid", data=b"x")
        assert cmd.query_params() == {"blob_id": "bid"}

    def test_tipped_sends_tokens(self) -> None:
        cmd = UploadRelayBlob(
            blob_id="bid", data=b"x", register_tip_tx_digest="0xd", nonce="nn"
        )
        assert cmd.query_params() == {
            "blob_id": "bid",
            "tx_id": "0xd",
            "nonce": "nn",
        }

    def test_deletable_included_only_when_set(self) -> None:
        permanent = UploadRelayBlob(blob_id="bid", data=b"x")
        assert "deletable_blob_object" not in permanent.query_params()
        deletable = UploadRelayBlob(
            blob_id="bid", data=b"x", deletable_blob_object="0xobj"
        )
        assert deletable.query_params()["deletable_blob_object"] == "0xobj"

    def test_encoding_type_included_only_when_set(self) -> None:
        default = UploadRelayBlob(blob_id="bid", data=b"x")
        assert "encoding_type" not in default.query_params()
        explicit = UploadRelayBlob(blob_id="bid", data=b"x", encoding_type=1)
        assert explicit.query_params()["encoding_type"] == 1

    def test_success_carries_uploaded_ack(self) -> None:
        response = httpx.Response(
            200,
            json={"blob_id": "bid", "confirmation_certificate": {"signers": [1, 2]}},
        )
        result = UploadRelayBlob(blob_id="bid", data=b"x").parse_response(response)
        assert result.is_ok()
        ack = result.result_data
        assert ack.outcome is RelayUploadOutcome.UPLOADED
        assert ack.confirmation_certificate == {"signers": [1, 2]}
        assert ack.relay_status == 200

    def test_refusal_carries_status_and_body(self) -> None:
        response = httpx.Response(402, text="tip too small")
        result = UploadRelayBlob(blob_id="bid", data=b"x").parse_response(response)
        assert result.is_err()
        ack = result.result_data
        assert isinstance(ack, RelayUploadAck)
        assert ack.outcome is RelayUploadOutcome.REFUSED
        assert ack.relay_status == 402
        assert ack.relay_message == "tip too small"

    def test_malformed_success_body_is_refused_not_retryable(self) -> None:
        response = httpx.Response(200, json={"blob_id": "bid"})
        result = UploadRelayBlob(blob_id="bid", data=b"x").parse_response(response)
        assert result.is_err()
        ack = result.result_data
        assert ack.outcome is RelayUploadOutcome.REFUSED
        assert ack.relay_status == 200

    def test_result_data_type_is_stable_across_outcomes(self) -> None:
        ok = UploadRelayBlob(blob_id="bid", data=b"x").parse_response(
            httpx.Response(
                200, json={"blob_id": "bid", "confirmation_certificate": {}}
            )
        )
        bad = UploadRelayBlob(blob_id="bid", data=b"x").parse_response(
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
                GetTipConfig(), timeout=None, headers=None, base_url=None
            )

    async def test_upload_relay_role_requires_explicit_base_url(
        self, client: Any
    ) -> None:
        with pytest.raises(ValueError, match="requires an explicit base_url"):
            await client._dispatch_walrus(
                UploadRelayBlob(blob_id="bid", data=b"x"),
                timeout=None,
                headers=None,
                base_url=None,
            )
