#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Shared argument, config, and submission plumbing for the tusky CLI.

Cross-domain helpers only -- a helper used by exactly one domain module
(read/write/query/exchange/lifecycle/native_upload/storage) lives in that
module instead, not here. See tusky_cmds_read.py, tusky_cmds_write.py,
tusky_cmds_query.py, tusky_cmds_exchange.py, tusky_cmds_lifecycle.py,
tusky_cmds_native_upload.py, and tusky_cmds_storage.py for the handlers
that use these helpers.

WHAT BELONGS HERE, under Plan #28 step 10's placement rule (the consumer
determines the module): argument and config plumbing, and the CLI's own
error handling -- turning a library exception into a message on stderr and a
non-zero exit. What does NOT: presentation, which is
:mod:`pytusk.tusky.tusky_format`; and domain logic an SDK user would also
call, which belongs in the library proper and is imported from there. See
:mod:`pytusk.core.chain.blob_fields`,
:func:`~pytusk.core.chain.extract_balance_change_costs` and
:func:`~pytusk.core.ops.coins.wal_balance_and_decimals` -- all three used to
live in this module.
"""

import argparse
import asyncio
import io
import logging
import os
import sys
import time
from pathlib import Path

import pysui.sui.sui_grpc.suimsgs.sui.rpc.v2 as sui_prot
from pysui import (
    ExecuteTransaction,
    GetCoinMetaData,
    PysuiConfiguration,
    SimulateTransaction,
    SuiRpcResult,
)

from pytusk import (
    PytuskConfiguration,
    WalrusClient,
    extract_balance_change_costs,
    resolve_package_id,
)
from pytusk import wal_balance_and_decimals as _wal_balance_and_decimals_lib

# The domain half of what this module used to do itself. Plan #28 step 10
# moved it into the library proper under the placement rule: matching a WAL
# coin type, parsing a Blob object, and reading costs out of a transaction's
# balance changes are things an SDK user wants, not CLI presentation. What
# stays here is the CLI's own error handling -- the library call raises, and
# this module turns that into a message on stderr and a non-zero exit.
from pytusk.tusky.tusky_format import format_token_amount

# --- tusky CLI logging configuration ------------------------------------
# pytusk (the library, everything outside pytusk/tusky/) only ever emits log
# records -- it never configures handlers, levels, or any other global
# logging state (see pytusk/__init__.py's NullHandler). tusky, as an
# APPLICATION built on top of pytusk, is entitled to configure logging, but
# only when the user explicitly asks via --log-file/--verbose, and never by
# writing to a derived or default path.
#
# This lives here rather than in a domain module because TWO domain modules
# need it: tusky_cmds_native_upload (sliver fan-out and confirmation
# progress) and tusky_cmds_relay (relay POST attempts and backoff). That is
# exactly this module's rule for a cross-domain helper.
# ------------------------------------------------------------------------

_UPLOAD_LOGGING_CONFIGURED: bool = False


def configure_upload_logging(*, log_file: Path | None, verbose: bool) -> None:
    """Configure the ``pytusk`` logger hierarchy per the user's CLI request.

    This is tusky's own opt-in logging setup, not library scaffolding -- see
    the module comment immediately above. Adds an INFO-level stdout stream
    handler to the ``pytusk`` logger only when ``verbose`` is True, and/or an
    INFO-level file handler at exactly ``log_file`` only when it is given --
    NEVER a derived or default path. Does nothing at all when neither is
    requested. The ``pytusk`` logger is the parent of every submodule's own
    ``logging.getLogger(__name__)`` -- the native upload's ``fanout`` and
    ``confirm``, and the relay's ``upload`` alike -- so their progress
    records propagate up to whichever destination(s) were configured. When
    ``verbose`` is True, stdout is also reconfigured for line buffering so
    progress is visible live even when output is redirected to a file.
    Idempotent: a second call in the same process is a no-op, so handlers are
    never duplicated.

    Args:
        log_file (Path | None): Path to write an INFO-level log file to,
            exactly as given (no default, no derived path); ``None`` to skip
            file logging.
        verbose (bool): Whether to emit INFO-level progress to stdout.

    Returns:
        None.
    """
    # Idempotent one-time setup guard.
    global _UPLOAD_LOGGING_CONFIGURED  # pylint: disable=global-statement

    if not log_file and not verbose:
        return
    if _UPLOAD_LOGGING_CONFIGURED:
        return

    logger = logging.getLogger("pytusk")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    if verbose:
        if isinstance(sys.stdout, io.TextIOWrapper):
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

    _UPLOAD_LOGGING_CONFIGURED = True


def config_from_args(args: argparse.Namespace) -> PytuskConfiguration:
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


def resolve_sender(*, config: PysuiConfiguration, sender_arg: str | None) -> str:
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


def resolve_sponsor(
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


async def wal_balance_and_decimals(
    *, client: WalrusClient, owner: str
) -> tuple[sui_prot.Balance, int]:
    """Discover the owner's WAL coin balance entry and WAL's decimal precision.

    CLI wrapper around
    :func:`~pytusk.core.ops.coins.wal_balance_and_decimals`, which holds the
    lookup itself. All this adds is the CLI's failure behaviour: a message on
    stderr and a non-zero exit, in place of the ``RuntimeError`` the library
    function raises. Nothing is spent by these reads, so exiting discards no
    recoverable state.

    Args:
        client (WalrusClient): Client used to query balances and metadata.
        owner (str): Address whose balances are checked for a WAL coin type.

    Returns:
        tuple[sui_prot.Balance, int]: The owner's WAL balance entry
            (exposing coin_type, balance, coin_balance, address_balance)
            and WAL's decimals.
    """
    try:
        return await _wal_balance_and_decimals_lib(client=client, owner=owner)
    except RuntimeError as exc:
        # The library function raises where this one used to exit: a library
        # must not terminate its caller's process. The message text is
        # unchanged, so what a tusky user sees on stderr is identical.
        print(str(exc), file=sys.stderr)
        sys.exit(1)


async def walrus_package_id(*, client: WalrusClient) -> tuple[str, str]:
    """Fetch the System object ID and the current Walrus package ID.

    Delegates to :func:`~pytusk.core.ops.system_reads.resolve_package_id`, which
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


# blob_deletable_and_end_epoch and blob_certified_epoch moved to
# pytusk.core.chain.blob_fields at Plan #28 step 10. They already raised
# rather than exiting, so the move was a relocation and not a change of
# contract, and callers now import them from the library directly rather
# than through this CLI module.


async def submit(
    *,
    client: WalrusClient,
    txdict: dict,
    mode: str,
    _time_execute: bool = False,
) -> SuiRpcResult:
    """Simulate or execute a signed transaction dict, per --mode.

    Args:
        client (WalrusClient): Client used to submit the transaction.
        txdict (dict): Result of AsyncSuiTransaction.build_and_sign().
        mode (str): "simulate" or "execute".
        _time_execute (bool): Diagnostic-only flag (default False, so no
            existing caller's behavior changes). When True and mode is
            "execute", wraps just the ``client.execute(command=
            ExecuteTransaction(...))`` round trip in a timer and attaches
            the elapsed seconds to the returned ``SuiRpcResult`` as a
            private ``_execute_duration`` attribute for the caller to read
            back. Added for Task #19's ``set_blob_metadata`` timing
            instrumentation; not intended as a general profiling pattern.

    Returns:
        SuiRpcResult: The result of the simulate or execute RPC call. When
            ``_time_execute`` is True and mode is "execute", carries an
            additional ``_execute_duration: float`` attribute (seconds).
    """
    if mode == "simulate":
        return await client.execute(
            command=SimulateTransaction(tx_bytestr=txdict["tx_bytestr"])
        )
    if _time_execute:
        start = time.perf_counter()
        result = await client.execute(command=ExecuteTransaction(**txdict))
        result._execute_duration = time.perf_counter() - start
        return result
    return await client.execute(command=ExecuteTransaction(**txdict))


# format_token_amount moved to pytusk.tusky.tusky_format at Plan #28 step 10
# -- it renders a value for display and nothing else, which is that module's
# whole remit. It is imported above, since the cost summary below still
# renders with it.


async def simulate_cost_from_balance_changes(
    *, client: WalrusClient, transaction: sui_prot.ExecutedTransaction | None
) -> tuple[dict[str, int | str | None], dict[str, int | str | None]]:
    """Derive SUI and WAL cost summaries from a simulated Tx1's balance changes.

    Cost is reported as the NEGATION of the on-chain net balance change
    (which is negative for an outgoing spend), so a positive value here
    means "this many units are spent" -- matching what a user asking "what
    will this cost?" wants to read, while still surfacing an unexpected
    positive on-chain delta (a net gain) as a negative cost rather than
    silently flipping its sign.

    The coin-type matching and the raw cost arithmetic now live in
    :func:`~pytusk.core.chain.extract_balance_change_costs`, which is
    client-free. What remains here is the part that genuinely needs a client
    and a renderer: reading WAL's on-chain decimals, and turning raw amounts
    into the decimal strings the CLI prints. The returned dict shape and every
    message string are unchanged.

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
    costs = extract_balance_change_costs(
        transaction=transaction,
        wal_coin_type=client.config.network.wal_coin_type,
    )

    sui_info: dict[str, int | str | None] = {
        "raw_mist": costs.sui_raw_mist,
        # SUI's decimal precision (9) is a fixed Sui protocol constant, not a
        # per-coin-type value read from CoinMetadata -- unlike WAL below,
        # which is a deployed coin whose decimals must never be assumed.
        "sui": (
            format_token_amount(raw=costs.sui_raw_mist, decimals=9)
            if costs.sui_raw_mist is not None
            else None
        ),
        "unavailable_reason": costs.sui_unavailable_reason,
    }
    wal_info: dict[str, int | str | None] = {
        "coin_type": costs.wal_coin_type,
        "raw_frost": costs.wal_raw_frost,
        "wal": None,
        "unavailable_reason": costs.wal_unavailable_reason,
    }

    # The one step that cannot be done without a client: WAL is a deployed
    # coin, so its decimals are read from chain rather than assumed. Only
    # attempted once a raw amount is actually in hand -- a missing WAL entry
    # keeps the reason the extractor already gave it.
    if costs.wal_raw_frost is not None and costs.wal_coin_type is not None:
        meta_result = await client.execute(
            command=GetCoinMetaData(coin_type=costs.wal_coin_type)
        )
        metadata = meta_result.result_data.metadata if meta_result.is_ok() else None
        if metadata is None or metadata.decimals is None:
            wal_info["unavailable_reason"] = (
                f"WAL coin metadata for {costs.wal_coin_type} could not "
                "be read or has no decimals field; raw_frost is known "
                "but its decimal rendering is not."
            )
        else:
            wal_info["wal"] = format_token_amount(
                raw=costs.wal_raw_frost, decimals=metadata.decimals
            )

    return sui_info, wal_info


def read_file_bytes(path: str | Path) -> bytes:
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


async def collect_quilt_patches(*, args: argparse.Namespace) -> dict[str, bytes]:
    """Merge the three patch sources into one identifier-to-bytes mapping.

    ``--paths`` keys each patch by its filename; ``--patch-file`` and
    ``--patch-content`` key it explicitly. All three are combinable, and the
    duplicate guard spans ALL of them rather than each in isolation -- two
    sources naming the same patch is a caller mistake to report, not an
    ambiguity to resolve silently.

    Shared by every quilt-writing subcommand, so the accepted syntax and the
    duplicate rule cannot come to differ between them.

    Args:
        args (argparse.Namespace): Parsed arguments carrying ``paths``,
            ``patch_file`` and ``patch_content``.

    Returns:
        dict[str, bytes]: Patch identifier mapped to its raw content.
    """
    files: dict[str, bytes] = {}
    for path in args.paths:
        key = os.path.basename(path)
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        try:
            files[key] = await asyncio.to_thread(read_file_bytes, path)
        except OSError as exc:
            print(f"Error reading file {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    for key, path in args.patch_file:
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        try:
            files[key] = await asyncio.to_thread(read_file_bytes, path)
        except OSError as exc:
            print(f"Error reading file {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    for key, text in args.patch_content:
        if key in files:
            print(f"Error: duplicate patch key {key!r}", file=sys.stderr)
            sys.exit(1)
        files[key] = text.encode("utf-8")

    if not files:
        print(
            "Error: at least one --paths, --patch-file, or --patch-content "
            "patch is required",
            file=sys.stderr,
        )
        sys.exit(1)
    return files
