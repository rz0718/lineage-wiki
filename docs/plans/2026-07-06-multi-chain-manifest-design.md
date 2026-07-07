# Multi-Chain Manifest Design

## Goal

Allow one OKF wiki repo to be generated and updated from multiple chain configs,
while preserving existing single-chain manifest history automatically.

## Recommended Approach

Keep one `.lineage-wiki/manifest.yml` per target wiki repo, but change its
shape to a versioned multi-chain manifest:

```yaml
version: 2
chains:
  example-revenue:
    chain_slug: example-revenue
    output_dir: okf
    generated_files: [...]
    managed_indexes: [...]
    source_fingerprints: ...
    last_run_at: ...
    last_content_snapshot: ...
    okf_git_head: ...
  example-spread:
    ...
```

Each chain entry owns only that chain's generated pages and source
fingerprints. Shared OKF indexes remain ordinary generated files under `okf/`;
they are regenerated from the whole OKF tree when a run touches content.

## Migration

Existing `version: 1` manifests load as a `version: 2` manifest in memory with a
single entry keyed by the old `chain_id`. The old generated files, managed
indexes, file snapshots, source fingerprints, timestamps, content snapshot, and
git baseline are preserved.

The next content-changing `generate` or `update` writes the migrated v2 shape.
No manual migration command is required.

## Generate Behavior

`generate --config chains/<chain>.yml --root <wiki>` should:

- load the multi-chain manifest, if present;
- use only the current chain's entry as the ownership baseline;
- protect human pages that are not owned by this chain;
- add or replace the current chain entry after staging and validation;
- preserve all other chain entries unchanged.

Generating a second chain in the same target repo appends a second chain entry
instead of rejecting the repo.

## Update Behavior

`update --config chains/<chain>.yml --root <wiki>` should:

- require a manifest entry for the current chain;
- diff only that chain's source fingerprints;
- rewrite only that chain's affected tool-owned pages;
- refresh only that chain's manifest entry;
- leave other chain entries unchanged.

If the repo has a manifest but no entry for the requested chain, update fails
with a clear message telling the user to run `generate` for that chain first.

## Compatibility And Safety

The existing overwrite protections remain intact. A chain cannot rewrite a page
unless that page is owned by that chain's manifest entry. Hand-written pages and
indexes remain protected.

Validation still runs against the full OKF tree after staged writes. Run
metadata remains under `.lineage-wiki/runs/`.

## Test Plan

Add and update tests for:

- v1 manifest load and save migration;
- v2 manifest round trip with multiple chain entries;
- generating a second chain in the same target repo without losing the first;
- updating a chain in a multi-chain manifest;
- update failure when the current chain has no manifest entry;
- existing single-chain no-op behavior.
