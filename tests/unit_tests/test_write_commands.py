#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for Sprint 2 write commands."""

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from pytusk.commands.walrus_command import BlobReceipt, QuiltReceipt
from pytusk.commands.write_commands import WriteBlob, WriteQuilt

PUB = "https://publisher.example.com"
RECIPIENT = "0xabc"


def mock_response(
    *,
    is_error: bool = False,
    text: str = "",
    json_data: dict[str, Any] | None = None,
) -> MagicMock:
    r: MagicMock = MagicMock(spec=httpx.Response)
    r.is_error = is_error
    r.status_code = 400 if is_error else 200
    r.text = text
    r.json.return_value = json_data or {}
    return r


class TestWriteBlob:
    def test_http_method(self) -> None:
        assert WriteBlob(data=b"x", epochs=1, send_object_to=RECIPIENT).http_method() == "PUT"

    def test_url_path(self) -> None:
        cmd = WriteBlob(data=b"x", epochs=1, send_object_to=RECIPIENT)
        assert cmd.url_path(PUB) == f"{PUB}/v1/blobs"

    def test_query_params_default_permanent(self) -> None:
        cmd = WriteBlob(data=b"x", epochs=3, send_object_to=RECIPIENT)
        params = cmd.query_params()
        assert params["epochs"] == 3
        assert params["permanent"] == "false"
        assert "deletable" not in params

    def test_query_params_permanent(self) -> None:
        cmd = WriteBlob(data=b"x", epochs=3, send_object_to=RECIPIENT, permanent=True)
        params = cmd.query_params()
        assert params["permanent"] == "true"

    def test_request_body(self) -> None:
        cmd = WriteBlob(data=b"hello", epochs=1, send_object_to=RECIPIENT)
        assert cmd.request_body() == b"hello"

    def test_form_files_none(self) -> None:
        assert WriteBlob(data=b"x", epochs=1, send_object_to=RECIPIENT).form_files() is None

    def test_parse_response_success(self) -> None:
        cmd = WriteBlob(data=b"data", epochs=5, send_object_to=RECIPIENT)
        result = cmd.parse_response(
            mock_response(
                json_data={
                    "newlyCreated": {
                        "blobObject": {
                            "id": "obj-abc",
                            "blobId": "blob-abc",
                            "deletable": False,
                            "storage": {"endEpoch": 10},
                        },
                        "cost": 100,
                    }
                }
            )
        )
        assert result.is_ok()
        assert isinstance(result.result_data, BlobReceipt)
        receipt: BlobReceipt = result.result_data
        assert receipt.blob_id == "blob-abc"
        assert receipt.cost == 100
        assert receipt.expiry_epoch == 10
        assert receipt.deletable is False

    def test_parse_response_success_deletable(self) -> None:
        cmd = WriteBlob(data=b"data", epochs=5, send_object_to=RECIPIENT)
        result = cmd.parse_response(
            mock_response(
                json_data={
                    "newlyCreated": {
                        "blobObject": {
                            "id": "obj-xyz",
                            "blobId": "blob-xyz",
                            "deletable": True,
                            "storage": {"endEpoch": 7},
                        },
                        "cost": 50,
                    }
                }
            )
        )
        assert result.is_ok()
        receipt: BlobReceipt = result.result_data
        assert receipt.deletable is True

    def test_parse_response_already_certified_permanent(self) -> None:
        cmd = WriteBlob(data=b"data", epochs=5, send_object_to=RECIPIENT, permanent=True)
        result = cmd.parse_response(
            mock_response(
                json_data={
                    "alreadyCertified": {
                        "blobId": "blob-cached",
                        "event": {"txDigest": "digest", "eventSeq": "0"},
                        "endEpoch": 12,
                    }
                }
            )
        )
        assert result.is_ok()
        receipt: BlobReceipt = result.result_data
        assert receipt.deletable is False

    def test_parse_response_already_certified_deletable(self) -> None:
        cmd = WriteBlob(data=b"data", epochs=5, send_object_to=RECIPIENT)
        result = cmd.parse_response(
            mock_response(
                json_data={
                    "alreadyCertified": {
                        "blobId": "blob-cached",
                        "event": {"txDigest": "digest", "eventSeq": "0"},
                        "endEpoch": 12,
                    }
                }
            )
        )
        assert result.is_ok()
        receipt: BlobReceipt = result.result_data
        assert receipt.deletable is True

    def test_parse_response_missing_fields_use_defaults(self) -> None:
        cmd = WriteBlob(data=b"data", epochs=1, send_object_to=RECIPIENT)
        result = cmd.parse_response(
            mock_response(json_data={"newlyCreated": {"blobObject": {}}})
        )
        assert result.is_ok()
        receipt: BlobReceipt = result.result_data
        assert receipt.blob_id == ""
        assert receipt.cost == 0
        assert receipt.expiry_epoch == 0

    def test_parse_response_error(self) -> None:
        cmd = WriteBlob(data=b"data", epochs=1, send_object_to=RECIPIENT)
        result = cmd.parse_response(mock_response(is_error=True, text="Upload failed"))
        assert result.is_err()
        assert "Upload failed" in result.result_string

    def test_requires_data_and_epochs(self) -> None:
        with pytest.raises(TypeError):
            WriteBlob()  # type: ignore[call-arg]

    def test_requires_data(self) -> None:
        with pytest.raises(TypeError):
            WriteBlob(epochs=1, send_object_to=RECIPIENT)  # type: ignore[call-arg]

    def test_requires_epochs(self) -> None:
        with pytest.raises(TypeError):
            WriteBlob(data=b"x", send_object_to=RECIPIENT)  # type: ignore[call-arg]

    def test_requires_send_object_to(self) -> None:
        with pytest.raises(TypeError):
            WriteBlob(data=b"x", epochs=1)  # type: ignore[call-arg]


class TestWriteQuilt:
    def test_http_method(self) -> None:
        assert (
            WriteQuilt(files={"a": b"x"}, epochs=1, send_object_to=RECIPIENT).http_method()
            == "PUT"
        )

    def test_url_path(self) -> None:
        cmd = WriteQuilt(files={"a": b"x"}, epochs=1, send_object_to=RECIPIENT)
        assert cmd.url_path(PUB) == f"{PUB}/v1/quilts"

    def test_query_params(self) -> None:
        cmd = WriteQuilt(files={"a": b"x"}, epochs=4, send_object_to=RECIPIENT)
        assert cmd.query_params() == {
            "epochs": 4,
            "permanent": "false",
            "send_object_to": RECIPIENT,
        }

    def test_query_params_permanent(self) -> None:
        cmd = WriteQuilt(files={"a": b"x"}, epochs=4, send_object_to=RECIPIENT, permanent=True)
        assert cmd.query_params()["permanent"] == "true"

    def test_request_body_none(self) -> None:
        assert (
            WriteQuilt(files={"a": b"x"}, epochs=1, send_object_to=RECIPIENT).request_body()
            is None
        )

    def test_form_files(self) -> None:
        files = {"file_a": b"content_a", "file_b": b"content_b"}
        cmd = WriteQuilt(files=files, epochs=2, send_object_to=RECIPIENT)
        assert cmd.form_files() == files

    def test_form_files_identity(self) -> None:
        files = {"k": b"v"}
        cmd = WriteQuilt(files=files, epochs=1, send_object_to=RECIPIENT)
        assert cmd.form_files() is files

    def test_parse_response_success(self) -> None:
        cmd = WriteQuilt(files={"a": b"x"}, epochs=3, send_object_to=RECIPIENT)
        result = cmd.parse_response(
            mock_response(
                json_data={
                    "blobStoreResult": {
                        "newlyCreated": {
                            "blobObject": {
                                "id": "obj-quilt-123",
                                "blobId": "quilt-123",
                                "storage": {"endEpoch": 15},
                            },
                            "cost": 200,
                        }
                    },
                    "storedQuiltBlobs": [
                        {"identifier": "a", "quiltPatchId": "patch-a"},
                        {"identifier": "b", "quiltPatchId": "patch-b"},
                    ],
                }
            )
        )
        assert result.is_ok()
        assert isinstance(result.result_data, QuiltReceipt)
        receipt: QuiltReceipt = result.result_data
        assert receipt.quilt_id == "quilt-123"
        assert receipt.patch_keys == ["a", "b"]
        assert receipt.cost == 200
        assert receipt.expiry_epoch == 15

    def test_parse_response_already_certified(self) -> None:
        cmd = WriteQuilt(files={"a": b"x"}, epochs=3, send_object_to=RECIPIENT)
        result = cmd.parse_response(
            mock_response(
                json_data={
                    "blobStoreResult": {
                        "alreadyCertified": {
                            "blobId": "quilt-existing-id",
                            "objectId": "obj-existing-123",
                            "endEpoch": 20,
                        }
                    },
                    "storedQuiltBlobs": [
                        {"identifier": "a", "quiltPatchId": "patch-a"},
                    ],
                }
            )
        )
        assert result.is_ok()
        receipt: QuiltReceipt = result.result_data
        assert receipt.quilt_id == "quilt-existing-id"
        assert receipt.object_id == "obj-existing-123"
        assert receipt.cost == 0
        assert receipt.expiry_epoch == 20
        assert receipt.patch_keys == ["a"]

    def test_parse_response_missing_fields_use_defaults(self) -> None:
        cmd = WriteQuilt(files={"a": b"x"}, epochs=1, send_object_to=RECIPIENT)
        result = cmd.parse_response(
            mock_response(
                json_data={
                    "blobStoreResult": {"newlyCreated": {"blobObject": {}}},
                    "storedQuiltBlobs": [],
                }
            )
        )
        assert result.is_ok()
        receipt: QuiltReceipt = result.result_data
        assert receipt.quilt_id == ""
        assert receipt.patch_keys == []
        assert receipt.cost == 0
        assert receipt.expiry_epoch == 0

    def test_parse_response_error(self) -> None:
        cmd = WriteQuilt(files={"a": b"x"}, epochs=1, send_object_to=RECIPIENT)
        result = cmd.parse_response(mock_response(is_error=True, text="Quilt store failed"))
        assert result.is_err()
        assert "Quilt store failed" in result.result_string

    def test_requires_files_and_epochs(self) -> None:
        with pytest.raises(TypeError):
            WriteQuilt()  # type: ignore[call-arg]

    def test_requires_send_object_to(self) -> None:
        with pytest.raises(TypeError):
            WriteQuilt(files={"a": b"x"}, epochs=1)  # type: ignore[call-arg]
