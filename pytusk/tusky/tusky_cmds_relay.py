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
import sys
import time

from pysui.sui.sui_common.async_txn import AsyncSuiTransaction

from pytusk import (
    BlobTooLargeError,
    RelayOutcome,
    RelayStageTimings,
    RelayUploadError,
    TipComposition,
    WalrusClient,
    add_registration_sequence,
    build_auth_package,
    encode_blob,
    encoded_blob_length,
    preflight_payment,
    preflight_sponsor,
    quote_tip,
)
from pytusk import store_blob_relay as _store_blob_relay_pipeline
from pytusk.tusky.tusky_cmds_common import (
    config_from_args,
    read_file_bytes,
    resolve_sender,
    resolve_sponsor,
    simulate_cost_from_balance_changes,
    submit,
    walrus_package_id,
)


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
                    tip_source=args.tip_source,
                )
            except BlobTooLargeError as exc:
                print(f"Error in encode: {exc}", file=sys.stderr)
                sys.exit(1)
            except RelayUploadError as exc:
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

        # --mode simulate: Tx1 only (tip + reserve_space + register_blob).
        # Only encode, the tip quote, and Tx1 build+simulate actually run,
        # so only those stages get a timing -- the rest are honestly
        # reported as None because they did not run. Composition and guards
        # below mirror pytusk.core.pipelines.write.store_blob_relay
        # exactly (add_registration_sequence, preflight_sponsor,
        # preflight_payment) so this path cannot drift from the library's
        # execute path the way it once did.
        pipeline_start = time.monotonic()

        # FAIL FAST: sponsor signability is checked FIRST, before any encode
        # or network work -- matching store_blob_relay's own ordering (see
        # preflight_sponsor's docstring).
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

        encode_start = time.monotonic()
        try:
            encoded = await asyncio.to_thread(
                functools.partial(encode_blob, data=data, n_shards=committee.n_shards)
            )
        except BlobTooLargeError as exc:
            print(f"Error in encode: {exc}", file=sys.stderr)
            sys.exit(1)
        encode_duration = time.monotonic() - encode_start

        tip_config_start = time.monotonic()
        try:
            quote = await quote_tip(
                client=client,
                relay_url=relay_url,
                unencoded_length=len(data),
                n_shards=committee.n_shards,
            )
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
                tip_source=args.tip_source if quote.requires_payment else None,
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
                payment_coin=args.tip_source,
            )
            if auth_package is not None
            else None
        )
        # add_registration_sequence composes the tip (when given) FIRST,
        # then reserve_space+register_blob, then the transfer that consumes
        # the new Blob -- the same fixed order
        # pytusk.core.pipelines.write.store_blob_relay composes, so
        # Tx1's shape here cannot diverge from the library's.
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
        summary = {
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
                "source": args.tip_source,
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
        print(json.dumps(summary, indent=2))
