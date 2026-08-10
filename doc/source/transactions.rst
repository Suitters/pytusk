Transactions
============

``pytusk`` transactions are built and executed through the same
:py:class:`~pytusk.WalrusClient.transaction` builder pysui provides —
there is nothing pytusk-specific about the mechanics. This page covers
how ``PytuskConfiguration`` and ``PysuiConfiguration`` relate for
transaction purposes, then walks through the Walrus-specific PTBs
``pytusk`` builds internally for blob lifecycle and WAL/SUI exchange
operations.

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
