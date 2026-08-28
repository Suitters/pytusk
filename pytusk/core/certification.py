#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Walrus ``certify_blob`` wire-format concerns.

This module owns everything needed to turn a set of per-node storage
confirmations into a certificate that ``certify_blob`` will accept:
signer-bitmap packing, quorum arithmetic, storage-confirmation verification,
and BLS aggregation.

CRITICAL ARCHITECTURAL CONSTRAINT: this module must never import from
``pytusk.core.committee``. ``committee.py`` imports the signer-bitmap
functions FROM here for backward-compatible re-export, and a cycle must not
be allowed to form. This module may depend on ``pytusk.core.encoding`` (a
one-way dependency, not a cycle) and on ``pysui_fastcrypto`` and the standard
library -- nothing else in pytusk.

BLS FAILURE-MODE NOTE: ``pysui_fastcrypto``'s BLS functions (``bls_verify``,
``bls_aggregate``, ``bls_aggregate_verify``) have TWO distinct failure modes,
per their type stubs: they return ``False`` when inputs are well-formed but a
signature does not verify, and they RAISE ``ValueError`` when an input cannot
be parsed at all (e.g. a corrupted signature that is not a valid G2 point
encoding, or a malformed public key). Storage-node confirmations come from
~101 untrusted nodes, so malformed bytes are expected input, not an
exceptional case. This module deliberately normalises both failure modes
into its own error/boolean contract (``InvalidConfirmationError`` in
``build_certificate``, plain ``False`` in the ``verify_*`` predicates) --
do not let a raw ``ValueError`` from the extension leak through either path.
"""

import dataclasses
import logging
from collections.abc import Iterable, Sequence

from pysui_fastcrypto import (
    bls_aggregate,
    bls_aggregate_verify,
    bls_confirmation_bytes,
    bls_verify,
)

_logger = logging.getLogger(__name__)

__all__ = [
    "Certificate",
    "ConfirmationMismatchError",
    "InvalidConfirmationError",
    "NodeConfirmation",
    "QuorumNotReachedError",
    "build_certificate",
    "confirmation_message",
    "is_quorum",
    "min_weight_for_quorum",
    "pack_signers_bitmap",
    "unpack_signers_bitmap",
    "verify_certificate",
    "verify_confirmation",
]


class ConfirmationMismatchError(ValueError):
    """Raised when storage nodes returned differing confirmation messages.

    The client passes exactly ONE node's ``serialized_message`` bytes,
    verbatim, into ``certify_blob`` -- see :class:`NodeConfirmation`. If the
    nodes being certified together do not agree on that message, there is no
    single message the resulting certificate can be meaningful against, and
    building one must fail before any signature work is wasted on it.
    """


class InvalidConfirmationError(ValueError):
    """Raised when a node's signature fails verification against its key.

    This means the storage node's committee public key, as resolved by the
    caller, does not authenticate the signature it returned over the expected
    confirmation message.
    """


class QuorumNotReachedError(RuntimeError):
    """Raised when accumulated signer weight is below the quorum threshold."""


@dataclasses.dataclass(kw_only=True, frozen=True)
class NodeConfirmation:
    """A single storage node's signed confirmation of a stored blob.

    ``position`` is the node's COMMITTEE POSITION -- the index within the
    committee ordering that ``signers_bitmap`` indexes (see
    ``pytusk.core.committee.WalrusCommittee``) -- and is neither the node's
    ID nor a shard index. ``serialized_message`` is taken verbatim from the
    node's response and is NEVER reconstructed by the client; it is what
    :func:`build_certificate` compares across nodes and what ultimately flows
    unmodified into ``certify_blob``.

    Attributes:
        node_id (str): On-chain object ID of the node's staking pool.
        position (int): Zero-based committee position -- the
            ``signers_bitmap`` index for this node.
        weight (int): This node's shard count, i.e. its contribution toward
            quorum weight.
        public_key (bytes): The node's committee BLS public key, either the
            96-byte uncompressed or the 48-byte compressed encoding.
        serialized_message (bytes): The confirmation message bytes exactly as
            returned by the node.
        signature (bytes): The node's BLS signature over
            ``serialized_message``.
    """

    node_id: str
    position: int
    weight: int
    public_key: bytes
    serialized_message: bytes
    signature: bytes


@dataclasses.dataclass(kw_only=True, frozen=True)
class Certificate:
    """A quorum-backed certificate ready for submission to ``certify_blob``.

    These fields map directly onto the on-chain entry point's signature:
    ``certify_blob(blob, signature, signers_bitmap, message)``, where
    ``signature`` is :attr:`aggregate_signature` and ``message`` is
    :attr:`serialized_message`.

    Attributes:
        serialized_message (bytes): The agreed confirmation message every
            signer signed.
        aggregate_signature (bytes): The BLS aggregate of every signer's
            signature over ``serialized_message``.
        signers_bitmap (bytes): Packed bitmap of signer committee positions,
            as produced by :func:`pack_signers_bitmap`.
        signer_positions (tuple[int, ...]): Ascending, deduplicated committee
            positions of the signers contributing to this certificate.
        weight (int): Total shard weight contributed by the signers.
    """

    serialized_message: bytes
    aggregate_signature: bytes
    signers_bitmap: bytes
    signer_positions: tuple[int, ...]
    weight: int


def pack_signers_bitmap(
    *, signer_positions: Iterable[int], committee_size: int
) -> bytes:
    """Pack committee positions into a Walrus ``signers_bitmap``.

    Allocates ``ceil(committee_size / 8)`` zero bytes, then sets bit
    ``position % 8`` of byte ``position // 8`` for each signer, LSB-first within
    each byte. This matches the on-chain encoding consumed by ``certify_blob``.

    Args:
        signer_positions (Iterable[int]): Zero-based POSITIONS WITHIN THE
            COMMITTEE ORDERING of the nodes that signed. These are not node IDs
            and not shard indices.
        committee_size (int): Number of committee members. Determines the bitmap
            length; ``n_shards`` is not a substitute.

    Returns:
        bytes: The packed bitmap.

    Raises:
        ValueError: If ``committee_size`` is negative, or a signer position
            falls outside ``range(committee_size)``.
    """
    if committee_size < 0:
        raise ValueError(f"committee_size must be non-negative, got {committee_size}")
    bitmap = bytearray((committee_size + 7) // 8)
    for position in signer_positions:
        if position < 0 or position >= committee_size:
            raise ValueError(
                f"Signer position {position} outside committee of size {committee_size}"
            )
        bitmap[position // 8] |= 1 << (position % 8)
    return bytes(bitmap)


def unpack_signers_bitmap(*, bitmap: bytes, committee_size: int) -> tuple[int, ...]:
    """Unpack a Walrus ``signers_bitmap`` into committee positions.

    Inverse of :func:`pack_signers_bitmap`. Not required by the upload flow, but
    it makes the round-trip property directly unit-testable and materially helps
    when diagnosing a rejected certification.

    Args:
        bitmap (bytes): Packed bitmap as produced by ``pack_signers_bitmap``.
        committee_size (int): Number of committee members. Bits at positions at
            or beyond this value are padding and are ignored.

    Returns:
        tuple[int, ...]: Ascending signer positions.

    Raises:
        ValueError: If ``committee_size`` is negative, or ``bitmap`` is shorter
            than ``committee_size`` requires.
    """
    if committee_size < 0:
        raise ValueError(f"committee_size must be non-negative, got {committee_size}")
    expected_length = (committee_size + 7) // 8
    if len(bitmap) < expected_length:
        raise ValueError(
            f"Bitmap of {len(bitmap)} bytes too short for committee of size "
            f"{committee_size} (expected at least {expected_length})"
        )
    return tuple(
        position
        for position in range(committee_size)
        if bitmap[position // 8] & (1 << (position % 8))
    )


def is_quorum(*, weight: int, n_shards: int) -> bool:
    """Return whether ``weight`` meets the Walrus quorum threshold.

    ``weight`` is a SHARD COUNT, never a node count -- Walrus quorum is
    defined over shards, and a node's contribution is its own shard count
    (see ``NodeConfirmation.weight``). The threshold, per ``bls_aggregate.move``,
    is ``3 * weight >= 2 * n_shards + 1``.

    Args:
        weight (int): Accumulated shard weight to test.
        n_shards (int): Total shard count for the committee.

    Returns:
        bool: True if ``weight`` reaches quorum for ``n_shards``.
    """
    return 3 * weight >= 2 * n_shards + 1


def min_weight_for_quorum(*, n_shards: int) -> int:
    """Return the smallest weight that reaches quorum for ``n_shards``.

    Computed as ``ceil((2 * n_shards + 1) / 3)``, the smallest ``weight`` for
    which :func:`is_quorum` returns True.

    This value coincides with ``min_n_correct(n_shards=n_shards)`` (see
    ``pytusk.core.encoding``) for every shard count exercised by this
    project's tests. That is treated as an observation, not an assumed
    identity -- a dedicated test asserts the equality across a wide range of
    shard counts so that a future divergence between the quorum formula and
    the Byzantine fault-tolerance formula is caught rather than silently
    assumed to still hold.

    Args:
        n_shards (int): Total shard count for the committee.

    Returns:
        int: The minimum weight satisfying quorum.
    """
    return -(-(2 * n_shards + 1) // 3)


def confirmation_message(
    *, epoch: int, blob_id: bytes, object_id: bytes | None = None
) -> bytes:
    """Return the exact bytes a storage node signs to confirm a blob.

    The result is the BCS-encoded ``Confirmation``: 40 bytes for a permanent
    blob, 72 bytes for a deletable one. ``object_id`` is omitted for a
    permanent blob and supplied for a deletable one.

    This reconstructs INDEPENDENTLY what a node should have signed, so a
    caller can cross-check a node's response. It does not replace the node's
    verbatim ``serialized_message`` in :class:`NodeConfirmation` -- that value
    is still what flows unmodified into ``certify_blob``.

    Args:
        epoch (int): Walrus epoch the confirmation is for.
        blob_id (bytes): Raw 32-byte blob ID.
        object_id (bytes | None): Raw blob object ID, for a deletable blob;
            ``None`` for a permanent blob.

    Returns:
        bytes: The BCS-encoded confirmation message.
    """
    return bls_confirmation_bytes(epoch, blob_id, object_id)


def verify_confirmation(
    *, confirmation: NodeConfirmation, expected_message: bytes
) -> bool:
    """Verify a single node confirmation against an expected message.

    Returns False outright if the node's ``serialized_message`` does not
    match ``expected_message`` -- a signature can only be meaningfully
    checked once the message itself is agreed. Otherwise defers to
    ``bls_verify`` against the node's declared public key.

    A malformed public key or signature -- bytes that cannot even be parsed
    as a BLS12-381 point, as opposed to bytes that parse but do not verify --
    is treated as non-verifying, i.e. this function returns False rather than
    propagating the ``ValueError`` ``bls_verify`` raises for that case.
    Confirmations originate from untrusted storage nodes and this function is
    a boolean predicate, not a validator: callers should not need to guard
    every call with a try/except for input they do not control.

    Args:
        confirmation (NodeConfirmation): The node's confirmation to verify.
        expected_message (bytes): The confirmation message the node is
            expected to have signed.

    Returns:
        bool: True if the confirmation's message matches and its signature
        verifies against its public key. False if the message does not
        match, the signature fails verification, or either the public key or
        signature is malformed and cannot be parsed.
    """
    if confirmation.serialized_message != expected_message:
        return False
    try:
        return bls_verify(
            confirmation.public_key, confirmation.signature, expected_message
        )
    except ValueError as exc:
        _logger.warning(
            "bls_verify rejected malformed input for node confirmation "
            "(code=%s): %s",
            exc.args[0] if exc.args else None,
            exc,
        )
        return False


def build_certificate(
    *,
    confirmations: Sequence[NodeConfirmation],
    committee_size: int,
    n_shards: int,
    expected_message: bytes | None = None,
) -> Certificate:
    """Build a quorum-backed certificate from per-node confirmations.

    Validates message agreement, then aggregates every signature and
    verifies the AGGREGATE ONCE via ``bls_aggregate_verify`` rather than
    verifying each signature individually -- a large cut in BLS work for a
    quorum with many signers, since one aggregate-verify call replaces one
    ``bls_verify`` call per confirmation. Only when the aggregate fails to
    verify (or cannot even be built, e.g. a malformed signature) does this
    fall back to the original per-node ``bls_verify`` loop. Confirmations
    that fail this per-node check are EXCLUDED, not fatal on their own: the
    remaining confirmations are then re-aggregated and quorum is rechecked
    against just them, exactly as if the excluded nodes had never responded
    -- one corrupted signature out of a large committee should not abort an
    otherwise-valid certificate. Only when no confirmation can be excluded
    (an aggregate failure with no individually-bad signature -- see
    ``Raises`` below) or the surviving confirmations no longer reach quorum
    does this actually fail. Deduplicates by committee position and checks
    quorum after the signatures are confirmed valid, so the first thing
    wrong with an inbound confirmation set is still what gets reported.

    Args:
        confirmations (Sequence[NodeConfirmation]): Per-node confirmations to
            combine into a certificate.
        committee_size (int): Number of committee members, used to size the
            signer bitmap.
        n_shards (int): Total shard count for the committee, used for the
            quorum check.
        expected_message (bytes | None): The confirmation message every
            confirmation must carry. When omitted, the first confirmation's
            ``serialized_message`` is used as the reference.

    Returns:
        Certificate: The assembled, quorum-backed certificate.

    Raises:
        ValueError: If ``confirmations`` is empty, or more than one
            confirmation shares the same committee ``position``.
        ConfirmationMismatchError: If any confirmation's
            ``serialized_message`` differs from the reference message.
        InvalidConfirmationError: If the aggregate fails to verify and every
            individual confirmation still passes ``bls_verify`` (the
            aggregate/per-node mismatch is unreachable in practice, but
            guarded), or if every confirmation is excluded by the per-node
            fallback (all signatures/public keys are malformed or fail
            verification, leaving nothing to build a certificate from).
        QuorumNotReachedError: If the deduplicated signer weight does not
            reach quorum for ``n_shards`` -- including after invalid
            confirmations have been excluded by the per-node fallback.
    """
    if not confirmations:
        raise ValueError("confirmations must not be empty")

    reference_message = (
        expected_message
        if expected_message is not None
        else confirmations[0].serialized_message
    )

    for confirmation in confirmations:
        if confirmation.serialized_message != reference_message:
            raise ConfirmationMismatchError(
                f"Node {confirmation.node_id} returned a confirmation message "
                f"that differs from the agreed message"
            )

    signatures = [confirmation.signature for confirmation in confirmations]
    public_keys = [confirmation.public_key for confirmation in confirmations]

    aggregate_signature: bytes | None = None
    bad_nodes: list[str] = []
    try:
        aggregate_signature = bls_aggregate(signatures)
        aggregate_verified = bls_aggregate_verify(
            aggregate_signature, public_keys, reference_message
        )
    except ValueError as exc:
        # Either the aggregate itself could not be built (a malformed
        # signature) or aggregate-verify could not parse an input. Either
        # way, fall through to the per-node loop below to name the culprit.
        _logger.warning(
            "bls_aggregate/bls_aggregate_verify rejected malformed input "
            "(code=%s): %s",
            exc.args[0] if exc.args else None,
            exc,
        )
        aggregate_verified = False

    if not aggregate_verified:
        # Aggregate-first failed or could not even be attempted -- fall back
        # to verifying each confirmation individually. A bad confirmation is
        # EXCLUDED rather than aborting the whole build -- one corrupted
        # signature out of a large committee should not force complete
        # failure when the remaining confirmations still reach quorum on
        # their own. The shared dedup/weight/quorum check below then runs
        # against the narrowed set exactly as it would for any other
        # confirmation set.
        good_confirmations: list[NodeConfirmation] = []
        for confirmation in confirmations:
            try:
                verified = bls_verify(
                    confirmation.public_key, confirmation.signature, reference_message
                )
            except ValueError as exc:
                _logger.warning(
                    "Node %s confirmation could not be parsed as a valid "
                    "BLS public key/signature (excluded): %s",
                    confirmation.node_id,
                    exc,
                )
                bad_nodes.append(confirmation.node_id)
                continue
            if not verified:
                _logger.warning(
                    "Node %s signature failed verification against its "
                    "committee public key (excluded)",
                    confirmation.node_id,
                )
                bad_nodes.append(confirmation.node_id)
                continue
            good_confirmations.append(confirmation)

        if not bad_nodes:
            # Unreachable in practice: BLS aggregate-verify succeeds against
            # a message/public-key set whenever every individual signature
            # verifies against it, so reaching here means every confirmation
            # passed bls_verify above yet the aggregate still did not.
            # Guarded anyway so this can never silently return an
            # unverified certificate.
            raise InvalidConfirmationError(
                "Aggregate signature failed verification even though every "
                "individual confirmation verified"
            )
        if not good_confirmations:
            raise InvalidConfirmationError(
                f"All {len(bad_nodes)} confirmation(s) failed verification "
                f"(nodes: {', '.join(bad_nodes)})"
            )

        confirmations = good_confirmations
        aggregate_signature = bls_aggregate(
            [confirmation.signature for confirmation in confirmations]
        )

    seen_positions: set[int] = set()
    weight = 0
    for confirmation in confirmations:
        if confirmation.position in seen_positions:
            raise ValueError(
                f"Duplicate committee position {confirmation.position} "
                f"(node {confirmation.node_id}); a node may contribute "
                f"weight only once"
            )
        seen_positions.add(confirmation.position)
        weight += confirmation.weight

    if not is_quorum(weight=weight, n_shards=n_shards):
        required = min_weight_for_quorum(n_shards=n_shards)
        if bad_nodes:
            raise QuorumNotReachedError(
                f"Signer weight {weight} does not reach quorum: requires at "
                f"least {required} for n_shards={n_shards} (excluded "
                f"{len(bad_nodes)} confirmation(s) that failed verification: "
                f"{', '.join(bad_nodes)})"
            )
        raise QuorumNotReachedError(
            f"Signer weight {weight} does not reach quorum: requires at "
            f"least {required} for n_shards={n_shards}"
        )

    signer_positions = tuple(sorted(seen_positions))
    signers_bitmap = pack_signers_bitmap(
        signer_positions=signer_positions, committee_size=committee_size
    )
    # aggregate_signature was already built and verified above (via the
    # aggregate-first path -- the fallback branch above always raises before
    # reaching here) -- no need to recompute it. The assert only narrows the
    # type for static checkers; aggregate_verified being True guarantees
    # aggregate_signature was assigned.
    assert aggregate_signature is not None

    return Certificate(
        serialized_message=reference_message,
        aggregate_signature=aggregate_signature,
        signers_bitmap=signers_bitmap,
        signer_positions=signer_positions,
        weight=weight,
    )


def verify_certificate(
    *, certificate: Certificate, public_keys: Sequence[bytes]
) -> bool:
    """Locally verify a certificate before submitting it to ``certify_blob``.

    This is a final local self-check: it lets a bad certificate fail here,
    for free, instead of failing on-chain after gas has been spent submitting
    it.

    A malformed aggregate signature or public key -- bytes that cannot even
    be parsed as a BLS12-381 point, as opposed to bytes that parse but do not
    verify -- is treated as non-verifying. Confirmations feeding a
    certificate originate from untrusted storage nodes and this function is
    a boolean predicate, not a validator, so it returns False rather than
    propagating the ``ValueError`` ``bls_aggregate_verify`` raises for that
    case.

    Args:
        certificate (Certificate): The certificate to verify.
        public_keys (Sequence[bytes]): Public keys of the signers whose
            signatures were aggregated into the certificate, in any order.

    Returns:
        bool: True if the aggregate signature verifies against the given
        public keys and the certificate's message. False if verification
        fails or the aggregate signature/public keys are malformed and
        cannot be parsed.
    """
    try:
        return bls_aggregate_verify(
            certificate.aggregate_signature,
            list(public_keys),
            certificate.serialized_message,
        )
    except ValueError as exc:
        _logger.warning(
            "bls_aggregate_verify rejected malformed input for certificate "
            "(code=%s): %s",
            exc.args[0] if exc.args else None,
            exc,
        )
        return False
