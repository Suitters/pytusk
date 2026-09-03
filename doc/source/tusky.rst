tusky CLI
=========

``tusky`` is a command-line tool for interactive and scriptable use of pytusk's
Walrus support. It wraps :class:`~pytusk.client.walrus_client.WalrusClient` and
the underlying pysui transaction builder, exposing blob/quilt storage,
inspection, and blob-lifecycle operations as shell subcommands.

Installation & Invocation
--------------------------

``tusky`` is registered as a console script (``pyproject.toml``,
``[project.scripts]``: ``tusky = "pytusk.tusky.tusky:main"``), so once pytusk
is installed in the active environment it is available directly as a shell
command — no ``python -m`` prefix needed:

.. code-block:: console

   $ tusky --help
   $ tusky <subcommand> --help

Every subcommand accepts ``-h``/``--help`` for its own detailed usage.

Global Options
--------------

Configuration options
~~~~~~~~~~~~~~~~~~~~~~

Every subcommand accepts these options to control which
:class:`~pytusk.config.tusk_config.PytuskConfiguration` is loaded:

``--path``
   Directory containing ``PytuskConfig.json`` (default: ``~/.pysui``).

``--network``
   Override the active Walrus network, e.g. ``testnet`` or ``mainnet``
   (default: value stored in ``PytuskConfig.json``).

``--pysui-config-path``
   Override the pysui config folder path (default: value stored in
   ``PytuskConfig.json``).

``--pysui-group``
   Override the pysui ``ProfileGroup`` (protocol) for this session.

``--pysui-profile``
   Override the pysui ``Profile`` for this session (default: ``testnet``).

``--pysui-address`` / ``--pysui-alias``
   Set the active Sui address for this session, either directly or by
   alias. Mutually exclusive.

Signing options (PTB commands only)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The commands that build and submit a Programmable Transaction Block (PTB)
-- ``store_blob_native``, ``store_blob_relay``, ``store_quilt_relay``,
``certify_blob``, ``exchange_for_wal``, ``exchange_for_sui``,
``extend_blob_expiration``, ``set_blob_metadata``, ``drop_blob_metadata``,
``delete_blob``, ``burn_blob``, ``split_storage``, ``fuse_storage``,
``reclaim_storage`` and ``extend_blob_with_storage`` -- additionally
accept:

``--sender``
   Address to build and sign the transaction as (default: active address).

``--sponsor``
   Address to sponsor gas for the transaction, if different from the
   sender.

``--mode``
   ``simulate`` (default) dry-runs the transaction and prints the
   projected effects without touching chain state; ``execute`` submits it
   for real.

The pure-HTTP commands (``store_blob``, ``read_blob``, ``store_quilt``,
``read_quilt``) do **not** accept ``--mode`` — storing/reading a blob over
HTTP has no simulate/execute distinction; every invocation is live against
the configured publisher/aggregator.

Blob & Quilt Storage (HTTP)
----------------------------

These four commands talk directly to the Walrus publisher/aggregator over
HTTP — no Sui transaction is involved in the store/read itself (though
storing does register a ``Blob``/quilt object on-chain as a side effect of
the publisher's own processing).

store_blob
~~~~~~~~~~

Store a single blob via the Walrus HTTP publisher.

.. code-block:: console

   tusky store_blob (--content TEXT | --file PATH) --epochs N
                     [--permanent] [--recipient ADDRESS]

``--content`` / ``--file``
   Mutually exclusive, one required. ``--content`` stores UTF-8 text
   directly; ``--file`` reads and stores a file's raw bytes.

``--epochs``
   Number of epochs to store the blob for (a duration, not an absolute
   epoch number).

``--permanent``
   Store as permanent (cannot be deleted before expiry). **Blobs are
   deletable by default** — Walrus's ``deletable`` HTTP query parameter has
   been deprecated since Walrus v1.35 and has no effect, so ``permanent``
   is the only lever that actually controls persistence. Omitting this
   flag means the stored blob *is* deletable.

``--recipient``
   Sui address to receive the stored blob object (default: active
   address). The publisher creates the blob object under its own wallet
   unless this is set, so ownership never transfers to the caller
   otherwise.

Example:

.. code-block:: console

   $ tusky store_blob --content "gap1-test" --epochs 1
   {
     "object_id": "0x3e163d3b14bef322b3d7eea360dc4a15c2a8f1cb488a2be9de980b3268d08b66",
     "blob_id": "OgrPHsCfZIQm_m3U3fzddQff5ubQOFuSE0RHkMY7sa8",
     "cost": 748755,
     "expiry_epoch": 486,
     "deletable": true
   }

read_blob
~~~~~~~~~

Read blob content via the Walrus HTTP aggregator, writing raw bytes to
stdout.

.. code-block:: console

   tusky read_blob -b BLOB_ID

``-b`` / ``--blob-id``
   The **Walrus blob ID** (URL-safe base64, content hash) to read. This
   is a different flag from ``-o``/``--object-id`` (used by every other
   blob-targeting command — see the note under `blob inspection &
   reporting`_ below), because ``read_blob`` addresses content by its
   Walrus blob ID rather than by the blob's Sui object ID.

store_quilt
~~~~~~~~~~~

Store a quilt — multiple named files batched into a single blob — via the
Walrus HTTP publisher. Batching avoids paying Walrus's fixed
~64 MB per-blob metadata overhead once per file; every patch shares one
registration.

.. code-block:: console

   tusky store_quilt [--paths PATH [PATH ...]]
                      [--patch-file KEY=PATH ...] [--patch-content KEY=TEXT ...]
                      --epochs N [--permanent] [--recipient ADDRESS]

``--paths``
   File paths to include in the quilt, shell-expandable (e.g. ``*.py``).
   The patch key for each is derived from its filename.

``--patch-file``
   Patch key and file path, in ``KEY=PATH`` form (repeatable). Use this
   when you need an explicit patch key that differs from the filename.

``--patch-content``
   Patch key and inline UTF-8 text content, in ``KEY=TEXT`` form
   (repeatable).

``--paths``, ``--patch-file``, and ``--patch-content`` are combinable —
at least one patch from any of the three is required. Duplicate patch
keys across all three sources are rejected as an error.

``--epochs``, ``--permanent``, ``--recipient``
   Same semantics as ``store_blob``.

Example — explicit inline content:

.. code-block:: console

   $ tusky store_quilt --patch-content patch1="hello quilt" --epochs 1
   {
     "quilt_id": "sF5cQqDEYm3FTvdKAr8PnUP_BdEg5DwpU1kPzDiQXHY",
     "patch_keys": ["patch1"],
     "cost": 748755,
     "expiry_epoch": 486,
     "object_id": "0x720b429f5a9140909bca4cb0d73ac997e462f045fa297c09f861405e28a64eae"
   }

Example — glob expansion via ``--paths``, patch keys derived from
filenames:

.. code-block:: console

   $ tusky store_quilt --paths pytusk/commands/*.py --epochs 5
   {
     "quilt_id": "Fu6mKvsPyUmPCImX5VOCZ0v0QjmjLP4uJ6PoKHtGyvc",
     "patch_keys": [
       "__init__.py",
       "read_commands.py",
       "walrus_command.py",
       "write_commands.py"
     ],
     "cost": 1747179,
     "expiry_epoch": 490,
     "object_id": "0x51944421443f6e6d346d4135e5cbd93081f65b787ea1cf694af7ef1c31dd686d"
   }

read_quilt
~~~~~~~~~~

Read a single patch from a quilt via the Walrus HTTP aggregator, writing
its raw bytes to stdout. Two mutually exclusive addressing modes: either
``--quilt-id`` and ``--patch-key`` together, or ``--patch-id`` alone.

.. code-block:: console

   tusky read_quilt --quilt-id QUILT_ID --patch-key KEY
   tusky read_quilt --patch-id PATCH_ID

``--quilt-id``
   The quilt's Walrus identifier (as returned in ``store_quilt``'s
   ``quilt_id`` field). Used with ``--patch-key``.

``--patch-key``
   The key identifying the patch within the quilt (as returned in
   ``store_quilt``'s ``patch_keys`` list). Used with ``--quilt-id``.

``--patch-id``
   The patch's Walrus QuiltPatchId (as returned in ``quilt_patches``'
   per-patch ``patch_id`` field), addressing it directly without a
   separate quilt ID. Not combined with ``--quilt-id``/``--patch-key``.

Example, continuing from the ``store_quilt`` example above:

.. code-block:: console

   $ tusky read_quilt --quilt-id sF5cQqDEYm3FTvdKAr8PnUP_BdEg5DwpU1kPzDiQXHY --patch-key patch1
   hello quilt

Or, reading the same patch directly by its ``QuiltPatchId``:

.. code-block:: console

   $ tusky read_quilt --patch-id sF5cQqDEYm3FTvdKAr8PnUP_BdEg5DwpU1kPzDiQXHYBAQBdAg
   hello quilt

Blob & Quilt Inspection & Reporting
-----------------------------------

.. note::

   Except for ``read_blob`` above (which takes ``-b``/``--blob-id``),
   every ``-o``/``--object-id`` argument in ``tusky`` — including
   throughout this section and the lifecycle commands below — takes the
   blob's **Sui object ID** (0x-prefixed), not the Walrus blob ID (content
   hash).

quilt_patches
~~~~~~~~~~~~~

List the patches contained in a quilt, gating on expiry first: since a
quilt is a blob like any other, its content can be expired or
never-existed, both of which the aggregator's list-patches-in-quilt
endpoint reports identically as a bare ``BLOB_NOT_FOUND``. Resolving the
committee's own verdict first gives a clearer answer, and for a still-live
quilt avoids ever reaching that ambiguity at all.

.. code-block:: console

   tusky quilt_patches -b BLOB_ID
   tusky quilt_patches -o OBJECT_ID

``-b`` / ``--blob-id``
   The **Walrus blob ID** (URL-safe base64, content hash) of the quilt.
   Validated at parse time — a malformed ID is rejected before any network
   call.

``-o`` / ``--object-id``
   The **Sui object ID** of the quilt's ``Blob``. The blob ID is read from
   the object. Mutually exclusive with ``-b``; exactly one is required.

``--timeout``
   Overall deadline for the committee status query used to gate on
   expiry, in seconds (default: 10).

**Expiry gate.** How the check is made depends on the committee's verdict:

1. ``NonexistentStatus`` / ``InvalidStatus`` → the quilt does not exist;
   exits before any aggregator call.
2. ``PermanentStatus`` → checked directly against its own ``end_epoch``, no
   extra network call needed.
3. ``DeletableStatus`` → committee status alone carries no ``end_epoch``,
   so the on-chain ``Blob`` object(s) are resolved and checked instead. An
   object that could not be resolved is never treated as evidence of
   expiry — only an object actually resolved and found expired blocks the
   read, and only if every resolved object agrees.
4. ``UnresolvedStatus`` → no committee verdict reached, so there is no
   reliable expiry signal either way; the read proceeds and the aggregator
   answers.

Output is JSON, enriched with the facts resolved during the gate:

.. code-block:: console

   $ tusky quilt_patches -b 8XiGin_tGIB1hpkJINtSgKToT0EFo3qdYMY4U9nBJK4
   {
     "blob_id": "8XiGin_tGIB1hpkJINtSgKToT0EFo3qdYMY4U9nBJK4",
     "status": "PermanentStatus",
     "committee_epoch": 507,
     "end_epoch": 508,
     "patches": [
       {
         "patch_key": "testdata_10mb.bin",
         "patch_id": "8XiGin_tGIB1hpkJINtSgKToT0EFo3qdYMY4U9nBJK4BAQBdAg",
         "tags": {}
       },
       {
         "patch_key": "testdata_1mb.bin",
         "patch_id": "8XiGin_tGIB1hpkJINtSgKToT0EFo3qdYMY4U9nBJK4BXQKaAg",
         "tags": {}
       }
     ]
   }

``ladder_tier`` appears alongside ``end_epoch`` when the ``DeletableStatus``
resolution path actually ran; it is omitted when the ``PermanentStatus``
fast path answered directly, or when nothing could be resolved at all.

blobs
~~~~~

List all Walrus ``Blob`` objects owned by the active address.

.. code-block:: console

   tusky blobs [--deletable any|true|false] [--status any|active|expired] [--show-type]

``--deletable``
   Filter by deletable status (default: ``any``).

``--status``
   Filter by expiry status relative to the current Walrus epoch (default:
   ``any``).

``--show-type``
   Also show each blob's type (blob/quilt), read from the
   ``_walrusBlobType`` metadata attribute pytusk itself writes at store
   time -- NOT an authoritative content check: correct only for as long
   as that tag is set and retained. A real quilt reports as ``blob`` if
   the tag was never set (e.g. stored by other tooling) or was later
   removed via ``drop_blob_metadata`` (default: off, since this costs
   one extra RPC call per listed blob).

.. warning::
   ``--show-type`` reports quilt vs. blob purely by reading the
   ``_walrusBlobType`` metadata attribute that pytusk itself writes when
   storing a quilt. It is **not** an authoritative content check. The
   result is correct only for as long as that tag remains set: a real
   quilt reports as ``blob`` if the tag was never written (e.g. stored by
   other tooling) or was later removed (e.g. via ``drop_blob_metadata
   --all`` or ``--keys _walrusBlobType``).

Each matching blob prints one line:

.. code-block:: console

   $ tusky blobs
   0x9726699cf5440c3bb6e62568becefc4b1f519b3d3c70442d20007074f5dde627  blob_id=vj24cJ8WBaO0Cs99h1_7Ozp_u5HZ9VAQnGvuYxpgWjM  deletable=True  end_epoch=496  status=active  size=1048576

With ``--show-type``, each line gains a trailing ``type=blob``/``type=quilt``
field:

.. code-block:: console

   $ tusky blobs --show-type
   0x9726699cf5440c3bb6e62568becefc4b1f519b3d3c70442d20007074f5dde627  blob_id=vj24cJ8WBaO0Cs99h1_7Ozp_u5HZ9VAQnGvuYxpgWjM  deletable=True  end_epoch=496  status=active  size=1048576  type=blob

blob
~~~~

Show full on-chain details for one blob object, as formatted JSON.

.. code-block:: console

   tusky blob -o OBJECT_ID

Example (``bcs`` payload abbreviated for readability):

.. code-block:: console

   $ tusky blob -o 0x3e163d3b14bef322b3d7eea360dc4a15c2a8f1cb488a2be9de980b3268d08b66
   {
     "bcs": { "...": "..." },
     "objectId": "0x3e163d3b14bef322b3d7eea360dc4a15c2a8f1cb488a2be9de980b3268d08b66",
     "version": "972402030",
     "owner": { "kind": "ADDRESS", "address": "0xa9e2..." },
     "objectType": "0xd847...::blob::Blob",
     "json": {
       "registered_epoch": 485,
       "blob_id": "79467892988835579831563385470262646650425135586010591905919968817413438245434",
       "size": "9",
       "storage": { "start_epoch": 485, "end_epoch": 486, "storage_size": "66034000" },
       "deletable": true
     }
   }

The ``json.deletable`` field is the authoritative on-chain persistence
state for the blob.

blob_status
~~~~~~~~~~~

Ask the Walrus storage committee what it knows about a blob, and resolve
the answers against shard-weight thresholds.

Unlike `blob`_, which reads one on-chain object you name, this answers for
the CONTENT regardless of who owns it. On-chain blob_sui_object details are
layered on afterwards where they can be reached.

.. code-block:: console

   tusky blob_status -b BLOB_ID
   tusky blob_status -o OBJECT_ID

``-b`` / ``--blob-id``
   The **Walrus blob ID** (URL-safe base64, content hash). Validated at
   parse time — a malformed ID is rejected before any network call.

``-o`` / ``--object-id``
   The **Sui object ID** of a ``Blob``. The blob ID is read from the
   object, so blob_sui_object details are always available. Mutually
   exclusive with ``-b``; exactly one is required.

``--details``
   List every committee member: those confirming the verdict, and those
   dissenting with the reason each did not contribute.

``--timeout``
   Overall deadline for the whole query, in seconds (default: 10). NOTE
   this differs from ``store_blob_relay``'s ``--timeout``, which bounds
   each attempt. Because the fan-out stops early once a threshold is met,
   this mostly governs stragglers rather than the common case.

**blob_sui_object enrichment ladder.** How much on-chain detail
accompanies the verdict depends on what can be reached:

1. Node verdict — always.
2. The configured address owns a blob_sui_object for this blob → every
   owned blob_sui_object is listed.
3. Otherwise, if the blob is permanent → its status event is resolved to
   the ``Blob`` object, so blob_sui_object facts appear even for a blob
   you do not own. Never available for a deletable blob, which has no
   status event.
4. Otherwise (deletable-and-unowned, or nonexistent/invalid) → node status
   only.

The tier reached is printed as ``ladder_tier``. A thin result means no
object was reachable, **not** that the blob has no objects.

**Exit codes.**

``0``
   A verdict was reached, by quorum or by validity.

``1``
   The invocation failed — no such object, not a ``Blob`` object, or the
   committee could not be fetched.

``2``
   No verdict. The committee did not produce enough agreeing weight before
   the deadline. Distinct from ``1`` on purpose: a script must be able to
   tell "your invocation was wrong" from "the network did not answer."

Example:

.. code-block:: console

   $ tusky blob_status -b lATLPe9-w0AkcvWAtDThx49IlvNrn8GLWkh2tv9pXGw
   blob_id: lATLPe9-w0AkcvWAtDThx49IlvNrn8GLWkh2tv9pXGw
   status: permanent
   resolution: QUORUM (73 confirming, 2 dissenting, 26 unreachable, 0 not-checked)
   committee_epoch: 507
   ladder_tier: 2
   end_epoch: 508 (1 epoch remaining)
   certified: True
   initial_certified_epoch: 507
   deletable_objects: 0 total, 0 certified
   blob_sui_object: 0x7ad6fc95a7b557cc4bae10040f46ac85f974bb930575914b9036dad2346a8269  deletable=False  end_epoch=508 (1 epoch remaining)

The four counts on the ``resolution`` line are deliberately separate.
*dissenting* nodes answered with a different status; *unreachable* nodes
supplied no answer at all; *not-checked* nodes were never queried or were
cancelled once the verdict had already settled. Collapsing them would
report a committee as split when it is merely patchy in reachability.

get_blob_metadata
~~~~~~~~~~~~~~~~~~

Show a blob's on-chain metadata (Walrus "attribute") key/value pairs, read
directly from its metadata dynamic field. Pure read -- no transaction is
built or submitted.

.. code-block:: console

   tusky get_blob_metadata -o OBJECT_ID

``-o`` / ``--object-id``
   Sui object ID of the blob (not the Walrus blob ID).

A blob with no metadata set at all prints a one-line message instead of a
table. Otherwise, output is a two-column Key/Value table sized to its
content:

.. code-block:: console

   $ tusky get_blob_metadata -o 0x9726699cf5440c3bb6e62568becefc4b1f519b3d3c70442d20007074f5dde627
   Key           Value
   ------------  ----------------------
   content-type  application/octet-stream

See :doc:`transactions`'s Blob Metadata section for the underlying read
and the PTB-based ``set_blob_metadata``/``drop_blob_metadata`` operations.

epoch
~~~~~

Print the current Walrus epoch as a single integer.

.. code-block:: console

   tusky epoch

committee
~~~~~~~~~

Show the active Walrus storage committee for the configured network.

.. code-block:: console

   tusky committee

Prints one summary line (current epoch, total shard count, and committee
size), followed by one line per member: its position in the on-chain
ordering, its shard count, node ID, a truncated public key, and its
storage-node network address. A member's position is the index that
``signers_bitmap`` refers to when the native upload pipeline aggregates
confirmation signatures — see :doc:`logging` and the ``store_blob_native``
/ ``certify_blob`` commands below.

.. code-block:: console

   $ tusky committee
   epoch 485  shards 1000  members 101
      0     4 shards  0x2c91e0e719574046515477754ad9a0750ca470f27890b2ae71339ac9dddfb113  a1b2c3d4..e5f6a7b8  storage.example.io:9185
      1    12 shards  0xf44ca6660cc41df51531915fc1c7543fdbabc11a9acbeb10db2dc7eeb3dbe532  b2c3d4e5..f6a7b8c9  storage2.example.io:9185

No transaction is involved — this is a read-only on-chain query. The
committee is always resolved live, never cached: a stale committee
carried across an epoch boundary would send slivers or confirmation
requests to the wrong storage nodes.

expiry_report
~~~~~~~~~~~~~~

Print an aging report of owned blobs — object ID, storage end epoch,
current epoch, and epochs remaining — sorted soonest-to-expire first.

.. code-block:: console

   tusky expiry_report [--address ADDRESS]

``--address``
   Report on a different address (default: active address).

A blob's status is ``expired`` when remaining epochs is negative,
``expiring`` when exactly zero, and ``active`` when positive.

.. code-block:: console

   $ tusky expiry_report
   OBJECT ID                                                            END_EPOCH  CURRENT_EPOCH  REMAINING  STATUS
   0x1382979ea8716a95a02b2cb8266916cc4c4ed525b997bcad52c5ac6d2512ee99         492            493         -1  expired
   0x4821c4d9418ccf763d550c6159f38e23d7052437916e1ab0d247d7feef24687e         492            493         -1  expired
   0x5b0727fd1b2710e80d132d66a21c034a10924670ed9af67b4f2ac98d86f12682         492            493         -1  expired
   0x7938b7368131b0ee5748acd5bd6c02cfe58dc6ac03f57270b6c7554f8e14229f         492            493         -1  expired
   0x848710717e47e4695b4e9a6757ba9a3de3e58c33db40f45af682f5287136e7f3         492            493         -1  expired
   0x4280c2303826e9bec80eac1e43edc596563bf69b1b516d181b1a69c1b96aaef7         498            493          5  active
   0xa126a389363413c4b1dae9d9a42fab9cdae2afaaf4ce73ea0a3d3a7f33a99a55         498            493          5  active

wal_coins
~~~~~~~~~

List WAL coin objects owned by an address, with per-coin and aggregate
balances (styled on pysui's gas-listing layout).

.. code-block:: console

   tusky wal_coins [--address ADDRESS]

``--address``
   List coins for a different address (default: active address).

No transaction is involved — this is an informational query only.

WAL / SUI Exchange (pysui PTB)
------------------------------

Both commands call into the ``wal_exchange`` Move package and require a
network with configured exchange objects — use ``--network`` to target a
specific network. Testnet ships with exchange objects pre-populated;
mainnet ships with an empty list by default, and these commands will
error on any network without exchange objects configured. See
:doc:`configuration` for how to register exchange object IDs via
``PytuskConfiguration.set_exchange_objects()``.

exchange_for_wal
~~~~~~~~~~~~~~~~~

Exchange SUI for WAL.

.. code-block:: console

   tusky exchange_for_wal --amount MIST [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``--amount``
   Amount of SUI to exchange, in MIST.

**Coin handling:** ``exchange_for_wal`` splits ``--amount`` MIST from the
sender's gas coin client-side (an explicit ``split_coin`` call), then
passes the split coin to ``wal_exchange::exchange_all_for_wal``. The
resulting WAL coin is transferred to the sender.

exchange_for_sui
~~~~~~~~~~~~~~~~~

Exchange WAL for SUI.

.. code-block:: console

   tusky exchange_for_sui --amount FROST [--merge]
                           [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``--amount``
   Amount of WAL to exchange, in FROST.

``--merge``
   If no single owned WAL coin covers ``--amount``, merge owned WAL coins
   largest-first into the largest coin until the running total covers the
   amount, before exchanging. Without ``--merge``, an amount that no
   single coin covers is a clean error.

**Coin handling:** unlike ``exchange_for_wal``, this command does **not**
always split client-side. The largest available WAL coin (after any
``--merge``) is selected: if its balance exactly equals ``--amount``, the
whole coin is consumed via ``wal_exchange::exchange_all_for_sui``; if it's
larger, ``wal_exchange::exchange_for_sui`` is called with the amount, and
the Move function performs the split internally — no client-side
``split_coin`` in that path. The resulting SUI coin is transferred to the
sender.

Blob Lifecycle Management (pysui PTB)
--------------------------------------

extend_blob_expiration
~~~~~~~~~~~~~~~~~~~~~~~~

Extend a blob's storage expiration via ``system::extend_blob``. Only the
expiry epoch changes — content, size, and object ID are unaffected.

.. code-block:: console

   tusky extend_blob_expiration -o OBJECT_ID --epochs N [--merge]
                                 [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``-o`` / ``--object-id``
   Sui object ID of the blob (not the Walrus blob ID).

``--epochs``
   Number of epochs to extend by, counted from the blob's *current*
   ``end_epoch`` (a duration, not an absolute epoch number).

``--merge``
   Merge all owned WAL coins into one before extending, in case no single
   coin covers the extension cost.

The target blob must not already be expired — expired blobs cannot be
extended, regardless of deletable/permanent status; ``extend_blob`` does
not gate on that flag at all. The WAL payment coin is passed by mutable
reference: ``system::extend_blob`` deducts what it needs and leaves the
remainder in the same coin object, so the exact cost never needs to be
computed up front. On success, ``tusky`` reports the actual WAL spent
(read back from the transaction's balance changes).

delete_blob
~~~~~~~~~~~

Delete one blob, or all active deletable blobs, via
``system::delete_blob``.

.. code-block:: console

   tusky delete_blob (-o OBJECT_ID | --all) [--burn]
                      [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``-o`` / ``--object-id`` / ``--all``
   Mutually exclusive, one required. ``-o`` targets a single blob by Sui
   object ID; ``--all`` targets every active, deletable blob owned
   by the sender.

``--burn``
   Fallback to burning instead of deleting. In ``-o`` mode: burns the
   target blob if it isn't eligible for ``delete_blob`` (already expired,
   or not deletable). In ``--all`` mode: additionally burns expired
   blobs of any type in a separate batched pass.

A blob is only eligible for ``delete_blob`` itself when it is
**deletable and not yet expired**. Attempting to delete an ineligible
blob without ``--burn`` fails cleanly:

.. code-block:: console

   $ tusky delete_blob -o 0x1ba8360de6ab4222211670f09fe12427b059d4272364d4f75d9eebb8328ead24
   0x1ba8360de6ab4222211670f09fe12427b059d4272364d4f75d9eebb8328ead24 is not eligible for delete_blob (deletable=False, end_epoch=486, current_epoch=485); pass --burn to burn it instead.

Burning a blob that is *not yet expired* (i.e. it's being burned only
because it isn't deletable) prints a warning first — this destroys an
active, paid-for blob irreversibly, with no storage refund. Deleting an
eligible blob, by contrast, is a normal reclaim: it removes the blob's
slivers from current and future storage nodes and returns a storage
rebate, though other copies of identical content stored separately (e.g.
by a different owner, or the same content re-stored elsewhere) may still
persist, since deletion only affects this specific blob registration.

In ``--all`` mode, operations are batched (up to the PTB per-batch
limit); each batch's result is printed as it completes, so a failure
partway through still leaves a record of every batch that succeeded
beforehand.

burn_blob
~~~~~~~~~

Burn one or more blob objects directly via ``blob::burn``, bypassing
``delete_blob``'s deletable/expiry eligibility check entirely.

.. code-block:: console

   tusky burn_blob -o OBJECT_ID [-o OBJECT_ID ...]
                    [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``-o`` / ``--object-id``
   Sui object ID of a blob to burn (repeatable — pass multiple ``-o``
   flags to burn several blobs in one invocation). Duplicate IDs are
   de-duplicated before batching.

Burning is **irreversible and returns no storage refund** — ``blob::burn``
simply destroys the object. Each target's on-chain state is checked
before burning: a blob ID that can't be fetched, isn't a Walrus ``Blob``
object, or has unreadable on-chain data is skipped with a warning rather
than burned blind. A blob that is not yet expired prints a warning before
burning, since that destroys an active, paid-for blob. Operations are
batched, same as ``delete_blob --all``.

.. note::

   ``delete_blob`` is the right tool for reclaiming a deletable,
   unexpired blob's storage (it returns a rebate). ``burn_blob`` is for
   cleaning up objects that ``delete_blob`` can't touch — permanent
   blobs, or blobs you're willing to destroy outright without the
   eligibility check. Both are irreversible for what they consume; only
   ``delete_blob`` returns anything back.

set_blob_metadata
~~~~~~~~~~~~~~~~~~

Insert or update one or more metadata (Walrus "attribute") key/value
pairs on a blob via ``insert_or_update_metadata_pair`` -- one
``move_call`` per pair, in one PTB.

.. code-block:: console

   tusky set_blob_metadata -o OBJECT_ID --attr KEY VALUE [--attr KEY VALUE ...]
                            [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``-o`` / ``--object-id``
   Sui object ID of the blob (not the Walrus blob ID).

``--attr``
   Metadata key and value to set (repeatable, e.g. ``--attr k1 v1 --attr
   k2 v2``); at least one is required.

Upsert semantics: a key not yet present is inserted; an existing key's
value is overwritten. Because every pair either inserts or overwrites,
there is no guaranteed-abort condition to pre-check, unlike
``drop_blob_metadata`` below.

drop_blob_metadata
~~~~~~~~~~~~~~~~~~~

Drop metadata (Walrus "attribute") from a blob: ``--keys`` removes one or
more named keys via ``remove_metadata_pair`` (one ``move_call`` per key,
in one PTB); ``--all`` drops the whole metadata set via a single
``take_metadata`` call.

.. code-block:: console

   tusky drop_blob_metadata -o OBJECT_ID (--keys KEY [KEY ...] | --all)
                             [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``-o`` / ``--object-id``
   Sui object ID of the blob (not the Walrus blob ID).

``--keys`` / ``--all``
   Mutually exclusive, one required. ``--keys`` takes one or more
   metadata keys to remove; ``--all`` removes all metadata from the blob.

Both modes check existence against the blob's current metadata *before*
composing any transaction: ``--keys`` requires every named key to
currently exist, and ``--all`` requires some metadata to exist at all. A
failed check reports a clean error instead of paying gas for a
guaranteed on-chain abort (``EMissingMetadata`` / ``vec_map::remove``).

See :doc:`transactions`'s Blob Metadata section for the underlying PTB
composition (including the pre-transaction existence gate) and
``get_blob_metadata``'s pure-read counterpart.

Native Upload (pysui PTB + Storage Nodes)
--------------------------------------------

These two commands implement pytusk's native Walrus write path: talking
directly to the storage-node committee and Sui, with no dependency on a
third-party HTTP publisher. This is a fuller-featured, higher-effort
alternative to ``store_blob`` above.

store_blob_native
~~~~~~~~~~~~~~~~~~

Store a blob via the full native upload pipeline: ``reserve_space`` +
``register_blob`` (Tx1), sliver fan-out to the storage-node committee,
confirmation-signature collection, and ``certify_blob`` (Tx2).

.. code-block:: console

   tusky store_blob_native (--content TEXT | --file PATH) --epochs N
                            [--permanent] [--recipient ADDRESS]
                            [--full-json] [--log-file PATH] [--verbose]
                            [--sender ADDRESS] [--sponsor ADDRESS]
                            [--mode simulate|execute]

``--content`` / ``--file``
   Mutually exclusive, one required. Same semantics as ``store_blob``.

``--epochs``
   Number of epochs to store the blob for, counted from now (a duration,
   not an absolute epoch number).

``--permanent``
   Store as a permanent blob (cannot be deleted before expiry).

``--recipient``
   Sui address to receive the stored blob object (default: sender).
   Unlike ``store_blob``, transfer to a non-sender recipient happens in
   Tx2 (``certify_blob``), not Tx1.

``--mode``
   ``simulate`` (default) or ``execute``. **Simulate mode only dry-runs
   Tx1** (``reserve_space`` + ``register_blob``) — sliver fan-out and Tx2
   are skipped entirely, because simulation never registers the blob
   on-chain, so storage nodes would reject sliver uploads and there would
   be nothing yet to certify. Simulate mode prints a concise JSON cost
   summary (blob size, encoded storage amount, SUI/WAL cost, stage
   timings) rather than the full raw simulate result, since the latter
   can run to thousands of lines; pass ``--full-json`` to also print the
   complete raw result.

``--log-file`` / ``--verbose``
   Opt-in progress logging for the pipeline. Neither is enabled by
   default — see :doc:`logging`.

Example — execute mode, full pipeline:

.. code-block:: console

   $ tusky store_blob_native --file testdata_1mb.bin --epochs 5 --mode execute
   {
     "blob_id": "bY4GUMK5eCHB3KBjsDN2wSzdlIY4IzQAHjzd7UHHkvw",
     "object_id": "0x4280c2303826e9bec80eac1e43edc596563bf69b1b516d181b1a69c1b96aaef7",
     "certified": true,
     "end_epoch": 498,
     "failed_stage": null,
     "timings": {
       "encode": 0.33,
       "register_tx1": 3.22,
       "sliver_upload": 5.07,
       "confirmations": 1.06,
       "certify_tx2": 0.72,
       "total": 11.91
     }
   }

If any stage after Tx1 fails, the same receipt shape is printed with
``certified: false`` and ``failed_stage`` naming the stage that broke —
the blob is registered on-chain but not yet certified. Use
``certify_blob`` below to resume from that point.

certify_blob
~~~~~~~~~~~~

Recover the confirmation-collection and ``certify_blob`` (Tx2) stages for
a blob that is already registered on-chain (Tx1 succeeded) but not yet
certified — the recovery counterpart to a partial ``store_blob_native``
failure, or to a ``store_blob_relay`` write that returned any outcome
other than ``CERTIFIED``.

.. code-block:: console

   tusky certify_blob -o OBJECT_ID [--recover (--content TEXT | --file PATH)]
                       [--sender ADDRESS] [--sponsor ADDRESS]
                       [--mode simulate|execute]

``-o`` / ``--object-id``
   Sui object ID of the already-registered ``Blob``.

``--recover``
   Also re-upload slivers before collecting confirmations, for a blob
   whose sliver fan-out never ran (for example, an upload that failed
   during the register stage). Requires ``--content`` or ``--file``. By
   default (no ``--recover``), the command assumes slivers were already
   uploaded and only collects confirmations and submits Tx2 — if the
   original fan-out never reached quorum, confirmation collection here
   will also fail.

``--content`` / ``--file``
   The original blob content, required with ``--recover``. Re-encoded and
   checked: the resulting ``blob_id`` must match what's already
   registered on-chain for ``-o``, or the command errors out before
   uploading anything mismatched.

``--mode``
   ``simulate`` (default) or ``execute``. Unlike ``store_blob_native``,
   simulate mode here is a fully honest simulation — confirmation
   collection is real (the blob is already registered), only Tx2 itself
   is simulated.

Example — resuming a blob whose sliver fan-out never ran:

.. code-block:: console

   $ tusky certify_blob -o 0x4280c2303826e9bec80eac1e43edc596563bf69b1b516d181b1a69c1b96aaef7 --recover --file testdata_1mb.bin --mode execute
   {
     "blob_id": "bY4GUMK5eCHB3KBjsDN2wSzdlIY4IzQAHjzd7UHHkvw",
     "object_id": "0x4280c2303826e9bec80eac1e43edc596563bf69b1b516d181b1a69c1b96aaef7",
     "certified": true,
     "end_epoch": 498,
     "failed_stage": null,
     "timings": {
       "encode": 0.31,
       "register_tx1": null,
       "sliver_upload": 4.88,
       "confirmations": 1.02,
       "certify_tx2": 0.70,
       "total": null
     }
   }

Upload Relay (pysui PTB + Relay HTTP)
--------------------------------------------

These two commands implement pytusk's upload-relay write path. A relay
performs the sliver fan-out that ``store_blob_native`` does on the client,
in exchange for a tip paid on-chain in the same transaction that registers
the blob. The blob is still encoded locally on either path -- that is how
the blob id the registration needs is derived. This is the practical path for Mainnet
writes, where no public publisher exists. See :doc:`configuration` for
managing the relays a network knows about, and :doc:`transactions` for the
underlying PTB composition.

relay_configs
~~~~~~~~~~~~~

List the relays configured for the active network and what each would
charge to upload a blob of a given size. Informational only — nothing is
composed, signed, or spent.

.. code-block:: console

   tusky relay_configs (--size BYTES | --file PATH | --content TEXT)

``--size`` / ``--file`` / ``--content``
   Mutually exclusive, one required — three ways of stating how large the
   hypothetical upload is. ``--file`` is measured with ``os.path.getsize``
   and never read, since pricing a hypothetical upload needs no bytes.
   ``--content`` is measured as its UTF-8 encoded byte length rather than
   its character count, so a non-ASCII string is not under-priced.

Every configured relay is queried concurrently, and the network's active
relay is marked with ``*``. A relay that fails to answer is reported
alongside the ones that did, rather than aborting the command.

store_blob_relay
~~~~~~~~~~~~~~~~

Store a blob through an upload relay: tip + ``reserve_space`` +
``register_blob`` (Tx1), a POST of the raw blob bytes to the relay, then
``certify_blob`` (Tx2) using the confirmation certificate the relay
returns.

.. code-block:: console

   tusky store_blob_relay (--content TEXT | --file PATH) --epochs N
                           [--permanent] [--relay NAME]
                           [--tip-gas-source from_gas|COIN_ID] [--max-tip MIST]
                           [--timeout SECONDS]
                           [--recipient ADDRESS] [--full-json]
                           [--log-file PATH] [--verbose]
                           [--sender ADDRESS] [--sponsor ADDRESS]
                           [--mode simulate|execute]

``--relay``
   Name of the relay to use. Defaults to the network's active relay; if
   neither an explicit name nor an active relay resolves, the command
   errors naming the relays available on that network.

``--tip-gas-source``
   ``from_gas`` (default) splits the tip from whichever party funds the
   transaction — the sponsor when one is given, otherwise the sender. An
   explicit coin object ID is verified against sender/sponsor ownership
   before anything is composed.

``--max-tip``
   Refuse the write when the relay's quote exceeds this many MIST. Checked
   BEFORE any PTB is built, so nothing is signed or spent when it trips.
   Leaving it unset applies no ceiling — that is a deliberate no-op, not a
   failure — but the quoted tip is printed before Tx1 is composed either
   way, so an unexpected figure is visible rather than silent.

``--timeout``
   Per-attempt timeout for the POST to the relay, in seconds. Every retry
   re-sends the blob from the beginning, so a large blob over a slow link
   can spend the whole retry budget on timeouts without ever finishing;
   raise this when that is a risk. Leaving it unset uses the client's
   configured timeout, which is a real bound, not "no timeout".

``--mode``
   ``simulate`` (default) or ``execute``. Simulate covers **Tx1 only** —
   the tip and the registration — and that is the most it can cover. The
   POST to the relay is a real network write against a tip that has to have
   been genuinely paid, and Tx2 cannot be composed at all until the relay
   has returned a certificate that does not yet exist. So a clean simulate
   is not a promise that the upload or the certification will succeed; it
   prices the transaction you are about to sign. Unlike
   ``store_blob_native``, though, simulate here does include the tip cost,
   because the tip is composed into the very transaction being simulated.

``--log-file`` / ``--verbose``
   Opt-in progress logging for the pipeline. ``--log-file`` writes an
   INFO-level log of this run's relay upload progress to the given path;
   ``--verbose`` emits INFO-level relay upload progress to stdout.
   Neither is enabled by default — see :doc:`logging`.

In ``execute`` mode the relay's quoted tip and the Sui address it will be
paid to are printed before Tx1 is composed, so the figure is visible before
anything is signed. The full receipt is then printed as JSON *before* the
outcome is inspected, and the command then exits non-zero for any outcome other than
``CERTIFIED``. A ``RESUMABLE`` receipt's transaction digest and nonce are
therefore always on stdout rather than being swallowed by the non-zero exit
— see ``certify_blob`` above for resuming from that state.

.. warning::

   A relay enforces a maximum request-body size, in practice about 1 GiB,
   that it does not advertise anywhere. No tip-configuration field reports
   it, and ``--mode simulate`` will happily price a blob the relay will
   later refuse. Keep relay writes under roughly 1 GiB, and use
   ``store_blob_native`` for anything larger.

store_quilt_relay
~~~~~~~~~~~~~~~~~

Store several files as one quilt through an upload relay: tip +
``reserve_space`` + ``register_blob`` (Tx1), a POST of the assembled quilt
bytes to the relay, then ``certify_blob`` (Tx2). The whole batch costs one
registration and one certification rather than one each. Unlike
``store_quilt``, which needs an HTTP publisher, this is the path that works
on Mainnet.

.. code-block:: console

   tusky store_quilt_relay [--paths PATH [PATH ...]]
                           [--patch-file KEY=PATH ...] [--patch-content KEY=TEXT ...]
                           --epochs N [--permanent] [--relay NAME]
                           [--tip-gas-source from_gas|COIN_ID] [--max-tip MIST]
                           [--timeout SECONDS]
                           [--recipient ADDRESS] [--full-json]
                           [--log-file PATH] [--verbose]
                           [--sender ADDRESS] [--sponsor ADDRESS]
                           [--mode simulate|execute]

``--paths`` / ``--patch-file`` / ``--patch-content``
   The same three ways of naming quilt members as ``store_quilt`` above:
   combinable, and duplicate patch keys across all three are rejected.

``--relay``, ``--tip-gas-source``, ``--max-tip``, ``--mode``, ``--log-file``, ``--verbose``
   Same semantics as ``store_blob_relay`` above. The relay upload progress
   that ``--log-file``/``--verbose`` surface comes from the shared upload
   stage, so this command reports exactly what the blob command does.

``--timeout``
   Per-attempt timeout for the POST to the relay, in seconds. A quilt is
   larger than any single file in it and every retry re-sends the whole
   assembled buffer from the start, so this generally wants raising above
   what a single blob would need.

Tx1 also writes the ``_walrusBlobType = "quilt"`` attribute onto the
``Blob`` object, which is what records on chain that the stored bytes are a
quilt rather than a plain blob.

The patch keys reported back are sorted by identifier, not listed in the
order they were given: packing order determines each patch's id, so it is a
function of the batch's content rather than of argument order.

.. warning::

   The same unadvertised relay body limit applies -- in practice about
   1 GiB -- but it applies to the ASSEMBLED quilt, not to any individual
   patch. A batch of individually small files can cross it. Use
   ``store_blob_native`` for anything larger.


Storage Management (pysui PTB)
-------------------------------

These commands manage standalone (unwrapped) ``Storage`` objects:
splitting, fusing, destroying, and using them to extend a blob's
expiration instead of paying WAL. See :doc:`transactions`'s Storage
Management section for the underlying PTB composition and the
Move-level constraints each operation enforces.

list_storage
~~~~~~~~~~~~

List standalone (unwrapped) Storage objects owned by the active address.
Storage still embedded in a Blob is a wrapped object with no independent
owner record and does not appear here -- see ``blob``/``blobs`` for that.

.. code-block:: console

   tusky list_storage [--status any|active|expired] [--details]

``--status``
   Filter by expiry status relative to the current Walrus epoch (default:
   ``any``). Expired storage is still splittable, fusable, and
   reclaimable, so this is a display filter, not a capability gate.

``--details``
   Replace the plain listing with an operation-oriented view: a heading
   per storage-related command, each followed by the objects it
   currently applies to. This is the fastest way to see which objects are
   eligible for which operation before running one -- the constraint each
   heading checks is noted below.

   ``split_storage`` is split into two sub-groups, since the two split
   modes have independent requirements:

   - ``split_by_epoch`` -- only objects whose epoch range spans at least
     2 epochs are listed. Move needs an interior epoch to split at
     (``start_epoch < split_epoch < end_epoch``); an object covering only
     one epoch (e.g. ``[491, 492)``) has no valid split point and will
     never appear here.
   - ``split_by_size`` -- only objects with ``storage_size >= 2`` bytes
     are listed (in practice, nearly every real object qualifies).

   ``fuse_storage`` lists two kinds of compatibility, since fuse always
   consumes exactly two objects and Move's own rule is NOT transitive:

   - ``fuse_amount`` -- every group of 2+ objects sharing an IDENTICAL
     epoch range; sizes are just summed, so a whole group can be merged
     in one transaction.
   - ``fuse_periods`` -- individual PAIRS with equal ``storage_size`` and
     ADJACENT epoch ranges (one begins exactly where the other ends).
     Listed as pairs only, never as an "all fusable together" group,
     since fusing one pair can change whether a third object becomes
     compatible with the survivor.

   ``reclaim_storage`` lists every filtered object -- ``destroy`` has no
   preconditions at all.

   ``extend_blob_with_storage`` makes one extra network call to fetch
   owned Blobs, since eligibility depends on them. An object is listed
   only if it could extend AT LEAST ONE currently owned, certified,
   unexpired blob -- which requires the object's ``end_epoch`` to fall
   strictly after that blob's current ``end_epoch``, AND its
   ``storage_size`` to EXACTLY MATCH that blob's existing storage size
   (the same rule ``fuse_periods`` enforces). A standalone Storage object
   bought without checking this will almost always show up as ``(none)``
   here -- it needs to be sized to match a specific blob's existing
   storage exactly, not just any capacity.

Example — an owned object listed under both ``split_by_size`` and
``reclaim_storage`` but neither ``split_by_epoch`` nor
``extend_blob_with_storage`` (single-epoch range, no compatible blob
owned):

.. code-block:: console

   $ tusky list_storage --details
   split_storage
     split_by_epoch (epoch range must span >= 2 epochs):
       (none)
     split_by_size (storage size must be >= 2 bytes):
       0xc2d69a4a39fab2921e173508091b03f6154e2620238dd399fdfceda4519d5cd8
   fuse_storage
     (none)
   reclaim_storage
     0xc2d69a4a39fab2921e173508091b03f6154e2620238dd399fdfceda4519d5cd8
   extend_blob_with_storage
     (none)

split_storage
~~~~~~~~~~~~~

Split a standalone Storage object by epoch or by size. Both variants
mutate the original object in place and transfer a NEW Storage (the
split-off portion) to ``--recipient`` (default: sender) -- Storage has no
``drop`` ability, so the new object cannot be left dangling. No epoch
gate applies: an already-expired object splits just as readily as a live
one.

.. code-block:: console

   tusky split_storage -s STORAGE_ID (--by-epoch N | --by-size N)
                        [--recipient ADDRESS] [--sender ADDRESS]
                        [--sponsor ADDRESS] [--mode simulate|execute]

``-s`` / ``--storage-id``
   Sui object ID of the Storage object to split (0x-prefixed).

``--by-epoch``
   Absolute epoch to split at: the original keeps
   ``[start_epoch, split_epoch)`` and the new Storage takes
   ``[split_epoch, end_epoch)``. Requires an INTERIOR epoch
   (``start_epoch < split_epoch < end_epoch``) -- only possible when the
   object's range spans at least 2 epochs. Check ``list_storage
   --details``'s ``split_by_epoch`` group before choosing a value.

``--by-size``
   Byte capacity to peel off into the new Storage; the original keeps the
   remainder over the same epoch range.

``--recipient``
   Sui address to receive the new split-off Storage object (default:
   sender).

fuse_storage
~~~~~~~~~~~~

Fuse Storage objects together, in one of three mutually exclusive modes.
Run ``list_storage --details`` first to preview which objects are
compatible before choosing.

.. code-block:: console

   tusky fuse_storage (--fuse-to ID --fuse-from ID [ID ...] |
                        --fuse-amount [--start-epoch N --end-epoch N] |
                        --fuse-periods [--fuse-to ID])
                       [--sender ADDRESS] [--sponsor ADDRESS]
                       [--mode simulate|execute]

``--fuse-to``
   Sui storage object id of the Storage that survives and absorbs the
   other(s) (0x-prefixed). Required for explicit mode; optional for
   ``--fuse-periods``, where it names which cluster's hub to consolidate
   around when more than one is owned.

``--fuse-from``
   One or more Sui storage object ids to fold into ``--fuse-to``, in
   order (0x-prefixed); each is consumed by its fuse. Explicit mode
   only.

``--fuse-amount``
   Bulk-fuse every owned Storage object sharing one identical epoch-range
   group -- no object IDs needed. Requires an IDENTICAL epoch range on
   every member of the group (sizes are just summed); this relation is
   transitive, so the whole group merges in a single transaction. If more
   than one such group is owned, ``--start-epoch``/``--end-epoch`` name
   which one.

``--fuse-periods``
   Bulk-fuse every owned Storage object reachable, via equal-size
   adjacent-range steps, from one hub -- no object IDs needed for the
   spokes. Requires EQUAL ``storage_size`` and epoch-range ADJACENCY
   between each step; this relation is NOT transitive -- fusing one pair
   can change whether another becomes compatible, so folding is greedy
   and can leave objects unfused (reported, not silently dropped). If
   more than one disjoint cluster is owned, ``--fuse-to`` names the hub.

``--start-epoch`` / ``--end-epoch``
   With ``--fuse-amount``: the epoch-range group's bounds, required only
   when more than one group is owned.

reclaim_storage
~~~~~~~~~~~~~~~

Destroy one or more standalone Storage objects, reclaiming their Sui
storage rebate. This does NOT refund the WAL originally paid to reserve
the capacity -- that is spent regardless. Move performs no checks at
all: an unexpired reservation is destroyed just as readily as a spent
one. **Irreversible.**

.. code-block:: console

   tusky reclaim_storage (-s STORAGE_ID [STORAGE_ID ...] | --all)
                          [--sender ADDRESS] [--sponsor ADDRESS]
                          [--mode simulate|execute]

``-s`` / ``--storage-id``
   One or more Sui object IDs of Storage objects to destroy.

``--all``
   Destroy every currently owned unwrapped Storage object -- no object
   IDs needed.

In ``execute`` mode, each destroyed object's Sui storage rebate (MIST
redeemed) is printed individually, with a running total after all
batches complete. ``simulate`` mode does not show this -- the simulated
response does not carry per-object rebate data.

Example — execute mode, two objects in one batch:

.. code-block:: console

   $ tusky reclaim_storage -s 0xc66d24f61597cd7679e60a74b74d610d0d1519bd79698a747756851e7d4066ad 0xe5b11f312838a84af2f2879e962cf11e836c732582cec36bbc9eec12bfd41133 --mode execute
   Destroyed batch 1/1 (2 storage object(s)).
   { ... }
     0xc66d24f61597cd7679e60a74b74d610d0d1519bd79698a747756851e7d4066ad: 1489600 MIST redeemed
     0xe5b11f312838a84af2f2879e962cf11e836c732582cec36bbc9eec12bfd41133: 1489600 MIST redeemed
   Total storage rebate redeemed: 2979200 MIST.

extend_blob_with_storage
~~~~~~~~~~~~~~~~~~~~~~~~

Extend a blob's expiration by consuming an owned Storage object instead
of paying WAL. Four requirements are pre-flighted before building the
transaction, so a violation reports the offending values instead of an
opaque Move abort:

- the blob must be CERTIFIED (``ENotCertified``);
- the blob must not already be expired (``EResourceBounds``);
- the Storage's ``end_epoch`` must be strictly LATER than the blob's
  current ``end_epoch`` (``EResourceBounds``);
- the Storage's ``storage_size`` must EXACTLY MATCH the blob's existing
  storage size, and their epoch ranges must be ADJACENT -- the same
  rules ``fuse_periods`` enforces (``EIncompatibleAmount`` /
  ``EIncompatibleEpochs``).

The Storage object is consumed by the call and ceases to exist. See
``list_storage --details``'s ``extend_blob_with_storage`` group to find
which owned objects, if any, currently satisfy this against which blobs.

.. code-block:: console

   tusky extend_blob_with_storage -o BLOB_ID -s STORAGE_ID
                                   [--sender ADDRESS] [--sponsor ADDRESS]
                                   [--mode simulate|execute]

``-o`` / ``--object-id``
   Sui object ID of the blob (0x-prefixed) -- not the Walrus blob ID
   (content hash).

``-s`` / ``--storage-id``
   Sui object ID of the Storage object to consume (0x-prefixed).
