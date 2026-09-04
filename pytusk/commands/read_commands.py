#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus read commands (Sprint 1)."""

import dataclasses
from typing import Any

import httpx
from pysui import SuiRpcResult

from pytusk.commands.walrus_command import (
    BlobData,
    BlobSlice,
    QuiltPatch,
    QuiltPatchItem,
    QuiltPatchListing,
    WalrusCommand,
    http_failure_message,
)


def _consistency_params(
    *, strict_consistency_check: bool, skip_consistency_check: bool
) -> dict[str, Any]:
    """Build the integrity-check query parameters shared by three read commands.

    ``ReadBlob``, ``ReadBlobByObjectId`` and ``ConcatBlobs`` all accept the
    same pair of flags and encode them identically. Held in one function so a
    correction to a parameter NAME cannot be applied to one command and missed
    on the other two -- the drift this was copy-pasted into being. The other
    two read commands do not carry these flags at all, which is why this is a
    helper the three call rather than behaviour inherited by all five.

    Args:
        strict_consistency_check (bool): Force strict integrity verification.
        skip_consistency_check (bool): Skip integrity verification entirely.

    Returns:
        dict[str, Any]: Only the flags that are set; an unset flag is omitted
            rather than sent as ``"false"``.
    """
    params: dict[str, Any] = {}
    if strict_consistency_check:
        params["strict_consistency_check"] = "true"
    if skip_consistency_check:
        params["skip_consistency_check"] = "true"
    return params


def _content_result(
    *,
    response: httpx.Response,
    content_type: type[BlobData] | type[BlobSlice] | type[QuiltPatch],
    context: str,
) -> SuiRpcResult:
    """Wrap a read response as a ``SuiRpcResult``, or report its failure.

    Every read command differs only in which content dataclass it builds and
    which identity it reports on failure, so those are the two parameters.
    Collapsing the five copies into this function means the error branch has
    a SINGLE definition -- the three content types stay distinct in the
    public API, but how a failed read is reported is decided in one place.

    Failures are reported through
    :func:`~pytusk.commands.walrus_command.http_failure_message`, the one
    idiom every Walrus command shares as of Plan #28 step 11. These commands
    previously returned a bare ``response.text``, which gave a caller no
    status code, no URL, no AIP-193 reason -- and no way to tell WHICH read
    failed when several were in flight.

    Args:
        response (httpx.Response): The aggregator's response.
        content_type: The dataclass to wrap successful content in.
        context (str): Identifying detail for the failure message, e.g.
            ``"blob_id=..."``.

    Returns:
        SuiRpcResult: The content on success; on an HTTP error, a failed
            result carrying the diagnostic message.
    """
    if response.is_error:
        return SuiRpcResult(
            False, http_failure_message(response=response, context=context)
        )
    return SuiRpcResult(True, "", content_type(content=response.content))


@dataclasses.dataclass(kw_only=True)
class ReadBlob(WalrusCommand):
    """Read a blob by its Walrus blob ID.

    GET {aggregator}/v1/blobs/{blob_id}

    Args:
        blob_id (str): Walrus blob identifier.
        strict_consistency_check (bool): If True, force strict integrity
            verification of the read blob against its metadata.
        skip_consistency_check (bool): If True, skip integrity verification.
            Only safe when the writer is known and trusted.
    """

    blob_id: str
    strict_consistency_check: bool = dataclasses.field(default=False)
    skip_consistency_check: bool = dataclasses.field(default=False)

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blobs/{self.blob_id}"

    def query_params(self) -> dict[str, Any]:
        return _consistency_params(
            strict_consistency_check=self.strict_consistency_check,
            skip_consistency_check=self.skip_consistency_check,
        )

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        return _content_result(
            response=response,
            content_type=BlobData,
            context=f"blob_id={self.blob_id}",
        )


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
        return _content_result(
            response=response,
            content_type=BlobSlice,
            context=(
                f"blob_id={self.blob_id} start={self.start} length={self.length}"
            ),
        )


@dataclasses.dataclass(kw_only=True)
class ReadBlobByObjectId(WalrusCommand):
    """Read a blob by its Sui object ID.

    GET {aggregator}/v1/blobs/by-object-id/{object_id}

    Args:
        object_id (str): Sui object ID of the blob.
        strict_consistency_check (bool): If True, force strict integrity
            verification of the read blob against its metadata.
        skip_consistency_check (bool): If True, skip integrity verification.
            Only safe when the writer is known and trusted.
    """

    object_id: str
    strict_consistency_check: bool = dataclasses.field(default=False)
    skip_consistency_check: bool = dataclasses.field(default=False)

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blobs/by-object-id/{self.object_id}"

    def query_params(self) -> dict[str, Any]:
        return _consistency_params(
            strict_consistency_check=self.strict_consistency_check,
            skip_consistency_check=self.skip_consistency_check,
        )

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        return _content_result(
            response=response,
            content_type=BlobData,
            context=f"object_id={self.object_id}",
        )


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
        return _content_result(
            response=response,
            content_type=QuiltPatch,
            context=f"quilt_id={self.quilt_id} patch_key={self.patch_key}",
        )


@dataclasses.dataclass(kw_only=True)
class ReadQuiltPatchById(WalrusCommand):
    """Read a single patch from a quilt directly by its QuiltPatchId.

    GET {aggregator}/v1/blobs/by-quilt-patch-id/{patch_id}

    Args:
        patch_id (str): Walrus QuiltPatchId (URL-safe base64) addressing
            the patch directly, independent of its containing quilt.
    """

    patch_id: str

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/blobs/by-quilt-patch-id/{self.patch_id}"

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        return _content_result(
            response=response,
            content_type=QuiltPatch,
            context=f"patch_id={self.patch_id}",
        )


@dataclasses.dataclass(kw_only=True)
class ListQuiltPatches(WalrusCommand):
    """List the patches contained in a quilt.

    GET {aggregator}/v1/quilts/{quilt_id}/patches

    Args:
        quilt_id (str): Walrus quilt identifier.
    """

    quilt_id: str

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1/quilts/{self.quilt_id}/patches"

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            return SuiRpcResult(
                False,
                http_failure_message(
                    response=response, context=f"quilt_id={self.quilt_id}"
                ),
            )
        patches = [
            QuiltPatchItem(
                patch_key=item.get("identifier", ""),
                patch_id=item.get("patch_id", ""),
                tags=item.get("tags", {}),
            )
            for item in response.json()
        ]
        return SuiRpcResult(True, "", QuiltPatchListing(patches=patches))


@dataclasses.dataclass(kw_only=True)
class ConcatBlobs(WalrusCommand):
    """Concatenate multiple blobs and return the combined content.

    GET {aggregator}/v1alpha/blobs/concat

    Args:
        ids (list[str]): Ordered list of Walrus blob IDs to concatenate.
        strict_consistency_check (bool): If True, force strict integrity
            verification of the concatenated blobs against their metadata.
        skip_consistency_check (bool): If True, skip integrity verification.
            Only safe when the writer is known and trusted.
    """

    ids: list[str]
    strict_consistency_check: bool = dataclasses.field(default=False)
    skip_consistency_check: bool = dataclasses.field(default=False)

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        return f"{base_url}/v1alpha/blobs/concat"

    def query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"ids": ",".join(self.ids)}
        params.update(
            _consistency_params(
                strict_consistency_check=self.strict_consistency_check,
                skip_consistency_check=self.skip_consistency_check,
            )
        )
        return params

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        return _content_result(
            response=response,
            content_type=BlobData,
            context=f"ids={','.join(self.ids)}",
        )
