#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for Sprint 1 read commands."""

from unittest.mock import MagicMock

import httpx
import pytest

from pytusk.commands.read_commands import (
    ConcatBlobs,
    ReadBlob,
    ReadBlobByObjectId,
    ReadBlobPartial,
    ReadQuiltPatch,
)
from pytusk.commands.walrus_command import BlobData, BlobSlice, QuiltPatch

AGG = "https://aggregator.example.com"


def mock_response(
    *,
    is_error: bool = False,
    text: str = "",
    content: bytes = b"",
    status_code: int | None = None,
    reason_phrase: str | None = None,
) -> MagicMock:
    """Build a stand-in for an aggregator response.

    Carries more than the three fields the success path reads, because as of
    Plan #28 step 11 the read commands report failures through
    ``http_failure_message``, which also reads ``status_code``,
    ``reason_phrase``, ``request.url`` and the AIP-193 error envelope from
    ``json()``. ``MagicMock(spec=...)`` raises on any attribute the real
    class has but the double does not set, so a thinner double fails the
    error path rather than silently returning a Mock.

    ``json()`` raises by default: an aggregator error body is plain text, not
    the AIP-193 envelope storage nodes return, so ``error_reason`` correctly
    finds no reason and the message carries no ``(reason=...)`` suffix.
    """
    r: MagicMock = MagicMock(spec=httpx.Response)
    r.is_error = is_error
    r.text = text
    r.content = content
    r.status_code = status_code if status_code is not None else (404 if is_error else 200)
    r.reason_phrase = (
        reason_phrase if reason_phrase is not None else ("Not Found" if is_error else "OK")
    )
    r.request = MagicMock(spec=httpx.Request)
    r.request.url = f"{AGG}/v1/blobs/abc"
    r.json.side_effect = ValueError("response body is not JSON")
    return r


class TestReadBlob:
    def test_http_method(self) -> None:
        assert ReadBlob(blob_id="abc").http_method() == "GET"

    def test_url_path(self) -> None:
        assert ReadBlob(blob_id="abc").url_path(AGG) == f"{AGG}/v1/blobs/abc"

    def test_query_params_empty(self) -> None:
        assert ReadBlob(blob_id="abc").query_params() == {}

    def test_request_body_none(self) -> None:
        assert ReadBlob(blob_id="abc").request_body() is None

    def test_form_files_none(self) -> None:
        assert ReadBlob(blob_id="abc").form_files() is None

    def test_parse_response_success(self) -> None:
        cmd = ReadBlob(blob_id="abc")
        result = cmd.parse_response(mock_response(content=b"blob content"))
        assert result.is_ok()
        assert isinstance(result.result_data, BlobData)
        assert result.result_data.content == b"blob content"

    def test_parse_response_error(self) -> None:
        cmd = ReadBlob(blob_id="abc")
        result = cmd.parse_response(mock_response(is_error=True, text="Not found"))
        assert result.is_err()
        assert "Not found" in result.result_string

    def test_requires_blob_id(self) -> None:
        with pytest.raises(TypeError):
            ReadBlob()  # type: ignore[call-arg]


class TestReadBlobPartial:
    def test_http_method(self) -> None:
        assert ReadBlobPartial(blob_id="abc", start=0, length=10).http_method() == "GET"

    def test_url_path(self) -> None:
        cmd = ReadBlobPartial(blob_id="abc", start=0, length=10)
        assert cmd.url_path(AGG) == f"{AGG}/v1/blobs/abc/byte-range"

    def test_query_params(self) -> None:
        cmd = ReadBlobPartial(blob_id="abc", start=100, length=50)
        params = cmd.query_params()
        assert params["start"] == 100
        assert params["length"] == 50

    def test_request_body_none(self) -> None:
        assert ReadBlobPartial(blob_id="abc", start=0, length=10).request_body() is None

    def test_parse_response_success(self) -> None:
        cmd = ReadBlobPartial(blob_id="abc", start=0, length=5)
        result = cmd.parse_response(mock_response(content=b"hello"))
        assert result.is_ok()
        assert isinstance(result.result_data, BlobSlice)
        assert result.result_data.content == b"hello"

    def test_parse_response_error(self) -> None:
        cmd = ReadBlobPartial(blob_id="abc", start=0, length=5)
        result = cmd.parse_response(mock_response(is_error=True, text="Range error"))
        assert result.is_err()
        assert "Range error" in result.result_string


class TestReadBlobByObjectId:
    def test_http_method(self) -> None:
        assert ReadBlobByObjectId(object_id="0xabc").http_method() == "GET"

    def test_url_path(self) -> None:
        cmd = ReadBlobByObjectId(object_id="0xabc")
        assert cmd.url_path(AGG) == f"{AGG}/v1/blobs/by-object-id/0xabc"

    def test_query_params_empty(self) -> None:
        assert ReadBlobByObjectId(object_id="0xabc").query_params() == {}

    def test_parse_response_success(self) -> None:
        cmd = ReadBlobByObjectId(object_id="0xabc")
        result = cmd.parse_response(mock_response(content=b"object blob"))
        assert result.is_ok()
        assert isinstance(result.result_data, BlobData)
        assert result.result_data.content == b"object blob"

    def test_parse_response_error(self) -> None:
        cmd = ReadBlobByObjectId(object_id="0xabc")
        result = cmd.parse_response(mock_response(is_error=True, text="Object not found"))
        assert result.is_err()


class TestReadQuiltPatch:
    def test_http_method(self) -> None:
        assert ReadQuiltPatch(quilt_id="q1", patch_key="file_a").http_method() == "GET"

    def test_url_path(self) -> None:
        cmd = ReadQuiltPatch(quilt_id="q1", patch_key="file_a")
        assert cmd.url_path(AGG) == f"{AGG}/v1/blobs/by-quilt-id/q1/file_a"

    def test_query_params_empty(self) -> None:
        assert ReadQuiltPatch(quilt_id="q1", patch_key="file_a").query_params() == {}

    def test_parse_response_success(self) -> None:
        cmd = ReadQuiltPatch(quilt_id="q1", patch_key="file_a")
        result = cmd.parse_response(mock_response(content=b"patch data"))
        assert result.is_ok()
        assert isinstance(result.result_data, QuiltPatch)
        assert result.result_data.content == b"patch data"

    def test_parse_response_error(self) -> None:
        cmd = ReadQuiltPatch(quilt_id="q1", patch_key="file_a")
        result = cmd.parse_response(mock_response(is_error=True, text="Patch not found"))
        assert result.is_err()


class TestConcatBlobs:
    def test_http_method(self) -> None:
        assert ConcatBlobs(ids=["a", "b"]).http_method() == "GET"

    def test_url_path(self) -> None:
        cmd = ConcatBlobs(ids=["a", "b"])
        assert cmd.url_path(AGG) == f"{AGG}/v1alpha/blobs/concat"

    def test_query_params(self) -> None:
        cmd = ConcatBlobs(ids=["id1", "id2", "id3"])
        params = cmd.query_params()
        assert params["ids"] == "id1,id2,id3"

    def test_request_body_none(self) -> None:
        assert ConcatBlobs(ids=["a"]).request_body() is None

    def test_parse_response_success(self) -> None:
        cmd = ConcatBlobs(ids=["a", "b"])
        result = cmd.parse_response(mock_response(content=b"combined"))
        assert result.is_ok()
        assert isinstance(result.result_data, BlobData)
        assert result.result_data.content == b"combined"

    def test_parse_response_error(self) -> None:
        cmd = ConcatBlobs(ids=["a", "b"])
        result = cmd.parse_response(mock_response(is_error=True, text="Concat failed"))
        assert result.is_err()
        assert "Concat failed" in result.result_string
