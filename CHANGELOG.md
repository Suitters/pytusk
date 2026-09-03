# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - Unpublished

### Added

- [Feature](https://github.com/Suitters/pytusk/issues/10) - Added mysten relays to testnet and mainnet support
- Added blob relay write support
- [Feature](https://github.com/Suitters/pytusk/issues/11) - Added local quilt assembly, relay support and reading
- tusky CLI added relay blob and quilt commands
- tusky CLI added blob metadata CRUD commands
- tusky CLI added makeing blob shared object and extension commands.

### Fixed

### Changed

- Codebase refactored for scalable growth
- `error_reason` moved to `pytusk.commands.walrus_command`; no longer importable from `pytusk.commands.node_commands`
- `submit_certification` now requires an `error_type` argument
- Post-registration failures return a receipt instead of raising, so a paid registration is never surfaced as an exception
- Relay upload attempts validated before any spend
- Relay certify failures raise `RelayCertifyTransactionError` rather than the native-named `CertifyTransactionError`
- Fixed undefined `RelayUploadOutcome` in relay upload
- Relay pipeline, relay commands, receipt protocols and chain/ops helpers now exported from `pytusk`
- Updated readthedocs coverage
- Tusky CLI arguments aligned for consistency and clarity

### Removed

## [0.4.0] - 2026-08-26

### Added

- Storage management implemented
- New commands in tusky: list_storage, split_storage, fuse_storage, reclaim_storage and extend_blob_with_storage

### Fixed

### Changed

- `tusky blobs`: added 'size=' to report the blobs storage size
- Supporting documentation (readthedocs) updated
- Refactored native_upload to it's own package

### Removed

## [0.3.0] - 2026-08-19

### Added

- Added native store blobs enabled

### Fixed

- Documentation generation fix

### Changed

- pyproject.toml licensing fix.
- `tusky committee` — Show the active Walrus storage committee.
- `tusky store_blob_native` — Store a blob via the native Walrus upload pipeline (reserve_space+register_blob, sliver fan-out, certify_blob).
- `tusky certify_blob` — Recover the confirmation-collection and certify_blob stages for a registered blob (optionally re-upload slivers first with --recover).

### Removed

## [0.2.0] - 2026-08-10

### Added

- Split `WalrusNetworkConfig.walrus_url` into `walrus_aggregator_url` and `walrus_publisher_url`, with formal `set_walrus_aggregator_url()`/`set_walrus_publisher_url()` setters on `PytuskConfiguration` for updating either field on any network, including reserved `testnet`/`mainnet`.
- `tusky` CLI for fundemental walrus blob information and changes (CRUD)
- Added documentation

### Fixed

### Changed

### Removed
