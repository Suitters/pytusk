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

Blob Metadata
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A Blob's on-chain metadata (Walrus's "attribute" concept) is a
``VecMap<String, String>`` held in a dynamic field, separate from the
``Blob`` struct's own fields. Three standalone, post-registration
operations manage it: setting/updating pairs, dropping pairs, and reading
them back.

**Setting metadata.** ``insert_or_update_metadata_pair`` gives upsert
semantics -- a key not yet present is inserted, an existing key's value is
overwritten. One ``move_call`` per pair is composed into a single PTB;
:func:`~pytusk.add_set_blob_metadata` does the composition:

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import PytuskConfiguration, WalrusClient, add_set_blob_metadata

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
            await add_set_blob_metadata(
                txn=txn,
                package_id=walrus_pkg,
                blob_object=blob_object_id,
                pairs={"content-type": "application/octet-stream"},
            )

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

**Dropping metadata.** Two Move entry points cover this, with no batch
primitive for either: ``remove_metadata_pair`` removes one named key (one
``move_call`` per requested key, composed into one PTB via
:func:`~pytusk.add_drop_blob_metadata_keys`); ``take_metadata`` drops the
whole metadata set in a single call
(:func:`~pytusk.add_drop_blob_metadata_all`). Both abort on chain
(``EMissingMetadata``) if the ``Blob`` has no metadata field at all, and
``remove_metadata_pair`` additionally aborts (``vec_map::remove``) if a
requested key is not present. Rather than pay gas for a transaction
guaranteed to abort, :func:`~pytusk.validate_blob_metadata_keys_exist` /
:func:`~pytusk.validate_blob_metadata_exists` fetch the blob's current
metadata and raise ``ValueError`` **before any PTB is composed** if the
target key(s) -- or any metadata at all, for the drop-all case -- don't
exist:

.. code-block:: python

    import asyncio
    from pysui import ExecuteTransaction, GetObject
    from pytusk import (
        PytuskConfiguration,
        WalrusClient,
        add_drop_blob_metadata_keys,
        validate_blob_metadata_keys_exist,
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
            blob_object_id = "0x..."  # the blob's Sui object ID, not its Walrus blob ID

            # Raises ValueError pre-spend if "content-type" isn't currently set,
            # rather than letting remove_metadata_pair abort after gas is spent.
            await validate_blob_metadata_keys_exist(
                client=client, blob_object=blob_object_id, keys=["content-type"]
            )

            txn = await client.transaction(initial_sender=sender)
            await add_drop_blob_metadata_keys(
                txn=txn,
                package_id=walrus_pkg,
                blob_object=blob_object_id,
                keys=["content-type"],
            )

            txdict = await txn.build_and_sign()
            result = await client.execute(command=ExecuteTransaction(**txdict))
            if result.is_ok():
                print(result.result_data)

    asyncio.run(main())

Dropping everything instead swaps ``add_drop_blob_metadata_keys``/``keys=[...]``
for ``add_drop_blob_metadata_all`` (no ``keys`` argument), and
``validate_blob_metadata_keys_exist`` for ``validate_blob_metadata_exists``
-- the rest of the PTB is identical.

**Reading metadata.** There is no Move getter -- ``metadata()`` /
``metadata_or_create()`` are private in ``blob.move`` -- so a read is a
pure client-side operation, no PTB involved:
:meth:`~pytusk.client.walrus_client.WalrusClient.get_blob_metadata` fetches
the blob's ``metadata`` dynamic field directly and returns ``None`` if it
doesn't exist at all:

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            blob_object_id = "0x..."  # the blob's Sui object ID, not its Walrus blob ID
            metadata = await client.get_blob_metadata(blob_object=blob_object_id)
            if metadata is None:
                print(f"{blob_object_id} has no metadata set.")
            else:
                for entry in metadata.data:
                    print(entry.key, entry.value)

    asyncio.run(main())

See `Quilt Upload Relay`_ below for the other place
``insert_or_update_metadata_pair`` is used in this project -- there it is
composed INSIDE Tx1's registration sequence, to write the
``_walrusBlobType = "quilt"`` marker at store time, rather than as a
standalone post-registration transaction like the operations above.

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

Upload Relay
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

An upload relay stages the same on-chain work as a native upload — Tx1
(``reserve_space`` + ``register_blob``), then Tx2 (``certify_blob``) — but
delegates the off-chain half. ``pytusk`` still encodes the blob locally, to
derive the blob id and root hash the registration needs, but instead of
fanning slivers out to the committee yourself you POST the raw blob bytes
and the relay does that, returning a signed confirmation certificate.
The relay is paid a tip, composed into the SAME transaction as the
registration. See :doc:`intro` for choosing between the three write paths,
and :doc:`configuration` for configuring which relays a network knows about.

The highest-level entry point runs the whole pipeline in one call:

.. code-block:: python

    import asyncio
    from pytusk import (
        PytuskConfiguration,
        RelayOutcome,
        WalrusClient,
        store_blob_relay,
    )

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            receipt = await store_blob_relay(
                client=client,
                data=b"hello walrus",
                epochs=5,
                deletable=True,
                sender=sender,
                max_tip=1_000_000,
                # Per-attempt cap on the POST to the relay. Unset uses the
                # client's configured timeout; raise it for a large blob,
                # because every retry re-sends the body from the start.
                timeout=900.0,
            )
            if receipt.outcome is RelayOutcome.CERTIFIED:
                print(receipt.blob_id, receipt.object_id)
            else:
                print(receipt.outcome)

    asyncio.run(main())

``store_blob_relay()`` orchestrates, in order: a sponsor pre-flight, a tip
quote, the ``max_tip`` ceiling check, the ``on_quote`` callback, a payment
pre-flight, Tx1 (tip +
``reserve_space`` + ``register_blob``), the POST to the relay, parsing the
returned certificate, and Tx2 (``certify_blob``). Everything that can refuse
the write does so BEFORE anything is spent: an unsignable sponsor, an
unusable tip coin, and a quote above ``max_tip`` all raise — the last as
:py:class:`~pytusk.TipCeilingExceededError` — before a PTB is built.

``on_quote`` is an optional callback invoked with the
:py:class:`~pytusk.TipQuote` immediately after the ceiling check and before
any PTB is composed. It receives the same quote that is composed into Tx1
rather than a second, separately fetched one, so a figure shown to a user
cannot disagree with what actually gets signed. Raising from it aborts the
write cleanly, since nothing has been spent at that point — which makes it
the place to put a confirmation prompt, or any refusal rule that ``max_tip``
alone cannot express.

.. code-block:: python

    def show_tip(quote):
        if quote.requires_payment:
            print(f"tip: {quote.amount} MIST to {quote.address}")

    receipt = await store_blob_relay(
        client=client,
        data=data,
        epochs=5,
        on_quote=show_tip,
    )

After Tx1 succeeds, failures are RETURNED rather than raised. The result is
a :py:class:`~pytusk.RelayBlobReceipt` whose
:py:class:`~pytusk.RelayOutcome` is one of ``CERTIFIED``, ``RESUMABLE``,
``REJECTED`` or ``NOT_STARTED``. Branch on ``outcome`` — never on whether a
certificate happens to be present. A ``RESUMABLE`` receipt carries the
digest and nonce needed to retry, and retrying is free: the relay applies no
replay protection, so re-submitting the same transaction digest and nonce
costs nothing beyond the tip already paid. ``tusky certify_blob``
(:doc:`tusky`) recovers the same state from the CLI.

.. warning::

   A relay enforces a maximum request-body size — in practice about 1 GiB —
   that it does not advertise: no field in the tip configuration reports it
   and no endpoint exposes it. ``pytusk`` therefore applies no client-side
   guard, and a simulate run will price a blob the relay will later refuse.
   Treat roughly 1 GiB as the practical ceiling for a relay write and use
   native upload for anything larger.

Composing the Relay Write by Hand
''''''''''''''''''''''''''''''''''''''

:py:func:`~pytusk.store_blob_relay` builds, signs, and submits both
transactions for you. An SDK developer who needs to own the transactions —
to choose sender and sponsor per transaction, or interleave their own
``move_call`` commands — can compose the same stages from the public
building blocks.

The one hard rule: **the tip must be the first thing added to Tx1.** The
relay locates the authentication package by a positional read of PTB input
0, so anything registered ahead of it makes the relay reject the upload.

.. code-block:: python

    import asyncio
    from pytusk import (
        PytuskConfiguration,
        WalrusClient,
        add_tip,
        assert_tip_within_ceiling,
        build_auth_package,
        parse_relay_certificate,
        quote_tip,
        upload_to_relay,
    )

    async def main():
        config = PytuskConfiguration(active_network="testnet")
        data = b"hello walrus"

        async with WalrusClient(pytusk_config=config) as client:
            relay_url = config.relay_url_for(network_name="testnet")
            committee = await client.committee()

            # Price this exact upload. A tip formula can depend on
            # encoded size, so the same relay charges differently for
            # different blobs -- there is no per-relay flat rate to cache.
            quote = await quote_tip(
                client=client,
                relay_url=relay_url,
                unencoded_length=len(data),
                n_shards=committee.n_shards,
            )

            # Refuse an over-budget quote here, while refusing still costs
            # nothing. Past Tx1 the tip is spent whatever happens next.
            assert_tip_within_ceiling(quote=quote, max_tip=1_000_000)

            # Bind the tip to THIS blob. Each call mints a fresh 32-byte
            # nonce, which is what distinguishes one attempt from another.
            # Resuming an interrupted upload must reuse the package built
            # here rather than build a second one, or the tip already paid
            # stops matching the upload the relay is asked to honour.
            auth = build_auth_package(data=data)

            # Compose Tx1 -- and add the tip BEFORE anything else. The relay
            # reads the authentication package out of PTB input 0 by
            # position, so any command registered ahead of it shifts the
            # package and the relay rejects the upload.
            txn = await client.transaction()
            await add_tip(
                txn=txn,
                relay_address=quote.address,
                tip_amount=quote.amount,
                auth_package=auth,
            )

            # Add the registration commands, then sign and submit Tx1 however
            # you like -- owning this step is the point of composing by hand.
            # Two of its results are needed below: the blob_id that
            # registration derived, and the digest of the transaction that
            # paid the tip.
            blob_id, tx1_digest = await register_and_submit_tx1(txn)

            # Hand the relay the raw bytes. It re-encodes them itself and
            # fans the slivers out to the committee; the digest is what
            # proves the tip was actually paid.
            result = await upload_to_relay(
                client=client,
                relay_url=relay_url,
                blob_id=blob_id,
                data=data,
                register_tip_tx_digest=tx1_digest,
                # Same per-attempt cap the one-call pipeline exposes; the
                # retry budget is spent here, not by the caller.
                timeout=900.0,
            )

            # The certificate arrives as raw JSON whose three parts are each
            # encoded differently. Parsing is a separate step so a decoding
            # problem reports as one, instead of resurfacing later as what
            # looks like a corrupt signature.
            #
            # ``blob_id`` and ``object_id`` are not decoration: the parser
            # rebuilds the confirmation message and rejects a certificate
            # that confirms some OTHER blob -- one that would verify locally
            # and then abort inside Move with the tip already spent. Pass
            # ``object_id=None`` for a permanent blob; for a deletable one
            # pass the registered ``Blob`` object id as raw bytes, via
            # ``object_id_to_raw_bytes``.
            certificate = parse_relay_certificate(
                payload=result.certificate,
                committee=committee,
                blob_id=blob_id,
                object_id=None,
            )

            # Certify in Tx2. From here the relay path and the native path
            # are identical -- see Composing Tx1/Tx2 by Hand above.

    asyncio.run(main())

``register_and_submit_tx1`` above stands in for your own Tx1 handling —
adding the registration commands, choosing signers, and submitting — since
that is exactly the part a hand-composed write exists to control. See
``Composing Tx1/Tx2 by Hand`` for the native equivalent.

``upload_to_relay`` reports rather than raises, exactly as the encapsulated
pipeline does: branch on ``result.outcome`` (``UPLOADED``, ``UNANSWERED`` or
``REFUSED``), not on whether ``result.certificate`` is set. Parsing is a
separate step because the relay encodes the certificate's three parts
inconsistently — committee positions as a JSON integer array, the message as
a plain integer array, and only the signature as base64 — and
``parse_relay_certificate`` is what reconciles that against the committee.

Use the convenience path unless you need to interleave custom ``move_call``
commands or control signing per transaction.


Quilt Upload Relay
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A quilt packs many small blobs into ONE Walrus blob, so a batch pays for one
registration and one certification instead of one each. Assembly is a purely
local step: once assembled, the quilt's bytes ARE an ordinary blob, and every
stage from the tip quote onward is identical to the blob relay write above.
That is what makes quilt writes practical on Mainnet, where no public
publisher exists. See :doc:`intro` for choosing between the write paths and
:doc:`tusky` for the ``store_quilt_relay`` command.

Only two things distinguish a quilt write from the blob write above. Tx1
carries one extra command -- ``insert_or_update_metadata_pair`` writing
``_walrusBlobType = "quilt"`` -- and the bytes registered are the assembled
buffer rather than a caller's blob. Tx2 is the same ``certify_blob``. See
`Blob Metadata`_ above for the standalone, post-registration form of the
same move_call, and how it differs from this write-time use inside Tx1.

The highest-level entry point runs the whole pipeline in one call:

.. code-block:: python

    import asyncio
    from pytusk import (
        PytuskConfiguration,
        QuiltPatchInput,
        RelayOutcome,
        WalrusClient,
        store_quilt_relay,
    )

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            sender = client.pysui_client.config.active_address
            receipt = await store_quilt_relay(
                client=client,
                patches=[
                    QuiltPatchInput(identifier="readme.md", contents=b"# hello"),
                    QuiltPatchInput(
                        identifier="notes.txt",
                        contents=b"second patch",
                        tags={"kind": "note"},
                    ),
                ],
                epochs=5,
                deletable=True,
                sender=sender,
                max_tip=1_000_000,
                # A quilt is larger than any single file in it, and every
                # retry re-sends the whole assembled buffer from the start.
                timeout=900.0,
            )
            if receipt.outcome is RelayOutcome.CERTIFIED:
                print(receipt.blob_id, receipt.object_id)
                for patch in receipt.patches:
                    print(patch.identifier, patch.patch_id)
            else:
                print(receipt.outcome)

    asyncio.run(main())

:py:class:`~pytusk.QuiltRelayReceipt` is a
:py:class:`~pytusk.RelayBlobReceipt` with exactly one field added:
``patches``, a tuple of :py:class:`~pytusk.QuiltPatchReceipt`. Every other
field -- ``outcome``, ``blob_id``, ``object_id``, the transaction digests,
the nonce -- means what it does on a blob relay write, so the error boundary
and the ``RESUMABLE`` recovery described above apply unchanged. ``blob_id``
IS the quilt id.

**Patches come back sorted by identifier, not in the order you passed them.**
Packing order fixes each patch's column range and therefore its
``QuiltPatchId``, so it has to be a function of the batch's content rather
than of how a caller happened to list it. Match patches by ``identifier``,
never by position.

Identifier rules -- non-empty, no trailing whitespace, no control
characters, and a 65535-byte ceiling that is a BYTE count rather than a
character one -- are enforced during assembly, which is pre-spend, so a bad
identifier raises before anything is paid for. To check a batch WITHOUT
assembling it, which is worth doing when identifiers come from user input,
call :py:func:`~pytusk.validate_quilt_identifier` on each one first.

.. warning::

   The relay's unadvertised request-body limit -- in practice about 1 GiB --
   applies to the ASSEMBLED quilt, not to any single patch. A batch of
   individually small files can cross it. Use native upload above that size.

Composing the Quilt Relay Write by Hand
''''''''''''''''''''''''''''''''''''''''

Assembly and encoding are the only stages that differ from
``Composing the Relay Write by Hand`` above. From the tip quote onward the
two paths are the same calls in the same order, so only the quilt-specific
front half is shown here.

One hard rule beyond the tip-first rule: **assemble and encode against the
SAME shard count.** The committee is never cached, so two separate fetches
can straddle an epoch change and disagree -- and a quilt assembled for one
``n_shards`` then encoded against another is a perfectly valid blob whose
geometry no reader can decode. Read the committee once, through
:py:func:`~pytusk.prepare_chain_context`, then take the encode's shard count
from the :py:class:`~pytusk.AssembledQuilt`'s own ``n_shards`` rather than
reading the committee a second time. The assembled quilt records what it was
packed for, so the two cannot drift apart.

.. code-block:: python

    import asyncio
    from pytusk import (
        QUILT_BLOB_ATTRIBUTES,
        PytuskConfiguration,
        QuiltPatchInput,
        WalrusClient,
        add_registration_sequence,
        assemble_quilt,
        encode_blob,
        prepare_chain_context,
        quilt_patch_id,
    )

    async def main():
        config = PytuskConfiguration(active_network="testnet")
        patches = [
            QuiltPatchInput(identifier="readme.md", contents=b"# hello"),
            QuiltPatchInput(identifier="notes.txt", contents=b"second patch"),
        ]

        async with WalrusClient(pytusk_config=config) as client:
            # One read for the committee, System object and package id.
            chain = await prepare_chain_context(client=client)

            # Pack the batch. Raises PRE-SPEND on a bad identifier or a
            # collision, which is the point of doing it before anything
            # is registered.
            assembled = assemble_quilt(
                patches=patches, n_shards=chain.committee.n_shards
            )

            # From here the buffer is an ordinary blob. The shard count
            # comes from the assembled quilt, not a second read of the
            # committee -- the quilt carries what it was packed for.
            encoded = encode_blob(
                data=assembled.data, n_shards=assembled.n_shards
            )

            # Patch ids need the assembled quilt's blob id, so they cannot
            # be composed before this point -- but they need nothing from
            # the chain, so they are known BEFORE Tx1 is signed.
            patch_ids = {
                layout.identifier: quilt_patch_id(
                    quilt_id=encoded.blob_id, layout=layout
                )
                for layout in assembled.patches
            }

            # Compose Tx1. add_registration_sequence orders the commands
            # for you -- tip first, then reserve+register, then the
            # attribute writes, then the transfer that consumes the Blob.
            txn = await client.transaction()
            await add_registration_sequence(
                txn=txn,
                encoded=encoded,
                epochs=5,
                deletable=True,
                package_id=chain.package_id,
                system_object=chain.system_object,
                recipient=sender,
                wal_payment_coin=payment_coin,
                tip=tip_composition,
                attributes=QUILT_BLOB_ATTRIBUTES,
            )

            # Sign and submit Tx1, then upload and certify exactly as in
            # Composing the Relay Write by Hand above -- upload_to_relay,
            # parse_relay_certificate, then certify_blob in Tx2.

    asyncio.run(main())

``sender``, ``payment_coin`` and ``tip_composition`` above stand in for your
own resolution of those: the address you are signing as, a ``Coin<WAL>`` you
own, and the tip built from ``quote_tip`` and ``build_auth_package``.
``wal_payment_coin`` is required here because
:py:func:`~pytusk.add_registration_sequence` is pure PTB composition and
makes no network calls, so it cannot resolve "no coin given" into a concrete
coin itself.

Passing ``attributes=QUILT_BLOB_ATTRIBUTES`` is not decoration: it is the
only on-chain record that these bytes are a quilt, and upstream Walrus
writes it on every quilt store. ``pytusk`` can still read a quilt stored
without it, because it parses the index out of the buffer itself, but the
stored blob would not identify itself as a quilt on chain.

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
