#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus storage-node commands (native upload, part 3).

Unlike aggregator/publisher commands, these talk DIRECTLY to a Walrus
storage node -- there is no configured base URL for them. Each command
declares ``endpoint_role = "storage_node"`` (see
:class:`~pytusk.commands.walrus_command.WalrusCommand`), and the caller
MUST resolve the node's network address from the committee and pass it as
an explicit ``base_url`` to :meth:`~pytusk.client.walrus_client.WalrusClient.execute`.
"""

import dataclasses
import json
from typing import Any, ClassVar

import httpx
from pysui import SuiRpcResult

from pytusk.commands.walrus_command import WalrusCommand
from pytusk.core.encoding import blob_id_to_url_base64, decode_standard_base64

_VALID_SLIVER_TYPES = ("primary", "secondary")

# --- STORAGE-NODE ERROR-BODY DIAGNOSTICS --------------------------------
# Captures and truncates storage-node HTTP error bodies so failures are
# diagnosable from the log line alone -- this is how INVALID_CONTENT_TYPE
# and NOT_REGISTERED failures were identified against live nodes. This
# bound keeps a flood of identical failure bodies (one per rejected
# sliver, across ~100 nodes) from blowing up the log; a truncated body is
# still diagnostic, an 8000-line log is not.
# ------------------------------------------------------------------------
_MAX_ERROR_BODY_CHARS: int = 512


def _response_body_for_log(*, response: httpx.Response) -> str:
    """Render an HTTP response body for diagnostic logging.

    Falls back to a safe ``repr`` of the raw bytes if the body cannot be
    decoded as text (a binary or otherwise undecodable payload must never
    raise out of a logging path), and truncates the result to
    ``_MAX_ERROR_BODY_CHARS`` characters, appending an explicit truncation
    marker when cut.

    Args:
        response (httpx.Response): The raw HTTP response.

    Returns:
        str: The (possibly truncated) response body, safe to log.
    """
    try:
        body = response.text
    except Exception:  # noqa: BLE001 - logging must never raise
        body = repr(response.content)
    if len(body) > _MAX_ERROR_BODY_CHARS:
        body = f"{body[:_MAX_ERROR_BODY_CHARS]}...[truncated]"
    return body


def _http_failure_message(*, response: httpx.Response, context: str) -> str:
    """Build a diagnostic failure message from a non-2xx storage-node response.

    Combines the HTTP status code and reason phrase, the request URL, the
    caller-supplied identifying context (e.g. blob/sliver identity), the
    AIP-193 ``reason`` detail when present (see :func:`error_reason`), and
    the truncated response body -- everything needed to diagnose a
    storage-node rejection from the log line alone, without re-running the
    request.

    Args:
        response (httpx.Response): The raw HTTP response.
        context (str): Caller-supplied identifying context, e.g.
            ``"blob_id=... sliver_pair_index=... sliver_type=..."``.

    Returns:
        str: The combined diagnostic message.
    """
    reason = error_reason(response=response)
    reason_suffix = f" (reason={reason})" if reason else ""
    try:
        url = str(response.request.url)
    except RuntimeError:
        url = "<unknown url>"
    body = _response_body_for_log(response=response)
    return (
        f"HTTP {response.status_code} {response.reason_phrase} for {url} "
        f"[{context}]{reason_suffix}: {body}"
    )


def error_reason(*, response: httpx.Response) -> str | None:
    """Parse the AIP-193 error envelope and return the first ``reason``.

    Storage-node error bodies look like::

        {"error": {"code": 400, "status": "FAILED_PRECONDITION",
                    "details": [{"@type": "ErrorInfo",
                                  "reason": "NOT_REGISTERED",
                                  "domain": "..."}]}}

    CRITICAL: ``NOT_REGISTERED`` and ``MISSING_SLIVERS`` BOTH arrive as
    HTTP 400 with status ``FAILED_PRECONDITION`` -- callers must branch on
    this ``reason`` string returned here, NEVER on the HTTP status code
    alone, to tell the two conditions apart.

    Args:
        response (httpx.Response): The raw HTTP response.

    Returns:
        str | None: The first ``ErrorInfo`` detail's ``reason``, or None if
        the body has no parseable error envelope.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    details = error.get("details")
    if not isinstance(details, list):
        return None
    for detail in details:
        if isinstance(detail, dict) and "reason" in detail:
            reason = detail["reason"]
            return reason if isinstance(reason, str) else None
    return None


@dataclasses.dataclass(kw_only=True, frozen=True)
class SliverAck:
    """Acknowledgement that a storage node accepted a sliver PUT.

    Used by: PutSliver.

    Args:
        blob_id (str): Blob ID the sliver belongs to, URL-safe base64.
        sliver_pair_index (int): Sliver-pair index the sliver was stored at.
        sliver_type (str): ``"primary"`` or ``"secondary"``.
    """

    blob_id: str
    sliver_pair_index: int
    sliver_type: str


@dataclasses.dataclass(kw_only=True, frozen=True)
class MetadataAck:
    """Acknowledgement that a storage node accepted a blob-metadata PUT.

    Used by: PutMetadata.

    Args:
        blob_id (str): Blob ID the metadata belongs to, URL-safe base64.
    """

    blob_id: str


@dataclasses.dataclass(kw_only=True, frozen=True)
class SignedConfirmation:
    """A storage node's signed confirmation that it holds a blob's slivers.

    Used by: GetStorageConfirmation.

    ``serialized_message`` is passed VERBATIM into ``certify_blob``; the
    client never reconstructs it.

    Args:
        serialized_message (bytes): The BCS-encoded message the node signed.
        signature (bytes): The node's signature over ``serialized_message``.
    """

    serialized_message: bytes
    signature: bytes


@dataclasses.dataclass(kw_only=True)
class PutSliver(WalrusCommand):
    """Store a primary or secondary sliver at a storage node.

    PUT {base_url}/v1/blobs/{blob_id}/slivers/{sliver_pair_index}/{sliver_type}

    ``base_url`` is a specific storage node's address resolved from the
    committee -- see :attr:`endpoint_role`.

    Args:
        blob_id (bytes): Raw 32-byte blob ID.
        sliver_pair_index (int): Sliver-pair index to store at.
        sliver_type (str): ``"primary"`` or ``"secondary"``, lowercase.
            These literals are the upstream ``Axis`` serde values -- the
            wire format is case-sensitive and accepts nothing else.
        data (bytes): Raw BCS-encoded sliver bytes.
    """

    endpoint_role: ClassVar[str] = "storage_node"

    blob_id: bytes
    sliver_pair_index: int
    sliver_type: str
    data: bytes

    def __post_init__(self) -> None:
        """Validate ``sliver_type`` against the upstream ``Axis`` values.

        Raises:
            ValueError: If ``sliver_type`` is not exactly ``"primary"`` or
                ``"secondary"``.
        """
        if self.sliver_type not in _VALID_SLIVER_TYPES:
            raise ValueError(
                f"sliver_type must be one of {_VALID_SLIVER_TYPES!r}, "
                f"got {self.sliver_type!r}"
            )

    def http_method(self) -> str:
        return "PUT"

    def url_path(self, base_url: str) -> str:
        blob_id_b64 = blob_id_to_url_base64(blob_id=self.blob_id)
        return (
            f"{base_url}/v1/blobs/{blob_id_b64}/slivers/"
            f"{self.sliver_pair_index}/{self.sliver_type}"
        )

    def request_body(self) -> bytes | None:
        # Raw BCS bytes; the node imposes no Content-Type requirement.
        return self.data

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            context = (
                f"blob_id={blob_id_to_url_base64(blob_id=self.blob_id)} "
                f"sliver_pair_index={self.sliver_pair_index} "
                f"sliver_type={self.sliver_type}"
            )
            return SuiRpcResult(
                False, _http_failure_message(response=response, context=context)
            )
        return SuiRpcResult(
            True,
            "",
            SliverAck(
                blob_id=blob_id_to_url_base64(blob_id=self.blob_id),
                sliver_pair_index=self.sliver_pair_index,
                sliver_type=self.sliver_type,
            ),
        )


@dataclasses.dataclass(kw_only=True)
class PutMetadata(WalrusCommand):
    """Store a blob's Red Stuff metadata at a storage node.

    PUT {base_url}/v1/blobs/{blob_id}/metadata

    A storage node requires this metadata to be stored BEFORE it will accept
    any sliver PUT for the same blob -- a node that has not received it
    rejects every sliver PUT with HTTP 400 FAILED_PRECONDITION /
    METADATA_NOT_FOUND. See
    :func:`~pytusk.core.native_upload._upload_node`, which issues this
    command first for each node and abandons the whole node (no sliver PUT
    attempted) if it fails.

    ``base_url`` is a specific storage node's address resolved from the
    committee -- see :attr:`endpoint_role`.

    Args:
        blob_id (bytes): Raw 32-byte blob ID.
        metadata_bcs (bytes): BCS-encoded Walrus ``BlobMetadata`` payload.
    """

    endpoint_role: ClassVar[str] = "storage_node"

    blob_id: bytes
    metadata_bcs: bytes

    def http_method(self) -> str:
        return "PUT"

    def url_path(self, base_url: str) -> str:
        blob_id_b64 = blob_id_to_url_base64(blob_id=self.blob_id)
        return f"{base_url}/v1/blobs/{blob_id_b64}/metadata"

    def request_body(self) -> bytes | None:
        # Raw BCS bytes; the node imposes no Content-Type requirement.
        return self.metadata_bcs

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        # 200 (already stored), 201 (stored) and 202 (buffered pending
        # registration) are ALL success -- see the class docstring. Using
        # response.is_error (True only for status >= 400) rather than an
        # explicit status-code allowlist naturally treats all three as
        # success, matching PutSliver's "not response.is_error" convention.
        if response.is_error:
            context = f"blob_id={blob_id_to_url_base64(blob_id=self.blob_id)}"
            return SuiRpcResult(
                False, _http_failure_message(response=response, context=context)
            )
        return SuiRpcResult(
            True,
            "",
            MetadataAck(blob_id=blob_id_to_url_base64(blob_id=self.blob_id)),
        )


@dataclasses.dataclass(kw_only=True)
class GetStorageConfirmation(WalrusCommand):
    """Fetch a storage node's signed confirmation for a blob's slivers.

    GET {base_url}/v1/blobs/{blob_id}/confirmation/permanent
    GET {base_url}/v1/blobs/{blob_id}/confirmation/deletable/{object_id}

    These are TWO SEPARATE upstream routes, not one route with a
    discriminator query parameter; the object id travels in the PATH.

    ``base_url`` is a specific storage node's address resolved from the
    committee -- see :attr:`endpoint_role`.

    Args:
        blob_id (bytes): Raw 32-byte blob ID.
        object_id (str | None): Sui object ID of the deletable ``Blob``
            object. None selects the permanent-blob route.
        wait_for_registration (bool): When True, the node long-polls until
            it observes the on-chain registration event before responding.
            This is SERVER-SIDE long-polling that replaces client-side
            backoff for the registration-propagation race.
        wait_millis (int | None): Upper bound in milliseconds for the
            node's long-poll wait. Omitted from the request when None.
    """

    endpoint_role: ClassVar[str] = "storage_node"

    blob_id: bytes
    object_id: str | None = None
    wait_for_registration: bool = True
    wait_millis: int | None = None

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        blob_id_b64 = blob_id_to_url_base64(blob_id=self.blob_id)
        if self.object_id is None:
            return f"{base_url}/v1/blobs/{blob_id_b64}/confirmation/permanent"
        return (
            f"{base_url}/v1/blobs/{blob_id_b64}/confirmation/deletable/{self.object_id}"
        )

    def query_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "wait_for_registration": str(self.wait_for_registration).lower()
        }
        if self.wait_millis is not None:
            params["wait_millis"] = self.wait_millis
        return params

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        if response.is_error:
            context = (
                f"blob_id={blob_id_to_url_base64(blob_id=self.blob_id)} "
                f"object_id={self.object_id!r}"
            )
            return SuiRpcResult(
                False, _http_failure_message(response=response, context=context)
            )

        data = response.json()
        success = data.get("success") if isinstance(data, dict) else None
        if not isinstance(success, dict):
            return SuiRpcResult(False, f"Unexpected confirmation response: {data}")
        inner = success.get("data")
        if not isinstance(inner, dict):
            return SuiRpcResult(
                False, f"Unexpected confirmation response: missing 'data': {data}"
            )
        signed = inner.get("signed")
        if not isinstance(signed, dict):
            return SuiRpcResult(
                False, f"Unexpected confirmation response: missing 'signed': {data}"
            )
        serialized_message_b64 = signed.get("serializedMessage")
        signature_b64 = signed.get("signature")
        if not isinstance(serialized_message_b64, str) or not isinstance(
            signature_b64, str
        ):
            return SuiRpcResult(
                False,
                "Unexpected confirmation response: missing 'serializedMessage' or "
                f"'signature': {data}",
            )

        # Standard PADDED base64 here -- deliberately different from the
        # URL-safe unpadded alphabet used for blob_id in the request path.
        # See pytusk.core.encoding for the split.
        return SuiRpcResult(
            True,
            "",
            SignedConfirmation(
                serialized_message=decode_standard_base64(value=serialized_message_b64),
                signature=decode_standard_base64(value=signature_b64),
            ),
        )
