# P1 Review Fixes Design

## Scope

Fix the three P1 findings from commit `44f6a114a4c1b9d85ab718d4d1aaad2fdff0887d`:

1. Preserve uncited human edits in enrichment-denied sections.
2. Preserve human additions to scaffold or LLM grounding verification status.
3. Keep default provider output limits compatible with supported legacy models.

The implementation must coexist with the in-progress LLM grounding-status
reconciliation already present in the worktree.

## Ownership Model

The manifest's per-file snapshot is the authoritative proof that an existing
page still has exactly the bytes last written by `lineage-wiki`.

- When the page matches its recorded snapshot, deterministic deny-listed
  sections may refresh and tool-authored Verification Status may refresh.
- When the page differs from its recorded snapshot, deny-listed sections and
  Verification Status are preserved exactly. Deny-listed sections also emit a
  warning naming the page and section because the tool cannot prove which
  section was edited.
- Cited deny-listed sections on unchanged pages are force-migrated back to the
  deterministic template, preserving the existing Goal 1 migration behavior.
- Verify-bq and human-authored status bodies remain preserved because they are
  not refreshable tool-authored status.

Whole-file snapshot gating is intentionally conservative. A manual edit in a
different section protects the ownership-sensitive sections too, because the
current manifest has no section-level hashes and cannot prove they are clean.

## Merge Flow

The runner computes the existing page snapshot match once. A migration policy
returns two independent section sets:

- `force`: cited deny-listed sections that are safe to restore from the draft.
- `preserve`: deny-listed sections on a drifted page, regardless of citations.

The runner extends `PRESERVED_SECTIONS` with the migration policy's `preserve`
set and passes the snapshot match into `merge_manual_sections`. Verification
Status refresh is allowed only when the snapshot matches and the existing/draft
status markers permit the transition.

Plan-only update uses the same policy, ensuring its predicted actions and
warnings match a real update.

## Provider Limit

Restore the provider interface, OpenAI provider, Anthropic provider, and mock
provider defaults to `4096` output tokens. This is compatible with models such
as GPT-4 Turbo and retains the new continuation mechanism for responses that
hit the cap. Explicit callers can still pass a larger value.

## Testing

Add regression tests proving:

- an uncited deny-listed section on a drifted page is preserved with a warning;
- an unchanged uncited deterministic section still refreshes normally;
- scaffold-plus-human and grounding-plus-human status survive generate/LLM
  reruns when the file has drifted;
- untouched scaffold and grounding status still reconcile as designed;
- OpenAI and Anthropic requests use `4096` by default and honor an explicit
  override;
- focused tests and the full suite pass without altering unrelated worktree
  changes.
