#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""WalrusCommand ABC and response dataclasses."""

import dataclasses
import json
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import httpx
from dataclasses_json import DataClassJsonMixin
from pysui import SuiRpcResult

# --- HTTP ERROR-BODY DIAGNOSTICS ----------------------------------------
# Captures and truncates HTTP error bodies so failures are diagnosable from
# the log line alone -- this is how INVALID_CONTENT_TYPE and NOT_REGISTERED
# failures were identified against live storage nodes. This bound keeps a
# flood of identical failure bodies (one per rejected sliver, across ~100
# nodes) from blowing up the log; a truncated body is still diagnostic, an
# 8000-line log is not.
#
# These live on the base module rather than in node_commands, where they
# were written, because at Plan #28 step 11 every command module converged
# on this one failure idiom. Read, write and relay commands each had their
# own thinner variant; a helper shared by all four modules belongs beside
# the ABC they all import, not inside one peer module the others would have
# to reach sideways into.
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
    # The body is written by an UNTRUSTED server and this string reaches a
    # terminal. ANSI escape sequences start with ESC (0x1b), so without
    # this a hostile server could clear the screen and forge output around
    # the diagnostic. Tab and newline are kept -- they carry real structure
    # in a JSON or HTML error body and neither moves the cursor.
    body = "".join(
        char if char in "\t\n" or char.isprintable() else " " for char in body
    )
    if len(body) > _MAX_ERROR_BODY_CHARS:
        body = f"{body[:_MAX_ERROR_BODY_CHARS]}...[truncated]"
    return body


def http_failure_message(*, response: httpx.Response, context: str) -> str:
    """Build a diagnostic failure message from a non-2xx response.

    Combines the HTTP status code and reason phrase, the request URL, the
    caller-supplied identifying context (e.g. blob/sliver identity), the
    AIP-193 ``reason`` detail when present (see :func:`error_reason`), and
    the truncated response body -- everything needed to diagnose a rejection
    from the log line alone, without re-running the request.

    THE SINGLE FAILURE IDIOM for every Walrus command. Renamed from
    ``_http_failure_message`` and made public at Plan #28 step 11, when the
    read, write and relay modules adopted it: a symbol crossing a module
    boundary does not keep a leading underscore.

    Args:
        response (httpx.Response): The raw HTTP response.
        context (str): Caller-supplied identifying context, e.g.
            ``"blob_id=... sliver_pair_index=... sliver_type=..."``. Each
            command supplies the identity a reader needs to tell WHICH
            request failed, which a status code alone cannot.

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


class StorageNodeEnvelopeError(ValueError):
    """Raised when a storage-node 2xx body is not a valid success envelope.

    Public rather than underscore-prefixed because it crosses a module
    boundary -- callers catch it by name. Same reasoning that made
    :func:`http_failure_message` public at Plan #28 step 11.
    """


def unwrap_storage_node_envelope(
    *, response: httpx.Response, context: str
) -> dict[str, object] | str:
    """Return the ``data`` member of a storage-node success envelope.

    Storage nodes wrap every 2xx payload::

        {"success": {"code": 200, "data": <payload>}}

    ``data`` is USUALLY an object but is NOT always: an externally tagged
    Rust enum with a unit variant serialises as a bare JSON string. The
    blob-status ``"nonexistent"`` variant is the live example, verified
    against testnet 2026-09-01. The return type is a union for exactly that
    reason, and callers MUST branch on ``str`` versus ``dict`` rather than
    assuming an object.

    THIS IS THE STORAGE-NODE ENVELOPE ONLY. Publisher responses
    (``StoreBlob``/``StoreQuilt``) use a different ``newlyCreated`` /
    ``alreadyCertified`` shape with no ``success`` wrapper and must never be
    passed here. Error bodies are not wrapped in ``success`` either -- they
    carry the AIP-193 shape :func:`error_reason` parses -- so call this only
    after ``response.is_error`` has been checked.

    Args:
        response (httpx.Response): A non-error storage-node response.
        context (str): Caller-supplied identifying context for diagnostics,
            following the same convention as :func:`http_failure_message`.

    Returns:
        dict[str, object] | str: The unwrapped ``data`` member.

    Raises:
        StorageNodeEnvelopeError: If the body is not JSON, is not an object,
            or lacks a well-formed ``success``/``data`` envelope.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise StorageNodeEnvelopeError(
            f"Malformed storage-node response body [{context}]: {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise StorageNodeEnvelopeError(
            f"Unexpected storage-node response [{context}]: {body}"
        )
    success = body.get("success")
    if not isinstance(success, dict):
        raise StorageNodeEnvelopeError(
            f"Unexpected storage-node response, missing 'success' [{context}]: {body}"
        )
    if "data" not in success:
        raise StorageNodeEnvelopeError(
            f"Unexpected storage-node response, missing 'data' [{context}]: {body}"
        )
    data = success["data"]
    if not isinstance(data, (dict, str)):
        raise StorageNodeEnvelopeError(
            f"Unexpected storage-node 'data' member [{context}]: {data!r}"
        )
    return data


@dataclasses.dataclass(kw_only=True)
class WalrusCommand(ABC):
    """Abstract base class for all Walrus HTTP operations.

    Subclasses declare their input fields as dataclass fields and implement
    the abstract methods to describe the HTTP request and parse the response.
    """

    endpoint_role: ClassVar[str] = "aggregator"
    """Selects which base URL the client resolves for this command.

    Valid values:
        "aggregator" (default): Resolved from the active network's
            configured aggregator URL. Used by read commands.
        "publisher": Resolved from the active network's configured
            publisher URL. Used by write commands that go through the
            publisher daemon.
        "storage_node": NOT resolved from configuration. The target host
            is per-call data derived from the committee (a specific
            storage node's network address), not a fixed configured
            endpoint, so callers MUST supply an explicit ``base_url`` to
            :meth:`WalrusClient.execute`. Dispatch raises ``ValueError``
            if one is not supplied.
        "relay": NOT resolved from configuration either, for a different
            reason: a network configures a LIST of named upload relays
            rather than a single URL, so there is nothing unambiguous to
            fall back on. Callers resolve one by name with
            ``PytuskConfiguration.relay_url_for()`` and pass it as an
            explicit ``base_url``. Dispatch raises ``ValueError`` if one is
            not supplied -- without that branch a relay command would fall
            through to the aggregator URL and silently post to the wrong
            host.
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
            base_url (str): Walrus aggregator or publisher base URL, selected by
                the client according to this command's endpoint_role.

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

    Used by: ReadQuiltPatch, ReadQuiltPatchById.

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
        expiry_epoch (int): Walrus epoch at which the blob expires.
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
        expiry_epoch (int): Walrus epoch at which the quilt expires.
        object_id (str): Sui object ID of the stored quilt blob.
    """

    quilt_id: str = dataclasses.field(default="")
    patch_keys: list[str] = dataclasses.field(default_factory=list)
    cost: int = dataclasses.field(default=0)
    expiry_epoch: int = dataclasses.field(default=0)
    object_id: str = dataclasses.field(default="")


@dataclasses.dataclass
class QuiltPatchItem(DataClassJsonMixin):
    """One patch's identity within a quilt.

    Used by: ListQuiltPatches.

    Args:
        patch_key (str): Patch key assigned within the quilt. Named to
            match ``QuiltReceipt.patch_keys`` and ``read_quilt``'s
            ``--patch-key`` argument, though the aggregator's own wire
            field is ``identifier``.
        patch_id (str): Walrus QuiltPatchId (URL-safe base64) addressing
            this patch directly, independent of its containing quilt.
        tags (dict[str, str]): Tags attached to the patch.
    """

    patch_key: str = dataclasses.field(default="")
    patch_id: str = dataclasses.field(default="")
    tags: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class QuiltPatchListing(DataClassJsonMixin):
    """Patches contained in a quilt, as returned by list-patches-in-quilt.

    Used by: ListQuiltPatches.

    Args:
        patches (list[QuiltPatchItem]): One entry per patch in the quilt.
    """

    patches: list[QuiltPatchItem] = dataclasses.field(default_factory=list)
