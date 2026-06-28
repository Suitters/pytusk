#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus write commands (Sprint 2)."""

import dataclasses

from typing import Any

import httpx
from pysui import SuiRpcResult

from pytusk.commands.walrus_command import (
    WalrusCommand,
    BlobReceipt,
    QuiltReceipt,
)


@dataclasses.dataclass(kw_only=True)
class StoreBlob(WalrusCommand):
    """Store a blob on Walrus.

    PUT {walrus_url}/v1/blobs

    Args:
        data (bytes): Raw blob content to store.
        epochs (int): Number of epochs to store the blob for.
        deletable (bool): If True, the blob may be deleted before expiry.
    """

    data: bytes
    epochs: int
    deletable: bool = dataclasses.field(default=False)

    def http_method(self) -> str:
        return "PUT"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blobs"

    def query_params(self) -> dict[str, Any]:
        return {"epochs": self.epochs, "deletable": str(self.deletable).lower()}

    def request_body(self) -> bytes | None:
        return self.data

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(False, f"HTTP {response.status_code}: {response.text}")
        data = response.json()
        if "newlyCreated" in data:
            nc = data["newlyCreated"]
            blob_obj = nc["blobObject"]
            receipt = BlobReceipt(
                object_id=blob_obj.get("id", ""),
                blob_id=blob_obj.get("blobId", ""),
                cost=nc.get("cost", 0),
                expiry_epoch=blob_obj.get("storage", {}).get("endEpoch", 0),
                deletable=blob_obj.get("deletable", False),
            )
        elif "alreadyCertified" in data:
            ac = data["alreadyCertified"]
            receipt = BlobReceipt(
                object_id="",
                blob_id=ac.get("blobId", ""),
                cost=0,
                expiry_epoch=ac.get("endEpoch", 0),
                deletable=self.deletable,
            )
        else:
            return SuiRpcResult(False, f"Unexpected publisher response: {data}")
        return SuiRpcResult(True, "", receipt)


@dataclasses.dataclass(kw_only=True)
class StoreQuilt(WalrusCommand):
    """Store a quilt (collection of named blobs) on Walrus.

    PUT {walrus_url}/v1/quilts

    Args:
        files (dict[str, bytes]): Mapping of patch key to raw file content.
        epochs (int): Number of epochs to store the quilt for.
    """

    files: dict[str, bytes]
    epochs: int

    def http_method(self) -> str:
        return "PUT"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/quilts"

    def query_params(self) -> dict[str, Any]:
        return {"epochs": self.epochs}

    def form_files(self) -> dict[str, bytes] | None:
        return self.files

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(False, f"HTTP {response.status_code}: {response.text}")
        data = response.json()
        blob_result = data.get("blobStoreResult", {})
        stored_blobs = data.get("storedQuiltBlobs", [])

        quilt_id = ""
        object_id = ""
        cost = 0
        expiry_epoch = 0

        if "newlyCreated" in blob_result:
            nc = blob_result["newlyCreated"]
            blob_obj = nc.get("blobObject", {})
            quilt_id = blob_obj.get("blobId", "")
            object_id = blob_obj.get("id", "")
            cost = nc.get("cost", 0)
            expiry_epoch = blob_obj.get("storage", {}).get("endEpoch", 0)
        elif "alreadyCertified" in blob_result:
            ac = blob_result["alreadyCertified"]
            quilt_id = ac.get("blobId", "")
            object_id = ac.get("object", "")
            cost = 0
            expiry_epoch = ac.get("endEpoch", 0)
        else:
            return SuiRpcResult(False, f"Unexpected quilt response: {data}")

        patch_keys = [item.get("identifier", "") for item in stored_blobs]
        receipt = QuiltReceipt(
            quilt_id=quilt_id,
            patch_keys=patch_keys,
            cost=cost,
            expiry_epoch=expiry_epoch,
            object_id=object_id,
        )
        return SuiRpcResult(True, "", receipt)
