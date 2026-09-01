#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Result types for the storage-node blob-status query.

Two layers, deliberately kept apart:

**Layer 1 -- the verdict.** :data:`BlobStatus` is a union mirroring the
Walrus protocol's own externally-tagged status enum one member per variant,
plus :class:`UnresolvedStatus` for "the committee did not tell us." A flat
dataclass with optional fields was rejected: ``end_epoch`` is required on
:class:`PermanentStatus` and meaningless on :class:`DeletableStatus`, which
is exactly the signal that two shapes are hiding inside one. The union also
buys exhaustiveness -- a ``match`` over it that forgets a variant fails type
checking rather than production.

**Layer 2 -- the query outcome.** :class:`BlobStatusReport` is uniform
across every variant and is the SOLE return value of the SDK entry point. It
carries the verdict plus the evidence behind it.

Deliberately absent: lease facts (object IDs, ``deletable`` flags, owned-lease
lists, quilt markers). Those are chain-derived, and the blob-status SDK path
does no chain work beyond fetching the committee. Callers wanting lease
enrichment layer it on top -- that is what the ``tusky`` resolution ladder
does.
"""

import dataclasses
import enum
import typing


@dataclasses.dataclass(kw_only=True, frozen=True)
class EventRef:
    """Reference to a single Sui event by transaction and sequence.

    Storage nodes report the event that established a blob's status. The
    node returns ``eventSeq`` as a JSON STRING (Walrus serialises ``u64``
    that way for human-readable formats); it is normalised to ``int`` here
    so callers never handle the wire representation.

    Attributes:
        tx_digest (str): Base58 digest of the transaction carrying the event.
        event_seq (int): Zero-based index of the event within that
            transaction.
    """

    tx_digest: str
    event_seq: int


@dataclasses.dataclass(kw_only=True, frozen=True)
class DeletableCounts:
    """Counts of deletable blob objects registered for one blob ID.

    A single blob ID may back many objects; these counts are how a node
    reports that fan-out. Both variants that can coexist with deletable
    objects carry this.

    Attributes:
        total (int): Deletable objects registered for this blob ID.
        certified (int): Subset of ``total`` that are certified.
    """

    total: int
    certified: int


@dataclasses.dataclass(kw_only=True, frozen=True)
class NonexistentStatus:
    """The committee reports no record of this blob ID.

    Carries no fields: "we have never seen it" has nothing to describe.
    """


@dataclasses.dataclass(kw_only=True, frozen=True)
class InvalidStatus:
    """The blob was proven invalid on chain.

    Terminal and unrecoverable -- an invalidity certificate exists, so no
    amount of re-upload changes this verdict.

    Attributes:
        status_event (EventRef): The event recording the invalidation.
    """

    status_event: EventRef


@dataclasses.dataclass(kw_only=True, frozen=True)
class PermanentStatus:
    """A permanent (non-deletable) blob registration.

    ``deletable_counts`` is present because permanent and deletable
    registrations for the same blob ID are not mutually exclusive: the same
    content may back both.

    Attributes:
        end_epoch (int): Epoch at which the permanent lease expires.
        is_certified (bool): Whether the permanent registration is certified
            rather than merely registered.
        status_event (EventRef): The event establishing this status.
        deletable_counts (DeletableCounts): Deletable objects that also back
            this blob ID.
        initial_certified_epoch (int | None): Epoch of first certification,
            or ``None`` if not yet certified. The protocol genuinely models
            this as optional (``Option<Epoch>``), which is why it is the one
            remaining ``Optional`` in this module.
    """

    end_epoch: int
    is_certified: bool
    status_event: EventRef
    deletable_counts: DeletableCounts
    initial_certified_epoch: int | None


@dataclasses.dataclass(kw_only=True, frozen=True)
class DeletableStatus:
    """Only deletable registrations exist for this blob ID.

    Carries NO ``end_epoch`` and NO ``status_event``: a deletable
    registration has neither on the node side, and the protocol emits no
    status event for it. Code wanting an expiry for a deletable blob must
    read the owning ``Blob`` object from chain instead.

    Attributes:
        deletable_counts (DeletableCounts): Deletable objects backing this
            blob ID.
        initial_certified_epoch (int | None): Epoch of first certification,
            or ``None`` if none is certified.
    """

    deletable_counts: DeletableCounts
    initial_certified_epoch: int | None


@dataclasses.dataclass(kw_only=True, frozen=True)
class UnresolvedStatus:
    """No verdict could be established from the committee's answers.

    Reached when neither the quorum threshold nor the weaker validity
    threshold was met before the operation deadline. This is a NORMAL
    outcome of a distributed query, not an error -- which is why it is a
    union member rather than an exception. The evidence matters most in
    exactly this case, and it stays reachable on the enclosing
    :class:`BlobStatusReport`.
    """


BlobStatus: typing.TypeAlias = (
    NonexistentStatus
    | InvalidStatus
    | PermanentStatus
    | DeletableStatus
    | UnresolvedStatus
)
"""The verdict for one blob ID, as one of five mutually exclusive variants."""


class Resolution(str, enum.Enum):
    """How the verdict on a :class:`BlobStatusReport` was established.

    This is the honesty field. Walrus's own client discards which threshold
    produced an answer; pytusk keeps it, so a caller can tell a
    fully-agreed verdict from one resting on a single honest node.

    Attributes:
        QUORUM: 2f+1 shard weight agreed on one status.
        VALIDITY: f+1 shard weight agreed -- weak, but enough that at least
            one honest node reported it.
        UNRESOLVED: Neither threshold was met. Pairs only with
            :class:`UnresolvedStatus`.
    """

    __str__ = str.__str__

    QUORUM = "quorum"
    VALIDITY = "validity"
    UNRESOLVED = "unresolved"


class DissentReason(str, enum.Enum):
    """Why a committee member did not contribute to the verdict.

    Always required on a :class:`NodeDissent` -- there is no "dissented for
    no reason" state, so this is never optional.

    The first two values are blob-status semantics rather than transport
    outcomes: a node answering :class:`NonexistentStatus`, or answering
    differently from the eventual verdict, SUCCEEDED as far as the fan-out
    driver is concerned. Keeping that vocabulary here rather than in the
    driver is what stops the shared driver accreting one caller's concepts.

    Attributes:
        NOT_STORED: The node answered, reporting it does not hold the blob.
        DISAGREED: The node answered with a status other than the verdict.
        TIMEOUT: The node did not answer before the deadline.
        ERROR: The node answered unusably (transport or parse failure).
        NOT_CHECKED: Never queried, or cancelled once the verdict was
            already settled. Retained rather than dropped so the two
            ``--details`` lists always account for the whole committee.
    """

    __str__ = str.__str__

    NOT_STORED = "not-stored"
    DISAGREED = "disagreed"
    TIMEOUT = "timeout"
    ERROR = "error"
    NOT_CHECKED = "not-checked"


@dataclasses.dataclass(kw_only=True, frozen=True)
class NodeRef:
    """Identity and voting weight of one committee member.

    Deliberately narrower than
    :class:`~pytusk.core.chain.committee.WalrusCommitteeMember`: a caller
    reading a status report wants to know which node said what and how much
    its word counted, not the node's BLS public key.

    Attributes:
        node_id (str): On-chain object ID of the node's staking pool.
        network_address (str): Bare ``host:port`` of the node's public API.
        weight (int): Shards assigned to this node. Thresholds are computed
            over SHARD WEIGHT, never node count.
    """

    node_id: str
    network_address: str
    weight: int


@dataclasses.dataclass(kw_only=True, frozen=True)
class NodeDissent:
    """A committee member that did not back the verdict, and why.

    Attributes:
        node (NodeRef): The member.
        reason (DissentReason): Why it did not contribute. Never optional.
    """

    node: NodeRef
    reason: DissentReason


@dataclasses.dataclass(kw_only=True, frozen=True)
class BlobStatusReport:
    """The complete outcome of one blob-status query.

    Returned for every outcome including no-verdict, so the evidence behind
    an answer is never discarded and never has to be stuffed onto an
    exception.

    ``confirming`` and ``dissenting`` are two tuples rather than one list
    with an optional reason: they map 1:1 onto the two ``--details`` lists,
    and dissent always has a reason.

    Attributes:
        blob_id (bytes): The 32-byte blob ID queried.
        status (BlobStatus): The verdict.
        resolution (Resolution): Which threshold produced the verdict.
            :attr:`Resolution.UNRESOLVED` pairs only with
            :class:`UnresolvedStatus`.
        committee_epoch (int): Epoch of the committee that answered. Needed
            to turn an ``end_epoch`` into epochs-remaining, and to explain a
            verdict that later changes.
        confirming (tuple[NodeRef, ...]): Members whose answer matched the
            verdict.
        dissenting (tuple[NodeDissent, ...]): Every other member, each with
            its reason.
    """

    blob_id: bytes
    status: BlobStatus
    resolution: Resolution
    committee_epoch: int
    confirming: tuple[NodeRef, ...]
    dissenting: tuple[NodeDissent, ...]
