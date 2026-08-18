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

Client Design
--------------

``WalrusClient`` follows the same command-pattern style as pysui: a
single ``execute()`` entry point dispatches a command object rather than
exposing one method per operation.

- :py:class:`~pytusk.WalrusCommand` subclasses (e.g.
  :py:class:`~pytusk.ReadBlob`, :py:class:`~pytusk.StoreBlob`) are
  dispatched over HTTP to the Walrus aggregator or publisher, depending
  on the command.
- Sui-level commands (pysui ``SuiCommand`` subclasses) are forwarded
  directly to the underlying pysui client.
- ``await client.transaction(**kwargs)`` returns a pysui PTB builder for
  constructing custom Move calls — see :doc:`transactions` for the
  Walrus-specific PTBs ``pytusk`` builds internally.

Publisher/Aggregator vs. Native Upload
----------------------------------------

``pytusk`` offers two distinct ways to write and read Walrus blobs.
Both are fully supported; which to use is a per-application choice.

**Publisher/Aggregator (HTTP)** — :py:class:`~pytusk.StoreBlob`,
:py:class:`~pytusk.ReadBlob`, and the other :py:class:`~pytusk.WalrusCommand`
subclasses talk to a third-party Walrus publisher/aggregator over plain
HTTP. A store is one HTTP request; the publisher handles committee
resolution, sliver encoding/fan-out, and confirmation collection on your
behalf. This is the simpler, lower-latency path, and it's how
``tusky store_blob``/``read_blob`` work.

**Native Upload** — :py:func:`~pytusk.store_blob_native` (and the
``tusky store_blob_native``/``certify_blob`` CLI commands) talk directly
to the Sui chain and the storage-node committee: reserve/register on
Sui, encode and fan out slivers to storage nodes yourself, collect their
signed confirmations, and certify on Sui. No publisher is involved at
any point. See :doc:`transactions` for the underlying PTBs and
:doc:`tusky` for the CLI commands.

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

The trade-off is real: native upload takes two Sui transactions instead
of one HTTP call, is correspondingly slower, and — because it can fail
*between* those two transactions (Blob registered, not yet certified) —
exposes a partial-failure state that Publisher/Aggregator's single HTTP
call does not. ``tusky certify_blob`` (and the library's ``certify_blob``
entry points) exist specifically to recover from that state.

Next Steps
-----------

See :doc:`installation` to get set up, or :doc:`configuration` for how
``PytuskConfiguration`` and pysui's ``PysuiConfiguration`` line up.
