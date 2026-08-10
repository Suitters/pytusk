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

The blob-lifecycle move-call commands (``exchange_for_wal``,
``exchange_for_sui``, ``extend_blob_expiration``, ``delete_blob``,
``burn_blob``) build and submit a Programmable Transaction Block (PTB), so
they additionally accept:

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

   tusky read_blob -i BLOB_ID

``-i`` / ``--blobid``
   The **Walrus blob ID** (URL-safe base64, content hash) — not the Sui
   object ID. This is the opposite convention from every other
   ``-i``/``--blobid`` use in ``tusky`` (see the note under `blob
   inspection & reporting`_ below); ``read_blob`` reads by content hash,
   everything else identifies a blob by its on-chain object.

store_quilt
~~~~~~~~~~~

Store a quilt — multiple named files batched into a single blob — via the
Walrus HTTP publisher. Batching avoids paying Walrus's fixed
~64 MB per-blob metadata overhead once per file; every patch shares one
registration.

.. code-block:: console

   tusky store_quilt [--paths PATH [PATH ...]]
                      [--file KEY=PATH ...] [--content KEY=TEXT ...]
                      --epochs N [--permanent] [--recipient ADDRESS]

``--paths``
   File paths to include in the quilt, shell-expandable (e.g. ``*.py``).
   The patch key for each is derived from its filename.

``--file``
   Patch key and file path, in ``KEY=PATH`` form (repeatable). Use this
   when you need an explicit patch key that differs from the filename.

``--content``
   Patch key and inline UTF-8 text content, in ``KEY=TEXT`` form
   (repeatable).

``--paths``, ``--file``, and ``--content`` are combinable — at least one
patch from any of the three is required. Duplicate patch keys across all
three sources are rejected as an error.

``--epochs``, ``--permanent``, ``--recipient``
   Same semantics as ``store_blob``.

Example — explicit inline content:

.. code-block:: console

   $ tusky store_quilt --content patch1="hello quilt" --epochs 1
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
its raw bytes to stdout.

.. code-block:: console

   tusky read_quilt --quilt-id QUILT_ID --patch-key KEY

``--quilt-id``
   The quilt's Walrus identifier (as returned in ``store_quilt``'s
   ``quilt_id`` field).

``--patch-key``
   The key identifying the patch within the quilt (as returned in
   ``store_quilt``'s ``patch_keys`` list).

Example, continuing from the ``store_quilt`` example above:

.. code-block:: console

   $ tusky read_quilt --quilt-id sF5cQqDEYm3FTvdKAr8PnUP_BdEg5DwpU1kPzDiQXHY --patch-key patch1
   hello quilt

Blob Inspection & Reporting
-----------------------------

.. note::

   Except for ``read_blob`` above, every ``-i``/``--blobid`` argument in
   ``tusky`` — including throughout this section and the lifecycle
   commands below — takes the blob's **Sui object ID** (0x-prefixed), not
   the Walrus blob ID (content hash).

blobs
~~~~~

List all Walrus ``Blob`` objects owned by the active address.

.. code-block:: console

   tusky blobs [--deletable any|true|false] [--status any|active|expired]

``--deletable``
   Filter by deletable status (default: ``any``).

``--status``
   Filter by expiry status relative to the current Walrus epoch (default:
   ``any``).

Each matching blob prints one line:

.. code-block:: console

   $ tusky blobs
   0x9726699cf5440c3bb6e62568becefc4b1f519b3d3c70442d20007074f5dde627  blob_id=vj24cJ8WBaO0Cs99h1_7Ozp_u5HZ9VAQnGvuYxpgWjM  deletable=True  end_epoch=496  status=active

blob
~~~~

Show full on-chain details for one blob object, as formatted JSON.

.. code-block:: console

   tusky blob -i OBJECT_ID

Example (``bcs`` payload abbreviated for readability):

.. code-block:: console

   $ tusky blob -i 0x3e163d3b14bef322b3d7eea360dc4a15c2a8f1cb488a2be9de980b3268d08b66
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

epoch
~~~~~

Print the current Walrus epoch as a single integer.

.. code-block:: console

   tusky epoch

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
   0x1ba8360de6ab4222211670f09fe12427b059d4272364d4f75d9eebb8328ead24         486            485          1  active
   0x3e163d3b14bef322b3d7eea360dc4a15c2a8f1cb488a2be9de980b3268d08b66         486            485          1  active
   0x9726699cf5440c3bb6e62568becefc4b1f519b3d3c70442d20007074f5dde627         496            485         11  active

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

   tusky extend_blob_expiration -i OBJECT_ID --epochs N [--merge]
                                 [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``-i`` / ``--blobid``
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

   tusky delete_blob (-i OBJECT_ID | --all-blobs) [--burn]
                      [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``-i`` / ``--blobid`` / ``--all-blobs``
   Mutually exclusive, one required. ``-i`` targets a single blob by Sui
   object ID; ``--all-blobs`` targets every active, deletable blob owned
   by the sender.

``--burn``
   Fallback to burning instead of deleting. In ``-i`` mode: burns the
   target blob if it isn't eligible for ``delete_blob`` (already expired,
   or not deletable). In ``--all-blobs`` mode: additionally burns expired
   blobs of any type in a separate batched pass.

A blob is only eligible for ``delete_blob`` itself when it is
**deletable and not yet expired**. Attempting to delete an ineligible
blob without ``--burn`` fails cleanly:

.. code-block:: console

   $ tusky delete_blob -i 0x1ba8360de6ab4222211670f09fe12427b059d4272364d4f75d9eebb8328ead24
   0x1ba8360de6ab4222211670f09fe12427b059d4272364d4f75d9eebb8328ead24 is not eligible for delete_blob (deletable=False, end_epoch=486, current_epoch=485); pass --burn to burn it instead.

Burning a blob that is *not yet expired* (i.e. it's being burned only
because it isn't deletable) prints a warning first — this destroys an
active, paid-for blob irreversibly, with no storage refund. Deleting an
eligible blob, by contrast, is a normal reclaim: it removes the blob's
slivers from current and future storage nodes and returns a storage
rebate, though other copies of identical content stored separately (e.g.
by a different owner, or the same content re-stored elsewhere) may still
persist, since deletion only affects this specific blob registration.

In ``--all-blobs`` mode, operations are batched (up to the PTB per-batch
limit); each batch's result is printed as it completes, so a failure
partway through still leaves a record of every batch that succeeded
beforehand.

burn_blob
~~~~~~~~~

Burn one or more blob objects directly via ``blob::burn``, bypassing
``delete_blob``'s deletable/expiry eligibility check entirely.

.. code-block:: console

   tusky burn_blob -i OBJECT_ID [-i OBJECT_ID ...]
                    [--sender ADDRESS] [--sponsor ADDRESS] [--mode simulate|execute]

``-i`` / ``--blobid``
   Sui object ID of a blob to burn (repeatable — pass multiple ``-i``
   flags to burn several blobs in one invocation). Duplicate IDs are
   de-duplicated before batching.

Burning is **irreversible and returns no storage refund** — ``blob::burn``
simply destroys the object. Each target's on-chain state is checked
before burning: a blob ID that can't be fetched, isn't a Walrus ``Blob``
object, or has unreadable on-chain data is skipped with a warning rather
than burned blind. A blob that is not yet expired prints a warning before
burning, since that destroys an active, paid-for blob. Operations are
batched, same as ``delete_blob --all-blobs``.

.. note::

   ``delete_blob`` is the right tool for reclaiming a deletable,
   unexpired blob's storage (it returns a rebate). ``burn_blob`` is for
   cleaning up objects that ``delete_blob`` can't touch — permanent
   blobs, or blobs you're willing to destroy outright without the
   eligibility check. Both are irreversible for what they consume; only
   ``delete_blob`` returns anything back.
