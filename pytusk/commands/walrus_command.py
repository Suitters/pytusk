#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""WalrusCommand ABC and response dataclasses."""

import dataclasses
from abc import ABC, abstractmethod

from typing import Any

import httpx
from dataclasses_json import DataClassJsonMixin
from pysui import SuiRpcResult


@dataclasses.dataclass(kw_only=True)
class WalrusCommand(ABC):
    """Abstract base class for all Walrus HTTP operations.

    Subclasses declare their input fields as dataclass fields and implement
    the abstract methods to describe the HTTP request and parse the response.
    """

    @abstractmethod
    def http_method(self) -> str:
        """HTTP method for this command (e.g. 'GET', 'PUT', 'POST').

        Returns:
            str: Upper-case HTTP method string.
        """

    @abstractmethod
    def url_path(self, base_url: str) -> str:
        """Construct the full request URL.

        Args:
            base_url (str): Walrus daemon base URL from active network config.

        Returns:
            str: Full request URL.
        """

    def query_params(self) -> dict[str, Any]:
        """Optional query parameters for the request.

        Returns:
            dict: Query parameter mapping (empty by default).
        """
        return {}

    def request_body(self) -> bytes | None:
        """Optional request body bytes.

        Returns:
            bytes | None: Raw request body, or None if not applicable.
        """
        return None

    def form_files(self) -> dict[str, bytes] | None:
        """Optional multipart form files for the request.

        Returns:
            dict[str, bytes] | None: Mapping of field name to file bytes, or None
            if the request does not use multipart form data.
        """
        return None

    @abstractmethod
    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        """Parse an httpx response into a SuiRpcResult.

        On success, result.result_data holds the typed response object.
        On failure, result.is_err() is True and result.result_string holds
        the error message.

        Args:
            response (httpx.Response): The raw HTTP response.

        Returns:
            SuiRpcResult: Typed result wrapping success data or error.
        """


# ---------------------------------------------------------------------------
# Response dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BlobData(DataClassJsonMixin):
    """Raw blob content returned by blob read operations.

    Used by: ReadBlob, ReadBlobByObjectId, ConcatBlobs.

    Args:
        content (bytes): Raw blob bytes.
    """

    content: bytes = dataclasses.field(default=b"")


@dataclasses.dataclass
class BlobSlice(DataClassJsonMixin):
    """Partial blob content returned by a byte-range read.

    Used by: ReadBlobPartial.

    Args:
        content (bytes): Requested byte range from the blob.
    """

    content: bytes = dataclasses.field(default=b"")


@dataclasses.dataclass
class QuiltPatch(DataClassJsonMixin):
    """Quilt patch content returned by a quilt patch read.

    Used by: ReadQuiltPatch.

    Args:
        content (bytes): Raw patch bytes.
    """

    content: bytes = dataclasses.field(default=b"")


@dataclasses.dataclass
class BlobReceipt(DataClassJsonMixin):
    """Receipt returned after storing a blob.

    Used by: StoreBlob.

    Args:
        object_id (str): Sui object ID of the stored blob (empty for alreadyCertified blobs).
        blob_id (str): Walrus blob identifier.
        cost (int): Storage cost in MIST.
        expiry_epoch (int): Sui epoch at which the blob expires.
        deletable (bool): True if the blob was stored as deletable.
    """

    object_id: str = dataclasses.field(default="")
    blob_id: str = dataclasses.field(default="")
    cost: int = dataclasses.field(default=0)
    expiry_epoch: int = dataclasses.field(default=0)
    deletable: bool = dataclasses.field(default=False)


@dataclasses.dataclass
class QuiltReceipt(DataClassJsonMixin):
    """Receipt returned after storing a quilt.

    Used by: StoreQuilt.

    Args:
        quilt_id (str): Walrus quilt identifier.
        patch_keys (list[str]): Keys assigned to each patch in the quilt.
        cost (int): Storage cost in MIST.
        expiry_epoch (int): Sui epoch at which the quilt expires.
        object_id (str): Sui object ID of the stored quilt blob.
    """

    quilt_id: str = dataclasses.field(default="")
    patch_keys: list[str] = dataclasses.field(default_factory=list)
    cost: int = dataclasses.field(default=0)
    expiry_epoch: int = dataclasses.field(default=0)
    object_id: str = dataclasses.field(default="")
