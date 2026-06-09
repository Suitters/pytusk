#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus read commands (Sprint 1)."""

import dataclasses

from typing import Any

import httpx
from pysui import SuiRpcResult

from pytusk.commands.walrus_command import (
    WalrusCommand,
    BlobData,
    BlobSlice,
    QuiltPatch,
)


@dataclasses.dataclass(kw_only=True)
class ReadBlob(WalrusCommand):
    """Read a blob by its Walrus blob ID.

    GET {aggregator}/v1/blobs/{blob_id}

    Args:
        blob_id (str): Walrus blob identifier.
    """

    blob_id: str

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blobs/{self.blob_id}"

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(False, response.text)
        return SuiRpcResult(True, "", BlobData(content=response.content))


@dataclasses.dataclass(kw_only=True)
class ReadBlobPartial(WalrusCommand):
    """Read a byte range from a blob.

    GET {aggregator}/v1/blobs/{blob_id}/byte-range

    Args:
        blob_id (str): Walrus blob identifier.
        start (int): First byte offset (inclusive).
        length (int): Number of bytes to read.
    """

    blob_id: str
    start: int
    length: int

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blobs/{self.blob_id}/byte-range"

    def query_params(self) -> dict[str, Any]:
        return {"start": self.start, "length": self.length}

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(False, response.text)
        return SuiRpcResult(True, "", BlobSlice(content=response.content))


@dataclasses.dataclass(kw_only=True)
class ReadBlobByObjectId(WalrusCommand):
    """Read a blob by its Sui object ID.

    GET {aggregator}/v1/blobs/by-object-id/{object_id}

    Args:
        object_id (str): Sui object ID of the blob.
    """

    object_id: str

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blobs/by-object-id/{self.object_id}"

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(False, response.text)
        return SuiRpcResult(True, "", BlobData(content=response.content))


@dataclasses.dataclass(kw_only=True)
class ReadQuiltPatch(WalrusCommand):
    """Read a single patch from a quilt by quilt ID and patch key.

    GET {aggregator}/v1/blobs/by-quilt-id/{quilt_id}/{patch_key}

    Args:
        quilt_id (str): Walrus quilt identifier.
        patch_key (str): Key identifying the patch within the quilt.
    """

    quilt_id: str
    patch_key: str

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blobs/by-quilt-id/{self.quilt_id}/{self.patch_key}"

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(False, response.text)
        return SuiRpcResult(True, "", QuiltPatch(content=response.content))


@dataclasses.dataclass(kw_only=True)
class ConcatBlobs(WalrusCommand):
    """Concatenate multiple blobs and return the combined content.

    GET {aggregator}/v1alpha/blobs/concat

    Args:
        ids (list[str]): Ordered list of Walrus blob IDs to concatenate.
    """

    ids: list[str]

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1alpha/blobs/concat"

    def query_params(self) -> dict[str, Any]:
        return {"ids": self.ids}

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(False, response.text)
        return SuiRpcResult(True, "", BlobData(content=response.content))
