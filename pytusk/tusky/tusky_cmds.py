#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Command handlers for the tusky CLI.

Each handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk/pysui operation. Handlers are async;
tusky.py drives them via asyncio.run.
"""

import argparse
import asyncio
import base64
import dataclasses
import functools
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import cast

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import (
    ExecuteTransaction,
    GetAddressCoinBalances,
    GetCoinMetaData,
    GetCoins,
    GetObject,
    GetObjectsOwnedByAddress,
    PysuiConfiguration,
    SimulateTransaction,
    SuiRpcResult,
)
from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk import (
    BlobData,
    BlobTooLargeError,
    CertifyTransactionError,
    NativeUploadError,
    PytuskConfiguration,
    QuiltPatch,
    ReadBlob,
    ReadQuiltPatch,
    Registration,
    StageTimings,
    StoreBlob,
    StoreQuilt,
    WalrusClient,
    add_certify,
    add_reserve_and_register,
    certify,
    collect_confirmations,
    encode_blob,
    resolve_package_id,
    select_wal_payment_coin,
)
from pytusk import store_blob_native as _store_blob_native_pipeline

# Imported directly from the submodule, not the pytusk top-level package:
# this is an internal reuse of the same computation add_reserve_and_register
# already performs (see its _encoded_storage_amount), not a new public
# surface, so it is kept out of pytusk.__all__.
from pytusk.core.encoding import encoded_blob_length


def _config_from_args(args: argparse.Namespace) -> PytuskConfiguration:
    """Build a PytuskConfiguration from a subcommand's shared config arguments.

    Args:
        args (argparse.Namespace): Parsed arguments carrying the
            `_add_config_args` fields (from_cfg_path, active_network,
            pysui_config_path, pysui_group_name, pysui_profile_name,
            pysui_address, pysui_alias).

    Returns:
        PytuskConfiguration: Configuration for this CLI invocation.
    """
    try:
        return PytuskConfiguration(
            from_cfg_path=args.from_cfg_path,
            active_network=args.active_network,
            pysui_config_path=args.pysui_config_path,
            pysui_group_name=args.pysui_group_name,
            pysui_profile_name=args.pysui_profile_name,
            pysui_address=args.pysui_address,
            pysui_alias=args.pysui_alias,
        )
    except Exception as exc:  # noqa: BLE001 - CLI entry boundary: convert any
        # config-loading failure into a clean message instead of a raw
        # traceback exposing local file paths.
        print(f"Error loading configuration: {exc}", file=sys.stderr)
        sys.exit(1)


def _resolve_address_or_alias(*, config: PysuiConfiguration, arg: str) -> str:
    """Resolve an address or alias string to a Sui address known to PysuiConfiguration.

    Args:
        config (PysuiConfiguration): The active pysui configuration used to
            resolve addresses and aliases.
        arg (str): Address (0x-prefixed) or alias to resolve.

    Returns:
        str: The resolved address.

    Raises:
        ValueError: If arg is an address or alias not found in the active
            PysuiConfiguration group.
    """
    if arg.startswith("0x"):
        config.alias_for_address(address=arg)
        return arg
    return config.address_for_alias(alias_name=arg)


def _resolve_sender(*, config: PysuiConfiguration, sender_arg: str | None) -> str:
    """Resolve a --sender argument to a Sui address known to PysuiConfiguration.

    Args:
        config (PysuiConfiguration): The active pysui configuration used to
            resolve addresses and aliases.
        sender_arg (str | None): Address or alias from --sender, or None to
            use the active address.

    Returns:
        str: The resolved sender address.

    Raises:
        ValueError: If sender_arg is an address or alias not found in the
            active PysuiConfiguration group.
    """
    if not sender_arg:
        return config.active_address
    return _resolve_address_or_alias(config=config, arg=sender_arg)


def _resolve_sponsor(
    *, config: PysuiConfiguration, sponsor_arg: str | None
) -> str | None:
    """Resolve a --sponsor argument to a Sui address known to PysuiConfiguration.

    tusky assumes sponsors are addresses/keys already known to the active
    PysuiConfiguration, since build_and_sign() signs for sender and sponsor
    both by looking up local keypairs; an out-of-band sponsor-signing flow
    for externally-held sponsors is not supported here (that belongs in
    pysui itself, not tusky).

    Args:
        config (PysuiConfiguration): The active pysui configuration used to
            resolve addresses and aliases.
        sponsor_arg (str | None): Address or alias from --sponsor, or None
            if no sponsor was given.

    Returns:
        str | None: The resolved sponsor address, or None if no sponsor was
            given.

    Raises:
        ValueError: If sponsor_arg is an address or alias not found in the
            active PysuiConfiguration group.
    """
    if not sponsor_arg:
        return None
    return _resolve_address_or_alias(config=config, arg=sponsor_arg)


def _require_testnet_exchange(
    *, config: PytuskConfiguration, command_name: str
) -> None:
    """Exit with a clear error if the active network has no wal_exchange objects.

    Args:
        config (PytuskConfiguration): Configuration whose active network is checked.
        command_name (str): Name of the calling subcommand, used in the error message.
    """
    if not config.network.exchange_objects:
        print(
            f"{command_name} is not supported on network '{config.network.network_name}' "
            "— WAL must be acquired via a real exchange there, not the testnet swap contract.",
            file=sys.stderr,
        )
        sys.exit(1)


async def _wal_balance_and_decimals(
    *, client: WalrusClient, owner: str
) -> tuple[sui_prot.Balance, int]:
    """Discover the owner's WAL coin balance entry and WAL's decimal precision.

    Looks up the owner's coin balances to find the exact WAL coin type
    string, then fetches its on-chain CoinMetadata for the decimals value
    (not hardcoded, since it must not be assumed to match SUI's).

    Args:
        client (WalrusClient): Client used to query balances and metadata.
        owner (str): Address whose balances are checked for a WAL coin type.

    Returns:
        tuple[sui_prot.Balance, int]: The owner's WAL balance entry
            (exposing coin_type, balance, coin_balance, address_balance)
            and WAL's decimals.
    """
    balances_result = await client.execute_for_all(
        command=GetAddressCoinBalances(owner=owner)
    )
    if not balances_result.is_ok():
        print(
            f"Error listing coin balances: {balances_result.result_string}",
            file=sys.stderr,
        )
        sys.exit(1)
    wal_coin_type = client.config.network.wal_coin_type
    wal_entry = next(
        (
            entry
            for entry in balances_result.result_data.balances
            if entry.coin_type
            and _matches_wal_coin_type(
                coin_type=entry.coin_type, wal_coin_type=wal_coin_type
            )
        ),
        None,
    )
    if wal_entry is None:
        print(f"No WAL coins found for {owner}.", file=sys.stderr)
        sys.exit(1)
    meta_result = await client.execute(
        command=GetCoinMetaData(coin_type=wal_entry.coin_type)
    )
    if not meta_result.is_ok():
        print(
            f"Error fetching WAL coin metadata: {meta_result.result_string}",
            file=sys.stderr,
        )
        sys.exit(1)
    metadata = meta_result.result_data.metadata
    if metadata is None or metadata.decimals is None:
        print(
            f"WAL coin metadata for {wal_entry.coin_type} has no decimals field.",
            file=sys.stderr,
        )
        sys.exit(1)
    return wal_entry, metadata.decimals


_MAX_BLOB_OPS_PER_PTB = 100


def _matches_wal_coin_type(*, coin_type: str, wal_coin_type: str) -> bool:
    """Check whether a coin_type string identifies the WAL coin.

    Uses an exact match against `wal_coin_type` when the active network has
    a pinned value (currently mainnet only); falls back to a substring
    match when unpinned (e.g. testnet, whose contracts are redeployed and
    don't have a stable package address to pin against).

    Args:
        coin_type (str): The coin_type string to check.
        wal_coin_type (str): The active network's pinned WAL coin type, or
            "" if unpinned.

    Returns:
        bool: True if coin_type identifies the WAL coin.
    """
    if wal_coin_type:
        return coin_type == wal_coin_type
    return "::wal::WAL" in coin_type


def _format_token_amount(*, raw: int, decimals: int) -> str:
    """Render a raw integer token amount as an exact decimal string.

    Uses integer ``divmod`` rather than floating-point division, so the
    result is exact for any magnitude -- important here since raw amounts
    (MIST, FROST) can run into the billions and a float division could lose
    precision at that range.

    Args:
        raw (int): The raw integer amount (may be negative).
        decimals (int): Number of decimal places the token uses.

    Returns:
        str: Exact decimal string rendering, e.g. ``"0.004603480"`` for
            ``raw=4603480, decimals=9``.
    """
    sign = "-" if raw < 0 else ""
    divisor = 10**decimals
    whole, frac = divmod(abs(raw), divisor)
    return f"{sign}{whole}.{frac:0{decimals}d}"


async def _simulate_cost_from_balance_changes(
    *, client: WalrusClient, transaction: sui_prot.ExecutedTransaction | None
) -> tuple[dict[str, int | str | None], dict[str, int | str | None]]:
    """Derive SUI and WAL cost summaries from a simulated Tx1's balance changes.

    Cost is reported as the NEGATION of the on-chain net balance change
    (which is negative for an outgoing spend), so a positive value here
    means "this many units are spent" -- matching what a user asking "what
    will this cost?" wants to read, while still surfacing an unexpected
    positive on-chain delta (a net gain) as a negative cost rather than
    silently flipping its sign.

    SUI's coin_type is matched by substring (``"::sui::SUI"``) since the
    simulate response reports it in normalized long-address form (e.g.
    ``0x000...0002::sui::SUI``), not the short ``0x2::sui::SUI`` form. WAL's
    coin_type is matched via :func:`_matches_wal_coin_type`, the same
    pinned-exact/substring-fallback logic used everywhere else in this
    module (e.g. :func:`_wal_balance_and_decimals`), rather than a third
    variant of that logic.

    Neither currency's absence crashes this function or is reported as a
    silent zero: each missing/unreadable value gets its own
    ``unavailable_reason`` explaining why, independent of whether the other
    currency was found.

    Args:
        client (WalrusClient): Client used to look up WAL's CoinMetadata
            (for its decimal precision) once its coin_type is known from a
            matched balance change.
        transaction (sui_prot.ExecutedTransaction | None): The simulate
            result's ``transaction`` field (``result.result_data.transaction``),
            or ``None`` if the response had no such field.

    Returns:
        tuple[dict[str, int | str | None], dict[str, int | str | None]]:
            ``(sui_info, wal_info)``. ``sui_info`` has keys ``raw_mist``,
            ``sui``, ``unavailable_reason``. ``wal_info`` has keys
            ``coin_type``, ``raw_frost``, ``wal``, ``unavailable_reason``.
            A found value's ``unavailable_reason`` is ``None``; the
            corresponding amount fields are ``None`` when unavailable.
    """
    sui_info: dict[str, int | str | None] = {
        "raw_mist": None,
        "sui": None,
        "unavailable_reason": None,
    }
    wal_info: dict[str, int | str | None] = {
        "coin_type": None,
        "raw_frost": None,
        "wal": None,
        "unavailable_reason": None,
    }

    if transaction is None:
        reason = (
            "Simulate result had no 'transaction' field; cannot read "
            "balance_changes to determine cost."
        )
        sui_info["unavailable_reason"] = reason
        wal_info["unavailable_reason"] = reason
        return sui_info, wal_info

    balance_changes = getattr(transaction, "balance_changes", None) or []

    sui_change = next(
        (bc for bc in balance_changes if bc.coin_type and "::sui::SUI" in bc.coin_type),
        None,
    )
    if sui_change is None:
        sui_info["unavailable_reason"] = (
            "No SUI entry found in the simulate result's balance_changes; "
            "the response shape may differ from what this command expects."
        )
    else:
        try:
            cost_mist = -int(sui_change.amount)
        except (TypeError, ValueError):
            sui_info["unavailable_reason"] = (
                f"SUI balance change amount {sui_change.amount!r} could not "
                "be parsed as an integer; the response shape may differ "
                "from what this command expects."
            )
        else:
            sui_info["raw_mist"] = cost_mist
            # SUI's decimal precision (9) is a fixed Sui protocol constant,
            # not a per-coin-type value read from CoinMetadata -- unlike
            # WAL below, which is a deployed coin whose decimals must never
            # be assumed.
            sui_info["sui"] = _format_token_amount(raw=cost_mist, decimals=9)

    wal_coin_type = client.config.network.wal_coin_type
    wal_change = next(
        (
            bc
            for bc in balance_changes
            if bc.coin_type
            and _matches_wal_coin_type(
                coin_type=bc.coin_type, wal_coin_type=wal_coin_type
            )
        ),
        None,
    )
    if wal_change is None:
        wal_info["unavailable_reason"] = (
            "No WAL entry found in the simulate result's balance_changes; "
            "the response shape may differ from what this command expects."
        )
    else:
        wal_info["coin_type"] = wal_change.coin_type
        try:
            cost_frost = -int(wal_change.amount)
        except (TypeError, ValueError):
            wal_info["unavailable_reason"] = (
                f"WAL balance change amount {wal_change.amount!r} could not "
                "be parsed as an integer; the response shape may differ "
                "from what this command expects."
            )
        else:
            wal_info["raw_frost"] = cost_frost
            meta_result = await client.execute(
                command=GetCoinMetaData(coin_type=wal_change.coin_type)
            )
            metadata = (
                meta_result.result_data.metadata if meta_result.is_ok() else None
            )
            if metadata is None or metadata.decimals is None:
                wal_info["unavailable_reason"] = (
                    f"WAL coin metadata for {wal_change.coin_type} could not "
                    "be read or has no decimals field; raw_frost is known "
                    "but its decimal rendering is not."
                )
            else:
                wal_info["wal"] = _format_token_amount(
                    raw=cost_frost, decimals=metadata.decimals
                )

    return sui_info, wal_info


async def _walrus_package_id(*, client: WalrusClient) -> tuple[str, str]:
    """Fetch the System object ID and the current Walrus package ID.

    Delegates to :func:`~pytusk.core.system_ops.resolve_package_id`, which
    reads the package ID from System.package_id rather than assuming it from
    a Blob's type-tag address (the type-tag address can go stale after a
    package upgrade) -- the same lookup this function used to perform
    inline. This is now the single implementation of that lookup; CLI-style
    error handling (print to stderr, exit 1) is preserved here so existing
    tusky commands are unaffected from a user's perspective, even though the
    underlying failure now surfaces as a ``RuntimeError`` from a library
    call rather than an inline ``GetObject`` check.

    Args:
        client (WalrusClient): Client used to fetch the System object.

    Returns:
        tuple[str, str]: (system_obj_id, walrus_pkg).
    """
    system_obj_id = client.config.network.system_object
    try:
        walrus_pkg = await resolve_package_id(client=client, system_object=system_obj_id)
    except RuntimeError as exc:
        print(f"Error fetching System object: {exc}", file=sys.stderr)
        sys.exit(1)
    return system_obj_id, walrus_pkg


def _blob_deletable_and_end_epoch(obj: sui_prot.Object) -> tuple[bool, int]:
    """Extract a Blob object's deletable flag and storage end_epoch.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus Blob.

    Returns:
        tuple[bool, int]: (deletable, end_epoch).

    Raises:
        ValueError: If the object's JSON view is missing fields a Walrus
            Blob object is expected to have (e.g. an incomplete RPC
            response). This is distinct from a normal blob with a real
            deletable/end_epoch value and must not be silently treated as
            "not eligible" by callers.
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id} has no JSON view; cannot determine "
            "deletable/end_epoch."
        )
    fields = obj.json.struct_value.fields
    storage_val = fields.get("storage")
    if not (storage_val and storage_val.struct_value):
        raise ValueError(
            f"Object {obj.object_id} is missing its 'storage' field; "
            "cannot determine end_epoch."
        )
    end_epoch_val = storage_val.struct_value.fields.get("end_epoch")
    if end_epoch_val is None:
        raise ValueError(
            f"Object {obj.object_id}'s storage field is missing 'end_epoch'."
        )
    end_epoch = int(end_epoch_val.number_value or 0)
    deletable_val = fields.get("deletable")
    if deletable_val is None:
        raise ValueError(f"Object {obj.object_id} is missing its 'deletable' field.")
    deletable = bool(deletable_val.bool_value)
    return deletable, end_epoch


def _blob_certified_epoch(*, obj: sui_prot.Object) -> int | None:
    """Extract a Blob object's certified_epoch, if the blob has been certified.

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus Blob.

    Returns:
        int | None: The epoch the blob was certified in, or None if the
            blob has been registered but not yet certified by storage nodes.

    Raises:
        ValueError: If the object's JSON view is missing the certified_epoch
            field entirely (e.g. an incomplete RPC response). Distinct from
            a normal uncertified blob, whose certified_epoch field is
            present but null, and must not be silently treated the same.
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id} has no JSON view; cannot determine "
            "certified_epoch."
        )
    certified_epoch_val = obj.json.struct_value.fields.get("certified_epoch")
    if certified_epoch_val is None:
        raise ValueError(
            f"Object {obj.object_id} is missing its 'certified_epoch' field."
        )
    if certified_epoch_val.null_value is not None:
        return None
    return int(certified_epoch_val.number_value or 0)

def _blob_id_bytes_from_object(obj: sui_prot.Object) -> bytes:
    """Extract a Blob object's raw 32-byte Walrus blob ID from its on-chain u256 field.

    Ported field-for-field from the ``blob_id`` parsing block in ``blobs()``:
    the on-chain ``Blob.blob_id`` is a Move ``u256``, surfaced in JSON as a
    decimal string, and is converted to raw bytes the same way
    ``pytusk.core.encoding.blob_id_to_u256`` converts the other direction
    (little-endian).

    Args:
        obj (sui_prot.Object): A fetched object expected to be a Walrus Blob.

    Returns:
        bytes: The raw 32-byte blob ID.

    Raises:
        ValueError: If the object's JSON view is missing its 'blob_id' field.
    """
    if not (obj.json and obj.json.struct_value):
        raise ValueError(
            f"Object {obj.object_id} has no JSON view; cannot determine blob_id."
        )
    fields = obj.json.struct_value.fields
    blob_id_val = fields.get("blob_id")
    if not (blob_id_val and blob_id_val.string_value):
        raise ValueError(f"Object {obj.object_id} is missing its 'blob_id' field.")
    return int(blob_id_val.string_value).to_bytes(32, byteorder="little")


async def _submit(*, client: WalrusClient, txdict: dict, mode: str) -> SuiRpcResult:
    """Simulate or execute a signed transaction dict, per --mode.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        txdict (dict): Result of AsyncSuiTransaction.build_and_sign().
        mode (str): "simulate" or "execute".

    Returns:
        SuiRpcResult: The result of the simulate or execute RPC call.
    """
    if mode == "simulate":
        return await client.execute(
            command=SimulateTransaction(tx_bytestr=txdict["tx_bytestr"])
        )
    return await client.execute(command=ExecuteTransaction(**txdict))


async def _burn_blob_batches(
    *,
    client: WalrusClient,
    walrus_pkg: str,
    blob_ids: list[str],
    sender: str,
    sponsor: str | None,
    mode: str,
    label: str = "Burned batch",
) -> list[sui_prot.ExecuteTransactionResponse | sui_prot.SimulateTransactionResponse]:
    """Burn blob objects via blob::burn, batched _MAX_BLOB_OPS_PER_PTB per PTB.

    Each batch's result is printed to stdout as soon as it completes, so a
    failure partway through still leaves a full record of every batch that
    succeeded beforehand — nothing is buffered until the end. A single
    invalid or already-consumed blob ID within a batch aborts that entire
    batch (all-or-nothing per batch); other, already-submitted batches are
    unaffected.

    Args:
        client (WalrusClient): Client used to build and submit transactions.
        walrus_pkg (str): Walrus package ID (from System.package_id).
        blob_ids (list[str]): Sui object IDs of blobs to burn.
        sender (str): Resolved sender address.
        sponsor (str | None): Resolved sponsor address, if any.
        mode (str): "simulate" or "execute".
        label (str): Prefix used in each batch's progress line.

    Returns:
        list[sui_prot.ExecuteTransactionResponse | sui_prot.SimulateTransactionResponse]:
            One result per successfully submitted batch transaction.
    """
    results: list[
        sui_prot.ExecuteTransactionResponse | sui_prot.SimulateTransactionResponse
    ] = []
    total_batches = (len(blob_ids) + _MAX_BLOB_OPS_PER_PTB - 1) // _MAX_BLOB_OPS_PER_PTB
    for batch_num, batch_start in enumerate(
        range(0, len(blob_ids), _MAX_BLOB_OPS_PER_PTB), start=1
    ):
        batch = blob_ids[batch_start : batch_start + _MAX_BLOB_OPS_PER_PTB]
        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        for blob_id in batch:
            await txn.move_call(
                target=f"{walrus_pkg}::blob::burn",
                arguments=[blob_id],
                type_arguments=[],
            )
        txdict = await txn.build_and_sign()
        result = await _submit(client=client, txdict=txdict, mode=mode)
        if not result.is_ok():
            print(
                f"Error burning batch {batch_num}/{total_batches} "
                f"({len(batch)} blob(s)): {result.result_string}",
                file=sys.stderr,
            )
            if results:
                print(
                    f"{len(results)}/{total_batches} batch(es) burned "
                    "successfully before this failure (see above for their "
                    "receipts).",
                    file=sys.stderr,
                )
            sys.exit(1)
        print(f"{label} {batch_num}/{total_batches} ({len(batch)} blob(s)).")
        print(result.result_data.to_json(indent=2))
        results.append(result.result_data)
    return results


async def read_blob(args: argparse.Namespace) -> None:
    """Read a blob via the Walrus HTTP aggregator and write its content to stdout.

    Args:
        args (argparse.Namespace): Parsed `read_blob` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=ReadBlob(blob_id=args.blobid))
    if not result.is_ok():
        print(f"Error reading blob: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: BlobData = result.result_data
    sys.stdout.buffer.write(data.content)
    if sys.stdout.isatty():
        sys.stdout.buffer.write(b"\n")


async def read_quilt(args: argparse.Namespace) -> None:
    """Read a single quilt patch via the Walrus HTTP aggregator and write it to stdout.

    Args:
        args (argparse.Namespace): Parsed `read_quilt` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(
            command=ReadQuiltPatch(quilt_id=args.quilt_id, patch_key=args.patch_key)
        )
    if not result.is_ok():
        print(f"Error reading quilt patch: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    data: QuiltPatch = result.result_data
    sys.stdout.buffer.write(data.content)
    if sys.stdout.isatty():
        sys.stdout.buffer.write(b"\n")


def _read_file_bytes(path: str | Path) -> bytes:
    """Read a file's raw bytes synchronously.

    Args:
        path (str | Path): Filesystem path to read. A ``Path`` is accepted
            because ``--file`` arguments validated via
            ``pysui.sui.sui_common.validators.ValidateFile`` store a ``Path``
            in the parsed namespace, not a raw string.

    Returns:
        bytes: The file's raw content.
    """
    with open(path, "rb") as f:
        return f.read()


async def store_blob(args: argparse.Namespace) -> None:
    """Store a blob via the Walrus HTTP publisher and print the resulting receipt.

    Content comes from --content (UTF-8 text) or --file (raw bytes), whichever
    was given. The blob object is sent to --recipient if given, otherwise to
    the active address, so it transfers to a wallet instead of staying with
    the publisher.

    Args:
        args (argparse.Namespace): Parsed `store_blob` subcommand arguments.
    """
    if args.file:
        try:
            data = await asyncio.to_thread(_read_file_bytes, args.file)
        except OSError as exc:
            print(f"Error reading file {args.file}: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        data = args.content.encode("utf-8")

    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        recipient = args.recipient or client.pysui_client.config.active_address
        result = await client.execute(
            command=StoreBlob(
                data=data,
                epochs=args.epochs,
                send_object_to=recipient,
                permanent=args.permanent,
            )
        )
    if not result.is_ok():
        print(f"Error storing blob: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


async def store_quilt(args: argparse.Namespace) -> None:
    """Store a quilt via the Walrus HTTP publisher and print the resulting receipt.

    Patch content comes from --paths (file paths, patch key = filename),
    --file (KEY=PATH, read from disk), and/or --content (KEY=TEXT, UTF-8
    encoded) — all repeatable and combinable. The quilt object is sent to
    --recipient if given, otherwise to the active address, so it transfers
    to a wallet instead of staying with the publisher.

    Args:
        args (argparse.Namespace): Parsed `store_quilt` subcommand arguments.
    """
    files: dict[str, bytes] = {}
    for path in args.paths:
        key = os.path.basename(path)
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        try:
            files[key] = await asyncio.to_thread(_read_file_bytes, path)
        except OSError as exc:
            print(f"Error reading file {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    for key, path in args.file:
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        try:
            files[key] = await asyncio.to_thread(_read_file_bytes, path)
        except OSError as exc:
            print(f"Error reading file {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    for key, text in args.content:
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        files[key] = text.encode("utf-8")

    if not files:
        print(
            "Error: at least one --paths, --file, or --content patch is required",
            file=sys.stderr,
        )
        sys.exit(1)

    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        recipient = args.recipient or client.pysui_client.config.active_address
        result = await client.execute(
            command=StoreQuilt(
                files=files,
                epochs=args.epochs,
                send_object_to=recipient,
                permanent=args.permanent,
            )
        )
    if not result.is_ok():
        print(f"Error storing quilt: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


async def blobs(args: argparse.Namespace) -> None:
    """List blobs owned by the active address, with optional filtering.

    Args:
        args (argparse.Namespace): Parsed `blobs` subcommand arguments,
            including `deletable` ("any"/"true"/"false") and `status`
            ("any"/"active"/"expired") filters.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        current_epoch = await client.walrus_epoch()
        owner = client.pysui_client.config.active_address
        objects_result = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=owner)
        )
    if not objects_result.is_ok():
        print(
            f"Error listing owned objects: {objects_result.result_string}",
            file=sys.stderr,
        )
        sys.exit(1)

    found = False
    for obj in objects_result.result_data.objects:
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            continue
        if not (obj.json and obj.json.struct_value):
            continue
        fields = obj.json.struct_value.fields

        end_epoch = 0
        storage_val = fields.get("storage")
        if storage_val and storage_val.struct_value:
            end_epoch_val = storage_val.struct_value.fields.get("end_epoch")
            end_epoch = int(end_epoch_val.number_value) if end_epoch_val else 0
        status = "expired" if end_epoch <= current_epoch else "active"

        deletable_val = fields.get("deletable")
        deletable = bool(deletable_val and deletable_val.bool_value)

        if args.deletable != "any" and str(deletable).lower() != args.deletable:
            continue
        if args.status != "any" and status != args.status:
            continue

        blob_id_b64 = ""
        blob_id_val = fields.get("blob_id")
        if blob_id_val and blob_id_val.string_value:
            try:
                blob_id_b64 = (
                    base64.urlsafe_b64encode(
                        int(blob_id_val.string_value).to_bytes(32, byteorder="little")
                    )
                    .rstrip(b"=")
                    .decode()
                )
            except (ValueError, OverflowError):
                blob_id_b64 = "(unparseable)"

        found = True
        print(
            f"{obj.object_id}  blob_id={blob_id_b64}  "
            f"deletable={deletable}  end_epoch={end_epoch}  status={status}"
        )

    if not found:
        print("No blobs found matching the given filters.")


async def expiry_report(args: argparse.Namespace) -> None:
    """Print an aging report of owned blobs, sorted soonest-to-expire first.

    Lists each blob's Sui object ID alongside its storage end_epoch, the
    current Walrus epoch, and the number of epochs remaining before
    expiration. A blob's status is "uncertified" if it has not yet been
    certified by storage nodes (regardless of remaining epochs), otherwise
    "expired" when remaining epochs is negative, "expiring" when exactly
    zero, and "active" when positive.

    Args:
        args (argparse.Namespace): Parsed `expiry_report` subcommand
            arguments, including an optional `address` override.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        current_epoch = await client.walrus_epoch()
        owner = args.address or client.pysui_client.config.active_address
        objects_result = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=owner)
        )
    if not objects_result.is_ok():
        print(
            f"Error listing owned objects: {objects_result.result_string}",
            file=sys.stderr,
        )
        sys.exit(1)

    rows: list[tuple[str, int, int, bool]] = []
    for obj in objects_result.result_data.objects:
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            continue
        _, end_epoch = _blob_deletable_and_end_epoch(obj)
        certified = _blob_certified_epoch(obj=obj) is not None
        rows.append((obj.object_id, end_epoch, end_epoch - current_epoch, certified))

    if not rows:
        print("No blobs found.")
        return

    rows.sort(key=lambda row: row[2])

    print(
        f"{'OBJECT ID':<66}  {'END_EPOCH':>10}  {'CURRENT_EPOCH':>13}  "
        f"{'REMAINING':>9}  STATUS"
    )
    for object_id, end_epoch, remaining, certified in rows:
        if not certified:
            status = "uncertified"
        elif remaining < 0:
            status = "expired"
        elif remaining == 0:
            status = "expiring"
        else:
            status = "active"
        print(
            f"{object_id:<66}  {end_epoch:>10}  {current_epoch:>13}  "
            f"{remaining:>9}  {status}"
        )


async def blob(args: argparse.Namespace) -> None:
    """Show full on-chain details for one blob object.

    Args:
        args (argparse.Namespace): Parsed `blob` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        result = await client.execute(command=GetObject(object_id=args.blobid))
    if not result.is_ok():
        print(f"Error fetching object: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


async def epoch(args: argparse.Namespace) -> None:
    """Print the current Walrus epoch.

    Args:
        args (argparse.Namespace): Parsed `epoch` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            current_epoch = await client.walrus_epoch()
        except RuntimeError as exc:
            print(f"Cannot get current Walrus epoch: {exc}", file=sys.stderr)
            sys.exit(1)
    print(current_epoch)


async def committee(args: argparse.Namespace) -> None:
    """Print the active Walrus storage committee.

    One line is printed per member. A member's leading index is its committee
    position, which is what ``signers_bitmap`` indexes. Public keys are shown
    truncated; they are 96 bytes on chain.

    Args:
        args (argparse.Namespace): Parsed `committee` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            walrus_committee = await client.committee()
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"Cannot get Walrus committee: {exc}", file=sys.stderr)
            sys.exit(1)
    print(
        f"epoch {walrus_committee.epoch}  "
        f"shards {walrus_committee.n_shards}  "
        f"members {walrus_committee.committee_size}"
    )
    for position, member in enumerate(walrus_committee.members):
        public_key = member.public_key.hex()
        print(
            f"{position:>4}  "
            f"{len(member.shard_indices):>4} shards  "
            f"{member.node_id}  "
            f"{public_key[:8]}..{public_key[-8:]}  "
            f"{member.network_address}"
        )


async def exchange_for_wal(args: argparse.Namespace) -> None:
    """Exchange SUI for WAL via the wal_exchange contract.

    Builds a PTB that splits --amount MIST of SUI from gas, exchanges it for
    WAL via wal_exchange::exchange_all_for_wal, and transfers the resulting
    WAL coin to the resolved sender. In simulate mode (the default) the
    transaction is dry-run and the projected effects are printed; in execute
    mode it is submitted and the receipt is printed.

    Args:
        args (argparse.Namespace): Parsed `exchange_for_wal` subcommand arguments.
    """
    config = _config_from_args(args)
    _require_testnet_exchange(config=config, command_name="exchange_for_wal")

    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = _resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = _resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        exchange_obj_id = client.config.network.exchange_objects[0]
        exchange_result = await client.execute(
            command=GetObject(object_id=exchange_obj_id)
        )
        if not exchange_result.is_ok():
            print(
                f"Error fetching exchange object: {exchange_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        wal_exchange_pkg = exchange_result.result_data.object_type.split("::")[0]

        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        split = await txn.split_coin(coin=txn.gas, amounts=[args.amount])
        wal_coin = cast(
            bcs.Argument,
            await txn.move_call(
                target=f"{wal_exchange_pkg}::wal_exchange::exchange_all_for_wal",
                arguments=[exchange_obj_id, split],
                type_arguments=[],
            ),
        )
        await txn.transfer_objects(transfers=[wal_coin], recipient=sender)
        txdict = await txn.build_and_sign()

        result = await _submit(client=client, txdict=txdict, mode=args.mode)

    if not result.is_ok():
        print(f"Error in exchange_for_wal: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


async def exchange_for_sui(args: argparse.Namespace) -> None:
    """Exchange WAL for SUI via the wal_exchange contract.

    Selects WAL coin(s) owned by the resolved sender to cover --amount FROST.
    If the largest owned coin covers the amount, no client-side split is
    needed: a coin whose balance exactly matches --amount is consumed whole
    via exchange_all_for_sui, while a larger coin is passed by mutable
    reference to exchange_for_sui, which splits the amount internally. If no
    single coin covers the amount, --merge must be given: coins are merged
    largest-first into the largest coin, stopping as soon as the running
    total covers --amount, before applying the same exact/greater-than logic
    to the merged coin. The resulting SUI coin is transferred to the
    resolved sender. In simulate mode (the default) the transaction is
    dry-run and the projected effects are printed; in execute mode it is
    submitted and the receipt is printed.

    Args:
        args (argparse.Namespace): Parsed `exchange_for_sui` subcommand
            arguments.
    """
    config = _config_from_args(args)
    _require_testnet_exchange(config=config, command_name="exchange_for_sui")

    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = _resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = _resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        exchange_obj_id = client.config.network.exchange_objects[0]
        exchange_result = await client.execute(
            command=GetObject(object_id=exchange_obj_id)
        )
        if not exchange_result.is_ok():
            print(
                f"Error fetching exchange object: {exchange_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        wal_exchange_pkg = exchange_result.result_data.object_type.split("::")[0]

        balances_result = await client.execute_for_all(
            command=GetAddressCoinBalances(owner=sender)
        )
        if not balances_result.is_ok():
            print(
                f"Error listing coin balances: {balances_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        wal_coin_type = client.config.network.wal_coin_type
        wal_entry = next(
            (
                entry
                for entry in balances_result.result_data.balances
                if entry.coin_type
                and _matches_wal_coin_type(
                    coin_type=entry.coin_type, wal_coin_type=wal_coin_type
                )
            ),
            None,
        )
        if wal_entry is None or not wal_entry.balance:
            print(f"No WAL coins found for sender {sender}.", file=sys.stderr)
            sys.exit(1)
        if wal_entry.balance < args.amount:
            print(
                f"Insufficient WAL balance: have {wal_entry.balance}, "
                f"need {args.amount}.",
                file=sys.stderr,
            )
            sys.exit(1)

        coins_result = await client.execute_for_all(
            command=GetCoins(
                owner=sender, coin_type=f"0x2::coin::Coin<{wal_entry.coin_type}>"
            )
        )
        if not coins_result.is_ok():
            print(
                f"Error listing WAL coins: {coins_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        coins = sorted(
            coins_result.result_data.objects,
            key=lambda c: c.balance or 0,
            reverse=True,
        )
        if not coins:
            print(f"No WAL coins found for sender {sender}.", file=sys.stderr)
            sys.exit(1)

        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )

        use_coin_id = coins[0].object_id
        use_balance = coins[0].balance or 0
        if use_balance < args.amount:
            if not args.merge:
                print(
                    f"No single WAL coin covers --amount {args.amount} "
                    f"(largest available: {use_balance}); pass --merge to "
                    "combine multiple WAL coins.",
                    file=sys.stderr,
                )
                sys.exit(1)
            merge_from: list[str | sui_prot.Object | bcs.Argument] = []
            for coin in coins[1:]:
                if use_balance >= args.amount:
                    break
                merge_from.append(coin.object_id)
                use_balance += coin.balance or 0
            if use_balance < args.amount:
                print(
                    "Insufficient WAL balance across owned coins: have "
                    f"{use_balance}, need {args.amount}.",
                    file=sys.stderr,
                )
                sys.exit(1)
            await txn.merge_coins(merge_to=use_coin_id, merge_from=merge_from)

        if use_balance == args.amount:
            sui_coin = cast(
                bcs.Argument,
                await txn.move_call(
                    target=f"{wal_exchange_pkg}::wal_exchange::exchange_all_for_sui",
                    arguments=[exchange_obj_id, use_coin_id],
                    type_arguments=[],
                ),
            )
        else:
            sui_coin = cast(
                bcs.Argument,
                await txn.move_call(
                    target=f"{wal_exchange_pkg}::wal_exchange::exchange_for_sui",
                    arguments=[exchange_obj_id, use_coin_id, args.amount],
                    type_arguments=[],
                ),
            )
        await txn.transfer_objects(transfers=[sui_coin], recipient=sender)
        txdict = await txn.build_and_sign()

        result = await _submit(client=client, txdict=txdict, mode=args.mode)

    if not result.is_ok():
        print(f"Error in exchange_for_sui: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


async def extend_blob_expiration(args: argparse.Namespace) -> None:
    """Extend a blob's storage expiration via system::extend_blob.

    Only the blob's expiry epoch changes; content, size, and object ID are
    unaffected. The target blob must not be expired; extend_blob does not
    gate on deletable vs permanent. Payment is a WAL coin passed by mutable
    reference — system::extend_blob deducts what it needs per extended
    epoch and leaves the remainder in the same coin object, so the exact
    cost does not need to be computed up front. If no single owned WAL
    coin covers the eventual cost, pass --merge to combine all owned WAL
    coins into one before extending.

    Args:
        args (argparse.Namespace): Parsed `extend_blob_expiration`
            subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = _resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = _resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        blob_result = await client.execute(command=GetObject(object_id=args.blobid))
        if not blob_result.is_ok():
            print(
                f"Error fetching blob object: {blob_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        obj = blob_result.result_data
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            print(f"{args.blobid} is not a Walrus Blob object.", file=sys.stderr)
            sys.exit(1)
        try:
            _, end_epoch = _blob_deletable_and_end_epoch(obj)
        except ValueError as exc:
            print(f"Error reading blob {args.blobid}: {exc}", file=sys.stderr)
            sys.exit(1)

        try:
            current_epoch = await client.walrus_epoch()
        except RuntimeError as exc:
            print(f"Cannot get current Walrus epoch: {exc}", file=sys.stderr)
            sys.exit(1)
        if end_epoch <= current_epoch:
            print(
                f"{args.blobid} is expired (end_epoch={end_epoch}, "
                f"current_epoch={current_epoch}); expired blobs cannot be "
                "extended.",
                file=sys.stderr,
            )
            sys.exit(1)

        system_obj_id, walrus_pkg = await _walrus_package_id(client=client)

        wal_entry, decimals = await _wal_balance_and_decimals(
            client=client, owner=sender
        )
        coins_result = await client.execute_for_all(
            command=GetCoins(
                owner=sender, coin_type=f"0x2::coin::Coin<{wal_entry.coin_type}>"
            )
        )
        if not coins_result.is_ok():
            print(
                f"Error listing WAL coins: {coins_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        coins = sorted(
            coins_result.result_data.objects,
            key=lambda c: c.balance or 0,
            reverse=True,
        )
        if not coins:
            print(f"No WAL coins found for sender {sender}.", file=sys.stderr)
            sys.exit(1)

        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        payment_coin_id = coins[0].object_id
        if args.merge and len(coins) > 1:
            await txn.merge_coins(
                merge_to=payment_coin_id,
                merge_from=[coin.object_id for coin in coins[1:]],
            )

        await txn.move_call(
            target=f"{walrus_pkg}::system::extend_blob",
            arguments=[system_obj_id, args.blobid, args.epochs, payment_coin_id],
            type_arguments=[],
        )
        txdict = await txn.build_and_sign()
        result = await _submit(client=client, txdict=txdict, mode=args.mode)
        if not result.is_ok():
            hint = (
                ""
                if args.merge
                else " If this is an insufficient-balance abort, retry with --merge."
            )
            print(
                f"Error in extend_blob_expiration: {result.result_string}.{hint}",
                file=sys.stderr,
            )
            sys.exit(1)
        print(result.result_data.to_json(indent=2))
        print(f"Extended {args.blobid}'s expiration by {args.epochs} epoch(s).")
        transaction = getattr(result.result_data, "transaction", None)
        if transaction is None:
            print(
                "WAL estimated cost: unavailable (response had no "
                "transaction/balance_changes data).",
                file=sys.stderr,
            )
        else:
            balance_changes = getattr(transaction, "balance_changes", None) or []
            wal_change = next(
                (
                    bc
                    for bc in balance_changes
                    if bc.address == sender and bc.coin_type == wal_entry.coin_type
                ),
                None,
            )
            if wal_change is not None:
                spent = -int(wal_change.amount)
                divisor = 10**decimals
                print(
                    f"WAL estimated cost: {spent} Frosts -> {spent / divisor:.4f} WAL"
                )


async def delete_blob(args: argparse.Namespace) -> None:
    """Delete one blob, or all active deletable blobs, owned by the sender.

    In -i mode, the target blob is deleted via system::delete_blob if it is
    deletable and not expired; if it is not eligible and --burn was given,
    it is burned instead via blob::burn (an ineligible blob without --burn
    is a clean error). Burning a blob that is not yet expired (i.e. it was
    burned only because it isn't deletable) prints a warning first, since
    that destroys an active, paid-for blob irreversibly. In --all-blobs
    mode, all active deletable blobs owned by the sender are deleted,
    batched _MAX_BLOB_OPS_PER_PTB per PTB; if --burn was given, expired
    blobs (any type) are additionally burned in a separate batched pass.
    A blob object whose on-chain data can't be fully read (e.g. an
    incomplete RPC response) is treated as a hard error in -i mode, and
    skipped with a warning in --all-blobs mode — never silently treated as
    "not eligible."

    Args:
        args (argparse.Namespace): Parsed `delete_blob` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = _resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = _resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        try:
            current_epoch = await client.walrus_epoch()
        except RuntimeError as exc:
            print(f"Cannot get current Walrus epoch: {exc}", file=sys.stderr)
            sys.exit(1)

        system_obj_id, walrus_pkg = await _walrus_package_id(client=client)

        if args.blobid:
            blob_result = await client.execute(command=GetObject(object_id=args.blobid))
            if not blob_result.is_ok():
                print(
                    f"Error fetching blob object: {blob_result.result_string}",
                    file=sys.stderr,
                )
                sys.exit(1)
            obj = blob_result.result_data
            if not (obj.object_type and "::blob::Blob" in obj.object_type):
                print(f"{args.blobid} is not a Walrus Blob object.", file=sys.stderr)
                sys.exit(1)
            try:
                deletable, end_epoch = _blob_deletable_and_end_epoch(obj)
            except ValueError as exc:
                print(f"Error reading blob {args.blobid}: {exc}", file=sys.stderr)
                sys.exit(1)

            txn: AsyncSuiTransaction = await client.transaction(
                initial_sender=sender, initial_sponsor=sponsor
            )
            if deletable and end_epoch > current_epoch:
                storage = cast(
                    bcs.Argument,
                    await txn.move_call(
                        target=f"{walrus_pkg}::system::delete_blob",
                        arguments=[system_obj_id, args.blobid],
                        type_arguments=[],
                    ),
                )
                await txn.transfer_objects(transfers=[storage], recipient=sender)
                action = "Deleted"
            elif args.burn:
                if end_epoch > current_epoch:
                    print(
                        f"Warning: {args.blobid} is not expired "
                        f"(end_epoch={end_epoch}, current_epoch={current_epoch}) "
                        "and not deletable; burning it now destroys an "
                        "active, paid-for blob irreversibly.",
                        file=sys.stderr,
                    )
                await txn.move_call(
                    target=f"{walrus_pkg}::blob::burn",
                    arguments=[args.blobid],
                    type_arguments=[],
                )
                action = "Burned"
            else:
                print(
                    f"{args.blobid} is not eligible for delete_blob "
                    f"(deletable={deletable}, end_epoch={end_epoch}, "
                    f"current_epoch={current_epoch}); pass --burn to burn it instead.",
                    file=sys.stderr,
                )
                sys.exit(1)

            txdict = await txn.build_and_sign()
            result = await _submit(client=client, txdict=txdict, mode=args.mode)
            if not result.is_ok():
                print(f"Error in delete_blob: {result.result_string}", file=sys.stderr)
                sys.exit(1)
            print(f"{action} {args.blobid}.")
            print(result.result_data.to_json(indent=2))
            return

        objects_result = await client.execute_for_all(
            command=GetObjectsOwnedByAddress(owner=sender)
        )
        if not objects_result.is_ok():
            print(
                f"Error listing owned objects: {objects_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)

        active_deletable: list[str] = []
        expired: list[str] = []
        for obj in objects_result.result_data.objects:
            if not (obj.object_type and "::blob::Blob" in obj.object_type):
                continue
            try:
                deletable, end_epoch = _blob_deletable_and_end_epoch(obj)
            except ValueError as exc:
                print(
                    f"Warning: skipping blob {obj.object_id}: {exc}",
                    file=sys.stderr,
                )
                continue
            if end_epoch <= current_epoch:
                expired.append(obj.object_id)
            elif deletable:
                active_deletable.append(obj.object_id)

        if not active_deletable and not (args.burn and expired):
            print("No blobs to delete.")
            return

        if active_deletable:
            total_batches = (
                len(active_deletable) + _MAX_BLOB_OPS_PER_PTB - 1
            ) // _MAX_BLOB_OPS_PER_PTB
            for batch_num, batch_start in enumerate(
                range(0, len(active_deletable), _MAX_BLOB_OPS_PER_PTB), start=1
            ):
                batch = active_deletable[
                    batch_start : batch_start + _MAX_BLOB_OPS_PER_PTB
                ]
                txn = await client.transaction(
                    initial_sender=sender, initial_sponsor=sponsor
                )
                storage_objects = []
                for blob_id in batch:
                    storage = cast(
                        bcs.Argument,
                        await txn.move_call(
                            target=f"{walrus_pkg}::system::delete_blob",
                            arguments=[system_obj_id, blob_id],
                            type_arguments=[],
                        ),
                    )
                    storage_objects.append(storage)
                await txn.transfer_objects(transfers=storage_objects, recipient=sender)
                txdict = await txn.build_and_sign()
                result = await _submit(client=client, txdict=txdict, mode=args.mode)
                if not result.is_ok():
                    print(
                        f"Error deleting batch {batch_num}/{total_batches} "
                        f"({len(batch)} blob(s)): {result.result_string}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                print(
                    f"Deleted batch {batch_num}/{total_batches} ({len(batch)} blob(s))."
                )
                print(result.result_data.to_json(indent=2))

        if args.burn and expired:
            await _burn_blob_batches(
                client=client,
                walrus_pkg=walrus_pkg,
                blob_ids=expired,
                sender=sender,
                sponsor=sponsor,
                mode=args.mode,
                label="Burned expired batch",
            )


async def burn_blob(args: argparse.Namespace) -> None:
    """Burn one or more blob objects directly via blob::burn.

    Duplicate object IDs passed via repeated -i/--blobid are de-duplicated
    (preserving first-seen order) before batching, since a repeated
    reference to an already-consumed object argument would abort that
    batch. Each target's on-chain state is checked before burning; a blob
    that is not yet expired prints a warning first, since burning it
    destroys an active, paid-for blob irreversibly. A blob ID that can't
    be fetched, isn't a Walrus Blob object, or has unreadable on-chain
    data is skipped with a warning rather than burned blind.

    Args:
        args (argparse.Namespace): Parsed `burn_blob` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = _resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = _resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        try:
            current_epoch = await client.walrus_epoch()
        except RuntimeError as exc:
            print(f"Cannot get current Walrus epoch: {exc}", file=sys.stderr)
            sys.exit(1)

        _, walrus_pkg = await _walrus_package_id(client=client)
        blob_ids = list(dict.fromkeys(args.blobid))

        confirmed_ids: list[str] = []
        for blob_id in blob_ids:
            blob_result = await client.execute(command=GetObject(object_id=blob_id))
            if not blob_result.is_ok():
                print(
                    f"Warning: skipping {blob_id}: {blob_result.result_string}",
                    file=sys.stderr,
                )
                continue
            obj = blob_result.result_data
            if not (obj.object_type and "::blob::Blob" in obj.object_type):
                print(
                    f"Warning: skipping {blob_id}: not a Walrus Blob object.",
                    file=sys.stderr,
                )
                continue
            try:
                _, end_epoch = _blob_deletable_and_end_epoch(obj)
            except ValueError as exc:
                print(f"Warning: skipping {blob_id}: {exc}", file=sys.stderr)
                continue
            if end_epoch > current_epoch:
                print(
                    f"Warning: {blob_id} is not expired (end_epoch={end_epoch}, "
                    f"current_epoch={current_epoch}); burning it now destroys "
                    "an active, paid-for blob irreversibly.",
                    file=sys.stderr,
                )
            confirmed_ids.append(blob_id)

        if not confirmed_ids:
            print("No blobs to burn.")
            return

        await _burn_blob_batches(
            client=client,
            walrus_pkg=walrus_pkg,
            blob_ids=confirmed_ids,
            sender=sender,
            sponsor=sponsor,
            mode=args.mode,
        )


async def wal_coins(args: argparse.Namespace) -> None:
    """List WAL coin objects owned by an address, styled on pysui's gas layout.

    Args:
        args (argparse.Namespace): Parsed `wal_coins` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        owner = args.address or client.pysui_client.config.active_address
        wal_entry, decimals = await _wal_balance_and_decimals(
            client=client, owner=owner
        )
        coins_result = await client.execute_for_all(
            command=GetCoins(
                owner=owner, coin_type=f"0x2::coin::Coin<{wal_entry.coin_type}>"
            )
        )
    if not coins_result.is_ok():
        print(f"Error listing WAL coins: {coins_result.result_string}", file=sys.stderr)
        sys.exit(1)

    divisor = 10**decimals
    print()
    print(f"{'WAL Object ID':^75}   {'Frost':>12}   {'WAL':>10}")
    print("-" * 107)
    for coin in coins_result.result_data.objects:
        balance = coin.balance or 0
        print(f"{coin.object_id} has {balance:>12} -> {balance / divisor:>8.4f}")
    print()

    coin_balance = wal_entry.coin_balance or 0
    address_balance = wal_entry.address_balance or 0
    total = wal_entry.balance or 0
    print(f"Coin-All wal: {coin_balance:>12} -> {coin_balance / divisor:>8.4f}")
    print(f"Addr-All wal: {address_balance:>12} -> {address_balance / divisor:>8.4f}")
    print(f"   Total wal: {total:>12} -> {total / divisor:>8.4f}")


# --- tusky CLI logging configuration ------------------------------------
# pytusk (the library, everything outside pytusk/tusky/) only ever emits
# log records -- it never configures handlers, levels, or any other
# global logging state (see pytusk/__init__.py's NullHandler). tusky, as
# an APPLICATION built on top of pytusk, is entitled to configure logging,
# but only when the user explicitly asks for it via --log-file/--verbose
# on store_blob_native (the only command with progress/heartbeat
# instrumentation today -- see pytusk.core.native_upload's module-level
# comment near _HEARTBEAT_INTERVAL_SECONDS), and never by writing to a
# derived or default path.
# ------------------------------------------------------------------------

_NATIVE_UPLOAD_LOGGING_CONFIGURED: bool = False


def _configure_native_upload_logging(
    *, log_file: Path | None, verbose: bool
) -> None:
    """Configure the ``pytusk`` logger hierarchy per the user's CLI request.

    This is tusky's own opt-in logging setup, not library scaffolding --
    see the module comment immediately above. Adds an INFO-level stdout
    stream handler to the ``pytusk`` logger only when ``verbose`` is True,
    and/or an INFO-level file handler at exactly ``log_file`` only when it
    is given -- NEVER a derived or default path. Does nothing at all when
    neither is requested. The ``pytusk`` logger is the parent of
    ``pytusk.core.native_upload``'s ``_logger = logging.getLogger(__name__)``,
    so that module's progress/heartbeat records propagate up to whichever
    destination(s) were configured. When ``verbose`` is True, stdout is
    also reconfigured for line buffering so progress is visible live even
    when output is redirected to a file. Idempotent: a second call in the
    same process is a no-op, so handlers are never duplicated.

    Args:
        log_file (Path | None): Path to write an INFO-level log file to,
            exactly as given (no default, no derived path); ``None`` to
            skip file logging.
        verbose (bool): Whether to emit INFO-level progress to stdout.
    """
    global _NATIVE_UPLOAD_LOGGING_CONFIGURED

    if not log_file and not verbose:
        return
    if _NATIVE_UPLOAD_LOGGING_CONFIGURED:
        return

    logger = logging.getLogger("pytusk")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    if verbose:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    if log_file is not None:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _NATIVE_UPLOAD_LOGGING_CONFIGURED = True


async def store_blob_native(args: argparse.Namespace) -> None:
    """Store a blob via the native Walrus upload pipeline and print the receipt.

    Content comes from --content (UTF-8 text) or --file (raw bytes), whichever
    was given. In execute mode (the full pipeline: reserve_space+register_blob,
    sliver fan-out, confirmation collection, certify_blob) this delegates to
    the library's :func:`~pytusk.core.native_upload.store_blob_native`
    convenience. In simulate mode ONLY Tx1 (reserve_space+register_blob) is
    simulated -- a real simulation, unlike the rest of the pipeline, which is
    skipped because simulation never registers the blob on-chain, so storage
    nodes would reject sliver PUTs and there would be nothing to certify.

    Simulate mode prints a concise JSON cost summary by DEFAULT (blob size,
    encoded storage_amount, epochs, SUI/WAL cost, stage timings) rather than
    the full raw simulate transaction JSON, since the latter can run to
    thousands of lines (a multi-kilobyte base64 Walrus-state dump included)
    for a single-digit-line answer to "what will this cost?". Pass
    --full-json to additionally print the complete raw simulate result.

    Pass --log-file PATH to additionally write an INFO-level log of this
    run's progress to PATH, and/or --verbose to emit that same INFO-level
    progress to stdout live; neither is enabled by default (see
    _configure_native_upload_logging()).

    Args:
        args (argparse.Namespace): Parsed `store_blob_native` subcommand
            arguments.
    """
    # tusky's opt-in logging setup -- see the module comment near
    # _configure_native_upload_logging() above. Does nothing unless the
    # user passed --log-file and/or --verbose.
    _configure_native_upload_logging(log_file=args.log_file, verbose=args.verbose)
    if args.log_file is not None:
        print(f"Native upload log: {args.log_file}")

    if args.file:
        try:
            data = await asyncio.to_thread(_read_file_bytes, args.file)
        except OSError as exc:
            print(f"Error reading file {args.file}: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        data = args.content.encode("utf-8")

    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = _resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = _resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        if args.mode == "execute":
            try:
                receipt = await _store_blob_native_pipeline(
                    client=client,
                    data=data,
                    epochs=args.epochs,
                    deletable=not args.permanent,
                    sender=sender,
                    sponsor=sponsor,
                    recipient=args.recipient,
                )
            except BlobTooLargeError as exc:
                print(f"Error in encode: {exc}", file=sys.stderr)
                sys.exit(1)
            except NativeUploadError as exc:
                print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
                sys.exit(1)
            except (RuntimeError, KeyError, TypeError, ValueError) as exc:
                # Everything before registration (committee/epoch reads,
                # package-ID resolution, WAL coin selection, and Tx1 itself)
                # is bucketed under "register (Tx1)" -- the library does not
                # distinguish these sub-stages the way it does for the
                # post-registration NativeUploadError family.
                print(f"Error in register (Tx1): {exc}", file=sys.stderr)
                sys.exit(1)

            if receipt.failed_stage is not None:
                print(json.dumps(dataclasses.asdict(receipt), indent=2))
                print(
                    f"Error in {receipt.failed_stage}: upload did not "
                    "complete (blob registered but not certified).",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(json.dumps(dataclasses.asdict(receipt), indent=2))
            return

        # --mode simulate: Tx1 only. Only encode and Tx1 build+simulate
        # actually run, so only those two stages get a timing -- the rest
        # are honestly reported as None (they did not run).
        pipeline_start = time.monotonic()
        try:
            committee = await client.committee()
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"Error in register (Tx1): {exc}", file=sys.stderr)
            sys.exit(1)
        encode_start = time.monotonic()
        try:
            encoded = await asyncio.to_thread(
                functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
            )
        except BlobTooLargeError as exc:
            print(f"Error in encode: {exc}", file=sys.stderr)
            sys.exit(1)
        encode_duration = time.monotonic() - encode_start

        system_obj_id, walrus_pkg = await _walrus_package_id(client=client)
        try:
            payment_coin = await select_wal_payment_coin(client=client, owner=sender)
        except RuntimeError as exc:
            print(f"Error selecting WAL payment coin: {exc}", file=sys.stderr)
            sys.exit(1)

        register_tx1_start = time.monotonic()
        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        blob = await add_reserve_and_register(
            txn=txn,
            package_id=walrus_pkg,
            system_object=system_obj_id,
            encoded=encoded,
            epochs=args.epochs,
            deletable=not args.permanent,
            payment_coin=payment_coin,
        )
        # Tx1 always transfers the newly registered Blob to the sender --
        # matching pytusk.core.system_ops.execute_reserve_and_register.
        # --recipient (when given) is handled by Tx2 (certify_blob), which
        # this simulate mode does NOT model (see the "notice" field below),
        # so it plays no part in this Tx1-only cost estimate.
        await txn.transfer_objects(transfers=[blob], recipient=sender)
        txdict = await txn.build_and_sign()
        result = await _submit(client=client, txdict=txdict, mode="simulate")
        register_tx1_duration = time.monotonic() - register_tx1_start
        if not result.is_ok():
            print(f"Error in register (Tx1): {result.result_string}", file=sys.stderr)
            sys.exit(1)

        if args.full_json:
            print(result.result_data.to_json(indent=2))

        storage_amount = encoded_blob_length(
            unencoded_length=encoded.unencoded_length, n_shards=encoded.n_shards
        )
        transaction = getattr(result.result_data, "transaction", None)
        sui_cost, wal_cost = await _simulate_cost_from_balance_changes(
            client=client, transaction=transaction
        )
        timings = StageTimings(
            encode=encode_duration,
            register_tx1=register_tx1_duration,
            sliver_upload=None,
            confirmations=None,
            certify_tx2=None,
            total=time.monotonic() - pipeline_start,
        )
        summary = {
            "mode": "simulate",
            "blob_size_bytes": len(data),
            "encoded_storage_amount_bytes": storage_amount,
            "epochs": args.epochs,
            "permanent": args.permanent,
            "cost": {"sui": sui_cost, "wal": wal_cost},
            "timings": dataclasses.asdict(timings),
            "notice": (
                "Only Tx1 (reserve_space + register_blob) was simulated. "
                "Sliver fan-out and Tx2 (certify_blob) were SKIPPED because "
                "simulation does not register the blob on-chain, so storage "
                "nodes would reject sliver PUTs and there is nothing yet to "
                "certify. --recipient (if given) transfers the Blob as part "
                "of Tx2, so it is not reflected in this Tx1-only cost "
                "estimate; Tx1 always transfers the Blob to --sender."
            ),
        }
        print(json.dumps(summary, indent=2))


async def certify_blob(args: argparse.Namespace) -> None:
    """Recover the confirmation-collection and certify_blob stages for an
    already-registered blob.

    This is the recovery entry point for a native upload that completed Tx1
    (reserve_space+register_blob) but died before or during Tx2
    (certify_blob): given only the blob's Sui object ID, it re-derives the
    real Walrus blob ID from the on-chain Blob object's `blob_id` u256 field
    (see :func:`_blob_id_bytes_from_object`), re-collects a fresh quorum of
    storage-node confirmations, and certifies. It does NOT re-upload slivers
    -- if the original sliver fan-out did not reach quorum, confirmation
    collection here will also fail, and there is no `--file` recovery path
    (nothing here reconstructs the original bytes to re-encode and re-upload
    slivers).

    In simulate mode, confirmation collection is real (registration already
    exists on-chain) and only Tx2 itself is simulated -- unlike
    store_blob_native's simulate mode, this is a fully honest simulation.

    Args:
        args (argparse.Namespace): Parsed `certify_blob` subcommand arguments.
    """
    config = _config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        pipeline_start = time.monotonic()
        try:
            sender = _resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = _resolve_sponsor(
                config=client.pysui_client.config, sponsor_arg=args.sponsor
            )
        except ValueError as exc:
            print(f"Error resolving --sender/--sponsor: {exc}", file=sys.stderr)
            sys.exit(1)

        blob_result = await client.execute(command=GetObject(object_id=args.blobid))
        if not blob_result.is_ok():
            print(
                f"Error fetching blob object: {blob_result.result_string}",
                file=sys.stderr,
            )
            sys.exit(1)
        obj = blob_result.result_data
        if not (obj.object_type and "::blob::Blob" in obj.object_type):
            print(f"{args.blobid} is not a Walrus Blob object.", file=sys.stderr)
            sys.exit(1)
        try:
            blob_id_bytes = _blob_id_bytes_from_object(obj)
            deletable, end_epoch = _blob_deletable_and_end_epoch(obj)
        except ValueError as exc:
            print(f"Error reading blob {args.blobid}: {exc}", file=sys.stderr)
            sys.exit(1)

        try:
            committee = await client.committee()
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"Cannot get Walrus committee: {exc}", file=sys.stderr)
            sys.exit(1)

        registration = Registration(
            object_id=args.blobid,
            blob_id=blob_id_bytes,
            end_epoch=end_epoch,
            deletable=deletable,
            digest="",  # Tx1's digest is not known in this recovery flow.
        )
        system_obj_id, walrus_pkg = await _walrus_package_id(client=client)
        staking_object = client.config.network.staking_object

        confirmations_start = time.monotonic()
        try:
            certificate = await collect_confirmations(
                client=client,
                committee=committee,
                blob_id=blob_id_bytes,
                registration=registration,
            )
        except NativeUploadError as exc:
            print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
            sys.exit(1)
        finally:
            confirmations_duration = time.monotonic() - confirmations_start

        if args.mode == "execute":
            try:
                receipt = await certify(
                    client=client,
                    committee=committee,
                    blob_id=blob_id_bytes,
                    registration=registration,
                    certificate=certificate,
                    package_id=walrus_pkg,
                    system_object=system_obj_id,
                    staking_object=staking_object,
                    sender=sender,
                    sponsor=sponsor,
                    stage_timings=StageTimings(
                        encode=None,
                        register_tx1=None,
                        sliver_upload=None,
                        confirmations=confirmations_duration,
                        certify_tx2=None,
                        total=None,
                    ),
                )
            except CertifyTransactionError as exc:
                # Tx2 (certify_blob) failed to submit or aborted on-chain.
                # Before native_upload's certify() converted this into a
                # CertifyTransactionError, it reached this handler as a
                # bare RuntimeError and was reported here with this exact
                # "(Tx2)" wording -- kept unchanged so the CLI's Tx2-failure
                # output is unaffected by that internal conversion.
                print(f"Error in certify (Tx2): {exc}", file=sys.stderr)
                sys.exit(1)
            except NativeUploadError as exc:
                print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
                sys.exit(1)
            except RuntimeError as exc:
                print(f"Error in certify (Tx2): {exc}", file=sys.stderr)
                sys.exit(1)

            receipt = dataclasses.replace(
                receipt,
                timings=dataclasses.replace(
                    receipt.timings, total=time.monotonic() - pipeline_start
                ),
            )
            if receipt.failed_stage is not None:
                print(json.dumps(dataclasses.asdict(receipt), indent=2))
                print(
                    f"Error in {receipt.failed_stage}: certification did "
                    "not complete.",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(json.dumps(dataclasses.asdict(receipt), indent=2))
            return

        # --mode simulate: confirmations above are real; only Tx2 is simulated.
        certify_tx2_start = time.monotonic()
        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=sponsor
        )
        await add_certify(
            txn=txn,
            package_id=walrus_pkg,
            system_object=system_obj_id,
            blob_object_id=args.blobid,
            certificate=certificate,
        )
        txdict = await txn.build_and_sign()
        result = await _submit(client=client, txdict=txdict, mode="simulate")
        certify_tx2_duration = time.monotonic() - certify_tx2_start
        if not result.is_ok():
            print(f"Error in certify (Tx2): {result.result_string}", file=sys.stderr)
            sys.exit(1)
        print(result.result_data.to_json(indent=2))
        timings = StageTimings(
            encode=None,
            register_tx1=None,
            sliver_upload=None,
            confirmations=confirmations_duration,
            certify_tx2=certify_tx2_duration,
            total=time.monotonic() - pipeline_start,
        )
        print(json.dumps({"timings": dataclasses.asdict(timings)}, indent=2))
