#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Exchange command handlers for the tusky CLI.

Handlers for exchanging SUI/WAL via the testnet wal_exchange contract.
Each handler takes the parsed argparse.Namespace for its subcommand and
performs the corresponding pytusk/pysui operation. Handlers are async;
tusky.py drives them via asyncio.run.
"""

import argparse
import sys
from typing import cast

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import GetAddressCoinBalances, GetCoins, GetObject
from pysui.sui.sui_bcs import bcs
from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk import PytuskConfiguration, WalrusClient, matches_wal_coin_type
from pytusk.tusky.tusky_cmds_common import (
    config_from_args,
    resolve_sender,
    resolve_sponsor,
    submit,
)


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
    config = config_from_args(args)
    _require_testnet_exchange(config=config, command_name="exchange_for_wal")

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

        result = await submit(client=client, txdict=txdict, mode=args.mode)

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
    config = config_from_args(args)
    _require_testnet_exchange(config=config, command_name="exchange_for_sui")

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
                and matches_wal_coin_type(
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

        result = await submit(client=client, txdict=txdict, mode=args.mode)

    if not result.is_ok():
        print(f"Error in exchange_for_sui: {result.result_string}", file=sys.stderr)
        sys.exit(1)
    print(result.result_data.to_json(indent=2))
