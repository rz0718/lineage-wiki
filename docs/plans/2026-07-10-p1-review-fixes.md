# P1 Review Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all three P1 review findings without overwriting the in-progress LLM grounding-status work.

**Architecture:** Use the manifest's existing per-file snapshot as the ownership gate for both enrichment-denied sections and tool-authored Verification Status. Represent migration decisions as force/preserve sets, and restore a conservative provider output-token default while retaining continuation support.

**Tech Stack:** Python 3.11+, Pydantic, pytest, Typer, YAML-backed manifests.

---

### Task 1: Restore Provider Compatibility

**Files:**
- Modify: `lineage_wiki/providers.py:28-300`
- Test: `tests/test_llm.py:88-193`

**Step 1: Write failing provider-limit tests**

Extend the existing mocked OpenAI and Anthropic completion tests to capture the
first request payload and assert:

```python
assert requests[0]["max_tokens"] == 4096
```

Add an explicit override assertion using `max_tokens=8192` so the fix does not
remove caller control. Use `inspect.signature` or direct calls to cover the
base and mock provider defaults.

**Step 2: Run the provider tests and verify failure**

Run:

```bash
pytest tests/test_llm.py -k 'provider and (token or truncated or continues)' -v
```

Expected: the default-token assertions fail with `32000`.

**Step 3: Implement the provider default**

Define one module-level constant and use it in every provider signature:

```python
DEFAULT_MAX_TOKENS = 4096
```

Keep the request payload and continuation loop unchanged, so explicit larger
values and multi-round responses still work.

**Step 4: Run the provider tests and verify success**

Run the focused command from Step 2 and expect all selected tests to pass.

### Task 2: Gate Verification Status Refresh on Manifest Ownership

**Files:**
- Modify: `lineage_wiki/okf/sections.py:103-308`
- Modify: `lineage_wiki/agent/runner.py:355-430`
- Test: `tests/test_dry_run.py:220-275`
- Test: `tests/test_llm.py:1050-1172`
- Test: `tests/test_llm_invalidation.py:290-315`

**Step 1: Write failing status-preservation tests**

Add direct merge tests proving that refresh is disabled by default for:

```python
scaffold + "\n\nHuman note."
grounding + "\n\nHuman note."
```

Add end-to-end tests that edit the generated status while retaining the tool
marker, rerun deterministic and LLM generation, and assert the complete edited
body survives. Cover invalidation as well so the post-loop grounding-status
revert cannot overwrite a drifted status.

Update existing tests that model an unchanged manifest-owned page to pass an
explicit status-refresh flag.

**Step 2: Run focused status tests and verify failure**

Run:

```bash
pytest tests/test_dry_run.py tests/test_llm.py tests/test_llm_invalidation.py -k 'status or verification' -v
```

Expected: mixed-marker status is replaced before the fix.

**Step 3: Add the merge ownership gate**

Extend `merge_manual_sections` with:

```python
allow_status_refresh: bool = False
```

Require this flag both when `_status_refreshable` selects the draft and when
the post-loop invalidation logic reverts grounding status to scaffold.

In `_write_page`, compute whether `existing` matches `migration_snapshot` and
pass that boolean into the merge. Use the same calculation in plan-only update
assessment so predictions match real writes.

**Step 4: Run the focused status tests and verify success**

Run the command from Step 2 and expect all selected tests to pass.

### Task 3: Preserve Drifted Enrichment-Denied Sections

**Files:**
- Modify: `lineage_wiki/agent/runner.py:355-430`
- Test: `tests/test_llm.py:1175-1255`
- Test: `tests/test_dry_run.py`

**Step 1: Write failing migration-policy tests**

Add coverage for all four ownership cases:

1. cited plus snapshot match: force deterministic draft;
2. cited plus drift: preserve and warn;
3. uncited plus snapshot match: normal deterministic refresh;
4. uncited plus drift: preserve and warn.

Include a plan-only assertion for case 4 so its predicted action is unchanged
and its warning names the page and section.

**Step 2: Run focused migration tests and verify failure**

Run:

```bash
pytest tests/test_llm.py tests/test_dry_run.py -k 'migration or denylisted' -v
```

Expected: uncited drift is overwritten without a warning.

**Step 3: Implement a force/preserve migration policy**

Replace `_migration_force_sections` with a small immutable policy:

```python
@dataclass(frozen=True)
class _MigrationSectionPolicy:
    force: tuple[str, ...] = ()
    preserve: tuple[str, ...] = ()
```

On snapshot match, put citation-bearing denied headings in `force`. On drift,
put every existing denied heading in `preserve` and emit a generic warning
that does not assume the body was written by an LLM.

Pass `PRESERVED_SECTIONS + policy.preserve` and
`force_sections + policy.force` into `merge_manual_sections` in real and
plan-only paths.

**Step 4: Run focused migration tests and verify success**

Run the command from Step 2 and expect all selected tests to pass.

### Task 4: Full Verification and Completion Audit

**Files:**
- Verify all modified source and test files.

**Step 1: Run formatting/static checks available in the repository**

Run:

```bash
git diff --check
python -m compileall -q lineage_wiki tests
```

Expected: no new whitespace errors and compilation succeeds. Pre-existing
whitespace outside the P1 edits must be reported separately rather than
silently rewritten.

**Step 2: Run the full test suite**

Run:

```bash
pytest -q
```

Expected: all tests pass, with only the repository's intentional skips.

**Step 3: Audit each P1 requirement**

Inspect the final diff and test evidence to confirm:

- drifted cited and uncited deny-listed bodies are preserved with warnings;
- unchanged deterministic bodies still refresh;
- mixed human status survives deterministic, LLM, and invalidation paths;
- untouched tool-authored status still reconciles;
- provider payloads default to 4096 and explicit overrides still work;
- pre-existing Goal 2 changes remain present and are not reverted.

Implementation commits are intentionally omitted because source and test files
already contain user-owned uncommitted Goal 2 work; staging those files would
mix unrelated work into a P1 commit.
