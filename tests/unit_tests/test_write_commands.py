#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Unit tests for Sprint 2 write commands."""

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from pytusk.commands.write_commands import StoreBlob, StoreQuilt
from pytusk.commands.walrus_command import BlobReceipt, QuiltReceipt

PUB = "https://publisher.example.com"


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


class TestStoreBlob:
    def test_http_method(self) -> None:
        assert StoreBlob(data=b"x", epochs=1).http_method() == "PUT"

    def test_url_path(self) -> None:
        cmd = StoreBlob(data=b"x", epochs=1)
        assert cmd.url_path(PUB) == f"{PUB}/v1/blobs"

    def test_query_params_not_deletable(self) -> None:
        cmd = StoreBlob(data=b"x", epochs=3)
        params = cmd.query_params()
        assert params["epochs"] == 3
        assert params["deletable"] == "false"

    def test_query_params_deletable(self) -> None:
        cmd = StoreBlob(data=b"x", epochs=3, deletable=True)
        params = cmd.query_params()
        assert params["deletable"] == "true"

    def test_request_body(self) -> None:
        cmd = StoreBlob(data=b"hello", epochs=1)
        assert cmd.request_body() == b"hello"

    def test_form_files_none(self) -> None:
        assert StoreBlob(data=b"x", epochs=1).form_files() is None

    def test_parse_response_success(self) -> None:
        cmd = StoreBlob(data=b"data", epochs=5)
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
        cmd = StoreBlob(data=b"data", epochs=5, deletable=True)
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

    def test_parse_response_missing_fields_use_defaults(self) -> None:
        cmd = StoreBlob(data=b"data", epochs=1)
        result = cmd.parse_response(
            mock_response(json_data={"newlyCreated": {"blobObject": {}}})
        )
        assert result.is_ok()
        receipt: BlobReceipt = result.result_data
        assert receipt.blob_id == ""
        assert receipt.cost == 0
        assert receipt.expiry_epoch == 0

    def test_parse_response_error(self) -> None:
        cmd = StoreBlob(data=b"data", epochs=1)
        result = cmd.parse_response(mock_response(is_error=True, text="Upload failed"))
        assert result.is_err()
        assert "Upload failed" in result.result_string

    def test_requires_data_and_epochs(self) -> None:
        with pytest.raises(TypeError):
            StoreBlob()  # type: ignore[call-arg]

    def test_requires_data(self) -> None:
        with pytest.raises(TypeError):
            StoreBlob(epochs=1)  # type: ignore[call-arg]

    def test_requires_epochs(self) -> None:
        with pytest.raises(TypeError):
            StoreBlob(data=b"x")  # type: ignore[call-arg]


class TestStoreQuilt:
    def test_http_method(self) -> None:
        assert StoreQuilt(files={"a": b"x"}, epochs=1).http_method() == "PUT"

    def test_url_path(self) -> None:
        cmd = StoreQuilt(files={"a": b"x"}, epochs=1)
        assert cmd.url_path(PUB) == f"{PUB}/v1/quilts"

    def test_query_params(self) -> None:
        cmd = StoreQuilt(files={"a": b"x"}, epochs=4)
        assert cmd.query_params() == {"epochs": 4}

    def test_request_body_none(self) -> None:
        assert StoreQuilt(files={"a": b"x"}, epochs=1).request_body() is None

    def test_form_files(self) -> None:
        files = {"file_a": b"content_a", "file_b": b"content_b"}
        cmd = StoreQuilt(files=files, epochs=2)
        assert cmd.form_files() == files

    def test_form_files_identity(self) -> None:
        files = {"k": b"v"}
        cmd = StoreQuilt(files=files, epochs=1)
        assert cmd.form_files() is files

    def test_parse_response_success(self) -> None:
        cmd = StoreQuilt(files={"a": b"x"}, epochs=3)
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
        cmd = StoreQuilt(files={"a": b"x"}, epochs=3)
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
        cmd = StoreQuilt(files={"a": b"x"}, epochs=1)
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
        cmd = StoreQuilt(files={"a": b"x"}, epochs=1)
        result = cmd.parse_response(mock_response(is_error=True, text="Quilt store failed"))
        assert result.is_err()
        assert "Quilt store failed" in result.result_string

    def test_requires_files_and_epochs(self) -> None:
        with pytest.raises(TypeError):
            StoreQuilt()  # type: ignore[call-arg]
