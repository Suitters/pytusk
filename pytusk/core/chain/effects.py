#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Transaction-effects inspection.

Both helpers here operate on an already-fetched transaction result/effects
object -- neither takes a client or performs I/O of its own, which is what
keeps this module client-free alongside
:mod:`pytusk.core.chain.committee`.
"""

import dataclasses

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot

from pytusk.core.chain.coin_types import matches_wal_coin_type

__all__ = [
    "BalanceChangeCosts",
    "extract_balance_change_costs",
    "find_created_object_id",
    "find_created_shared_object_id",
    "require_success",
]


@dataclasses.dataclass(kw_only=True, frozen=True)
class BalanceChangeCosts:
    """SUI and WAL costs read out of a transaction's balance changes.

    Costs are the NEGATION of the on-chain net balance change (which is
    negative for an outgoing spend), so a positive value means "this many
    raw units are spent". An unexpected net GAIN therefore surfaces as a
    negative cost rather than silently flipping sign.

    Each currency carries its own ``*_unavailable_reason``, independent of
    the other: a response missing WAL says nothing about whether SUI was
    readable, and neither absence is reported as a silent zero.

    Attributes:
        sui_raw_mist (int | None): SUI cost in MIST, or ``None`` when
            unavailable.
        sui_unavailable_reason (str | None): Why SUI is unavailable, or
            ``None`` when it was read.
        wal_coin_type (str | None): The matched WAL coin type, or ``None``
            when no WAL entry was found.
        wal_raw_frost (int | None): WAL cost in FROST, or ``None`` when
            unavailable.
        wal_unavailable_reason (str | None): Why WAL is unavailable, or
            ``None`` when it was read.
    """

    sui_raw_mist: int | None = None
    sui_unavailable_reason: str | None = None
    wal_coin_type: str | None = None
    wal_raw_frost: int | None = None
    wal_unavailable_reason: str | None = None


def extract_balance_change_costs(
    *,
    transaction: sui_prot.ExecutedTransaction | None,
    wal_coin_type: str,
) -> BalanceChangeCosts:
    """Read SUI and WAL costs out of a transaction's balance changes.

    Client-free by construction: the caller passes the pinned WAL coin type
    its config carries rather than this function reaching for a client to
    find it. Rendering the raw amounts into decimal strings needs WAL's
    on-chain decimals, which IS a client read, so that step deliberately
    stays with the caller -- see ``pytusk.tusky.tusky_format`` for the CLI's
    rendering of this result.

    SUI's coin_type is matched by substring (``"::sui::SUI"``) because the
    simulate response reports it in normalized long-address form (e.g.
    ``0x000...0002::sui::SUI``), not the short ``0x2::sui::SUI`` form. WAL is
    matched via :func:`~pytusk.core.chain.coin_types.matches_wal_coin_type`, whose
    pinned-exact/substring-fallback logic is not duplicated here.

    Args:
        transaction (sui_prot.ExecutedTransaction | None): The result's
            ``transaction`` field, or ``None`` when the response had none.
        wal_coin_type (str): The active network's pinned WAL coin type, or
            ``""`` when unpinned.

    Returns:
        BalanceChangeCosts: Raw costs, with a per-currency reason wherever a
            value could not be read.
    """
    if transaction is None:
        reason = (
            "Simulate result had no 'transaction' field; cannot read "
            "balance_changes to determine cost."
        )
        return BalanceChangeCosts(
            sui_unavailable_reason=reason, wal_unavailable_reason=reason
        )

    balance_changes = getattr(transaction, "balance_changes", None) or []

    sui_raw_mist: int | None = None
    sui_unavailable_reason: str | None = None
    sui_change = next(
        (bc for bc in balance_changes if bc.coin_type and "::sui::SUI" in bc.coin_type),
        None,
    )
    if sui_change is None:
        sui_unavailable_reason = (
            "No SUI entry found in the simulate result's balance_changes; "
            "the response shape may differ from what this command expects."
        )
    else:
        try:
            sui_raw_mist = -int(sui_change.amount)
        except (TypeError, ValueError):
            sui_unavailable_reason = (
                f"SUI balance change amount {sui_change.amount!r} could not "
                "be parsed as an integer; the response shape may differ "
                "from what this command expects."
            )

    matched_wal_coin_type: str | None = None
    wal_raw_frost: int | None = None
    wal_unavailable_reason: str | None = None
    wal_change = next(
        (
            bc
            for bc in balance_changes
            if bc.coin_type
            and matches_wal_coin_type(
                coin_type=bc.coin_type, wal_coin_type=wal_coin_type
            )
        ),
        None,
    )
    if wal_change is None:
        wal_unavailable_reason = (
            "No WAL entry found in the simulate result's balance_changes; "
            "the response shape may differ from what this command expects."
        )
    else:
        matched_wal_coin_type = wal_change.coin_type
        try:
            wal_raw_frost = -int(wal_change.amount)
        except (TypeError, ValueError):
            wal_unavailable_reason = (
                f"WAL balance change amount {wal_change.amount!r} could not "
                "be parsed as an integer; the response shape may differ "
                "from what this command expects."
            )

    return BalanceChangeCosts(
        sui_raw_mist=sui_raw_mist,
        sui_unavailable_reason=sui_unavailable_reason,
        wal_coin_type=matched_wal_coin_type,
        wal_raw_frost=wal_raw_frost,
        wal_unavailable_reason=wal_unavailable_reason,
    )


def find_created_object_id(
    *, effects: sui_prot.TransactionEffects, owner: str
) -> str:
    """Find the object ID of the single object created and owned by ``owner``.

    Public so a caller who now owns transaction submission (per the
    caller-owns-the-transaction model described in
    :mod:`pytusk.core.ops.blob_compose`) and holds their own ``TransactionEffects``
    can locate a PTB's created object without reimplementing this lookup.

    Reads ``TransactionEffects.changed_objects`` (verified directly against
    the installed pysui proto definitions: ``ChangedObject.id_operation`` and
    ``ChangedObject.output_owner.address``) rather than following an existing
    pytusk pattern, because no existing pytusk code path parses a PTB's
    created-object effects -- every other PTB builder in this repo (
    ``extend_blob_expiration``, ``delete_blob``, ``exchange_for_wal``) only
    needed to check ``.status``/``.gas_used``/``.balance_changes``, never an
    object ID minted by the PTB itself.

    UNVERIFIED against a live node -- see the deliverable-4 report. If this
    assumption is wrong, it fails loudly here (after the transaction has
    already succeeded on-chain) rather than corrupting a ``Registration``.

    Args:
        effects (sui_prot.TransactionEffects): Effects of a successfully
            executed transaction.
        owner (str): Address expected to own the newly created object.

    Returns:
        str: Object ID of the created object owned by ``owner``.

    Raises:
        RuntimeError: If no such object is found in ``effects``.
    """
    for change in effects.changed_objects or []:
        if (
            change.id_operation == sui_prot.ChangedObjectIdOperation.CREATED
            and change.output_owner
            and change.output_owner.address == owner
            and change.object_id
        ):
            return change.object_id
    raise RuntimeError(
        f"Could not find a newly created object owned by {owner} in the "
        "transaction effects."
    )


def find_created_shared_object_id(
    *, effects: sui_prot.TransactionEffects, object_type_substring: str
) -> str:
    """Find the object ID of the single object created and shared, by type.

    Companion to :func:`find_created_object_id` for a Move call that shares
    its output internally (``transfer::share_object``) rather than
    transferring it to an address -- a ``SHARED``-kind ``output_owner``
    carries no wallet address to filter on (``Owner.address`` is populated
    for ``ADDRESS``/``OBJECT`` owners, not ``SHARED``), so this filters by
    ``object_type`` instead.

    Verified against the installed pysui proto definitions:
    ``ChangedObject.id_operation``, ``ChangedObject.output_owner.kind``
    (``sui_prot.OwnerOwnerKind.SHARED``), and ``ChangedObject.object_type``.
    First use case:
    :func:`~pytusk.core.ops.shared_blob_execute.execute_share_blob`, whose
    ``shared_blob::new`` move_call returns unit and shares the new
    ``SharedBlob`` internally, so its object ID is only knowable from these
    effects.

    Args:
        effects (sui_prot.TransactionEffects): Effects of a successfully
            executed transaction.
        object_type_substring (str): Substring the created object's
            ``object_type`` must contain (e.g. ``"shared_blob::SharedBlob"``).

    Returns:
        str: Object ID of the created, shared object matching
        ``object_type_substring``.

    Raises:
        RuntimeError: If no such object is found in ``effects``.
    """
    for change in effects.changed_objects or []:
        if (
            change.id_operation == sui_prot.ChangedObjectIdOperation.CREATED
            and change.output_owner
            and change.output_owner.kind == sui_prot.OwnerOwnerKind.SHARED
            and change.object_type
            and object_type_substring in change.object_type
            and change.object_id
        ):
            return change.object_id
    raise RuntimeError(
        f"Could not find a newly created shared object matching "
        f"{object_type_substring!r} in the transaction effects."
    )


def require_success(
    *, result_data: object, label: str
) -> sui_prot.TransactionEffects:
    """Check an ExecuteTransaction result's effects.status and return the effects.

    Status is read from ``result_data.effects.status`` -- NOT
    ``result_data.transaction.effects.status`` -- per the pysui result
    shape confirmed against a live node.

    Args:
        result_data (object): ``SuiRpcResult.result_data`` from a successful
            ``ExecuteTransaction`` call (i.e. ``result.is_ok()`` already True).
        label (str): Human-readable name of the transaction, used in errors.

    Returns:
        sui_prot.TransactionEffects: The transaction's effects.

    Raises:
        RuntimeError: If the transaction aborted on-chain.
    """
    effects = result_data.effects  # type: ignore[attr-defined]
    status = effects.status if effects else None
    if not (status and status.success):
        desc = status.error.description if status and status.error else "unknown error"
        raise RuntimeError(f"{label} transaction aborted on-chain: {desc}")
    return effects
