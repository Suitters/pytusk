#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for storage-node commands and their base-URL dispatch."""

import base64
import json
import tempfile
from typing import Any

import httpx
import pytest

from pytusk.commands.node_commands import (
    GetMetadata,
    GetSliver,
    GetStorageConfirmation,
    MetadataAck,
    MetadataData,
    PutMetadata,
    PutSliver,
    SignedConfirmation,
    SliverAck,
    SliverData,
)
from pytusk.commands.walrus_command import error_reason
from pytusk.core.encoding import blob_id_to_url_base64

BASE_URL = "https://node-1.example.com:9185"


def _error_response(*, reason: str) -> httpx.Response:
    """Build an httpx.Response with an AIP-193 error envelope."""
    body = {
        "error": {
            "code": 400,
            "status": "FAILED_PRECONDITION",
            "details": [
                {
                    "@type": "ErrorInfo",
                    "reason": reason,
                    "domain": "walrus.mystenlabs.com",
                }
            ],
        }
    }
    return httpx.Response(
        status_code=400,
        content=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


class TestErrorReason:
    def test_extracts_not_registered(self) -> None:
        response = _error_response(reason="NOT_REGISTERED")
        assert error_reason(response=response) == "NOT_REGISTERED"

    def test_extracts_missing_slivers(self) -> None:
        response = _error_response(reason="MISSING_SLIVERS")
        assert error_reason(response=response) == "MISSING_SLIVERS"

    def test_returns_none_for_no_details(self) -> None:
        body = {"error": {"code": 400, "status": "FAILED_PRECONDITION"}}
        response = httpx.Response(
            status_code=400,
            content=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        assert error_reason(response=response) is None

    def test_returns_none_for_non_json_body(self) -> None:
        response = httpx.Response(status_code=400, content=b"not json at all")
        assert error_reason(response=response) is None

    def test_returns_none_for_empty_body(self) -> None:
        response = httpx.Response(status_code=400, content=b"")
        assert error_reason(response=response) is None


class TestPutSliver:
    def test_endpoint_role_is_storage_node(self) -> None:
        assert PutSliver.endpoint_role == "storage_node"

    def test_url_path_exact_string(self) -> None:
        blob_id = bytes(range(32))
        cmd = PutSliver(
            blob_id=blob_id, sliver_pair_index=7, sliver_type="primary", data=b"x"
        )
        expected_b64 = blob_id_to_url_base64(blob_id=blob_id)
        assert (
            cmd.url_path(BASE_URL)
            == f"{BASE_URL}/v1/blobs/{expected_b64}/slivers/7/primary"
        )

    def test_url_path_blob_id_segment_is_url_safe_unpadded(self) -> None:
        # A blob_id chosen so the standard alphabet would emit '+', '/', '='.
        blob_id = bytes([0xFF, 0xFE, 0xFD]) + bytes(29)
        cmd = PutSliver(
            blob_id=blob_id, sliver_pair_index=0, sliver_type="secondary", data=b"x"
        )
        url = cmd.url_path(BASE_URL)
        segment = url.split("/v1/blobs/", 1)[1].split("/slivers/", 1)[0]
        assert "=" not in segment
        assert "+" not in segment
        assert "/" not in segment

    @pytest.mark.parametrize("sliver_type", ["primary", "secondary"])
    def test_accepts_valid_sliver_type(self, sliver_type: str) -> None:
        cmd = PutSliver(
            blob_id=b"x" * 32, sliver_pair_index=0, sliver_type=sliver_type, data=b"x"
        )
        assert cmd.sliver_type == sliver_type

    @pytest.mark.parametrize(
        "sliver_type", ["Primary", "PRIMARY", "other", "", "SECONDARY"]
    )
    def test_rejects_invalid_sliver_type(self, sliver_type: str) -> None:
        with pytest.raises(ValueError):
            PutSliver(
                blob_id=b"x" * 32,
                sliver_pair_index=0,
                sliver_type=sliver_type,
                data=b"x",
            )

    def test_http_method(self) -> None:
        cmd = PutSliver(
            blob_id=b"x" * 32, sliver_pair_index=0, sliver_type="primary", data=b"x"
        )
        assert cmd.http_method() == "PUT"

    def test_request_body_returns_exact_bytes(self) -> None:
        payload = b"\x00\x01\xffraw-bcs-bytes"
        cmd = PutSliver(
            blob_id=b"x" * 32, sliver_pair_index=0, sliver_type="primary", data=payload
        )
        assert cmd.request_body() == payload

    def test_parse_response_success(self) -> None:
        blob_id = b"x" * 32
        cmd = PutSliver(
            blob_id=blob_id, sliver_pair_index=3, sliver_type="secondary", data=b"x"
        )
        response = httpx.Response(
            status_code=201,
            content=json.dumps({"success": {"code": 201, "data": "stored"}}).encode(),
            headers={"content-type": "application/json"},
        )
        result = cmd.parse_response(response)
        assert result.is_ok()
        assert isinstance(result.result_data, SliverAck)
        ack: SliverAck = result.result_data
        assert ack.blob_id == blob_id_to_url_base64(blob_id=blob_id)
        assert ack.sliver_pair_index == 3
        assert ack.sliver_type == "secondary"

    def test_parse_response_error_not_registered(self) -> None:
        cmd = PutSliver(
            blob_id=b"x" * 32, sliver_pair_index=0, sliver_type="primary", data=b"x"
        )
        result = cmd.parse_response(_error_response(reason="NOT_REGISTERED"))
        assert result.is_err()
        assert "NOT_REGISTERED" in result.result_string

    def test_parse_response_error_missing_slivers(self) -> None:
        cmd = PutSliver(
            blob_id=b"x" * 32, sliver_pair_index=0, sliver_type="primary", data=b"x"
        )
        result = cmd.parse_response(_error_response(reason="MISSING_SLIVERS"))
        assert result.is_err()
        assert "MISSING_SLIVERS" in result.result_string


class TestPutMetadata:
    def test_endpoint_role_is_storage_node(self) -> None:
        assert PutMetadata.endpoint_role == "storage_node"

    def test_url_path_exact_string(self) -> None:
        blob_id = bytes(range(32))
        cmd = PutMetadata(blob_id=blob_id, metadata_bcs=b"x")
        expected_b64 = blob_id_to_url_base64(blob_id=blob_id)
        assert cmd.url_path(BASE_URL) == f"{BASE_URL}/v1/blobs/{expected_b64}/metadata"

    def test_url_path_blob_id_segment_is_url_safe_unpadded(self) -> None:
        # A blob_id chosen so the standard alphabet would emit '+', '/', '='.
        blob_id = bytes([0xFF, 0xFE, 0xFD]) + bytes(29)
        cmd = PutMetadata(blob_id=blob_id, metadata_bcs=b"x")
        url = cmd.url_path(BASE_URL)
        segment = url.split("/v1/blobs/", 1)[1].split("/metadata", 1)[0]
        assert "=" not in segment
        assert "+" not in segment
        assert "/" not in segment

    def test_http_method(self) -> None:
        cmd = PutMetadata(blob_id=b"x" * 32, metadata_bcs=b"x")
        assert cmd.http_method() == "PUT"

    def test_request_body_returns_exact_bytes(self) -> None:
        payload = b"\x00\x01\xffraw-bcs-metadata-bytes"
        cmd = PutMetadata(blob_id=b"x" * 32, metadata_bcs=payload)
        assert cmd.request_body() == payload

    @pytest.mark.parametrize("status_code", [200, 201, 202])
    def test_parse_response_success_status_codes(self, status_code: int) -> None:
        """200 (already stored), 201 (stored) and 202 (buffered pending
        registration) are ALL success."""
        blob_id = b"x" * 32
        cmd = PutMetadata(blob_id=blob_id, metadata_bcs=b"x")
        response = httpx.Response(
            status_code=status_code,
            content=json.dumps(
                {"success": {"code": status_code, "data": "stored"}}
            ).encode(),
            headers={"content-type": "application/json"},
        )
        result = cmd.parse_response(response)
        assert result.is_ok()
        assert isinstance(result.result_data, MetadataAck)
        ack: MetadataAck = result.result_data
        assert ack.blob_id == blob_id_to_url_base64(blob_id=blob_id)

    def test_parse_response_error_includes_status_and_reason(self) -> None:
        cmd = PutMetadata(blob_id=b"x" * 32, metadata_bcs=b"x")
        result = cmd.parse_response(_error_response(reason="METADATA_NOT_FOUND"))
        assert result.is_err()
        assert "400" in result.result_string
        assert "METADATA_NOT_FOUND" in result.result_string


class TestGetStorageConfirmation:
    def test_endpoint_role_is_storage_node(self) -> None:
        assert GetStorageConfirmation.endpoint_role == "storage_node"

    def test_url_path_permanent(self) -> None:
        blob_id = bytes(range(32))
        cmd = GetStorageConfirmation(blob_id=blob_id)
        expected_b64 = blob_id_to_url_base64(blob_id=blob_id)
        assert (
            cmd.url_path(BASE_URL)
            == f"{BASE_URL}/v1/blobs/{expected_b64}/confirmation/permanent"
        )

    def test_url_path_deletable_includes_object_id_in_path(self) -> None:
        blob_id = bytes(range(32))
        cmd = GetStorageConfirmation(blob_id=blob_id, object_id="0xabc123")
        expected_b64 = blob_id_to_url_base64(blob_id=blob_id)
        assert (
            cmd.url_path(BASE_URL)
            == f"{BASE_URL}/v1/blobs/{expected_b64}/confirmation/deletable/0xabc123"
        )

    def test_http_method(self) -> None:
        assert GetStorageConfirmation(blob_id=b"x" * 32).http_method() == "GET"

    def test_query_params_omits_wait_millis_when_unset(self) -> None:
        cmd = GetStorageConfirmation(blob_id=b"x" * 32)
        params = cmd.query_params()
        assert "wait_millis" not in params
        assert params["wait_for_registration"] == "true"

    def test_query_params_includes_wait_millis_when_set(self) -> None:
        cmd = GetStorageConfirmation(blob_id=b"x" * 32, wait_millis=500)
        params = cmd.query_params()
        assert params["wait_millis"] == 500

    def test_query_params_wait_for_registration_false(self) -> None:
        cmd = GetStorageConfirmation(blob_id=b"x" * 32, wait_for_registration=False)
        assert cmd.query_params()["wait_for_registration"] == "false"

    def test_parse_response_success_round_trips_standard_base64(self) -> None:
        serialized_message = b"\x00" * 40
        signature = b"\x01" * 96
        envelope = {
            "success": {
                "code": 200,
                "data": {
                    "signed": {
                        "serializedMessage": base64.b64encode(
                            serialized_message
                        ).decode("ascii"),
                        "signature": base64.b64encode(signature).decode("ascii"),
                    }
                },
            }
        }
        response = httpx.Response(
            status_code=200,
            content=json.dumps(envelope).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        cmd = GetStorageConfirmation(blob_id=b"x" * 32)
        result = cmd.parse_response(response)
        assert result.is_ok()
        assert isinstance(result.result_data, SignedConfirmation)
        confirmation: SignedConfirmation = result.result_data
        assert confirmation.serialized_message == serialized_message
        assert confirmation.signature == signature

    def test_parse_response_error_not_registered(self) -> None:
        cmd = GetStorageConfirmation(blob_id=b"x" * 32)
        result = cmd.parse_response(_error_response(reason="NOT_REGISTERED"))
        assert result.is_err()
        assert "NOT_REGISTERED" in result.result_string

    def test_parse_response_error_missing_slivers(self) -> None:
        cmd = GetStorageConfirmation(blob_id=b"x" * 32)
        result = cmd.parse_response(_error_response(reason="MISSING_SLIVERS"))
        assert result.is_err()
        assert "MISSING_SLIVERS" in result.result_string

    def test_parse_response_unexpected_shape_is_error(self) -> None:
        response = httpx.Response(
            status_code=200,
            content=json.dumps({"success": {"code": 200, "data": {}}}).encode(),
            headers={"content-type": "application/json"},
        )
        cmd = GetStorageConfirmation(blob_id=b"x" * 32)
        result = cmd.parse_response(response)
        assert result.is_err()


class TestBaseUrlResolution:
    """Base-URL resolution against a real WalrusClient.

    Constructing a real WalrusClient only exercises pysui's config-driven
    client_factory (no network I/O at construction time), so a real client
    is used here instead of a mock hierarchy. What IS mocked: WalrusClient
    _send (the actual httpx transport call) -- these tests only cover
    _dispatch_walrus's URL-resolution branching, not a live HTTP round
    trip against a storage node, which is uncovered since the integration
    suite was deleted 2026-08-28.
    """

    @pytest.fixture
    def client(self) -> Any:
        # Imported lazily so a missing pysui/pytusk config dependency
        # surfaces as a clear test failure at fixture time, not collection.
        from pytusk.client.walrus_client import WalrusClient
        from pytusk.config.tusk_config import PytuskConfiguration

        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = PytuskConfiguration(from_cfg_path=tmp_dir)
            yield WalrusClient(pytusk_config=cfg)

    @pytest.mark.asyncio
    async def test_storage_node_command_without_base_url_raises(
        self, client: Any
    ) -> None:
        cmd = PutSliver(
            blob_id=b"x" * 32, sliver_pair_index=0, sliver_type="primary", data=b"x"
        )
        with pytest.raises(ValueError, match="requires an explicit base_url"):
            await client._dispatch_walrus(
                cmd, timeout=None, headers=None, base_url=None
            )

    @pytest.mark.asyncio
    async def test_storage_node_command_with_base_url_is_used(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_send(
            *, command: Any, base_url: str, timeout: Any, headers: Any
        ) -> str:
            captured["base_url"] = base_url
            return "sent"

        monkeypatch.setattr(client, "_send", fake_send)
        cmd = PutSliver(
            blob_id=b"x" * 32, sliver_pair_index=0, sliver_type="primary", data=b"x"
        )
        result = await client._dispatch_walrus(
            cmd, timeout=None, headers=None, base_url=BASE_URL
        )
        assert result == "sent"
        assert captured["base_url"] == BASE_URL

    @pytest.mark.asyncio
    async def test_explicit_base_url_wins_over_publisher_role(
        self, client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pytusk.commands.write_commands import StoreBlob

        captured: dict[str, Any] = {}

        async def fake_send(
            *, command: Any, base_url: str, timeout: Any, headers: Any
        ) -> str:
            captured["base_url"] = base_url
            return "sent"

        monkeypatch.setattr(client, "_send", fake_send)
        cmd = StoreBlob(data=b"x", epochs=1, send_object_to="0xabc")
        await client._dispatch_walrus(
            cmd, timeout=None, headers=None, base_url="https://override.example.com"
        )
        assert captured["base_url"] == "https://override.example.com"


class TestGetSliver:
    """Sliver GET against a storage node."""

    @staticmethod
    def _command() -> GetSliver:
        """Build a representative sliver GET."""
        return GetSliver(
            blob_id=b"x" * 32, sliver_pair_index=3, sliver_type="primary"
        )

    def test_endpoint_role_is_storage_node(self) -> None:
        """Sliver reads target a storage node, not the aggregator."""
        assert self._command().endpoint_role == "storage_node"

    def test_http_method(self) -> None:
        """A sliver read is a GET."""
        assert self._command().http_method() == "GET"

    def test_url_path_mirrors_the_put_form(self) -> None:
        """The read URL is byte-identical in shape to the sliver PUT URL."""
        blob_id = b"x" * 32
        cmd = GetSliver(
            blob_id=blob_id, sliver_pair_index=7, sliver_type="secondary"
        )
        expected = (
            f"http://node/v1/blobs/{blob_id_to_url_base64(blob_id=blob_id)}"
            f"/slivers/7/secondary"
        )
        assert cmd.url_path("http://node") == expected

    def test_rejects_unknown_sliver_type(self) -> None:
        """An unknown axis is rejected at construction."""
        with pytest.raises(ValueError, match="sliver_type must be one of"):
            GetSliver(
                blob_id=b"x" * 32, sliver_pair_index=0, sliver_type="diagonal"
            )

    def test_parse_response_returns_raw_bytes(self) -> None:
        """A successful read yields the node's bytes unmodified."""
        payload = b"\x00\x01\x02raw sliver bytes\xff"
        response = httpx.Response(
            status_code=200,
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )
        result = self._command().parse_response(response)
        assert result.is_ok()
        assert isinstance(result.result_data, SliverData)
        assert result.result_data.content == payload

    def test_json_shaped_body_is_not_unwrapped(self) -> None:
        """REGRESSION: a sliver body never goes through the JSON envelope.

        ``unwrap_storage_node_envelope`` belongs to the status and
        confirmation endpoints. Sliver bytes that happen to parse as an
        envelope must still come back byte-for-byte -- otherwise a sliver
        would be silently replaced by its own "data" field.
        """
        payload = json.dumps(
            {"success": {"code": 200, "data": "not-a-sliver"}}
        ).encode()
        response = httpx.Response(
            status_code=200,
            content=payload,
            headers={"content-type": "application/json"},
        )
        result = self._command().parse_response(response)
        assert result.is_ok()
        assert isinstance(result.result_data, SliverData)
        assert result.result_data.content == payload

    def test_parse_response_error(self) -> None:
        """An HTTP error yields a failed result rather than raising."""
        response = httpx.Response(
            status_code=404,
            content=b"not found",
            headers={"content-type": "text/plain"},
        )
        result = self._command().parse_response(response)
        assert not result.is_ok()


class TestGetMetadata:
    """Blob-metadata GET against a storage node."""

    @staticmethod
    def _command() -> GetMetadata:
        """Build a representative metadata GET."""
        return GetMetadata(blob_id=b"x" * 32)

    def test_endpoint_role_is_storage_node(self) -> None:
        """Metadata reads target a storage node, not the aggregator."""
        assert self._command().endpoint_role == "storage_node"

    def test_http_method(self) -> None:
        """A metadata read is a GET."""
        assert self._command().http_method() == "GET"

    def test_url_path(self) -> None:
        """The metadata URL uses the URL-safe base64 blob ID."""
        blob_id = b"x" * 32
        cmd = GetMetadata(blob_id=blob_id)
        expected = (
            f"http://node/v1/blobs/"
            f"{blob_id_to_url_base64(blob_id=blob_id)}/metadata"
        )
        assert cmd.url_path("http://node") == expected

    def test_parse_response_returns_raw_bytes(self) -> None:
        """A successful read yields the node's BCS bytes unmodified."""
        payload = b"\x20\x00outer BlobMetadataWithId bytes\xfe"
        response = httpx.Response(
            status_code=200,
            content=payload,
            headers={"content-type": "application/octet-stream"},
        )
        result = self._command().parse_response(response)
        assert result.is_ok()
        assert isinstance(result.result_data, MetadataData)
        assert result.result_data.content == payload

    def test_json_shaped_body_is_not_unwrapped(self) -> None:
        """REGRESSION: metadata bytes never go through the JSON envelope."""
        payload = json.dumps(
            {"success": {"code": 200, "data": "not-metadata"}}
        ).encode()
        response = httpx.Response(
            status_code=200,
            content=payload,
            headers={"content-type": "application/json"},
        )
        result = self._command().parse_response(response)
        assert result.is_ok()
        assert isinstance(result.result_data, MetadataData)
        assert result.result_data.content == payload

    def test_parse_response_error(self) -> None:
        """An HTTP error yields a failed result rather than raising."""
        response = httpx.Response(
            status_code=500,
            content=b"boom",
            headers={"content-type": "text/plain"},
        )
        result = self._command().parse_response(response)
        assert not result.is_ok()
