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

* ``endpoint_role`` — a ``ClassVar[str]``. Values include
  ``"aggregator"`` (reads), ``"publisher"`` (writes), and
  ``"storage_node"`` (operations sent directly to an individual storage
  node). Selects which Walrus endpoint the command is sent to.
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
``endpoint_role``, builds the URL via ``url_path()``, gathers
``query_params()``/``request_body()``/``form_files()``, makes the HTTP
request, and returns ``parse_response(response)``.

Response types
----------------

Every command's ``parse_response()`` returns a ``SuiRpcResult``. The
dataclass below is the payload carried on its ``result_data`` attribute:

* :py:class:`~pytusk.BlobData` — raw blob content
* :py:class:`~pytusk.BlobSlice` — a partial byte range of a blob
* :py:class:`~pytusk.QuiltPatch` — a single patch's content from a quilt
* :py:class:`~pytusk.BlobReceipt` — object/blob ID, cost, expiry, and
  deletable flag for a newly stored blob
* :py:class:`~pytusk.QuiltReceipt` — quilt ID, patch keys, cost, expiry,
  and object ID for a newly stored quilt
* :py:class:`~pytusk.SliverAck` — acknowledgement that a storage node
  accepted a sliver PUT
* :py:class:`~pytusk.MetadataAck` — acknowledgement that a storage node
  accepted a blob-metadata PUT
* :py:class:`~pytusk.SignedConfirmation` — a storage node's signed
  confirmation that it holds a blob's slivers
* :py:class:`~pytusk.BlobStatus` — one storage node's view of a blob's
  status. Unlike every entry above, this is a UNION rather than a single
  dataclass: :py:class:`~pytusk.NonexistentStatus`,
  :py:class:`~pytusk.InvalidStatus`, :py:class:`~pytusk.PermanentStatus`,
  :py:class:`~pytusk.DeletableStatus`, or
  :py:class:`~pytusk.UnresolvedStatus`. The variants mirror the protocol's
  own enum, so fields that are meaningful on only one of them (such as
  ``end_epoch``, which a deletable registration does not have) cannot be
  read on a variant that lacks them

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
     - Result data: :py:class:`~pytusk.BlobData`.
   * - :py:class:`~pytusk.ReadBlobPartial`
     - Read a byte range from a blob.
     - Result data: :py:class:`~pytusk.BlobSlice`.
   * - :py:class:`~pytusk.ReadBlobByObjectId`
     - Read a blob by its Sui object ID.
     - Result data: :py:class:`~pytusk.BlobData`.
   * - :py:class:`~pytusk.ReadQuiltPatch`
     - Read a single patch from a quilt by quilt ID and patch key.
     - Result data: :py:class:`~pytusk.QuiltPatch`.
   * - :py:class:`~pytusk.ConcatBlobs`
     - Read and concatenate multiple blobs into a single response.
     - Result data: :py:class:`~pytusk.BlobData`. Uses the ``v1alpha`` endpoint.
   * - :py:class:`~pytusk.StoreBlob`
     - Store a blob on Walrus.
     - Result data: :py:class:`~pytusk.BlobReceipt`. Deletable by default —
       pass ``permanent=True`` to disable.
   * - :py:class:`~pytusk.StoreQuilt`
     - Store a collection of named blobs (a quilt) on Walrus.
     - Result data: :py:class:`~pytusk.QuiltReceipt`. Deletable by default —
       pass ``permanent=True`` to disable.
   * - :py:class:`~pytusk.PutMetadata`
     - Store a blob's Red Stuff metadata at a storage node.
     - Result data: :py:class:`~pytusk.MetadataAck`. Must succeed before
       the node will accept any sliver PUT for that blob.
   * - :py:class:`~pytusk.PutSliver`
     - Store a primary or secondary sliver at a storage node.
     - Result data: :py:class:`~pytusk.SliverAck`.
   * - :py:class:`~pytusk.GetStorageConfirmation`
     - Fetch a storage node's signed confirmation for a blob's slivers.
     - Result data: :py:class:`~pytusk.SignedConfirmation`.
   * - :py:class:`~pytusk.GetBlobStatus`
     - Ask ONE storage node for its view of a blob's status.
     - Result data: one of the :py:class:`~pytusk.BlobStatus` variants. A
       per-node opinion, never a verdict.

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

Result data: :py:class:`~pytusk.BlobData`.

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

Result data: :py:class:`~pytusk.BlobSlice`.

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

Result data: :py:class:`~pytusk.BlobData`.

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

Result data: :py:class:`~pytusk.QuiltPatch`.

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

Result data: :py:class:`~pytusk.BlobData`. Uses the Walrus ``v1alpha`` API.

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

Result data: :py:class:`~pytusk.BlobReceipt`.

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

Result data: :py:class:`~pytusk.QuiltReceipt`.

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

Storage Node Commands
---------------------

Detail for the commands sent directly to an individual storage node.
The base URL for these is a specific node's address resolved from the
committee, not an aggregator or publisher daemon.

PutMetadata
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Store a blob's Red Stuff metadata at a storage node. A node requires this
metadata before it will accept any sliver PUT for the same blob — a node
that has not received it rejects every sliver PUT with HTTP 400
``FAILED_PRECONDITION`` / ``METADATA_NOT_FOUND``.

* ``blob_id: bytes`` — raw 32-byte blob ID.
* ``metadata_bcs: bytes`` — BCS-encoded Walrus ``BlobMetadata`` payload.

Result data: :py:class:`~pytusk.MetadataAck`.

PutSliver
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Store a primary or secondary sliver at a storage node.

* ``blob_id: bytes`` — raw 32-byte blob ID.
* ``sliver_pair_index: int`` — sliver-pair index to store at.
* ``sliver_type: str`` — ``"primary"`` or ``"secondary"``, lowercase.
  These literals are the upstream ``Axis`` serde values; the wire format
  is case-sensitive and accepts nothing else.
* ``data: bytes`` — raw BCS-encoded sliver bytes.

Result data: :py:class:`~pytusk.SliverAck`.

GetStorageConfirmation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Fetch a storage node's signed confirmation that it holds a blob's
slivers. The permanent and deletable forms are two separate upstream
routes — the object ID travels in the URL path, not as a query
parameter.

* ``blob_id: bytes`` — raw 32-byte blob ID.
* ``object_id: str | None = None`` — Sui object ID of the deletable
  ``Blob`` object. ``None`` selects the permanent-blob route.
* ``wait_for_registration: bool = True`` — when True, the node long-polls
  until it observes the on-chain registration event before responding.
  This is server-side long-polling that replaces client-side backoff for
  the registration-propagation race.
* ``wait_millis: int | None = None`` — upper bound in milliseconds for
  the node's long-poll wait. Omitted from the request when ``None``.

The result's ``serialized_message`` is passed VERBATIM into
``certify_blob``; the client never reconstructs it.

Result data: :py:class:`~pytusk.SignedConfirmation`.

GetBlobStatus
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Ask one storage node what it knows about a blob:
``GET /v1/blobs/{blob_id}/status``.

This is a per-node OPINION, never a verdict. A single node can be stale,
byzantine, or simply unaware of a recent registration. Establishing a
verdict means fanning this command across the committee and applying a
shard-weight threshold — which is what
:py:func:`~pytusk.fetch_blob_status` does. Reach for that unless you
specifically want one node's answer.

* ``blob_id: bytes`` — raw 32-byte blob ID.

Parse failures come back as a failed ``SuiRpcResult`` rather than raising,
matching ``GetStorageConfirmation``: one bad node response must never
abort a committee-wide fan-out.

Result data: one of the :py:class:`~pytusk.BlobStatus` variants.
