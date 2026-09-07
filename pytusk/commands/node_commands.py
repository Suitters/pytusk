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

import binascii
import dataclasses
from typing import ClassVar

import httpx
from pysui import SuiRpcResult

from pytusk.commands.walrus_command import (
    StorageNodeEnvelopeError,
    WalrusCommand,
    http_failure_message,
    unwrap_storage_node_envelope,
)
from pytusk.core.encoding import blob_id_to_url_base64, decode_standard_base64
from pytusk.core.types.blob_status import (
    DeletableCounts,
    DeletableStatus,
    EventRef,
    InvalidStatus,
    NonexistentStatus,
    PermanentStatus,
)

_VALID_SLIVER_TYPES = ("primary", "secondary")

# The error-body diagnostics that used to live here -- `_MAX_ERROR_BODY_CHARS`,
# `_response_body_for_log`, `http_failure_message` and `error_reason` -- moved
# to `pytusk.commands.walrus_command` at Plan #28 step 11, when the read,
# write and relay modules adopted the same idiom. This module imports only
# `http_failure_message`, the one helper it still uses. `error_reason` is no
# longer reachable through this module: `pytusk/__init__.py` re-exports it
# directly from `walrus_command`. Its AIP-193 contract is unchanged.




@dataclasses.dataclass(kw_only=True, frozen=True)
class SliverAck:
    """Acknowledgement that a storage node accepted a sliver PUT.

    Used by: PutSliver.

    Attributes:
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

    Attributes:
        blob_id (str): Blob ID the metadata belongs to, URL-safe base64.
    """

    blob_id: str


@dataclasses.dataclass(kw_only=True, frozen=True)
class SignedConfirmation:
    """A storage node's signed confirmation that it holds a blob's slivers.

    Used by: GetStorageConfirmation.

    ``serialized_message`` is passed VERBATIM into ``certify_blob``; the
    client never reconstructs it.

    Attributes:
        serialized_message (bytes): The BCS-encoded message the node signed.
        signature (bytes): The node's signature over ``serialized_message``.
    """

    serialized_message: bytes
    signature: bytes


@dataclasses.dataclass(kw_only=True, frozen=True)
class SliverData:
    """Raw sliver bytes returned by a storage-node sliver GET.

    Attributes:
        content (bytes): The sliver's raw BCS bytes, exactly as the node
            returned them. Not decoded here -- decoding is the caller's job.
    """

    content: bytes


@dataclasses.dataclass(kw_only=True, frozen=True)
class MetadataData:
    """Raw blob-metadata bytes returned by a storage-node metadata GET.

    Attributes:
        content (bytes): Raw BCS bytes of the OUTER ``BlobMetadataWithId``,
            exactly as the node returned them. This is the form
            :func:`~pytusk.core.encoding.redstuff.verify_blob_metadata`
            expects, and differs from the inner ``BlobMetadata`` that a
            metadata PUT sends by a leading 32-byte blob ID.
    """

    content: bytes


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
                False, http_failure_message(response=response, context=context)
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
    :func:`~pytusk.core.native_upload.fanout._upload_node`, which issues
    this command first for each node and abandons the whole node (no sliver
    PUT attempted) if it fails.

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
                False, http_failure_message(response=response, context=context)
            )
        return SuiRpcResult(
            True,
            "",
            MetadataAck(blob_id=blob_id_to_url_base64(blob_id=self.blob_id)),
        )


@dataclasses.dataclass(kw_only=True)
class GetSliver(WalrusCommand):
    """Fetch a primary or secondary sliver from a storage node.

    ``GET {base_url}/v1/blobs/{blob_id}/slivers/{sliver_pair_index}/{sliver_type}``

    The response body is RAW BCS bytes with ``Content-Type:
    application/octet-stream`` and NO JSON envelope. This is the same wire
    form :class:`PutSliver` sends, and is deliberately NOT parsed with
    ``unwrap_storage_node_envelope`` -- that helper is for the status and
    confirmation endpoints, which do use the ``{success, data}`` JSON
    envelope. Using it here would corrupt a sliver whose bytes happen to
    parse as JSON.

    ``sliver_pair_index`` is a SLIVER-PAIR index, not a shard index; the two
    differ by a per-blob rotation (see
    :func:`~pytusk.core.encoding.redstuff.shard_index_to_pair_index`).

    Attributes:
        blob_id (bytes): Raw 32-byte blob ID.
        sliver_pair_index (int): Sliver-pair index to fetch.
        sliver_type (str): ``"primary"`` or ``"secondary"``.
    """

    endpoint_role: ClassVar[str] = "storage_node"

    blob_id: bytes
    sliver_pair_index: int
    sliver_type: str
    max_bytes: int | None = None

    def __post_init__(self) -> None:
        """Validate the sliver type.

        Raises:
            ValueError: If ``sliver_type`` is not a known axis.
        """
        if self.sliver_type not in _VALID_SLIVER_TYPES:
            raise ValueError(
                f"sliver_type must be one of {_VALID_SLIVER_TYPES!r}, "
                f"got {self.sliver_type!r}"
            )

    def http_method(self) -> str:
        """Return the HTTP method.

        Returns:
            str: Always ``"GET"``.
        """
        return "GET"

    def max_response_bytes(self) -> int | None:
        """Return the caller-supplied response cap, if any.

        Returns:
            int | None: ``max_bytes`` as given, or None for no limit.
        """
        return self.max_bytes

    def url_path(self, base_url: str) -> str:
        """Build the sliver URL for a storage node.

        Args:
            base_url (str): Storage node base URL.

        Returns:
            str: The fully-qualified sliver URL.
        """
        blob_id_b64 = blob_id_to_url_base64(blob_id=self.blob_id)
        return (
            f"{base_url}/v1/blobs/{blob_id_b64}/slivers/"
            f"{self.sliver_pair_index}/{self.sliver_type}"
        )

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        """Return the sliver's raw bytes, unmodified.

        Args:
            response (httpx.Response): The node's response.

        Returns:
            SuiRpcResult: Carrying :class:`SliverData` on success.
        """
        if response.is_error:
            context = (
                f"blob_id={blob_id_to_url_base64(blob_id=self.blob_id)} "
                f"sliver_pair_index={self.sliver_pair_index} "
                f"sliver_type={self.sliver_type}"
            )
            return SuiRpcResult(
                False, http_failure_message(response=response, context=context)
            )
        return SuiRpcResult(True, "", SliverData(content=response.content))


@dataclasses.dataclass(kw_only=True)
class GetMetadata(WalrusCommand):
    """Fetch a blob's metadata from a storage node.

    ``GET {base_url}/v1/blobs/{blob_id}/metadata``

    Returns the OUTER ``BlobMetadataWithId`` as raw BCS bytes, with no JSON
    envelope -- see :class:`GetSliver` for why
    ``unwrap_storage_node_envelope`` must not be used on this response.

    Attributes:
        blob_id (bytes): Raw 32-byte blob ID.
    """

    endpoint_role: ClassVar[str] = "storage_node"

    blob_id: bytes
    max_bytes: int | None = None

    def http_method(self) -> str:
        """Return the HTTP method.

        Returns:
            str: Always ``"GET"``.
        """
        return "GET"

    def max_response_bytes(self) -> int | None:
        """Return the caller-supplied response cap, if any.

        Returns:
            int | None: ``max_bytes`` as given, or None for no limit.
        """
        return self.max_bytes

    def url_path(self, base_url: str) -> str:
        """Build the metadata URL for a storage node.

        Args:
            base_url (str): Storage node base URL.

        Returns:
            str: The fully-qualified metadata URL.
        """
        blob_id_b64 = blob_id_to_url_base64(blob_id=self.blob_id)
        return f"{base_url}/v1/blobs/{blob_id_b64}/metadata"

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        """Return the metadata's raw bytes, unmodified.

        Args:
            response (httpx.Response): The node's response.

        Returns:
            SuiRpcResult: Carrying :class:`MetadataData` on success.
        """
        if response.is_error:
            context = f"blob_id={blob_id_to_url_base64(blob_id=self.blob_id)}"
            return SuiRpcResult(
                False, http_failure_message(response=response, context=context)
            )
        return SuiRpcResult(True, "", MetadataData(content=response.content))


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

    def query_params(self) -> dict[str, str | int]:
        params: dict[str, str | int] = {
            "wait_for_registration": str(self.wait_for_registration).lower()
        }
        if self.wait_millis is not None:
            params["wait_millis"] = self.wait_millis
        return params

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        context = (
            f"blob_id={blob_id_to_url_base64(blob_id=self.blob_id)} "
            f"object_id={self.object_id!r}"
        )
        if response.is_error:
            return SuiRpcResult(
                False, http_failure_message(response=response, context=context)
            )

        # The `success`/`data` unwrap is the shared storage-node envelope,
        # not a confirmation concern -- see unwrap_storage_node_envelope.
        # Everything below the unwrap IS confirmation-specific payload shape.
        try:
            inner = unwrap_storage_node_envelope(
                response=response, context=context
            )
        except StorageNodeEnvelopeError as exc:
            return SuiRpcResult(False, str(exc))
        if not isinstance(inner, dict):
            return SuiRpcResult(
                False,
                f"Unexpected confirmation response: 'data' is not an object "
                f"[{context}]: {inner!r}",
            )
        signed = inner.get("signed")
        if not isinstance(signed, dict):
            return SuiRpcResult(
                False,
                f"Unexpected confirmation response: missing 'signed' "
                f"[{context}]: {inner}",
            )
        serialized_message_b64 = signed.get("serializedMessage")
        signature_b64 = signed.get("signature")
        if not isinstance(serialized_message_b64, str) or not isinstance(
            signature_b64, str
        ):
            return SuiRpcResult(
                False,
                "Unexpected confirmation response: missing 'serializedMessage' or "
                f"'signature' [{context}]: {inner}",
            )

        # Standard PADDED base64 here -- deliberately different from the
        # URL-safe unpadded alphabet used for blob_id in the request path.
        # See pytusk.core.encoding for the split.
        try:
            return SuiRpcResult(
                True,
                "",
                SignedConfirmation(
                    serialized_message=decode_standard_base64(
                        value=serialized_message_b64
                    ),
                    signature=decode_standard_base64(value=signature_b64),
                ),
            )
        except (binascii.Error, ValueError) as exc:
            # base64.b64decode raises binascii.Error (a ValueError
            # subclass) on malformed input -- caught explicitly here since
            # one bad node response must not raise out of parse_response
            # and abort the whole confirmation fan-out (see
            # collect_confirmations / _confirm_node).
            return SuiRpcResult(
                False,
                f"Malformed confirmation response signature encoding: {exc}",
            )


def _parse_event_ref(*, raw: object, context: str) -> EventRef:
    """Build an :class:`EventRef` from a node's event object.

    ``eventSeq`` arrives as a JSON STRING -- Walrus serialises ``u64`` that
    way for human-readable formats -- so it is converted here rather than
    left for callers to trip over.

    Args:
        raw (object): The decoded ``statusEvent``/``event`` member.
        context (str): Diagnostic context for error messages.

    Returns:
        EventRef: The normalised reference.

    Raises:
        TypeError: If the shape is wrong (wrong JSON type, missing member).
        ValueError: If the sequence encoding is unusable. Both are converted
            by the caller into a failed ``SuiRpcResult``; neither escapes
            ``parse_response``.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"event is not an object [{context}]: {raw!r}")
    tx_digest = raw.get("txDigest")
    event_seq = raw.get("eventSeq")
    if not isinstance(tx_digest, str):
        raise TypeError(f"event missing 'txDigest' [{context}]: {raw}")
    # Accept int defensively: the wire form is a string, but a numeric
    # encoding would still be unambiguous and rejecting it would buy
    # nothing.
    if not isinstance(event_seq, (str, int)) or isinstance(event_seq, bool):
        raise TypeError(f"event missing 'eventSeq' [{context}]: {raw}")
    try:
        return EventRef(tx_digest=tx_digest, event_seq=int(event_seq))
    except ValueError as exc:
        raise ValueError(f"event has non-integer 'eventSeq' [{context}]: {exc}") from exc


def _parse_deletable_counts(*, raw: object, context: str) -> DeletableCounts:
    """Build :class:`DeletableCounts` from a node's ``deletableCounts`` object.

    NOTE the mixed casing, which is a real property of the wire format and
    not a transcription slip: the KEY is camelCase (``deletableCounts``)
    while its CHILDREN are snake_case (``count_deletable_total``,
    ``count_deletable_certified``). Verified against a live testnet node.

    Args:
        raw (object): The decoded ``deletableCounts`` member.
        context (str): Diagnostic context for error messages.

    Returns:
        DeletableCounts: The parsed counts.

    Raises:
        TypeError: If the shape is unusable.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"deletableCounts is not an object [{context}]: {raw!r}")
    total = raw.get("count_deletable_total")
    certified = raw.get("count_deletable_certified")
    if not isinstance(total, int) or isinstance(total, bool):
        raise TypeError(
            f"deletableCounts missing 'count_deletable_total' [{context}]: {raw}"
        )
    if not isinstance(certified, int) or isinstance(certified, bool):
        raise TypeError(
            f"deletableCounts missing 'count_deletable_certified' [{context}]: {raw}"
        )
    return DeletableCounts(total=total, certified=certified)


def _parse_optional_epoch(*, raw: object, field: str, context: str) -> int | None:
    """Read an ``Option<Epoch>`` member, which is genuinely nullable.

    Args:
        raw (object): The decoded member, possibly ``None`` or absent.
        field (str): Member name, for error messages.
        context (str): Diagnostic context for error messages.

    Returns:
        int | None: The epoch, or None when unset.

    Raises:
        TypeError: If present but not an integer.
    """
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise TypeError(f"{field} is not an integer [{context}]: {raw!r}")
    return raw


@dataclasses.dataclass(kw_only=True)
class GetBlobStatus(WalrusCommand):
    """Ask ONE storage node for its view of a blob's status.

    ``GET /v1/blobs/{blob_id}/status``. This is a per-node opinion, never a
    verdict: a single node can be stale, byzantine, or simply not yet aware
    of a recent registration. Establishing a verdict requires fanning this
    command across the committee and applying a shard-weight threshold --
    see the blob-status orchestration in :mod:`pytusk.core.ops`.

    ``base_url`` is a specific storage node's address resolved from the
    committee -- see :attr:`endpoint_role`.

    Parse failures come back as a failed ``SuiRpcResult`` rather than
    raising, matching :class:`GetStorageConfirmation`: one bad node
    response must never abort the whole fan-out.

    Attributes:
        blob_id (bytes): Raw 32-byte blob ID.
    """

    endpoint_role: ClassVar[str] = "storage_node"

    blob_id: bytes

    def http_method(self) -> str:
        return "GET"

    def url_path(self, base_url: str) -> str:
        blob_id_b64 = blob_id_to_url_base64(blob_id=self.blob_id)
        return f"{base_url}/v1/blobs/{blob_id_b64}/status"

    def parse_response(self, response: httpx.Response) -> SuiRpcResult:
        context = f"blob_id={blob_id_to_url_base64(blob_id=self.blob_id)}"
        if response.is_error:
            return SuiRpcResult(
                False, http_failure_message(response=response, context=context)
            )

        try:
            data = unwrap_storage_node_envelope(response=response, context=context)
        except StorageNodeEnvelopeError as exc:
            return SuiRpcResult(False, str(exc))

        # Externally tagged serde enum: a UNIT variant is a bare string,
        # every other variant a single-key object. Both are valid; assuming
        # an object here breaks on a legitimate `nonexistent` response.
        if isinstance(data, str):
            if data == "nonexistent":
                return SuiRpcResult(True, "", NonexistentStatus())
            return SuiRpcResult(
                False, f"Unknown blob-status variant [{context}]: {data!r}"
            )

        if len(data) != 1:
            return SuiRpcResult(
                False,
                f"Expected exactly one blob-status variant [{context}]: "
                f"{sorted(data)}",
            )
        variant, payload = next(iter(data.items()))
        if not isinstance(payload, dict):
            return SuiRpcResult(
                False,
                f"blob-status variant {variant!r} payload is not an object "
                f"[{context}]: {payload!r}",
            )

        try:
            if variant == "permanent":
                end_epoch = payload.get("endEpoch")
                is_certified = payload.get("isCertified")
                if not isinstance(end_epoch, int) or isinstance(end_epoch, bool):
                    raise ValueError(f"missing 'endEpoch' [{context}]: {payload}")
                if not isinstance(is_certified, bool):
                    raise ValueError(f"missing 'isCertified' [{context}]: {payload}")
                return SuiRpcResult(
                    True,
                    "",
                    PermanentStatus(
                        end_epoch=end_epoch,
                        is_certified=is_certified,
                        status_event=_parse_event_ref(
                            raw=payload.get("statusEvent"), context=context
                        ),
                        deletable_counts=_parse_deletable_counts(
                            raw=payload.get("deletableCounts"), context=context
                        ),
                        initial_certified_epoch=_parse_optional_epoch(
                            raw=payload.get("initialCertifiedEpoch"),
                            field="initialCertifiedEpoch",
                            context=context,
                        ),
                    ),
                )
            if variant == "deletable":
                return SuiRpcResult(
                    True,
                    "",
                    DeletableStatus(
                        deletable_counts=_parse_deletable_counts(
                            raw=payload.get("deletableCounts"), context=context
                        ),
                        initial_certified_epoch=_parse_optional_epoch(
                            raw=payload.get("initialCertifiedEpoch"),
                            field="initialCertifiedEpoch",
                            context=context,
                        ),
                    ),
                )
            if variant == "invalid":
                return SuiRpcResult(
                    True,
                    "",
                    InvalidStatus(
                        status_event=_parse_event_ref(
                            raw=payload.get("event"), context=context
                        )
                    ),
                )
        except (TypeError, ValueError) as exc:
            # TypeError for a wrong-shaped member, ValueError for a
            # well-shaped one that will not convert. Neither may escape:
            # one bad node response must not abort the fan-out.
            return SuiRpcResult(
                False, f"Malformed blob-status {variant!r} payload: {exc}"
            )

        return SuiRpcResult(False, f"Unknown blob-status variant [{context}]: {variant!r}")
