#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Blob lifecycle command handlers for the tusky CLI.

Handlers for extending, deleting, and burning blobs. Each handler takes
the parsed argparse.Namespace for its subcommand and performs the
corresponding pytusk/pysui operation. Handlers are async; tusky.py drives
them via asyncio.run.
"""

import argparse
import sys
from typing import cast

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import GetCoins, GetObject, GetObjectsOwnedByAddress
from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk import WalrusClient, blob_deletable_and_end_epoch
from pytusk.tusky.tusky_cmds_common import (
    config_from_args,
    resolve_sender,
    resolve_sponsor,
    submit,
    wal_balance_and_decimals,
    walrus_package_id,
)

_MAX_BLOB_OPS_PER_PTB = 100


async def _burn_blob_batches(
    *,
    client: WalrusClient,
    walrus_pkg: str,
    blob_ids: list[str],
    sender: str,
    sponsor: str | None,
    mode: str,
    label: str = "Burned batch",
) -> list[sui_prot.ExecutedTransaction | sui_prot.SimulateTransactionResponse]:
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
        list[sui_prot.ExecutedTransaction | sui_prot.SimulateTransactionResponse]:
            One result per successfully submitted batch transaction.
    """
    results: list[
        sui_prot.ExecutedTransaction | sui_prot.SimulateTransactionResponse
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
        result = await submit(client=client, txdict=txdict, mode=mode)
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
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
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
            _, end_epoch = blob_deletable_and_end_epoch(obj=obj)
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

        system_obj_id, walrus_pkg = await walrus_package_id(client=client)

        wal_entry, decimals = await wal_balance_and_decimals(
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
        result = await submit(client=client, txdict=txdict, mode=args.mode)
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
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
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

        system_obj_id, walrus_pkg = await walrus_package_id(client=client)

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
                deletable, end_epoch = blob_deletable_and_end_epoch(obj=obj)
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
            result = await submit(client=client, txdict=txdict, mode=args.mode)
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
                deletable, end_epoch = blob_deletable_and_end_epoch(obj=obj)
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
                result = await submit(client=client, txdict=txdict, mode=args.mode)
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
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        try:
            sender = resolve_sender(
                config=client.pysui_client.config, sender_arg=args.sender
            )
            sponsor = resolve_sponsor(
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

        _, walrus_pkg = await walrus_package_id(client=client)
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
                _, end_epoch = blob_deletable_and_end_epoch(obj=obj)
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
