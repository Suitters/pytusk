Transactions
============

``pytusk`` transactions are built and executed through the same
:py:class:`~pytusk.WalrusClient.transaction` builder pysui provides —
there is nothing pytusk-specific about the mechanics. This page covers
how ``PytuskConfiguration`` and ``PysuiConfiguration`` relate for
transaction purposes, then walks through the Walrus-specific PTBs
``pytusk`` builds internally for blob lifecycle and WAL/SUI exchange
operations.

Every example on this page imports from the top-level package —
``from pytusk import ...``. All public names are re-exported there;
submodule paths are internal and may move between releases.

PytuskConfiguration and PysuiConfiguration
--------------------------------------------

Every ``WalrusClient`` wraps a pysui client underneath, and every PTB
built via ``client.transaction()`` is a pysui ``AsyncSuiTransaction``.
``PytuskConfiguration.pysui_configuration`` exposes the underlying
``PysuiConfiguration`` instance, and the ``pysui_group_name`` /
``pysui_profile_name`` / ``pysui_address`` / ``pysui_alias`` constructor
arguments let you pin a ``WalrusClient`` to a specific pysui profile for
signing. See :doc:`configuration` for the full breakdown of how these
line up.

Building Transactions
------------------------

``await client.transaction(**kwargs)`` returns a pysui PTB builder
directly — it is a pass-through to
``await self.pysui_client.transaction(**kwargs)``, with no pytusk-added
behavior. For the general builder API (available commands, gas/expiry
arguments, executors, and execution strategies), see pysui's own
documentation — this page only covers the Walrus-specific ``move_call``
patterns below.

Walrus-Specific PTBs
-----------------------

Each of the operations below is a ``move_call`` against either the
``wal_exchange`` package (WAL/SUI exchange) or the ``walrus`` package
(blob lifecycle). Object and package IDs are network-specific — see
:doc:`configuration` for how ``WalrusNetworkConfig`` exposes each
network's on-chain object IDs.

.. note::

   The exchange examples below assume ``testnet``, whose predefined
   exchange objects (``client.config.network.exchange_objects``) ship
   pre-populated in ``PytuskConfiguration``. ``mainnet`` ships with an
   **empty** exchange object list by default. To run the exchange
   examples against mainnet, first register mainnet's exchange object
   IDs via
   :py:meth:`PytuskConfiguration.set_exchange_objects() <pytusk.PytuskConfiguration.set_exchange_objects>`.

Exchanging SUI for WAL
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import PytuskConfiguration, WalrusClient

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            exchange_obj_id = client.config.network.exchange_objects[0]
            exchange_result = await client.execute(
                command=GetObject(object_id=exchange_obj_id)
            )
            wal_exchange_pkg = exchange_result.result_data.object_type.split("::")[0]
            amount = 1_000_000_000  # MIST of SUI to exchange

            txn = await client.transaction(initial_sender=sender)
            split = await txn.split_coin(coin=txn.gas, amounts=[amount])
            wal_coin = await txn.move_call(
                target=f"{wal_exchange_pkg}::wal_exchange::exchange_all_for_wal",
                arguments=[exchange_obj_id, split],
                type_arguments=[],
            )
            await txn.transfer_objects(transfers=[wal_coin], recipient=sender)

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Exchanging WAL for SUI
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

There are two ``move_call`` targets depending on whether the WAL coin
being exchanged has the exact balance requested, or a larger one:
``wal_exchange::exchange_all_for_sui`` (exact balance) or
``wal_exchange::exchange_for_sui`` (larger balance, taking the amount as
an explicit argument). The exact-balance case:

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import PytuskConfiguration, WalrusClient

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            exchange_obj_id = client.config.network.exchange_objects[0]
            exchange_result = await client.execute(
                command=GetObject(object_id=exchange_obj_id)
            )
            wal_exchange_pkg = exchange_result.result_data.object_type.split("::")[0]
            wal_coin_id = "0x..."  # WAL coin object with the exact balance to exchange

            txn = await client.transaction(initial_sender=sender)
            sui_coin = await txn.move_call(
                target=f"{wal_exchange_pkg}::wal_exchange::exchange_all_for_sui",
                arguments=[exchange_obj_id, wal_coin_id],
                type_arguments=[],
            )
            await txn.transfer_objects(transfers=[sui_coin], recipient=sender)

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Extending a Blob's Expiration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import PytuskConfiguration, WalrusClient

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            system_obj_id = client.config.network.system_object
            sys_result = await client.execute(
                command=GetObject(object_id=system_obj_id)
            )
            walrus_pkg = sys_result.result_data.json.struct_value.fields[
                "package_id"
            ].string_value
            blob_object_id = "0x..."  # the blob's Sui object ID, not its Walrus blob ID
            payment_coin_id = "0x..."  # WAL coin object to pay the extension cost
            additional_epochs = 5

            txn = await client.transaction(initial_sender=sender)
            await txn.move_call(
                target=f"{walrus_pkg}::system::extend_blob",
                arguments=[system_obj_id, blob_object_id, additional_epochs, payment_coin_id],
                type_arguments=[],
            )

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Deleting a Blob
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Only deletable blobs that have not yet expired can be deleted this way;
deleting returns the blob's storage resource, which this example
transfers back to the sender.

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import PytuskConfiguration, WalrusClient

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            system_obj_id = client.config.network.system_object
            sys_result = await client.execute(
                command=GetObject(object_id=system_obj_id)
            )
            walrus_pkg = sys_result.result_data.json.struct_value.fields[
                "package_id"
            ].string_value
            blob_object_id = "0x..."  # the blob's Sui object ID, not its Walrus blob ID

            txn = await client.transaction(initial_sender=sender)
            storage = await txn.move_call(
                target=f"{walrus_pkg}::system::delete_blob",
                arguments=[system_obj_id, blob_object_id],
                type_arguments=[],
            )
            await txn.transfer_objects(transfers=[storage], recipient=sender)

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Storage Management
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Standalone ``Storage`` objects (unwrapped from any ``Blob``) can be
split, fused, destroyed, or used to extend a blob's expiration instead
of paying WAL. All four operations are thin wrappers around
``storage_resource``/``system`` Move entry points; the sections below
show each, with the Move-level constraints that would otherwise surface
only as an opaque on-chain abort.

Splitting Storage
''''''''''''''''''''''''''''''''''''''''

Splitting mutates the original ``Storage`` in place and returns a NEW
``Storage`` for the split-off portion. Since ``Storage`` has no ``drop``
ability, that returned result must be consumed within the same PTB (here,
transferred to the sender) or the transaction aborts when built.

Splitting **by epoch** requires an interior split point: Move asserts
``start_epoch < split_epoch < end_epoch``, so the object's epoch range
must span at least two epochs -- a ``Storage`` covering only one epoch
(e.g. ``[491, 492)``) has no valid ``split_epoch`` and cannot be split
this way at all.

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import PytuskConfiguration, WalrusClient, add_split_by_epoch

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            system_obj_id = client.config.network.system_object
            sys_result = await client.execute(
                command=GetObject(object_id=system_obj_id)
            )
            walrus_pkg = sys_result.result_data.json.struct_value.fields[
                "package_id"
            ].string_value
            storage_object_id = "0x..."  # the Storage object's Sui object ID

            txn = await client.transaction(initial_sender=sender)
            new_storage = await add_split_by_epoch(
                txn=txn,
                package_id=walrus_pkg,
                storage_object_id=storage_object_id,
                split_epoch=500,  # must satisfy start_epoch < 500 < end_epoch
            )
            await txn.transfer_objects(transfers=[new_storage], recipient=sender)

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Splitting **by size** instead peels off a byte capacity, leaving the
original with the remainder; Move asserts ``storage_size >= split_size``
(``EIncompatibleAmount`` if the requested size is larger than what
exists). Swap ``add_split_by_epoch``/``split_epoch=...`` above for
``add_split_by_size``/``split_size=...`` -- the rest of the PTB is
identical.

Fusing Storage
''''''''''''''''''''''''''''''''''''''''

Fusing two Storage objects consumes one of them (called ``second`` below)
and folds its size and/or epoch range into the other (``first``), which
is mutated in place rather than consumed. ``storage_resource::fuse``
dispatches on whether the two share a ``start_epoch``:

- **Same start_epoch** ("fuse_amount" route) -- requires an IDENTICAL
  epoch range on both sides; sizes are simply summed.
- **Different start_epoch** ("fuse_periods" route) -- requires EQUAL
  sizes and ADJACENT ranges (one must begin exactly where the other
  ends); the epoch ranges are joined.

:func:`~pytusk.validate_fuse_pair` mirrors this dispatch and assertion
order exactly, so pre-flighting a pair before building the PTB reports
the same reason Move itself would abort with:

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import (
        PytuskConfiguration,
        WalrusClient,
        add_fuse,
        storage_from_object,
        validate_fuse_pair,
    )

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            system_obj_id = client.config.network.system_object
            sys_result = await client.execute(
                command=GetObject(object_id=system_obj_id)
            )
            walrus_pkg = sys_result.result_data.json.struct_value.fields[
                "package_id"
            ].string_value

            first_id = "0x..."  # survives and absorbs second_id
            second_id = "0x..."  # consumed by the fuse

            first_obj = await client.execute(command=GetObject(object_id=first_id))
            second_obj = await client.execute(command=GetObject(object_id=second_id))
            first = storage_from_object(obj=first_obj.result_data)
            second = storage_from_object(obj=second_obj.result_data)
            validate_fuse_pair(first=first, second=second)  # raises ValueError if incompatible

            txn = await client.transaction(initial_sender=sender)
            await add_fuse(
                txn=txn,
                package_id=walrus_pkg,
                first_storage_id=first_id,
                second_storage_id=second_id,
            )

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Reclaiming Storage
''''''''''''''''''''''''''''''''''''''''

``storage_resource::destroy`` consumes a ``Storage`` object and refunds
its Sui storage rebate to the sender -- it does NOT refund the WAL
originally paid to reserve the capacity, which is spent regardless. Move
performs no checks at all: an unexpired reservation is destroyed just as
readily as a spent one, so there is no pre-flight to run first.

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction
    from pytusk import PytuskConfiguration, WalrusClient, add_destroy_storage

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            system_obj_id = client.config.network.system_object
            sys_result = await client.execute(
                command=GetObject(object_id=system_obj_id)
            )
            walrus_pkg = sys_result.result_data.json.struct_value.fields[
                "package_id"
            ].string_value
            storage_object_id = "0x..."

            txn = await client.transaction(initial_sender=sender)
            await add_destroy_storage(
                txn=txn,
                package_id=walrus_pkg,
                storage_object_id=storage_object_id,
            )

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Extending a Blob with Storage
''''''''''''''''''''''''''''''''''''''''

``system::extend_blob_with_resource`` pays for a blob's expiration
extension with a Storage object instead of WAL. It bottoms out in
``blob::extend_with_resource``, which imposes four requirements:

- the blob must be CERTIFIED (``ENotCertified``);
- the blob must not already be expired (``EResourceBounds``);
- the extension's ``end_epoch`` must be strictly LATER than the blob's
  current ``end_epoch`` (``EResourceBounds``);
- the extension must satisfy the SAME rules as ``fuse_periods`` against
  the blob's existing storage -- **an exact ``storage_size`` match**
  (``EIncompatibleAmount``) and epoch-range adjacency
  (``EIncompatibleEpochs``). This is easy to get wrong: buying an
  arbitrary standalone ``Storage`` object will almost never satisfy the
  exact-size requirement -- it must be sized to match the blob's
  existing storage precisely.

The Storage object is consumed by the call and ceases to exist.
:func:`~pytusk.fuse_periods_incompatibility` (not
:func:`~pytusk.fuse_incompatibility`) mirrors the last check, since
``extend_with_resource`` calls ``fuse_periods`` unconditionally rather
than going through ``fuse``'s ``start_epoch`` dispatch:

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import (
        PytuskConfiguration,
        WalrusClient,
        fuse_periods_incompatibility,
        storage_from_blob,
        storage_from_object,
    )

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            system_obj_id = client.config.network.system_object
            sys_result = await client.execute(
                command=GetObject(object_id=system_obj_id)
            )
            walrus_pkg = sys_result.result_data.json.struct_value.fields[
                "package_id"
            ].string_value

            blob_object_id = "0x..."  # the blob's Sui object ID
            storage_object_id = "0x..."  # consumed by the extension

            blob_obj = await client.execute(command=GetObject(object_id=blob_object_id))
            storage_obj = await client.execute(
                command=GetObject(object_id=storage_object_id)
            )
            blob_storage = storage_from_blob(obj=blob_obj.result_data)
            extension = storage_from_object(obj=storage_obj.result_data)

            reason = fuse_periods_incompatibility(first=blob_storage, second=extension)
            if reason is not None:
                raise ValueError(reason)

            txn = await client.transaction(initial_sender=sender)
            await txn.move_call(
                target=f"{walrus_pkg}::system::extend_blob_with_resource",
                arguments=[system_obj_id, blob_object_id, storage_object_id],
                type_arguments=[],
            )

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Native Upload (Storage Nodes)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Unlike the operations above, a native blob upload is not a single
``move_call`` — it is two separate PTBs (``reserve_space`` +
``register_blob`` composed as one transaction, ``certify_blob`` as a
second, later transaction) bracketing an off-chain step: uploading
erasure-coded slivers to the storage-node committee and collecting their
signed confirmations. This is pytusk's alternative to the HTTP publisher/
aggregator commands (``StoreBlob``/``ReadBlob`` and friends) — see
:doc:`intro` for when to choose one over the other.

The highest-level entry point runs the whole pipeline in one call:

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient, store_blob_native

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            receipt = await store_blob_native(
                client=client,
                data=b"hello walrus",
                epochs=5,
                deletable=True,
                sender=sender,
            )
            print(receipt.blob_id, receipt.object_id, receipt.certified)

    asyncio.run(main())

``store_blob_native()`` orchestrates, in order: ``encode_blob`` (Reed-
Solomon/RedStuff encoding), Tx1 (``reserve_space`` + ``register_blob``),
sliver fan-out to the committee, confirmation collection, and Tx2
(``certify_blob``). On any failure after Tx1 has already succeeded, it
returns a :py:class:`~pytusk.NativeBlobReceipt` with ``certified=False``
and ``failed_stage`` set — the blob is registered on-chain but not
certified — rather than raising, since that partial state (WAL already
spent, a real ``Blob`` object already created) is worth reporting back.
See ``tusky certify_blob`` (:doc:`tusky`) for recovering from that state
via the CLI, and :doc:`logging` for attaching progress logging to this
pipeline.

Composing Tx1/Tx2 by Hand
'''''''''''''''''''''''''''

:py:func:`~pytusk.store_blob_native` builds, signs, and submits both
transactions for you. An SDK developer who needs to hold the transaction
themselves — choose sender/sponsor per transaction, interleave their own
move_calls, or decide simulate-vs-execute — composes Tx1 and Tx2 directly
instead, following this flow:

1. Get a ``WalrusClient`` and open a transaction (your own sender/sponsor).
2. Add Tx1's move_calls via
   :py:func:`~pytusk.add_reserve_and_register`.
3. Transfer (or otherwise consume) the ``Blob`` command result it returns
   — it is an **unconsumed object result**, and the PTB aborts at
   execution if it is not transferred or fed into a further move_call.
4. Simulate or execute, as you choose.
5. Observe the outcome and collect what Tx2 needs: the created ``Blob``'s
   object ID (via :py:func:`~pytusk.find_created_object_id`,
   reading directly off the ``TransactionEffects`` you already hold — no
   extra round trip) and its ``storage.end_epoch``/``deletable`` (read back
   via ``GetObject``, the same JSON shape ``tusky blob`` prints — see
   :doc:`tusky`).
6. Off-chain: fan slivers out to the committee and collect confirmations.
7. Get a **new** transaction from the client — same or different
   sender/sponsor — and add Tx2's move_call via
   :py:func:`~pytusk.add_certify`.
8. Simulate or execute.

.. code-block:: python

    import asyncio
    from pathlib import Path
    from pysui import ExecuteTransaction, GetObject
    from pytusk import (
        PytuskConfiguration,
        Registration,
        WalrusClient,
        add_certify,
        add_reserve_and_register,
        assert_certificate_epoch_current,
        collect_confirmations,
        encode_blob,
        find_created_object_id,
        resolve_package_id,
        select_wal_payment_coin,
        upload_slivers,
    )

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            system_obj_id = client.config.network.system_object
            walrus_pkg = await resolve_package_id(
                client=client, system_object=system_obj_id
            )
            committee = await client.committee()
            payment_coin = await select_wal_payment_coin(client=client, owner=sender)

            # Raw UTF-8 bytes work too, e.g.
            # encoded = encode_blob(data=b"hello walrus", n_shards=committee.n_shards)
            data = Path("Filename here").read_bytes()
            encoded = encode_blob(data=data, n_shards=committee.n_shards)
            epochs = 5
            deletable = True

            # --- Steps 1-4: hold a transaction, add Tx1's move_calls, transfer, execute ---
            txn = await client.transaction(initial_sender=sender)
            blob = await add_reserve_and_register(
                txn=txn,
                package_id=walrus_pkg,
                system_object=system_obj_id,
                encoded=encoded,
                epochs=epochs,
                deletable=deletable,
                payment_coin=payment_coin,
            )
            # add any move_calls of your own to `txn` here, before consuming `blob`
            await txn.transfer_objects(transfers=[blob], recipient=sender)
            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if not result.is_ok():
                raise RuntimeError(f"Tx1 failed: {result.result_string}")

            # --- Step 5: observe the outcome, collect what Tx2 needs ---
            object_id = find_created_object_id(
                effects=result.result_data.effects, owner=sender
            )
            blob_obj = await client.execute(command=GetObject(object_id=object_id))
            storage_fields = blob_obj.result_data.json.struct_value.fields[
                "storage"
            ].struct_value.fields
            end_epoch = int(storage_fields["end_epoch"].number_value)
            registration = Registration(
                object_id=object_id,
                blob_id=encoded.blob_id,
                end_epoch=end_epoch,
                deletable=deletable,
                digest=result.result_data.digest,
            )

            # --- Step 6: off-chain sliver fan-out and confirmation collection ---
            await upload_slivers(client=client, committee=committee, encoded=encoded)
            certificate = await collect_confirmations(
                client=client,
                committee=committee,
                blob_id=encoded.blob_id,
                registration=registration,
            )

            # --- Steps 7-8: a NEW transaction, Tx2 ---
            await assert_certificate_epoch_current(
                client=client,
                committee=committee,
                staking_object=client.config.network.staking_object,
            )
            txn2 = await client.transaction(initial_sender=sender)
            await add_certify(
                txn=txn2,
                package_id=walrus_pkg,
                system_object=system_obj_id,
                blob_object_id=object_id,
                certificate=certificate,
            )
            txdict2 = await txn2.build_and_sign()
            result2 = await client.execute(command=ExecuteTransaction(**txdict2))
            if result2.is_ok():
                print(object_id, "certified")

    asyncio.run(main())

The returned ``blob`` command result from ``add_reserve_and_register`` is
an **unconsumed object result** — the PTB aborts at execution if it is
not transferred or fed into a further move_call, exactly as
``delete_blob`` and ``exchange_for_wal`` handle their own move_call
results above. ``add_certify`` is the Tx2 counterpart and has no command
result of its own to consume.

Recovering After a Downstream Failure
'''''''''''''''''''''''''''''''''''''

Once Tx1 succeeds (step 4 above), the blob is registered and its storage
already paid for — that does not need to be redone even if sliver
fan-out, confirmation collection, or Tx2 subsequently fails. Within the
same process, recovery is just retrying from the failed stage with the
``registration``/``encoded``/``committee`` already built above — nothing
needs to be re-derived from chain:

This replaces Steps 6-8 in the ``async with WalrusClient(...) as client:``
block above — same ``registration``, ``encoded``, and ``committee``,
right after Tx1's ``result.is_ok()`` check, just wrapped for retry:

.. code-block:: python

    from pytusk import NativeUploadError

    # --- Steps 6-8, made retryable: sliver fan-out, confirmations, Tx2 ---
    for attempt in range(3):
        try:
            await upload_slivers(client=client, committee=committee, encoded=encoded)
            certificate = await collect_confirmations(
                client=client,
                committee=committee,
                blob_id=encoded.blob_id,
                registration=registration,
            )
            # ... Tx2 (Step 7-8) exactly as in the full example above ...
            break
        except NativeUploadError as exc:
            # exc.stage names the stage that failed (e.g. "sliver_upload",
            # "confirmations", "certify"). `registration` is still valid --
            # retry from here with the same object_id/blob_id/end_epoch;
            # Tx1 and the System object do not need to be touched again.
            print(f"Attempt {attempt} failed at stage {exc.stage}: {exc}")

If the *process itself* is gone by the time you retry — a later script
run, a different machine — that's a colder recovery: ``registration``
and ``encoded`` no longer exist in memory, so ``blob_id``/``end_epoch``/
``deletable`` have to be re-derived from the on-chain ``Blob`` object
instead. ``tusky certify_blob -i OBJECT_ID`` (:doc:`tusky`) is the
ready-made tool for exactly that case — pass ``--recover`` together with
the original content/file if sliver fan-out never completed either, so
it re-encodes and re-uploads before collecting confirmations.

``assert_certificate_epoch_current`` is a single cheap epoch read: a
``Certificate``'s ``signers_bitmap`` positions are only meaningful
against the committee ordering it was built from, so if the Walrus epoch
advances between confirmation collection and Tx2 submission, the
committee may have reordered and ``certify_blob`` would abort on-chain.
This is a **check only** — on a mismatch, the caller is responsible for
re-fetching the committee and redoing confirmation collection themselves;
:py:func:`~pytusk.certify` (used internally by ``store_blob_native``)
already retries this automatically, which is the main reason to prefer
the convenience path when you don't need to interleave custom move_calls.

Burning a Blob
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Burning destroys the blob object outright — irreversible, and no
storage resource is returned. Use this for non-deletable blobs, or
blobs you no longer want to track on-chain.

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import PytuskConfiguration, WalrusClient

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            system_obj_id = client.config.network.system_object
            sys_result = await client.execute(
                command=GetObject(object_id=system_obj_id)
            )
            walrus_pkg = sys_result.result_data.json.struct_value.fields[
                "package_id"
            ].string_value
            blob_object_id = "0x..."  # the blob's Sui object ID, not its Walrus blob ID

            txn = await client.transaction(initial_sender=sender)
            await txn.move_call(
                target=f"{walrus_pkg}::blob::burn",
                arguments=[blob_object_id],
                type_arguments=[],
            )

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())
