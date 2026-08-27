#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Tip quoting and payment for Walrus upload relays.

Deliberately split into a pure half and a networked half so the arithmetic
is unit-testable without a relay: :func:`compute_tip` and
:func:`build_auth_package` are pure, the tip-config fetch performs the
``GET``, and the quote convenience does both.
"""

from __future__ import annotations

import hashlib
import secrets

from pysui import ExecuteTransaction
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction
from pysui.sui.sui_common.txn_pure import PureInput

from pytusk.client.walrus_client import WalrusClient
from pytusk.commands.relay_commands import GetTipConfig
from pytusk.core.utils import require_success
from pytusk.core.relay_types import ConstTip, LinearTip, TipConfig, TipKind
from pytusk.core.relay_upload.common import (
    AuthPackage,
    TipConfigError,
    TipPaymentError,
    TipQuote,
    TipResult,
)
from pytusk.core.encoding import encoded_blob_length

__all__ = [
    "FROM_GAS",
    "add_tip",
    "build_auth_package",
    "compute_tip",
    "execute_tip",
    "fetch_tip_config",
    "quote_tip",
]

FROM_GAS: str = "from_gas"
"""Sentinel for ``payment_coin``: split the tip from whatever funds the transaction.

Deliberately a sentinel rather than passing pysui's gas argument directly.
Under sponsorship the gas coin belongs to the SPONSOR, so defaulting to it
silently bills a third party for the caller's tip. Naming ``FROM_GAS`` makes
that an explicit choice; an explicit coin id is the alternative.
"""


def build_auth_package(*, data: bytes) -> AuthPackage:
    """Build the authentication package binding a tip to one blob upload.

    Generates a FRESH 32-byte nonce on every call, so two attempts at the
    same blob produce different packages. That is what distinguishes a new
    attempt from the original -- and is exactly what a RESUMED upload must
    not do: resuming reuses the original package, or the tip already paid
    no longer matches what the relay is asked to honour.

    Args:
        data (bytes): The unencoded blob bytes about to be uploaded.

    Returns:
        AuthPackage: Package committing to ``data``, a fresh nonce, and the
            unencoded length.
    """
    nonce = secrets.token_bytes(32)
    return AuthPackage(
        nonce=nonce,
        blob_digest=hashlib.sha256(data).digest(),
        nonce_digest=hashlib.sha256(nonce).digest(),
        unencoded_length=len(data),
    )


def compute_tip(*, kind: TipKind, unencoded_length: int, n_shards: int) -> int:
    """Compute the tip, in MIST, that a relay charges for one upload.

    A :class:`~pytusk.core.relay_upload.common.LinearTip` scales with the
    ENCODED blob length, not with the unencoded length passed here -- the
    unencoded length is only an input to that calculation. This is why the
    committee read must precede the tip quote: the encoded length is a
    function of the committee's shard count.

    :func:`~pytusk.core.encoding.encoded_blob_length` is reused unchanged.
    It mirrors ``redstuff.move``, while Walrus's relay derives its tip from
    ``crates/walrus-core/src/encoding/config.rs``. Those two factor the
    shard-count multiplication differently -- Rust's ``slivers_size`` is
    already an all-shards total, Move factors ``n_shards`` out to the end --
    but they are ALGEBRAICALLY IDENTICAL, both expanding to
    ``n_shards * ((primary + secondary) * symbol_size + metadata_length)``
    and both yielding 70,038,000 bytes for a 1 MiB blob on 1000 shards. Do
    not "fix" either one to resemble the other.

    Rounding is UP. Walrus's own doc comment for the linear variant writes a
    floor, but its implementation calls ``div_ceil(1024)``, and the
    implementation is what the relay charges against.

    Args:
        kind (TipKind): The relay's tip formula, from its tip config.
        unencoded_length (int): Length in bytes of the unencoded blob.
        n_shards (int): Shard count of the committee that will store it.

    Returns:
        int: The tip in MIST.

    Raises:
        TypeError: If ``kind`` is not a known tip variant.
    """
    match kind:
        case ConstTip():
            return kind.amount
        case LinearTip():
            encoded = encoded_blob_length(
                unencoded_length=unencoded_length, n_shards=n_shards
            )
            kibs = (encoded + 1023) // 1024
            return kind.base + kibs * kind.encoded_size_mul_per_kib
    raise TypeError(f"Unsupported tip kind: {type(kind).__name__}")


async def fetch_tip_config(*, client: WalrusClient, relay_url: str) -> TipConfig:
    """Fetch a relay's advertised tipping policy.

    This RAISES rather than returning an outcome, and that is deliberate
    rather than an inconsistency with the rest of the package: it runs
    before anything is registered or spent, so there is no partial state
    and no resumption token to report. A result type would carry nothing
    but the failure. Once money is in play -- from
    :func:`~pytusk.core.relay_upload.upload.upload_to_relay` onward --
    outcomes are returned, never raised.

    Args:
        client (WalrusClient): Client used to issue the GET.
        relay_url (str): Base URL of the relay to ask.

    Returns:
        TipConfig: The relay's tipping policy.

    Raises:
        TipConfigError: If the request fails or the payload does not match
            Walrus's TipConfig shape.
    """
    result = await client.execute(command=GetTipConfig(), base_url=relay_url)
    if not result.is_ok():
        raise TipConfigError(
            message=f"Cannot fetch tip config from {relay_url}: {result.result_string}",
            stage="fetch_tip_config",
        )
    return result.result_data


async def quote_tip(
    *, client: WalrusClient, relay_url: str, unencoded_length: int, n_shards: int
) -> TipQuote:
    """Resolve what one specific upload will be charged by a relay.

    The convenience over :func:`fetch_tip_config` plus :func:`compute_tip`
    -- kept separate from both so the arithmetic stays unit-testable
    without a relay, and the fetch stays reusable without a blob.

    Args:
        client (WalrusClient): Client used to issue the GET.
        relay_url (str): Base URL of the relay to ask.
        unencoded_length (int): Length in bytes of the unencoded blob.
        n_shards (int): Shard count of the committee that will store it.

    Returns:
        TipQuote: Address and amount to pay, or a quote requiring no
            payment when the relay is ``no_tip``.

    Raises:
        TipConfigError: If the tip config cannot be fetched or parsed.
    """
    config = await fetch_tip_config(client=client, relay_url=relay_url)
    if not config.requires_payment:
        return TipQuote(address=None, amount=None, kind=None)
    return TipQuote(
        address=config.address,
        amount=compute_tip(
            kind=config.kind, unencoded_length=unencoded_length, n_shards=n_shards
        ),
        kind=config.kind,
    )


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
        tip_amount (int): Tip in MIST, as computed by :func:`compute_tip`.
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


async def execute_tip(
    *,
    client: WalrusClient,
    relay_address: str,
    tip_amount: int,
    auth_package: AuthPackage,
    payment_coin: str = FROM_GAS,
    sender: str | None = None,
    sponsor: str | None = None,
) -> TipResult:
    """Pay a relay tip in a transaction of its own.

    The encapsulated pipeline does NOT use this -- it bundles the tip into
    Tx1 alongside registration, which is cheaper and is what the TypeScript
    SDK does. This exists for the composable case the bundle cannot serve:
    paying a fresh tip for an upload whose earlier attempt can no longer be
    resumed. Walrus's own Rust client pays tips exactly this way, as a
    standalone transaction.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        relay_address (str): Sui address to transfer the tip to.
        tip_amount (int): Tip in MIST.
        auth_package (AuthPackage): Package binding this tip to one blob.
        payment_coin (str): :data:`FROM_GAS` or an explicit coin object id.
        sender (str | None): Address to sign as; defaults to the active
            address.
        sponsor (str | None): Address to sponsor gas, or ``None``.

    Returns:
        TipResult: The digest and nonce a later upload POST must send.

    Raises:
        TipPaymentError: If the transaction could not be composed.
        RuntimeError: If submission fails or the transaction aborts
            on-chain.
    """
    resolved_sender = sender or client.pysui_client.config.active_address
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=resolved_sender, initial_sponsor=sponsor
    )
    await add_tip(
        txn=txn,
        relay_address=relay_address,
        tip_amount=tip_amount,
        auth_package=auth_package,
        payment_coin=payment_coin,
    )
    txdict = await txn.build_and_sign()
    result = await client.execute(command=ExecuteTransaction(**txdict))
    if not result.is_ok():
        raise RuntimeError(f"Tip transaction failed: {result.result_string}")
    require_success(result_data=result.result_data, label="relay tip")
    return TipResult(
        digest=result.result_data.digest,
        nonce=auth_package.nonce_base64url,
        relay_address=relay_address,
        tip_amount=tip_amount,
    )
