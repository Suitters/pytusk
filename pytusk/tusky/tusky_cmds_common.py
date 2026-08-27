#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Shared helpers used across the tusky CLI's domain command modules.

Cross-domain helpers only -- a helper used by exactly one domain module
(read/write/query/exchange/lifecycle/native_upload/storage) lives in that
module instead, not here. See tusky_cmds_read.py, tusky_cmds_write.py,
tusky_cmds_query.py, tusky_cmds_exchange.py, tusky_cmds_lifecycle.py,
tusky_cmds_native_upload.py, and tusky_cmds_storage.py for the handlers
that use these helpers.
"""

import argparse
import sys
from pathlib import Path

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import (
    ExecuteTransaction,
    GetAddressCoinBalances,
    GetCoinMetaData,
    PysuiConfiguration,
    SimulateTransaction,
    SuiRpcResult,
)

from pytusk import PytuskConfiguration, WalrusClient, resolve_package_id

# Imported directly from the submodule, not the pytusk top-level package:
# this is an internal reuse of the same computation add_reserve_and_register
# already performs (see its _encoded_storage_amount), not a new public
# surface, so it is kept out of pytusk.__all__.
from pytusk.core.utils import _matches_wal_coin_type


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
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        # CLI entry boundary: convert any config-loading failure into a
        # clean message instead of a raw traceback exposing local file paths.
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


async def _walrus_package_id(*, client: WalrusClient) -> tuple[str, str]:
    """Fetch the System object ID and the current Walrus package ID.

    Delegates to :func:`~pytusk.core.utils.resolve_package_id`, which
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
    coin_type is matched via ``_matches_wal_coin_type``, the same
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
