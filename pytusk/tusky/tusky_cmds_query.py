#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Query command handlers for the tusky CLI.

Read-only informational commands: listing/inspecting owned blobs, the
current Walrus epoch, the active storage committee, and owned WAL coins.
Each handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk/pysui operation. Handlers are async;
tusky.py drives them via asyncio.run.
"""

import argparse
import base64
import sys

from pysui import GetCoins, GetObject, GetObjectsOwnedByAddress

from pytusk import WalrusClient, storage_from_blob
from pytusk.core.chain import (
    blob_certified_epoch,
    blob_deletable_and_end_epoch,
)
from pytusk.tusky.tusky_cmds_common import (
    config_from_args,
    wal_balance_and_decimals,
)


async def blobs(args: argparse.Namespace) -> None:
    """List blobs owned by the active address, with optional filtering.

    Args:
        args (argparse.Namespace): Parsed `blobs` subcommand arguments,
            including `deletable` ("any"/"true"/"false") and `status`
            ("any"/"active"/"expired") filters.
    """
    config = config_from_args(args)
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
        blob_size = storage_from_blob(obj=obj).storage_size
        print(
            f"{obj.object_id}  blob_id={blob_id_b64}  "
            f"deletable={deletable}  end_epoch={end_epoch}  status={status}  "
            f"size={blob_size}"
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
    config = config_from_args(args)
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
        _, end_epoch = blob_deletable_and_end_epoch(obj=obj)
        certified = blob_certified_epoch(obj=obj) is not None
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
    config = config_from_args(args)
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
    config = config_from_args(args)
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
    config = config_from_args(args)
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


async def wal_coins(args: argparse.Namespace) -> None:
    """List WAL coin objects owned by an address, styled on pysui's gas layout.

    Args:
        args (argparse.Namespace): Parsed `wal_coins` subcommand arguments.
    """
    config = config_from_args(args)
    async with WalrusClient(pytusk_config=config) as client:
        owner = args.address or client.pysui_client.config.active_address
        wal_entry, decimals = await wal_balance_and_decimals(
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
