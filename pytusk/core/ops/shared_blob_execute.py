#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""EXECUTE layer for SharedBlob transactions.

Contains :func:`execute_share_blob`, :func:`execute_fund_shared_blob`, and
:func:`execute_extend_shared_blob`. Each is a THIN CONVENIENCE WRAPPER
around the matching ``add_*`` function in
:mod:`pytusk.core.ops.shared_blob_compose` -- see that module's docstring
for the "caller owns the transaction lifecycle" split. They open a
transaction, delegate all composition to ``add_*``, then build, sign, and
submit.

None of the three Move calls has a client-side-preemptable guaranteed-abort
condition the way blob metadata's ``remove_metadata_pair``/``take_metadata``
do, so none of these gate on a pre-transaction existence check -- consistent
with :func:`~pytusk.core.ops.blob_metadata_execute.execute_set_blob_metadata`'s
"no pre-transaction gate" reasoning for the same class of failure (a bad
object ID aborts the same way any PTB targeting one would).
"""

from pysui import ExecuteTransaction
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk.client.walrus_client import WalrusClient
from pytusk.core.chain import find_created_shared_object_id, require_success
from pytusk.core.ops.coins import prepare_wal_coin_for_amount
from pytusk.core.ops.shared_blob_compose import (
    add_extend_shared_blob,
    add_fund_shared_blob,
    add_share_blob,
)
from pytusk.core.types.receipts import SharedBlobOpResult, SharedBlobReceipt

__all__ = [
    "execute_extend_shared_blob",
    "execute_fund_shared_blob",
    "execute_share_blob",
]


async def execute_share_blob(
    *,
    client: WalrusClient,
    package_id: str,
    blob_object: str,
    sender: str | None = None,
    sponsor: str | None = None,
) -> SharedBlobReceipt:
    """Build and execute a single ``shared_blob::new`` call.

    THIN WRAPPER around
    :func:`~pytusk.core.ops.shared_blob_compose.add_share_blob` -- see that
    module docstring's "caller owns the transaction lifecycle" note. All
    PTB composition lives there; this function opens a transaction,
    delegates to it, then builds, signs, and submits.

    ``new`` shares the newly created ``SharedBlob`` internally and returns
    unit to the PTB, so its object ID is only knowable from the executed
    transaction's effects -- read back here via
    :func:`~pytusk.core.chain.effects.find_created_shared_object_id`,
    matched by the ``shared_blob::SharedBlob`` type substring.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        blob_object (str): Object ID of the ``Blob`` to wrap into a new
            ``SharedBlob``. Consumed by the Move call.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own ``blob_object``.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.

    Returns:
        SharedBlobReceipt: The newly created ``SharedBlob``'s object ID and
        the transaction digest.

    Raises:
        RuntimeError: If transaction submission fails, the transaction
            aborts on-chain, or the new ``SharedBlob``'s object ID cannot
            be found in the transaction effects.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_share_blob(txn=txn, package_id=package_id, blob_object=blob_object)
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"share_blob transaction failed: {result.result_string}")
    effects = require_success(result_data=result.result_data, label="share_blob")
    object_id = find_created_shared_object_id(
        effects=effects, object_type_substring="shared_blob::SharedBlob"
    )
    return SharedBlobReceipt(object_id=object_id, digest=result.result_data.digest)


async def execute_fund_shared_blob(
    *,
    client: WalrusClient,
    package_id: str,
    shared_blob_object: str,
    amount: int,
    sender: str | None = None,
    sponsor: str | None = None,
) -> SharedBlobOpResult:
    """Build and execute a single ``shared_blob::fund`` call.

    THIN WRAPPER around
    :func:`~pytusk.core.ops.shared_blob_compose.add_fund_shared_blob` -- see
    that module docstring's "caller owns the transaction lifecycle" note.
    All PTB composition lives there; this function opens a transaction,
    delegates to it, then builds, signs, and submits.

    ``fund`` consumes its ``Coin<WAL>`` argument in full, so a coin holding
    EXACTLY ``amount`` is prepared first via
    :func:`~pytusk.core.ops.coins.prepare_wal_coin_for_amount`, which may
    add its own split/merge commands to the same transaction before
    ``add_fund_shared_blob`` composes the ``fund`` call.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        shared_blob_object (str): Object ID of the ``SharedBlob`` to fund.
        amount (int): Exact amount to deposit, in WAL's base units (FROST).
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Must own the WAL coin(s) spent.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.

    Returns:
        SharedBlobOpResult: The funded ``SharedBlob``'s object ID and the
        transaction digest.

    Raises:
        RuntimeError: If the sender's WAL balance is insufficient,
            balances/coins cannot be listed, transaction submission fails,
            or the transaction aborts on-chain.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    payment_coin = await prepare_wal_coin_for_amount(
        txn=txn, client=client, owner=resolved_sender, amount=amount
    )
    await add_fund_shared_blob(
        txn=txn,
        package_id=package_id,
        shared_blob_object=shared_blob_object,
        payment_coin=payment_coin,
    )
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(
            f"fund_shared_blob transaction failed: {result.result_string}"
        )
    require_success(result_data=result.result_data, label="fund_shared_blob")
    return SharedBlobOpResult(
        object_id=shared_blob_object, digest=result.result_data.digest
    )


async def execute_extend_shared_blob(
    *,
    client: WalrusClient,
    package_id: str,
    shared_blob_object: str,
    system_object: str,
    extended_epochs: int,
    sender: str | None = None,
    sponsor: str | None = None,
) -> SharedBlobOpResult:
    """Build and execute a single ``shared_blob::extend`` call.

    THIN WRAPPER around
    :func:`~pytusk.core.ops.shared_blob_compose.add_extend_shared_blob` --
    see that module docstring's "caller owns the transaction lifecycle"
    note. All PTB composition lives there; this function opens a
    transaction, delegates to it, then builds, signs, and submits.

    No payment coin is prepared or passed -- ``extend`` draws payment from
    the ``SharedBlob``'s own pooled funds, which can abort on-chain if
    insufficient; that balance is not pre-checkable client-side.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        package_id (str): Walrus package ID, as resolved by
            :func:`~pytusk.core.ops.system_reads.resolve_package_id`.
        shared_blob_object (str): Object ID of the ``SharedBlob`` to
            extend.
        system_object (str): Walrus ``System`` object ID.
        extended_epochs (int): Number of epochs to extend the wrapped
            ``Blob``'s storage by.
        sender (str | None): Address to sign as. Defaults to the active
            address when ``None``. Anyone may extend a shared blob.
        sponsor (str | None): Address to sponsor gas as, or ``None`` for no
            sponsorship.

    Returns:
        SharedBlobOpResult: The extended ``SharedBlob``'s object ID and the
        transaction digest.

    Raises:
        RuntimeError: If transaction submission fails, or the transaction
            aborts on-chain (including an insufficient pooled-funds
            balance).
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_extend_shared_blob(
        txn=txn,
        package_id=package_id,
        shared_blob_object=shared_blob_object,
        system_object=system_object,
        extended_epochs=extended_epochs,
    )
    txdict = await txn.build_and_sign()

    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(
            f"extend_shared_blob transaction failed: {result.result_string}"
        )
    require_success(result_data=result.result_data, label="extend_shared_blob")
    return SharedBlobOpResult(
        object_id=shared_blob_object, digest=result.result_data.digest
    )
