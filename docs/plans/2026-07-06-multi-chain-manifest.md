# Multi-Chain Manifest Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow multiple chain configs to share one target OKF wiki repo with automatic migration from existing single-chain manifests.

**Architecture:** Keep `.lineage-wiki/manifest.yml` as the single manifest file, but introduce a v2 wrapper with per-chain entries. Existing v1 data becomes one `ChainManifest` entry on load, while generate/update code works against the current chain entry and saves the full wrapper.

**Tech Stack:** Python 3.11+, Pydantic v2, PyYAML, Typer, pytest.

---

### Task 1: Manifest Data Model

**Files:**
- Modify: `lineage_wiki/storage/manifest.py`
- Test: `tests/test_manifest.py`

**Step 1: Write failing tests**

Add tests that:

- load a legacy v1 manifest and assert `manifest.version == 2`;
- assert the migrated entry is available at `manifest.chains["gold_pnl"]`;
- round-trip a v2 manifest with two entries;
- compare equality while ignoring only per-chain `last_run_at` and `okf_git_head`.

**Step 2: Run focused tests**

Run:

```bash
uv run pytest tests/test_manifest.py -q
```

Expected: failures because v2 types/helpers do not exist yet.

**Step 3: Implement manifest wrapper**

In `lineage_wiki/storage/manifest.py`:

- rename the current `Manifest` field set into a new `ChainManifest`;
- define `Manifest` as `version: int = 2` plus `chains: dict[str, ChainManifest]`;
- make `load_manifest` auto-migrate old v1 YAML into the new wrapper;
- make `save_manifest` always write v2 shape;
- update `manifests_equal_ignoring_run_time` to compare nested chain entries while excluding each entry's `last_run_at` and `okf_git_head`.

**Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_manifest.py -q
```

Expected: pass.

### Task 2: Generate Uses Current Chain Entry

**Files:**
- Modify: `lineage_wiki/agent/runner.py`
- Test: `tests/test_cli.py`

**Step 1: Write failing tests**

Add a test that:

- generates `chains/example.yml` into a temp target;
- creates a second config by deep-copying the example and changing `chain.id`, `chain.slug`, `chain.name`, and one metric name;
- generates the second config into the same target;
- asserts both framework pages exist;
- asserts the manifest has both chain entries.

**Step 2: Run focused test**

Run:

```bash
uv run pytest tests/test_cli.py::test_generate_second_chain_preserves_first_manifest_entry -q
```

Expected: fail because generate still treats the whole manifest as one chain.

**Step 3: Update generate plumbing**

In `lineage_wiki/agent/runner.py`:

- when loading the manifest, read `previous_all = load_manifest(root)`;
- derive `previous = previous_all.chains.get(cfg.chain.id)` for ownership and fingerprint comparison;
- pass both the full manifest and the current chain entry into finalization;
- rebuild only the current chain entry;
- save a full v2 manifest preserving other entries.

**Step 4: Run focused test**

Run the same focused test and expect pass.

### Task 3: Update Uses Current Chain Entry

**Files:**
- Modify: `lineage_wiki/agent/runner.py`
- Test: `tests/test_update.py`

**Step 1: Write failing tests**

Add tests that:

- a multi-chain manifest lets `update` run for the current chain instead of rejecting another entry;
- `update` fails clearly when the requested chain has no entry.

**Step 2: Run focused tests**

Run:

```bash
uv run pytest tests/test_update.py -q
```

Expected: old rejection behavior fails the new multi-chain case.

**Step 3: Update update plumbing**

In `run_update`:

- require only that `load_manifest(root)` returns a wrapper;
- look up `previous = previous_all.chains.get(cfg.chain.id)`;
- if missing, raise "no manifest entry for chain '<id>' ... run generate first";
- remove the old "multi-chain manifests land later" rejection;
- pass the full wrapper through finalization so other chains are preserved.

**Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_update.py -q
```

Expected: pass.

### Task 4: Preserve Compatibility Across Existing Tests

**Files:**
- Modify as needed: `tests/test_cli.py`, `tests/test_update.py`, `tests/test_plan_only.py`, `tests/test_dry_run.py`, `tests/test_llm.py`

**Step 1: Run all tests**

Run:

```bash
uv run pytest
```

Expected: failures where tests assume top-level v1 manifest fields.

**Step 2: Update test assertions only where the manifest shape changed**

Change direct assertions like `manifest.generated_files` to
`manifest.chains[chain_id].generated_files`.

**Step 3: Re-run all tests**

Run:

```bash
uv run pytest
```

Expected: pass.

### Task 5: Docs

**Files:**
- Modify: `README.md`

**Step 1: Update usage docs**

Mention that one target wiki repo can hold multiple chains, and that the
manifest is keyed by chain id.

**Step 2: Run relevant checks**

Run:

```bash
uv run pytest tests/test_manifest.py tests/test_cli.py tests/test_update.py -q
```

Expected: pass.
