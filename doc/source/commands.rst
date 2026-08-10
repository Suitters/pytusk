Commands
========

``pytusk`` models each Walrus HTTP operation as a
:py:class:`~pytusk.WalrusCommand` subclass, dispatched through
:py:meth:`WalrusClient.execute() <pytusk.WalrusClient.execute>` rather
than exposing one method per operation — the same pattern pysui uses for
its Unified Client Interface.

The WalrusCommand ABC
----------------------

:py:class:`~pytusk.WalrusCommand` is an abstract, keyword-only
(``kw_only=True``) dataclass. Each concrete command overrides:

* ``_endpoint_role`` — a ``ClassVar[str]``, either ``"aggregator"``
  (reads) or ``"publisher"`` (writes). Selects which Walrus daemon
  endpoint the command is sent to.
* ``http_method() -> str`` — the HTTP verb to use.
* ``url_path(base_url: str) -> str`` — the full request URL.
* ``parse_response(response: httpx.Response) -> SuiRpcResult`` — turns
  the raw HTTP response into a typed result.

and may optionally override (empty/``None`` by default):

* ``query_params() -> dict[str, Any]``
* ``request_body() -> bytes | None``
* ``form_files() -> dict[str, bytes] | None``

:py:meth:`WalrusClient.execute() <pytusk.WalrusClient.execute>` reads
``http_method()``, selects the aggregator or publisher base URL per
``_endpoint_role``, builds the URL via ``url_path()``, gathers
``query_params()``/``request_body()``/``form_files()``, makes the HTTP
request, and returns ``parse_response(response)``.

Response types
----------------

Each command's ``parse_response()`` returns one of these dataclasses:

* :py:class:`~pytusk.BlobData` — raw blob content
* :py:class:`~pytusk.BlobSlice` — a partial byte range of a blob
* :py:class:`~pytusk.QuiltPatch` — a single patch's content from a quilt
* :py:class:`~pytusk.BlobReceipt` — object/blob ID, cost, expiry, and
  deletable flag for a newly stored blob
* :py:class:`~pytusk.QuiltReceipt` — quilt ID, patch keys, cost, expiry,
  and object ID for a newly stored quilt

Command Reference
--------------------

.. list-table::
   :widths: 30 40 30
   :header-rows: 1

   * - Command
     - Description
     - Notes
   * - :py:class:`~pytusk.ReadBlob`
     - Read a blob by its Walrus blob ID.
     - Returns :py:class:`~pytusk.BlobData`.
   * - :py:class:`~pytusk.ReadBlobPartial`
     - Read a byte range from a blob.
     - Returns :py:class:`~pytusk.BlobSlice`.
   * - :py:class:`~pytusk.ReadBlobByObjectId`
     - Read a blob by its Sui object ID.
     - Returns :py:class:`~pytusk.BlobData`.
   * - :py:class:`~pytusk.ReadQuiltPatch`
     - Read a single patch from a quilt by quilt ID and patch key.
     - Returns :py:class:`~pytusk.QuiltPatch`.
   * - :py:class:`~pytusk.ConcatBlobs`
     - Read and concatenate multiple blobs into a single response.
     - Returns :py:class:`~pytusk.BlobData`. Uses the ``v1alpha`` endpoint.
   * - :py:class:`~pytusk.StoreBlob`
     - Store a blob on Walrus.
     - Returns :py:class:`~pytusk.BlobReceipt`. Deletable by default —
       pass ``permanent=True`` to disable.
   * - :py:class:`~pytusk.StoreQuilt`
     - Store a collection of named blobs (a quilt) on Walrus.
     - Returns :py:class:`~pytusk.QuiltReceipt`. Deletable by default —
       pass ``permanent=True`` to disable.

Read Commands
----------------

Detail for the read (aggregator) commands.

ReadBlob
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read a blob by its Walrus blob ID.

* ``blob_id: str`` — Walrus blob identifier.
* ``strict_consistency_check: bool = False`` — force strict integrity
  verification of the read blob against its metadata.
* ``skip_consistency_check: bool = False`` — skip integrity verification.
  Only safe when the writer is known and trusted.

Returns :py:class:`~pytusk.BlobData`.

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient, ReadBlob

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            result = await client.execute(command=ReadBlob(blob_id="..."))
            if result.is_ok():
                blob = result.result_data
                print(len(blob.content))

    asyncio.run(main())

ReadBlobPartial
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read a byte range from a blob.

* ``blob_id: str`` — Walrus blob identifier.
* ``start: int`` — first byte offset (inclusive).
* ``length: int`` — number of bytes to read.

Returns :py:class:`~pytusk.BlobSlice`.

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient, ReadBlobPartial

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            result = await client.execute(
                command=ReadBlobPartial(blob_id="...", start=0, length=1024)
            )
            if result.is_ok():
                blob_slice = result.result_data
                print(len(blob_slice.content))

    asyncio.run(main())

ReadBlobByObjectId
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read a blob by its Sui object ID, rather than its Walrus blob ID.

* ``object_id: str`` — Sui object ID of the blob.
* ``strict_consistency_check: bool = False``
* ``skip_consistency_check: bool = False``

Returns :py:class:`~pytusk.BlobData`.

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient, ReadBlobByObjectId

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            result = await client.execute(
                command=ReadBlobByObjectId(object_id="0x...")
            )
            if result.is_ok():
                blob = result.result_data
                print(len(blob.content))

    asyncio.run(main())

ReadQuiltPatch
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read a single patch from a quilt by quilt ID and patch key.

* ``quilt_id: str`` — Walrus quilt identifier.
* ``patch_key: str`` — key identifying the patch within the quilt.

Returns :py:class:`~pytusk.QuiltPatch`.

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient, ReadQuiltPatch

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            result = await client.execute(
                command=ReadQuiltPatch(quilt_id="...", patch_key="report.pdf")
            )
            if result.is_ok():
                patch = result.result_data
                print(len(patch.content))

    asyncio.run(main())

ConcatBlobs
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Read and concatenate multiple blobs into a single response, in the order
given.

* ``ids: list[str]`` — ordered list of Walrus blob IDs to concatenate.
* ``strict_consistency_check: bool = False``
* ``skip_consistency_check: bool = False``

Returns :py:class:`~pytusk.BlobData`. Uses the Walrus ``v1alpha`` API.

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient, ConcatBlobs

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            result = await client.execute(
                command=ConcatBlobs(ids=["blob_id_1", "blob_id_2"])
            )
            if result.is_ok():
                blob = result.result_data
                print(len(blob.content))

    asyncio.run(main())

Write Commands
----------------

Detail for the write (publisher) commands.

StoreBlob
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Store a blob on Walrus.

* ``data: bytes`` — raw blob content to store.
* ``epochs: int`` — number of epochs to store the blob for.
* ``send_object_to: str`` — Sui address to receive the created blob
  object. The publisher creates the blob object under its own wallet
  unless this is set, so ownership never transfers to the caller
  otherwise.
* ``permanent: bool = False`` — if ``True``, the blob cannot be deleted
  before expiry. Blobs are deletable by default (Walrus v1.33+); the
  publisher's ``deletable`` query parameter has been deprecated since
  v1.35 and has no effect, so this command does not send it —
  ``permanent`` is the only lever that controls persistence.

Returns :py:class:`~pytusk.BlobReceipt`.

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient, StoreBlob

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            result = await client.execute(
                command=StoreBlob(
                    data=b"hello walrus",
                    epochs=5,
                    send_object_to="0x...",
                )
            )
            if result.is_ok():
                receipt = result.result_data
                print(receipt.blob_id, receipt.cost)

    asyncio.run(main())

StoreQuilt
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Store a quilt — a collection of named blobs — on Walrus.

* ``files: dict[str, bytes]`` — mapping of patch key to raw file
  content.
* ``epochs: int`` — number of epochs to store the quilt for.
* ``send_object_to: str`` — Sui address to receive the created quilt
  blob object. The publisher creates the blob object under its own
  wallet unless this is set, so ownership never transfers to the caller
  otherwise.
* ``permanent: bool = False`` — if ``True``, the quilt cannot be deleted
  before expiry. Quilts are deletable by default (Walrus v1.33+),
  matching :py:class:`~pytusk.StoreBlob`'s persistence semantics.

Returns :py:class:`~pytusk.QuiltReceipt`.

.. code-block:: python

    import asyncio
    from pytusk import PytuskConfiguration, WalrusClient, StoreQuilt

    async def main():
        config = PytuskConfiguration(
            active_network="testnet",
            pysui_group_name="sui_grpc_config",
            pysui_profile_name="testnet",
        )
        async with WalrusClient(pytusk_config=config) as client:
            result = await client.execute(
                command=StoreQuilt(
                    files={"a.txt": b"...", "b.txt": b"..."},
                    epochs=5,
                    send_object_to="0x...",
                )
            )
            if result.is_ok():
                receipt = result.result_data
                print(receipt.quilt_id, receipt.patch_keys)

    asyncio.run(main())
