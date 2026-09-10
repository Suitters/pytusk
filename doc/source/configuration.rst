PytuskConfiguration Details
""""""""""""""""""""""""""""""

PytuskConfiguration wraps pysui's ``PysuiConfiguration`` and adds the
Walrus-specific configuration needed to store and retrieve blobs — per-network
aggregator/publisher endpoints and on-chain object IDs (System, Staking,
exchange objects). Every pytusk client is built from a PytuskConfiguration.

This page contains general class information that will aid developers
if they undertake configuration changes in their code.

General
========================================
PytuskConfiguration persists to its own configuration location
(defaults to ``~/.pysui/PytuskConfig.json``) and composes an underlying
``PysuiConfiguration`` for all Sui-level operations. Amongst other things:

#. On first run it auto-creates the config file with built-in ``testnet``
   and ``mainnet`` network entries pre-populated
#. It auto-detects the ``walrus`` binary path (``~/.cargo/bin/walrus`` or
   ``PATH``) on creation
#. It supports adding, removing, and updating additional networks beyond
   the two built-ins
#. And more...

Anatomy of PytuskConfiguration
========================================
The primary data model for PytuskConfiguration is a pair of related
``dataclass`` objects:

* The root data model is ``PytuskConfigModel``. It contains:

    * ``version`` — config schema version
    * ``pysui_config_path`` — path to the pysui config folder
    * ``walrus_binary_path`` — auto-detected path to the ``walrus`` binary,
      or ``None`` if not found
    * ``active_network`` — the currently active network name
    * One or more ``WalrusNetworkConfig``, network for short, each
      encapsulating a unique network's configuration:

        * ``network_name`` — network identifier (e.g. ``testnet``,
          ``mainnet``)
        * ``pysui_group_name`` / ``pysui_profile_name`` — the pysui
          ``ProfileGroup``/``Profile`` this network maps to
        * ``walrus_aggregator_url`` — Walrus aggregator URL for read
          operations
        * ``walrus_publisher_url`` — Walrus publisher URL for write
          operations. May be empty — Mysten runs no public unauthenticated
          publisher on mainnet by design
        * ``system_object`` / ``staking_object`` — Walrus on-chain object
          IDs
        * ``exchange_objects`` — SUI->WAL exchange object IDs (testnet only)
        * ``relays`` — named upload relay endpoints available on this
          network. Empty on user-defined networks, which ship no built-in
          relay — see Relays below
        * ``active_relay`` — name of the relay used when a caller does not
          name one explicitly, or ``None`` when the network has no default
        * ``wal_coin_type`` — canonical ``<package>::wal::WAL`` coin type
          for this network, used for exact WAL-coin identification rather
          than a substring match. Empty on networks whose WAL package
          address is not stable enough to pin (testnet, for example), where
          callers fall back to a substring match
        * ``network_type`` — ``NetworkType.TEST`` or
          ``NetworkType.PRODUCTION``

``testnet`` and ``mainnet`` are reserved network names: they always exist
and cannot be added or removed, though their individual fields (aggregator
URL, publisher URL, etc.) can still be updated — see Bottom Up Changes
below.

Session Properties
========================================
In addition to the ``PytuskConfigModel`` fields above, ``PytuskConfiguration``
exposes properties for the pysui session overrides passed to the
constructor, and for accessing derived state:

* ``pysui_group_name`` / ``pysui_profile_name`` — the pysui
  ``ProfileGroup``/``Profile`` name overrides passed at construction, if
  any.
* ``pysui_address`` / ``pysui_alias`` — the active Sui address/alias
  override passed at construction, if any.
* ``pysui_configuration`` — the underlying, lazily-constructed
  ``PysuiConfiguration`` instance.
* ``network`` (alias: ``active_network_entry``) — the ``WalrusNetworkConfig``
  for the currently active network.

PytuskConfiguration
========================================

PytuskConfiguration is the primary object to interact with when managing or
using the underlying networks. It composes a ``PysuiConfiguration`` for all
Sui-level operations — see the pysui documentation for that class' own
configuration model.

First time instantiation
------------------------------------
There is no separate initialization step — simply construct
``PytuskConfiguration``. If ``PytuskConfig.json`` does not yet exist at
the target path, it is created automatically with the built-in ``testnet``
and ``mainnet`` entries.

.. code-block:: python
    :linenos:

    from pytusk import PytuskConfiguration

    # Defaults to ~/.pysui/PytuskConfig.json; auto-created on first run
    cfg = PytuskConfiguration()
    print(cfg.active_network)   # "testnet"
    print(cfg.networks)         # [testnet entry, mainnet entry]

Walrus binary detection
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
During construction, ``~/.cargo/bin/walrus`` and ``PATH`` are checked for a
``walrus`` binary. If found, its path is captured in
``cfg.walrus_binary_path``; if not, the property is ``None``. The binary is
not required to use pytusk's HTTP-based read/write operations — see
``set_walrus_binary_path`` under Other Setters below to set or override it
explicitly.

Changing PytuskConfig Active
========================================
Defaults of what is considered 'active' is whatever was last persisted
but can be changed at runtime.

At PytuskConfig Construction
------------------------------------

* ``from_cfg_path`` (str) - Controls where PytuskConfiguration reads/writes
  ``PytuskConfig.json``. Defaults to ``~/.pysui``.
* ``active_network`` (str) - Sets the ``active_network`` for the session.
  Must match a ``network_name`` already present in the loaded configuration:

.. code-block:: python
    :linenos:

    from pytusk import PytuskConfiguration

    # Activate mainnet for this session
    cfg = PytuskConfiguration(active_network="mainnet")

* ``pysui_config_path`` (str) - Overrides the pysui config folder path
  passed through to the underlying ``PysuiConfiguration``.
* ``pysui_group_name`` / ``pysui_profile_name`` (str) - Override the pysui
  ``ProfileGroup``/``Profile`` used for this session, in place of the
  active network's defaults:

.. code-block:: python
    :linenos:

    from pytusk import PytuskConfiguration

    cfg = PytuskConfiguration(
        active_network="testnet",
        pysui_group_name="sui_gql_config",
        pysui_profile_name="testnet",
    )

* ``pysui_address`` / ``pysui_alias`` (str) - Sets the active Sui address
  for this session, by explicit address or alias.
* ``persist`` (bool) - Controls whether to persist ``active_network`` (or
  a newly created default file) to ``PytuskConfig.json``. If not set to
  ``True`` the change is in memory only.

After Construction
------------------------------------
Changing the active network after construction is done through
``PytuskConfiguration.set_active_network(...)``:

.. code-block:: python
    :linenos:

    from pytusk import PytuskConfiguration

    cfg = PytuskConfiguration()
    cfg.set_active_network("mainnet", persist=True)

**NOTE** Changing the active network invalidates any cached
``PysuiConfiguration`` built from the prior network's pysui group/profile —
a fresh one is built on next access. If you've already built a
``WalrusClient`` from this configuration, you'll need a new client instance
to pick up the new network.

Bottom Up Changes
========================================

Network
------------------------------------
A ``WalrusNetworkConfig`` (network, for short) encapsulates a unique
network's Walrus and pysui configuration. ``testnet`` and ``mainnet`` are
reserved and always present; any other name is a user-defined custom
network.

**WARNING** All methods below support an optional ``persist`` flag argument.
Keep in mind that this will persist *any* changes that may have occurred
previously where the ``persist`` flag was set to ``False``. If you want
changes to be ephemeral only, set this to ``False``.

The following methods are available on the PytuskConfiguration instance.

Creating a new Network
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Add a custom network. Will raise an exception if ``network_name`` is
``testnet`` or ``mainnet`` (reserved); replaces an existing custom network
of the same name.

.. code-block:: python

    def add_network(
        self,
        network: WalrusNetworkConfig,
        *,
        persist: bool = False,
    ) -> None:
        """Add or replace a network configuration entry."""

.. code-block:: python
    :linenos:

    from pytusk import NetworkType, PytuskConfiguration, WalrusNetworkConfig

    cfg = PytuskConfiguration()
    custom = WalrusNetworkConfig(
        network_name="my-devnet",
        pysui_group_name="sui_grpc_config",
        pysui_profile_name="devnet",
        walrus_aggregator_url="https://aggregator.example.com",
        walrus_publisher_url="https://publisher.example.com",
        system_object="0xabc",
        staking_object="0xdef",
        exchange_objects=["0x123"],
        wal_coin_type="0xabc::wal::WAL",
        network_type=NetworkType.TEST,
    )
    cfg.add_network(custom, persist=True)

Every field carries a default, so only ``network_name`` is strictly
required — but a network missing its pysui group/profile or its
``system_object``/``staking_object`` cannot be used for chain operations.
``relays`` and ``active_relay`` are deliberately absent above: a
user-defined network ships with no built-in relay. Add one afterwards with
``add_relay`` — see Relays below.

Removing a Network
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Remove a custom network. Will raise an exception if ``network_name`` is
``testnet`` or ``mainnet`` (reserved), or if it is not found.

.. code-block:: python

    def remove_network(
        self,
        network_name: str,
        *,
        persist: bool = False,
    ) -> None:
        """Remove a user-defined network configuration entry."""

Update Existing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unlike ``add_network``/``remove_network``, these two methods update a
single field on an *existing* network entry and may target the reserved
``testnet``/``mainnet`` networks. This is the intended way to configure a
self-hosted publisher for mainnet, which ships with an empty
``walrus_publisher_url`` default — Mysten runs no public unauthenticated
publisher on mainnet. Will raise an exception if ``network_name`` is not
found.

.. code-block:: python

    def set_walrus_aggregator_url(
        self,
        *,
        network_name: str,
        url: str,
        persist: bool = False,
    ) -> None:
        """Set the Walrus aggregator URL for a network."""

    def set_walrus_publisher_url(
        self,
        *,
        network_name: str,
        url: str,
        persist: bool = False,
    ) -> None:
        """Set the Walrus publisher URL for a network."""

    def set_exchange_objects(
        self,
        *,
        network_name: str,
        exchange_objects: list[str],
        persist: bool = False,
    ) -> None:
        """Set the WAL/SUI exchange object IDs for a network. Mainnet
        ships with an empty default — use this to register mainnet
        exchange object IDs once available."""

.. code-block:: python
    :linenos:

    from pytusk import PytuskConfiguration

    cfg = PytuskConfiguration(active_network="mainnet")

    # Point mainnet writes at a self-hosted or authenticated publisher
    cfg.set_walrus_publisher_url(
        network_name="mainnet",
        url="https://my-publisher.example.com",
        persist=True,
    )

Other Setters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Two additional setters exist outside the per-network model:

.. code-block:: python

    def set_pysui_config_path(self, path: str, *, persist: bool = False) -> None:
        """Set the pysui config folder path."""

    def set_walrus_binary_path(self, path: str, *, persist: bool = False) -> None:
        """Set the walrus binary path."""

Persisting Changes
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Two methods write the current in-memory configuration to disk directly,
independent of any setter's ``persist`` keyword:

.. code-block:: python

    def save(self) -> None:
        """Persist the current configuration to disk."""

    def save_to(self, path: str) -> None:
        """Persist the current configuration to an alternate directory."""

Most setters above accept a ``persist`` keyword that calls ``save()``
internally. Call ``save()`` or ``save_to()`` directly to persist changes
made without it, or to write the configuration to a different location.

Relays
------------------------------------
An upload relay is a Walrus service that performs the erasure-encoding and
sliver fan-out for a write on the client's behalf, in exchange for a tip.
Relays are configured per network: a ``RelayConfig`` carries only
``relay_name`` and ``relay_url``, and belongs to a network structurally, by
living in that network's ``relays`` list.

``testnet`` and ``mainnet`` are seeded with Mysten's public relay —
``https://upload-relay.testnet.walrus.space`` and
``https://upload-relay.mainnet.walrus.space`` respectively. Seeding never
modifies a relay you have already defined and never overwrites an active
relay you have already chosen. User-defined networks ship with no relay and
an empty list. A configuration file written before 0.5.0 is migrated on load
(schema ``1.0.0`` to ``1.1.0``) to add the relay fields, leaving everything
else untouched.

**WARNING** As with the network methods above, each method here accepts an
optional ``persist`` flag. Setting it writes out *any* earlier changes made
with ``persist=False`` as well.

Listing relays
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``relays_for`` returns a new list, so adding to or removing from the result
does not alter the configuration. ``active_relay_for`` returns ``None`` when
a network has no active relay — a legitimate state rather than an error,
since removing a relay clears the pointer that named it.

.. code-block:: python

    from pytusk import PytuskConfiguration, RelayConfig

    cfg = PytuskConfiguration()

    relays: list[RelayConfig] = cfg.relays_for(network_name="testnet")
    for relay in relays:
        print(relay.relay_name, relay.relay_url)

    print(cfg.active_relay_for(network_name="testnet"))

Adding a relay
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Register a new named relay on a network. Raises ``ValueError`` when
``relay_name`` is already used on that network. Pass ``make_active=True`` to
make it that network's active relay in the same call.

.. code-block:: python

    cfg.add_relay(
        network_name="mainnet",
        relay_name="my-relay",
        relay_url="https://relay.example.com",
        make_active=True,
        persist=True,
    )

Updating a relay
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Change an existing relay's URL, optionally making it active at the same
time. Raises when no relay of that name exists on the network.

.. code-block:: python

    cfg.update_relay(
        network_name="mainnet",
        relay_name="my-relay",
        relay_url="https://relay-2.example.com",
        persist=True,
    )

Removing a relay
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Remove a named relay. When it is the network's active relay, ``active_relay``
is cleared in the same call, so no dangling pointer is left behind.

.. code-block:: python

    cfg.remove_relay(
        network_name="mainnet",
        relay_name="my-relay",
        persist=True,
    )

Resolving a relay URL
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Resolve the URL a relay write will POST to. Resolution order is the
``relay_name`` argument, then the network's active relay; when neither
resolves it raises, naming the relays available on that network. This is the
same resolution :py:func:`~pytusk.store_blob_relay` performs internally, so
calling it is a way to confirm which relay a write would use.

.. code-block:: python

    url = cfg.relay_url_for(network_name="testnet")
    named = cfg.relay_url_for(network_name="testnet", relay_name="mysten")


FAQ
========================================

Why is mainnet's publisher URL empty by default?
------------------------------------------------------------
Mysten Labs deliberately runs no public unauthenticated publisher on
mainnet (aggregator only). Writing blobs on mainnet requires a
self-hosted or authenticated publisher — set it explicitly with
``set_walrus_publisher_url`` (see Update Existing above) before attempting
any ``WriteBlob``/``WriteQuilt`` command against mainnet; otherwise
``WalrusClient`` raises ``ValueError``.

Can I add or remove testnet/mainnet?
------------------------------------------------------------
No — both are reserved network names and always present. ``add_network``
and ``remove_network`` raise ``ValueError`` if given either name. Use
``set_walrus_aggregator_url``/``set_walrus_publisher_url`` to change their
URLs instead.

Two simultaneous configurations for different networks
------------------------------------------------------------
As with pysui, you generally don't want to switch ``active_network`` back
and forth on a single instance if you're also holding a live
``WalrusClient`` for it. Instead, create two ``PytuskConfiguration``
instances:

.. code-block:: python
    :linenos:

    from pytusk import PytuskConfiguration
    from pytusk.client.walrus_client import WalrusClient

    testnet_cfg = PytuskConfiguration(active_network="testnet")
    testnet_client = WalrusClient(pytusk_config=testnet_cfg)

    mainnet_cfg = PytuskConfiguration(active_network="mainnet")
    mainnet_client = WalrusClient(pytusk_config=mainnet_cfg)
