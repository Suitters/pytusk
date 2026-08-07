#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Command handlers for the tusky CLI.

Each handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk/pysui operation. Handlers are async;
tusky.py drives them via asyncio.run.
"""

import argparse
import base64
import sys
from typing import cast

from pysui import (
    ExecuteTransaction,
    GetAddressCoinBalances,
    GetCoinMetaData,
    GetCoins,
    GetObject,
    GetObjectsOwnedByAddress,
    PysuiConfiguration,
    SimulateTransaction,
)
from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction
import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot

from pytusk import (
    BlobData,
    PytuskConfiguration,
    ReadBlob,
    StoreBlob,
    WalrusClient,
    get_walrus_epoch,
)


def _config_from_args(args: argparse.Namespace) -> PytuskConfiguration:
    """Build a PytuskConfiguration from a subcommand's shared config arguments.

    Args:
        args (argparse.Namespace): Parsed arguments carrying the
            `_add_config_args` fields (from_cfg_path, pysui_config_path,
            pysui_group_name, pysui_profile_name, pysui_address, pysui_alias).

    Returns:
        PytuskConfiguration: Configuration for this CLI invocation.
    """
    return PytuskConfiguration(
        from_cfg_path=args.from_cfg_path,
        pysui_config_path=args.pysui_config_path,
        pysui_group_name=args.pysui_group_name,
        pysui_profile_name=args.pysui_profile_name,
        pysui_address=args.pysui_address,
        pysui_alias=args.pysui_alias,
    )


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
    if sender_arg.startswith("0x"):
        config.alias_for_address(address=sender_arg)
        return sender_arg
    return config.address_for_alias(alias_name=sender_arg)


def _require_testnet_exchange(*, config: PytuskConfiguration, command_name: str) -> None:
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
    wal_entry = next(
        (
            entry
            for entry in balances_result.result_data.balances
            if entry.coin_type and "::wal::WAL" in entry.coin_type
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
            with open(args.file, "rb") as f:
                data = f.read()
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
                deletable=args.deletable,
                permanent=args.permanent,
            )
        )
    if not result.is_ok():
        print(f"Error storing blob: {result.result_string}", file=sys.stderr)
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
        current_epoch = await get_walrus_epoch(client)
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
            blob_id_b64 = (
                base64.urlsafe_b64encode(
                    int(blob_id_val.string_value).to_bytes(32, byteorder="little")
                )
                .rstrip(b"=")
                .decode()
            )

        found = True
        print(
            f"{obj.object_id}  blob_id={blob_id_b64}  "
            f"deletable={deletable}  end_epoch={end_epoch}  status={status}"
        )

    if not found:
        print("No blobs found matching the given filters.")


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


async def exchange_for_wal(args: argparse.Namespace) -> None:
    """Exchange SUI for WAL via the testnet wal_exchange contract.

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
        except ValueError as exc:
            print(f"Error resolving --sender: {exc}", file=sys.stderr)
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
            initial_sender=sender, initial_sponsor=args.sponsor
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

        if args.mode == "simulate":
            result = await client.execute(
                command=SimulateTransaction(tx_bytestr=txdict["tx_bytestr"])
            )
        else:
            result = await client.execute(command=ExecuteTransaction(**txdict))

    if not result.is_ok():
        print(f"Error in exchange_for_wal: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


async def exchange_for_sui(args: argparse.Namespace) -> None:
    """Exchange WAL for SUI via the testnet wal_exchange contract.

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
        except ValueError as exc:
            print(f"Error resolving --sender: {exc}", file=sys.stderr)
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
        wal_entry = next(
            (
                entry
                for entry in balances_result.result_data.balances
                if entry.coin_type and "::wal::WAL" in entry.coin_type
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

        txn: AsyncSuiTransaction = await client.transaction(
            initial_sender=sender, initial_sponsor=args.sponsor
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

        if args.mode == "simulate":
            result = await client.execute(
                command=SimulateTransaction(tx_bytestr=txdict["tx_bytestr"])
            )
        else:
            result = await client.execute(command=ExecuteTransaction(**txdict))

    if not result.is_ok():
        print(f"Error in exchange_for_sui: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))


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
        print(
            f"Error listing WAL coins: {coins_result.result_string}", file=sys.stderr
        )
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
