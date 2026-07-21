# Historical archive manifest — design only

This infrastructure is read-only and opt-in. No production manifest or shard exists yet, active paths are unchanged, and the current Timeline rotation remains untouched.

## Manifest schema v1

The future `data/history/archive_manifest.json` contains `manifest_version`, `generated_at` and a `datasets` object. Each dataset defines `logical_dataset`, `active_path`, `ordering`, optional `deduplication_key`, `timestamp_fields`, `declared_gaps` and ordered `shards`.

Each closed shard records `shard_id`, project-relative `path`, `format` (`jsonl` or `jsonl.gz`), `start_time`, `end_time`, `record_count`, `uncompressed_size`, `compressed_size`, physical-file `sha256`, `schema_versions`, `created_at`, `closed`, `source_commit` and `sequence`.

Physical SHA-256 verifies storage integrity. The logical fingerprint is `sha256-canonical-jsonl-v1`: every logical record is parsed and serialized as sorted compact JSON before hashing. It is stable when the same ordered content moves between the active file, plain shards or gzip shards. Record order remains semantically significant.

## Compatibility and safety

- A missing manifest plus an explicit active path reads the current single JSONL unchanged.
- Closed shards are read by sequence before the active file.
- Missing optional active files represent a fully archived dataset; missing declared shards are errors.
- Paths must stay inside the project directory; network URLs and path traversal are rejected.
- Validation covers manifest version, ISO timestamps, sequence/id/path uniqueness, time overlap, monotonic records, exact or keyed logical duplicates, declarative gaps, files, checksums, byte sizes, counts and schema versions.
- Current consumers are not redirected yet. They continue reading their existing single paths.

Inspect read-only with:

```bash
.venv/bin/python trading/check_archive_manifest.py --manifest /path/to/archive_manifest.json --json
.venv/bin/python trading/check_archive_manifest.py --manifest /path/to/archive_manifest.json --strict
```

With no production manifest, the CLI reports compatibility mode and performs no write.

## Future Timeline migration (not active)

1. Add multi-shard reads to Timeline consumers behind an explicit feature flag.
2. Establish a writer/rotator lock and close a shard atomically.
3. Verify count, interval, schemas, sizes and checksum before manifest publication.
4. Publish the manifest through atomic replace; retain the active path for new events.
5. Compare old single-file and new logical fingerprints on a copied fixture.
6. Only after rollback and concurrent-write tests, replace destructive 5/4 MiB retention.

Capital ledger migration requires a separate accounting review and must never use destructive truncation.

## Timeline shadow rehearsal

`trading/run_timeline_shadow_migration.py` captures a verified complete-line prefix of the live Timeline, then builds monolithic, plain-shard and gzip-shard layouts only under `/tmp`. It validates both manifests, compares logical fingerprints and proves rollback by reading the monolith while ignoring the manifest.

The capture permits concurrent append but detects inode changes, shrinkage or rewritten prefixes. A real append can be observed with `--observe-seconds`; tests also exercise an active writer deterministically. This is evidence for a future migration, not activation authority.

```bash
.venv/bin/python trading/run_timeline_shadow_migration.py --observe-seconds 45 --strict
```

Production readers, writers, paths and the existing 5/4 MiB Timeline rotation remain unchanged.
