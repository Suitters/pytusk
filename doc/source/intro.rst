Introducing pytusk
===================

``pytusk`` is a Python SDK for interacting with `Walrus
<https://docs.walrus.site>`_ decentralized storage on Sui. It provides an
async client for storing and reading blobs and quilts, and for managing
their lifecycle on-chain.

Relationship to pysui
----------------------

``pytusk`` does not implement its own Sui-level configuration or
transport — it builds on top of `pysui
<https://github.com/FrankC01/pysui>`_ for both:

- :py:class:`~pytusk.PytuskConfiguration` wraps a pysui
  ``PysuiConfiguration``, adding Walrus-specific settings (aggregator and
  publisher endpoints, Walrus binary path) alongside the Sui network,
  address, and keystore configuration pysui already manages.
- :py:class:`~pytusk.WalrusClient` delegates all Sui-level operations —
  transaction building, execution, and querying — to the underlying
  pysui client. ``pytusk`` only adds the Walrus HTTP layer (aggregator/
  publisher requests) and the Walrus-specific move calls on top.

If you're already familiar with pysui's configuration and client model,
most of that knowledge carries over directly.

Walrus Terms
------------

.. list-table::
   :header-rows: 1
   :widths: 20 80
   :width: 100%

   * - Term
     - Definition
   * - Blob
     - Single unstructured data object stored on Walrus.
   * - Storage
     - A Sui object that reserves storage space for a blob over a duration,
       specified by ``start_epoch``/``end_epoch``; every blob is associated
       with a ``Storage`` object.
   * - Quilt
     - Batch storage feature that encodes multiple small blobs (up to 666
       for QuiltV1) into one unit, reducing per-byte overhead versus storing
       many small blobs individually.
   * - Shard
     - (Disjoint) Subset of erasure-encoded data of all blobs; at every
       point in time, a shard is assigned to and stored on a single storage
       node.
   * - Sliver
     - Erasure-encoded data of one shard corresponding to a single blob for
       one of the two encodings; this contains several erasure-encoded
       symbols of that blob but not the blob metadata.
   * - Stake
     - Delegating WAL tokens to a specific storage node operator, not the
       end user. Delegated stake determines storage-node committee
       membership and shard assignment each epoch, and earns a share of
       storage fees; principal is never slashed. ``pytusk``/``tusky`` is a
       storage client and does not expose staking/delegation operations —
       this term is included for protocol context only.

   * - Native Upload
     - Writing a blob directly against Sui and the storage-node committee:
       register on Sui, erasure-encode locally, fan the slivers out to
       storage nodes, collect their signed confirmations, then certify on
       Sui. No publisher or relay takes part.
   * - Upload Relay
     - A third-party service that performs the sliver fan-out on the
       client's behalf. The client registers the blob and
       pays the relay's tip in a single Sui transaction, POSTs the raw blob
       bytes to the relay, then certifies on Sui using the confirmation
       certificate the relay returns — delivery is delegated, certification
       is not.
   * - Native Read
     - Reading a blob directly from the storage-node committee: fetch and
       verify the blob's metadata, fan out for erasure-coded slivers until
       enough are held to decode, then reconstruct the content locally. No
       aggregator takes part, and a node that serves unusable slivers is
       identified and excluded rather than being allowed to fail the read.

Client Design
--------------

``WalrusClient`` follows the same command-pattern style as pysui: a
single ``execute()`` entry point dispatches a command object rather than
exposing one method per operation.

- :py:class:`~pytusk.WalrusCommand` subclasses (e.g.
  :py:class:`~pytusk.ReadBlob`, :py:class:`~pytusk.WriteBlob`) are
  dispatched over HTTP to the Walrus aggregator or publisher, depending
  on the command.
- Sui-level commands (pysui ``SuiCommand`` subclasses) are forwarded
  directly to the underlying pysui client.
- ``await client.transaction(**kwargs)`` returns a pysui PTB builder for
  constructing custom Move calls — see :doc:`transactions` for the
  Walrus-specific PTBs ``pytusk`` builds internally.

Choosing a Write Path
---------------------

``pytusk`` offers three ways to write Walrus blobs. All three are fully
supported; which to use is a per-application choice.

.. list-table::
   :header-rows: 1
   :widths: 22 26 26 26

   * -
     - Publisher/Aggregator (HTTP)
     - Native Upload
     - Upload Relay
   * - Sui transactions
     - None — one HTTP request
     - Two: register, then certify
     - Two: register (tip bundled into the same PTB), then certify
   * - Who encodes and fans out slivers
     - The publisher
     - ``pytusk``, on the client
     - ``pytusk`` encodes to derive the blob id; the relay re-encodes and
       fans out, from the raw bytes you POST
   * - Third-party dependency
     - Full — the publisher performs and attests the write
     - None beyond Sui RPC and the storage nodes
     - The relay, for delivery only — certification stays yours
   * - Cost above the protocol fee
     - Publisher markup, if any
     - None
     - A tip, quoted up front and capped by ``max_tip``
   * - Usable on Mainnet out of the box
     - Not out of the box — no URL configured by default; set your own
       self-hosted or authenticated publisher
     - Yes
     - Yes
   * - Partial-failure state
     - None
     - Registered but not certified
     - Registered and stored but not certified
   * - Blob size ceiling
     - ~13.6 GiB protocol maximum; a publisher may impose less
     - ~13.6 GiB, the Walrus protocol maximum
     - ~1 GiB relay request-body limit, which the relay does not advertise
   * - CLI commands
     - ``store_blob``, ``read_blob``, ``store_quilt``, ``read_quilt``
     - ``store_blob_native``, ``certify_blob``
     - ``store_blob_relay``, ``store_quilt_relay``, ``relay_configs``,
       ``certify_blob``
   * - Library entry point
     - :py:class:`~pytusk.WriteBlob`
     - :py:func:`~pytusk.store_blob_native`
     - :py:func:`~pytusk.store_blob_relay`,
       :py:func:`~pytusk.store_quilt_relay`
   * - Quilt writes
     - Yes, via a publisher -- ``store_quilt``
     - Not exposed by ``pytusk``
     - Yes -- ``store_quilt_relay``

**Publisher/Aggregator (HTTP)** — :py:class:`~pytusk.WriteBlob`,
:py:class:`~pytusk.ReadBlob`, and the other :py:class:`~pytusk.WalrusCommand`
subclasses talk to a third-party Walrus publisher/aggregator over plain
HTTP. A store is one HTTP request; the publisher handles committee
resolution, sliver encoding/fan-out, and confirmation collection on your
behalf. This is the simpler, lower-latency path, and it's how
``tusky store_blob``/``read_blob`` work.

**Native Upload** — :py:func:`~pytusk.store_blob_native` (and the
``tusky store_blob_native``/``certify_blob`` CLI commands) talk directly
to the Sui chain and the storage-node committee: reserve/register on
Sui, encode and fan out slivers to the storage nodes from your own
process, collect their signed confirmations, and certify on Sui. No publisher is involved at
any point. See :doc:`transactions` for the underlying PTBs and
:doc:`tusky` for the CLI commands.

**Upload Relay** — :py:func:`~pytusk.store_blob_relay` (and the
``tusky store_blob_relay``/``relay_configs`` CLI commands) registers the
blob on Sui and pays the relay's tip in a single PTB, POSTs the raw blob
bytes to the relay, then certifies on Sui using the confirmation
certificate the relay returns. ``pytusk`` still encodes locally on this
path -- that is how the blob id and root hash the registration needs are
derived -- but the relay re-encodes and performs the sliver fan-out that
native upload does on the client, which is what makes it practical for
Mainnet writes. :py:func:`~pytusk.store_quilt_relay` (and
``tusky store_quilt_relay``) writes a QUILT over that same path: the batch
is assembled into one blob locally, then registered, uploaded and certified
exactly as a single blob is, so a batch of small files costs one
registration and one certification instead of one each. See
:doc:`transactions` for the underlying PTBs and :doc:`tusky` for the CLI
commands.

Reasons to choose Publisher/Aggregator:

- Simplicity and speed — one HTTP call, no committee resolution or
  cryptographic work on the client.
- Testnet has a public, unauthenticated publisher/aggregator pair ready
  to use with no setup.

Reasons to choose Native Upload:

- **No third-party trust or availability dependency** — a publisher can
  rate-limit, add a tip on top of the protocol fee, go down, or reject
  requests; native upload uses only Sui RPC and the storage-node network,
  the same trust assumptions as the rest of the chain.
- **The default pytusk configuration ships with no Mainnet
  publisher/aggregator** — unlike testnet, ``PytuskConfiguration``'s
  built-in Mainnet network entry has an empty ``walrus_publisher_url``.
  The user can point it at a self-hosted publisher via
  ``set_walrus_publisher_url()``/``set_walrus_aggregator_url()`` (see
  :doc:`configuration`'s "Update Existing" section), or use native
  upload, which needs neither.
- **Cost transparency** — public publishers commonly add a markup/tip on
  top of the protocol's write price; native upload pays only the actual
  protocol cost.
- Predictable SLAs, custom retry/parallelism, and auditability of
  exactly whose signature certified the data — none of which is
  practical to build on top of a black-box third-party publisher.

Reasons to choose Upload Relay:

- **Mainnet writes without running infrastructure** — there is no public
  Mainnet publisher, and native upload's sliver fan-out is bandwidth-heavy
  and long-running for large blobs. The relay does that work for a tip.
- **The cost is quoted before it is spent** — ``relay_configs`` reports
  what each configured relay would charge for a given blob size, and
  ``max_tip`` rejects a quote above your ceiling *before* anything is
  composed, signed, or spent.
- **Certification stays yours** — the relay delivers slivers and returns a
  confirmation certificate, but your own Tx2 submits it. The relay never
  takes custody of the blob's on-chain identity, and cannot certify on
  your behalf.
- **It is the only path that writes quilts on Mainnet** — the publisher
  path needs a publisher Mainnet does not provide, and ``pytusk`` exposes
  no native-upload quilt command, so ``store_quilt_relay`` is how a batch
  of small files gets written there.

The trade-off is real: both native upload and the relay take two Sui
transactions instead of one HTTP call, are correspondingly slower, and can
fail *between* those two transactions — the blob registered and paid for,
but not yet certified. Neither strands what was purchased. ``tusky
certify_blob`` (and the library's certify entry points) recovers either
path from that state, and a relay upload may be re-submitted with the same
``tx_id``/``nonce`` at no additional cost. A relay write additionally
*returns* its outcome — ``RESUMABLE``, ``REJECTED`` or ``NOT_STARTED`` —
rather than raising, so a caller can decide whether to retry or certify
without unwinding storage it has already paid for.

Next Steps
-----------

See :doc:`installation` to get set up, or :doc:`configuration` for how
``PytuskConfiguration`` and pysui's ``PysuiConfiguration`` line up.
