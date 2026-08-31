#    Copyright Frank V. Castellucci
#    SPDX-License-Identifier: Apache-2.0

# -*- coding: utf-8 -*-

"""Upload relay command handlers for the tusky CLI.

Handler for storing blobs through a Walrus upload relay, as opposed to the
native pipeline (which fans slivers out to every storage node itself) or
the Walrus HTTP publisher. The relay performs the fan-out on the client's
behalf, which is what makes mainnet writes practical.

Handlers are async; tusky.py drives them via asyncio.run.
"""

import argparse
import asyncio
import dataclasses
import functools
import json
import os
import sys
import time
from collections.abc import Mapping

from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk import (
    QUILT_BLOB_ATTRIBUTES,
    BlobTooLargeError,
    EncodedBlob,
    QuiltAssemblyError,
    QuiltPatchInput,
    RelayOutcome,
    RelayStageTimings,
    RelayUploadError,
    TipComposition,
    TipQuote,
    WalrusClient,
    WalrusCommittee,
    add_registration_sequence,
    assemble_quilt,
    assert_tip_within_ceiling,
    build_auth_package,
    encode_blob,
    encoded_blob_length,
    preflight_payment,
    preflight_sponsor,
    quote_tip,
)
from pytusk import store_blob_relay as _store_blob_relay_pipeline
from pytusk import store_quilt_relay as _store_quilt_relay_pipeline
from pytusk.tusky.tusky_cmds_common import (
    collect_quilt_patches,
    config_from_args,
    configure_upload_logging,
    read_file_bytes,
    resolve_sender,
    resolve_sponsor,
    simulate_cost_from_balance_changes,
    submit,
    walrus_package_id,
)


def _print_tip_quote(quote: TipQuote) -> None:
    """Show what the relay will charge, before Tx1 is composed.

    Passed to store_blob_relay as its on_quote hook rather than quoting
    separately here: the figure printed is then the one actually composed
    into Tx1, so a relay that changes its price cannot leave the user
    having approved a number different from the one they signed.

    Args:
        quote (TipQuote): The relay's quote for this specific upload.
    """
    if not quote.requires_payment:
        print("Relay tip: none required by this relay.")
        return
    print(f"Relay tip: {quote.amount} MIST to {quote.address}")


def _priced_size(*, args: argparse.Namespace) -> int:
    """Resolve the byte count to price from --size, --file, or --content.

    ``--file`` is measured with ``os.path.getsize`` rather than read: this
    command prices a hypothetical upload and never needs the bytes, so a
    multi-gigabyte file costs a stat instead of a read. ``--content`` is
    measured ENCODED, because the relay charges on bytes -- a non-ASCII
    string measured by character count would be under-priced.

    Args:
        args (argparse.Namespace): Parsed `relay_configs` arguments. The
            mutually exclusive group guarantees exactly one is set.

    Returns:
        int: Unencoded blob length in bytes.
    """
    if args.size is not None:
        return args.size
    if args.file:
        return os.path.getsize(args.file)
    return len(args.content.encode("utf-8"))


async def relay_configs(args: argparse.Namespace) -> None:
    """Print each configured relay and what it would charge for a blob.

    Name, URL and the active marker come from PytuskConfig; the tip amount
    and address are fetched live from each relay. The output distinguishes
    them because a bad row is either a configuration problem or a relay
    problem, and the reader needs to know which.

    Relays are queried concurrently and independently. One unreachable
    relay reports its own error on its own line rather than aborting the
    listing -- which is precisely when this command earns its keep. Nothing
    is spent here, so a failure is reported rather than raised.

    Args:
        args (argparse.Namespace): Parsed `relay_configs` subcommand
            arguments.
    """
    config = config_from_args(args)
    network = config.active_network
    try:
        relays = config.relays_for(network_name=network)
        active = config.active_relay_for(network_name=network)
    except ValueError as exc:
        print(f"Cannot read relay configuration: {exc}", file=sys.stderr)
        sys.exit(1)
    if args.size is not None and args.size < 0:
        print("Error: --size must not be negative.", file=sys.stderr)
        sys.exit(1)
    size = _priced_size(args=args)
    if not relays:
        print(f"network {network}  relays 0")
        return
    async with WalrusClient(pytusk_config=config) as client:
        try:
            committee = await client.committee()
        except (RuntimeError, KeyError, TypeError, ValueError) as exc:
            print(f"Cannot get Walrus committee: {exc}", file=sys.stderr)
            sys.exit(1)
        quotes = await asyncio.gather(
            *(
                quote_tip(
                    client=client,
                    relay_url=relay.relay_url,
                    unencoded_length=size,
                    n_shards=committee.n_shards,
                )
                for relay in relays
            ),
            return_exceptions=True,
        )
    print(
        f"network {network}  relays {len(relays)}  "
        f"active {active or '(none)'}  pricing {size} bytes"
    )
    for relay, quote in zip(relays, quotes):
        marker = "*" if relay.relay_name == active else " "
        if isinstance(quote, BaseException):
            print(
                f"{marker} {relay.relay_name}  {relay.relay_url}  "
                f"UNREACHABLE: {quote}"
            )
        elif not quote.requires_payment:
            print(
                f"{marker} {relay.relay_name}  {relay.relay_url}  "
                "no tip required"
            )
        else:
            print(
                f"{marker} {relay.relay_name}  {relay.relay_url}  "
                f"{quote.amount} MIST to {quote.address}"
            )


async def _relay_simulate_context(
    *,
    client: WalrusClient,
    args: argparse.Namespace,
    sponsor: str | None,
) -> tuple[float, str, WalrusCommittee]:
    """Take the pipeline clock and the two reads every relay simulate needs.

    Shared by the blob and quilt simulate paths, so both fail identically on
    an unsignable sponsor, an unresolvable relay, or a bad committee read --
    and both start their clock at the same point.

    Sponsor signability is checked FIRST, before any encode or network work,
    matching the execute path's own ordering (see preflight_sponsor's
    docstring).

    Args:
        client (WalrusClient): Client used for the committee read.
        args (argparse.Namespace): Parsed arguments, read for ``--relay``.
        sponsor (str | None): Resolved sponsor address.

    Returns:
        tuple[float, str, WalrusCommittee]: Monotonic start, resolved relay
            URL, and the committee.
    """
    pipeline_start = time.monotonic()

    try:
        await preflight_sponsor(client=client, sponsor=sponsor)
    except RelayUploadError as exc:
        print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        relay_url = client.config.relay_url_for(
            network_name=client.config.active_network, relay_name=args.relay
        )
    except ValueError as exc:
        print(f"Error resolving --relay: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        committee = await client.committee()
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(f"Error in register (Tx1): {exc}", file=sys.stderr)
        sys.exit(1)

    return pipeline_start, relay_url, committee


async def _relay_simulate_report(
    *,
    client: WalrusClient,
    args: argparse.Namespace,
    sender: str,
    sponsor: str | None,
    data: bytes,
    encoded: EncodedBlob,
    encode_duration: float,
    committee: WalrusCommittee,
    relay_url: str,
    pipeline_start: float,
    extra_summary: dict[str, object],
    attributes: Mapping[str, str] | None = None,
) -> None:
    """Quote, compose Tx1, simulate it, and print the cost summary.

    Everything here is identical for a blob and for a quilt: once assembly
    has run, a quilt's bytes ARE an ordinary blob and Tx1 has the same shape.
    Only the payload and the summary's payload-specific keys differ, and both
    arrive as parameters.

    Composition and guards mirror
    :func:`~pytusk.core.pipelines.write.store_blob_relay` exactly
    (``add_registration_sequence``, ``preflight_payment``,
    ``assert_tip_within_ceiling``) so this preview cannot drift from the
    library's execute path the way it once did -- and sharing ONE body
    between the blob and quilt previews means that guarantee does not have to
    hold twice over.

    Args:
        client (WalrusClient): Client for the reads and the simulation.
        args (argparse.Namespace): Parsed arguments.
        sender (str): Resolved sender address.
        sponsor (str | None): Resolved sponsor address.
        data (bytes): The payload -- a blob, or an assembled quilt buffer.
        encoded (EncodedBlob): ``data`` after RedStuff encoding.
        encode_duration (float): Seconds the encode took.
        committee (WalrusCommittee): Committee for the current epoch.
        relay_url (str): Resolved relay base URL.
        pipeline_start (float): Monotonic start of the whole command.
        extra_summary (dict[str, object]): Payload-specific keys merged into
            the printed summary. Empty for a blob.
        attributes (Mapping[str, str] | None): On-chain metadata composed into
            the simulated Tx1, mirroring what the library pipeline writes.
            ``None`` (the default) is correct for a blob; the quilt preview
            passes :data:`~pytusk.QUILT_BLOB_ATTRIBUTES` so the simulated PTB
            carries the same command count -- and so the same cost -- as the
            transaction that will actually execute.
    """
    tip_config_start = time.monotonic()
    try:
        quote = await quote_tip(
            client=client,
            relay_url=relay_url,
            unencoded_length=len(data),
            n_shards=committee.n_shards,
        )
        # Simulate honours --max-tip too: a ceiling exists to catch a relay
        # quoting a price the caller will not pay, and a preview that stayed
        # silent would hide exactly the misconfigured or hostile relay the
        # flag exists to surface.
        assert_tip_within_ceiling(quote=quote, max_tip=args.max_tip)
    except RelayUploadError as exc:
        print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
        sys.exit(1)
    tip_config_duration = time.monotonic() - tip_config_start

    auth_package = build_auth_package(data=data) if quote.requires_payment else None

    system_obj_id, walrus_pkg = await walrus_package_id(client=client)

    try:
        resolved_wal_coin = await preflight_payment(
            client=client,
            sender=sender,
            sponsor=sponsor,
            tip_source=args.tip_gas_source if quote.requires_payment else None,
            tip_minimum_balance=quote.amount or 0,
            wal_payment_coin=None,
        )
    except RuntimeError as exc:
        print(f"Error in payment preflight: {exc}", file=sys.stderr)
        sys.exit(1)

    register_start = time.monotonic()
    txn: AsyncSuiTransaction = await client.transaction(
        initial_sender=sender, initial_sponsor=sponsor
    )
    tip_composition = (
        TipComposition(
            relay_address=quote.address or "",
            tip_amount=quote.amount or 0,
            auth_package=auth_package,
            payment_coin=args.tip_gas_source,
        )
        if auth_package is not None
        else None
    )
    # add_registration_sequence composes the tip (when given) FIRST, then
    # reserve_space+register_blob, then the transfer that consumes the new
    # Blob -- the same fixed order the library composes, so Tx1's shape here
    # cannot diverge from it.
    try:
        await add_registration_sequence(
            txn=txn,
            encoded=encoded,
            epochs=args.epochs,
            deletable=not args.permanent,
            package_id=walrus_pkg,
            system_object=system_obj_id,
            recipient=sender,
            wal_payment_coin=resolved_wal_coin,
            tip=tip_composition,
            attributes=attributes,
        )
    except RelayUploadError as exc:
        print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
        sys.exit(1)
    txdict = await txn.build_and_sign()
    result = await submit(client=client, txdict=txdict, mode="simulate")
    register_duration = time.monotonic() - register_start
    if not result.is_ok():
        print(f"Error in register (Tx1): {result.result_string}", file=sys.stderr)
        sys.exit(1)

    if args.full_json:
        print(result.result_data.to_json(indent=2))

    storage_amount = encoded_blob_length(
        unencoded_length=encoded.unencoded_length, n_shards=encoded.n_shards
    )
    transaction = getattr(result.result_data, "transaction", None)
    sui_cost, wal_cost = await simulate_cost_from_balance_changes(
        client=client, transaction=transaction
    )
    timings = RelayStageTimings(
        encode=encode_duration,
        tip_config=tip_config_duration,
        register_tip_tx=register_duration,
        relay_upload=None,
        certify_tx=None,
        total=time.monotonic() - pipeline_start,
    )
    summary: dict[str, object] = {
        "mode": "simulate",
        "relay_url": relay_url,
        "blob_size_bytes": len(data),
        "encoded_storage_amount_bytes": storage_amount,
        "epochs": args.epochs,
        "permanent": args.permanent,
        "tip": {
            "required": quote.requires_payment,
            "address": quote.address,
            "amount_mist": quote.amount,
            "max_mist": args.max_tip,
            "source": args.tip_gas_source,
        },
        "cost": {"sui": sui_cost, "wal": wal_cost},
        "timings": dataclasses.asdict(timings),
        "notice": (
            "Only Tx1 (relay tip + reserve_space + register_blob) was "
            "simulated. The relay upload and Tx2 (certify_blob) were "
            "SKIPPED because simulation does not register the blob "
            "on-chain, so the relay would answer 401 BlobIdNotRegistered "
            "and there is nothing yet to certify. The SUI cost above "
            "DOES include the relay tip, which is composed into this "
            "same transaction. --recipient (if given) transfers the Blob "
            "as part of Tx2, so it is not reflected here; Tx1 always "
            "transfers the Blob to --sender."
        ),
    }
    summary.update(extra_summary)
    print(json.dumps(summary, indent=2))


async def store_blob_relay(args: argparse.Namespace) -> None:
    """Store a blob through a Walrus upload relay and print the receipt.

    Content comes from --content (UTF-8 text) or --file (raw bytes),
    whichever was given. In execute mode this delegates to the library's
    :func:`~pytusk.core.pipelines.write.store_blob_relay` convenience, which
    bundles the relay tip with reserve_space+register_blob in one
    transaction, POSTs the blob to the relay, and certifies the result.

    In simulate mode ONLY that first transaction is simulated. The relay
    upload and certify_blob are skipped because simulation never registers
    the blob on-chain, so the relay would answer 401 BlobIdNotRegistered
    and there would be nothing to certify. Unlike the native command's
    simulate, the cost reported here DOES include the relay tip, because
    the tip is composed into the very transaction being simulated.

    Simulate prints a concise JSON cost summary by default; pass
    --full-json to additionally print the complete raw simulate result,
    which can run to thousands of lines for a large blob.

    Execute prints the full receipt as JSON and exits non-zero when the
    outcome is anything other than CERTIFIED -- the receipt is still
    printed first, because a RESUMABLE outcome carries the transaction
    digest and nonce needed to recover.

    Args:
        args (argparse.Namespace): Parsed `store_blob_relay` subcommand
            arguments.
    """
    # tusky's opt-in logging setup, from tusky_cmds_common. Does nothing
    # unless the user passed --log-file and/or --verbose.
    configure_upload_logging(log_file=args.log_file, verbose=args.verbose)
    if args.log_file is not None:
        print(f"Relay upload log: {args.log_file}")

    if args.file:
        try:
            data = await asyncio.to_thread(read_file_bytes, args.file)
        except OSError as exc:
            print(f"Error reading file {args.file}: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        data = args.content.encode("utf-8")

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

        if args.mode == "execute":
            try:
                receipt = await _store_blob_relay_pipeline(
                    client=client,
                    data=data,
                    epochs=args.epochs,
                    deletable=not args.permanent,
                    relay_name=args.relay,
                    sender=sender,
                    sponsor=sponsor,
                    recipient=args.recipient,
                    tip_source=args.tip_gas_source,
                    max_tip=args.max_tip,
                    timeout=args.timeout,
                    on_quote=_print_tip_quote,
                )
            except RelayUploadError as exc:
                # BlobTooLargeError is deliberately NOT caught here:
                # prepare_write already converts it into
                # RelayUploadError(stage="encode"), so a clause for it would
                # be dead code advertising a control path that cannot occur.
                print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
                sys.exit(1)
            # RegistrationPendingError is no longer caught here: the
            # pipeline converts it, at its own boundary, into a
            # RelayBlobReceipt with outcome=RelayOutcome.RESUMABLE and
            # failed_stage set ("register_finality" or
            # "register_readback") -- handled by the receipt.outcome
            # branch below like any other non-CERTIFIED receipt.
            except (RuntimeError, KeyError, TypeError, ValueError) as exc:
                # Everything before registration -- committee/epoch reads,
                # relay tip-config fetch, package-ID resolution, WAL coin
                # selection, and Tx1 itself -- is bucketed here. Nothing has
                # been spent at these points.
                print(f"Error in register (Tx1): {exc}", file=sys.stderr)
                sys.exit(1)

            # Printed BEFORE the error branch: a RESUMABLE receipt carries
            # the digest and nonce a caller needs to recover, and those must
            # not be swallowed by a non-zero exit.
            print(json.dumps(dataclasses.asdict(receipt), indent=2))
            if receipt.outcome is not RelayOutcome.CERTIFIED:
                print(
                    f"Error in {receipt.failed_stage}: relay upload did not "
                    f"complete (outcome {receipt.outcome.value}).",
                    file=sys.stderr,
                )
                sys.exit(1)
            return

        pipeline_start, relay_url, committee = await _relay_simulate_context(
            client=client, args=args, sponsor=sponsor
        )

        encode_start = time.monotonic()
        try:
            encoded = await asyncio.to_thread(
                functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
            )
        except BlobTooLargeError as exc:
            print(f"Error in encode: {exc}", file=sys.stderr)
            sys.exit(1)
        encode_duration = time.monotonic() - encode_start

        await _relay_simulate_report(
            client=client,
            args=args,
            sender=sender,
            sponsor=sponsor,
            data=data,
            encoded=encoded,
            encode_duration=encode_duration,
            committee=committee,
            relay_url=relay_url,
            pipeline_start=pipeline_start,
            extra_summary={},
        )


async def store_quilt_relay(args: argparse.Namespace) -> None:
    """Store several files as ONE quilt through a Walrus upload relay.

    Patch content comes from --paths (patch key = filename), --patch-file
    (KEY=PATH) and --patch-content (KEY=TEXT), all repeatable and
    combinable -- the same syntax ``store_quilt`` accepts, through the same
    shared collector, so the two cannot come to disagree about what is valid.

    A quilt costs ONE registration and ONE certification for the whole batch
    rather than one each, which is the reason to prefer it over storing the
    files separately.

    In execute mode this delegates to the library's
    :func:`~pytusk.core.pipelines.write.store_quilt_relay`. In simulate mode
    ONLY Tx1 is simulated, for the same reason as the blob command:
    simulation never registers anything on-chain, so the relay would answer
    401 BlobIdNotRegistered and there would be nothing to certify. The
    simulate path shares its body with the blob command, so the two previews
    cannot drift.

    Args:
        args (argparse.Namespace): Parsed `store_quilt_relay` subcommand
            arguments.
    """
    # tusky's opt-in logging setup, from tusky_cmds_common. Does nothing
    # unless the user passed --log-file and/or --verbose.
    configure_upload_logging(log_file=args.log_file, verbose=args.verbose)
    if args.log_file is not None:
        print(f"Relay upload log: {args.log_file}")

    files = await collect_quilt_patches(args=args)
    patches = [
        QuiltPatchInput(identifier=identifier, contents=contents)
        for identifier, contents in files.items()
    ]

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

        if args.mode == "execute":
            try:
                receipt = await _store_quilt_relay_pipeline(
                    client=client,
                    patches=patches,
                    epochs=args.epochs,
                    deletable=not args.permanent,
                    relay_name=args.relay,
                    sender=sender,
                    sponsor=sponsor,
                    recipient=args.recipient,
                    tip_source=args.tip_gas_source,
                    max_tip=args.max_tip,
                    timeout=args.timeout,
                    on_quote=_print_tip_quote,
                )
            except RelayUploadError as exc:
                # BlobTooLargeError is deliberately NOT caught here:
                # prepare_quilt_write already converts it into
                # RelayUploadError(stage="encode"), so a clause for it would
                # be dead code advertising a control path that cannot occur.
                print(f"Error in {exc.stage}: {exc}", file=sys.stderr)
                sys.exit(1)
            except (RuntimeError, KeyError, TypeError, ValueError) as exc:
                print(f"Error in register (Tx1): {exc}", file=sys.stderr)
                sys.exit(1)

            print(json.dumps(dataclasses.asdict(receipt), indent=2))
            if receipt.outcome is not RelayOutcome.CERTIFIED:
                print(
                    f"Error in {receipt.failed_stage}: relay upload did not "
                    f"complete (outcome {receipt.outcome.value}).",
                    file=sys.stderr,
                )
                sys.exit(1)
            return

        pipeline_start, relay_url, committee = await _relay_simulate_context(
            client=client, args=args, sponsor=sponsor
        )

        # Assembly sits between the committee read and the encode because the
        # quilt's column geometry derives from the shard count. Both halves
        # must see the SAME committee, which is why it is read once above and
        # passed down rather than fetched again here -- and why the encode
        # below takes its shard count from the assembled quilt itself rather
        # than reading `committee` a second time. A quilt packed for one
        # n_shards and encoded against another is a valid blob whose geometry
        # no reader can decode.
        try:
            assembled = await asyncio.to_thread(
                functools.partial(
                    assemble_quilt, patches=patches, n_shards=committee.n_shards
                )
            )
        except QuiltAssemblyError as exc:
            print(f"Error in assemble: {exc}", file=sys.stderr)
            sys.exit(1)

        encode_start = time.monotonic()
        try:
            encoded = await asyncio.to_thread(
                functools.partial(
                    encode_blob, data=assembled.data, n_shards=assembled.n_shards
                )
            )
        except BlobTooLargeError as exc:
            print(f"Error in encode: {exc}", file=sys.stderr)
            sys.exit(1)
        encode_duration = time.monotonic() - encode_start

        await _relay_simulate_report(
            client=client,
            args=args,
            sender=sender,
            sponsor=sponsor,
            data=assembled.data,
            encoded=encoded,
            encode_duration=encode_duration,
            committee=committee,
            relay_url=relay_url,
            pipeline_start=pipeline_start,
            extra_summary={
                "patch_count": len(assembled.patches),
                "patches": [layout.identifier for layout in assembled.patches],
            },
            attributes=QUILT_BLOB_ATTRIBUTES,
        )

