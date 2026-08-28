#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""COMPOSE layer for a Walrus upload relay's tip payment.

Contains :func:`add_tip` (splits a tip payment and registers the relay's
72-byte authentication package as PTB input 0). PURE PTB COMPOSITION: it
appends a pure input plus a ``split_coin``/``transfer_objects`` command pair
to a transaction the caller already created and makes no network call,
builds nothing, signs nothing, submits nothing.

NO-CLIENT INVARIANT: nothing in this module imports or references a
client / transaction-executor type (e.g.
:class:`~pytusk.client.walrus_client.WalrusClient`), enforced by
``tests/unit_tests/test_ops_layering.py`` -- see
:mod:`pytusk.core.ops.blob_compose`'s module docstring for the full
COMPOSE/EXECUTE split this package exists to enforce.

This module lives in :mod:`pytusk.core.ops` rather than
:mod:`pytusk.core.relay_upload` (its original home) so that
:func:`~pytusk.core.ops.blob_compose.add_registration_sequence` -- which
must interleave :func:`add_tip` with
:func:`~pytusk.core.ops.blob_compose.add_reserve_and_register` BEFORE
either composes anything onto the transaction, since the relay reads the
authentication package at input 0 -- can call it directly as a sibling
COMPOSE module, without :mod:`pytusk.core.ops` reaching back into
:mod:`pytusk.core.relay_upload` (which itself already imports FROM
:mod:`pytusk.core.ops`; the reverse edge would be a cycle).
:mod:`pytusk.core.relay_upload.tip` re-imports and re-exports
:func:`add_tip` for backward compatibility -- existing callers still reach
it via ``pytusk.core.relay_upload`` or ``pytusk.core.relay_upload.tip``.
"""

from pysui.sui.sui_common.async_txn import AsyncSuiTransaction
from pysui.sui.sui_common.txn_pure import PureInput

from pytusk.core.types import FROM_GAS, AuthPackage, TipPaymentError

__all__ = ["add_tip"]


async def add_tip(
    *,
    txn: AsyncSuiTransaction,
    relay_address: str,
    tip_amount: int,
    auth_package: AuthPackage,
    payment_coin: str = FROM_GAS,
) -> None:
    """Compose a relay tip payment into a caller-owned transaction.

    PURE PTB COMPOSITION -- makes no network calls, builds nothing, signs
    nothing, submits nothing.

    MUST BE THE FIRST THING ADDED TO ``txn``. The relay locates the
    authentication package by a hard positional read of transaction input
    zero::

        let Some(CallArg::Pure(bytes)) = ptb.inputs.first() else {
            return Err(WalrusUploadRelayError::MissingAuthPackage);
        };

    (``walrus-upload-relay/src/utils.rs``). ``ptb.inputs`` is flat and
    transaction-global, so ANY other input registered first displaces the
    package and the relay rejects the upload -- after the tip has been
    paid. Command count and command ORDER are unconstrained; only input
    zero is. This function therefore refuses to compose into a transaction
    that already holds inputs or commands, rather than producing a PTB that
    looks valid and fails at the relay.

    The 72-byte package is registered as a pure input that NO command
    consumes. That is deliberate and matches both reference clients: Walrus's
    Rust SDK does ``pt_builder.pure(auth_package.to_hashed_nonce())?`` under
    the comment "The first input is the authentication package", and the
    TypeScript SDK calls ``transaction.pure(authPayload)`` as a bare
    statement. The bytes are never executed -- the relay only inspects them
    off-chain.

    The payload must be EXACTLY 72 bytes with no length prefix, since the
    relay does ``bcs::from_bytes::<HashedAuthPackage>`` on it directly.
    ``PureInput.as_input`` dispatches ``bytes`` to ``list(arg)``, which
    emits the bytes verbatim -- passing a ``vector<u8>``-serialised value
    instead would prepend a ULEB128 length and be rejected.

    Args:
        txn (AsyncSuiTransaction): Caller's transaction, which must be empty.
        relay_address (str): Sui address to transfer the tip to, from the
            relay's tip config.
        tip_amount (int): Tip in MIST, as computed by
            :func:`~pytusk.core.relay_upload.tip.compute_tip`.
        auth_package (AuthPackage): Package binding this tip to one blob.
        payment_coin (str): :data:`FROM_GAS` to split from whatever funds
            the transaction, or an explicit coin object id.

    Returns:
        None.

    Raises:
        TipPaymentError: If ``txn`` already holds any input or command, so
            the authentication package could not be input zero.
    """
    if txn.builder.inputs or txn.builder.commands:
        raise TipPaymentError(
            message=(
                "add_tip must be the first composition on a transaction: the "
                "relay reads the authentication package at input 0, and this "
                f"transaction already holds {len(txn.builder.inputs)} input(s) "
                f"and {len(txn.builder.commands)} command(s)."
            ),
            stage="add_tip",
        )

    txn.builder.input_pure(PureInput.as_input(auth_package.bcs))
    source = txn.gas if payment_coin == FROM_GAS else payment_coin
    split = await txn.split_coin(coin=source, amounts=[tip_amount])
    await txn.transfer_objects(transfers=[split], recipient=relay_address)
