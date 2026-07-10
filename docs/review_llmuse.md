# Review: LLM-mode output quality (gold-pnl chain)

Date: 2026-07-10
Scope: audit of the pages generated into `../wiki-repo/okf/` by `lineage-wiki generate --use-llm`
(chain `chains/gold_pnl.yml`), cross-checked against the pipeline implementation
(`agent/llm_pipeline.py`, `agent/grounding.py`, `okf/templates.py`, prompt files) and the run
transcripts in `../wiki-repo/.lineage-wiki/runs/` (latest: `20260708T000000Z-generate-llm-3.json`).

## Summary

The grounding architecture works: 50 claims were accepted with verbatim-quote verification,
a genuine doc-vs-code divergence was recorded (doc names `gold_pnl_daily_snapshot`, deployed
schema is `..._v2`), and an LLM-invented `Total Daily PnL` formula was correctly rejected into
a Known Gap. Nothing hallucinated reached a page.

The weaknesses are on the quality/consistency side of what gets published, and they matter
because LLM mode is the normal path — the deterministic scaffold is effectively fallback text:

1. LLM-written sections are walls of text with heavy verbatim duplication across pages.
2. Deterministic sections (Verification Status, footers, Known Gaps wording) contradict the
  LLM-enriched content after a run.
3. The change-check page structurally rejects **all** of its LLM sections every run, wasting
  writer/reviewer calls and leaving the worst scaffold content (raw docstrings, TOC anchors)
   in place.

## What works well

- Grounding checks (`grounding.py`): quote-in-evidence verification, formula source-type
gating, column claims gated on ingested BQ schemas, section citation traceability.
- Section whitelisting: the planner cannot invent pages; `_FORBIDDEN_SECTIONS` protects
verify-bq / divergence / Known Gaps sections.
- Rejected formulas become auditable Known Gap bullets instead of disappearing.
- The divergence flow produced a correct, dual-cited doc-vs-code finding.

## Findings

### F1 — Wall-of-text sections (readability)

`okf/frameworks/gold-pnl.md` "Core Assumptions" and "Core Formula" are each a single ~250-word
paragraph of concatenated claim sentences, `[src:]` after every sentence; "Components" packs a
13-term glossary into one paragraph. Eight formulas in one paragraph is unusable as a reference.
The writer prompt asks for "readable prose" but gives no structural guidance.

### F2 — Verbatim duplication across pages

The same claim sentence is pasted on 3–4 pages (e.g. "The `gold_pnl_daily_snapshot_v2` BigQuery
table includes a `wac` FLOAT column…" appears on the framework, code-link, output, and report
pages). The Day Zero WAC (2,753,000 IDR/gram) is stated twice in the same framework section via
two near-duplicate accepted claims. The writer prompt says "detailed definition in one page,
lightweight links elsewhere," but each page job runs independently against the full claim list,
so the model cannot honor it.

### F3 — Deterministic sections contradict LLM-enriched content

After a successful LLM run, the framework's Verification Status still reads "extraction
pending", "cross-check pending", "Report mappings | Not ingested (scaffold)", and the footer
says "No fact on this page has been cross-checked" — directly above a recorded doc-vs-code
divergence and pages full of extracted facts. `templates.py` (~lines 497–565) renders this from
ingestion state only; `run_llm_enrichment` never updates it. Same stale footer on the report
page; framework Known Gaps still says "cross-checking lands in a later milestone".

### F4 — Change-check page: guaranteed 100% rejection

All 7 sections of `change-checks/gold-pnl-review-rules.md` were rejected in the latest run:

- Procedural sections ("How to Trigger a Review", "Required Agent Behavior") can never carry
`[src:]` markers, so `check_section_body` rejects them unconditionally.
- The other four failed "citation not traceable to an accepted claim" (writer cited raw
evidence ids not backed by claims).

Net effect: ~7 wasted writer + reviewer LLM calls per run, and the page keeps deterministic
scaffold content that pastes raw docstrings and table-of-contents anchors (lines 41–44, 69–71,
111–117) — exactly the formatting the writer prompt forbids.

### F5 — Claims lost to extractor prompt-stub / claim-kind mismatch

The extractor invented kind `source_table`; 3 real input-table claims were rejected at the
unknown-kind check (`grounding.py:90`). Root cause (corrected after fourth-pass review): the
*assembled* prompt already enumerates the allowed kinds in its JSON schema
(`agent/prompts.py:118` — "formula | definition | column | code_path | mapping | fact"); the
contradiction is the editable stub `.lineage-wiki/prompts/extractor.md`, which asks the model
to extract "source tables, output tables" while no such claim kind exists — inviting exactly
the invented kind that gets rejected.

### F6 — LLM enrichment deletes the deterministic schema table from output pages

`outputs/gold-pnl-daily-snapshot-v2.md` shows only the 3–4 columns that happened to have
claims. Root cause (corrected after second-pass review): the deterministic template *already*
renders a full `Column | Type | Mode | Description` table from the loaded BQ schema
(`okf/templates.py:776`), but the LLM job includes "Column Definitions" in its sections and
`replace_section` (`agent/llm_pipeline.py:382`) replaces the whole body, discarding the table.
The run-llm-2 reviewer flagged the resulting gap ("other loaded BigQuery schema columns remain
undocumented, but no Known Gap records this"); nothing changed.

### F7 — Point-in-time Slack figures inside the methodology page

The framework "Reports" section embeds the 2026-07-07 daily PnL numbers. Dated evidence belongs
on the report page; because Slack ingestion pulls the newest matching message, the framework
page churns on every run — making every run content-changing (which also defeats the
transcripts-only-on-content-change setup).

### F8 — Dangling references and leftovers

- Output-page "Grain" and report-page "Purpose" both say "see Known Gaps" — no matching bullet
exists.
- Framework `## Source` is a stale scaffold remnant (its LLM rewrite was rejected for lacking
citations) containing one orphaned WAC definition.
- `gold_spread_revenue_spread_cost` is a documented output table with no output page, no config
entry, and no Known Gap — shown as "—" in the change-check trigger table.
- Change-check "Output Change Triggers" maps the non-v2 table name to the v2 output page
silently, despite the recorded divergence.
- Cosmetics: "Gold Pnl Daily Snapshot V2" title casing; duplicated `okf` tag in `okf/index.md`
frontmatter.
- Config leftover: `bigquery_verification` in `chains/gold_pnl.yml` still targets the example
`example-project.analytics.example_daily_snapshot`, so `verify-bq` would check a nonexistent
table.

## Plan (prioritized — revised after second-pass review)

Guiding principle from the review: **keep grounding strict**. Every fix below prefers a
deterministic control (exclusion, typed validation, protected rendering, per-page routing)
over relaxing a verifier check or relying on prompt wording alone.

1. **Fix the change-check enrichment dead-end — by exclusion, never exemption** (F4).
  Do NOT add a citation-exempt path through `check_section_body` — that would open an
   ungrounded LLM write path into agent-facing review instructions ("Required Agent
   Behavior"). Instead, exclude procedural sections from enrichment entirely: add a
   per-page-type deny-list alongside `_FORBIDDEN_SECTIONS` so the planner never offers them,
   and keep rendering them deterministically from config/templates. The evidence-bearing
   trigger sections stay enrichable under the full citation rules; improve their deterministic
   rendering (no raw docstrings/TOC anchors) since it remains the published content when the
   LLM output is rejected.
   **Migration for newly deterministic sections:** the improved deterministic bodies will not
   land on existing repos by themselves — `merge_manual_sections` preserves an existing
   citation-bearing section over a draft block without citations when its evidence is not
   stale (`okf/sections.py:211`), and deny-listed sections never enter `sections_written` /
   `force_by_rel` (`agent/runner.py:795`). The current wiki-repo pages are exactly in this
   state (cited bodies in `outputs/gold-pnl-daily-snapshot-v2.md` "Column Definitions" and
   the change-check trigger sections). Add an explicit force path for sections the template
   newly owns, scoped to avoid clobbering human work. `[src:]` alone is NOT sufficient proof
   a section is machine-written — the marker only identifies evidence-written *style*
   (`okf/sections.py:46`); a human can edit a generated section and keep its citations. Gate
   the auto-replace on manifest drift instead: force only when the existing body carries
   `[src:]` AND the page still matches its last recorded tool snapshot in the manifest's
   per-file `file_snapshots` (`storage/manifest.py:52`) — i.e. nobody edited the file since
   the tool last wrote it. On any drift (or an uncited body), leave the section alone and
   surface a warning naming the page and section so the owner can migrate manually.
   Touches: `agent/llm_pipeline.py` (planner filtering), `okf/templates.py` (scaffold
   cleanup), `agent/runner.py` / `okf/sections.py` (migration force path).
   `check_section_body` in `agent/grounding.py` is intentionally left unchanged.
2. **Reflect LLM-run results in page status — without touching verify-bq's territory** (F3).
  `Verification Status` is in `PRESERVED_SECTIONS` because verify-bq results and human notes
   can own it (`okf/sections.py` merge rules, `SCAFFOLD_STATUS_MARK`). So: update only
   status bodies still carrying the scaffold mark, and record LLM-run results as clearly
   labeled *grounding* rows/lines (e.g. "LLM extraction: N claims accepted, M divergences
   recorded") — distinct wording from credentialed BigQuery verification, so accepted claims
   are never confusable with verify-bq results. The status text must stay **date-free**:
   scaffold body text deliberately omits dates because a body date defeats no-op detection
   across days (`okf/templates.py:360`), and LLM transcripts are only written on
   content-changing runs (`agent/runner.py:737`) — run dates belong in the frontmatter
   `timestamp` and run metadata only. The scaffold-marked reconciliation path is the
   primary design; a separate "LLM Grounding Status" section is only acceptable if it is
   template-owned with explicit merge/invalidation semantics — `merge_manual_sections`
   retains existing-only sections verbatim on later rewrites (`okf/sections.py:230`), so a
   non-template, non-cited section would persist stale indefinitely. Also fix the footer and
   the stale
   "lands in a later milestone" gap bullets, derived deterministically from the transcript.
   Implementation rule: never add "Verification Status" to `sections_written` /
   `force_by_rel` — forced sections bypass preservation during merge
   (`agent/runner.py:795`), which would let the update trample verify-bq or human-owned
   content. Handle it as scaffold-only reconciliation inside the merge path instead.
   Touches: `agent/llm_pipeline.py`, `okf/templates.py`, `okf/sections.py` (merge awareness).
3. **Structure + canonicalization — verifier first, prompts second** (F1, F2).
  The writer is *required* to copy claim `text` verbatim (`agent/prompts.py` writer prompt)
   and the grounding check verifies that text appears in the body; the prompt also forbids
   pipe-delimited fragments. Prompt-only "use tables" guidance would therefore be rejected by
   the verifier. Order of work:
   a. Extend claims with structured fields (e.g. formula `expression` + `meaning`,
      definition `term` + `definition`) and validate them at *both* levels: `check_claim`
      must verify the new fields against evidence (they are model output too — e.g.
      `expression`/`term` must be quote-verifiable in the cited evidence) before claims
      reach the writer, and `check_section_body` must accept a structured rendering
      (list/table row built from those validated fields) as equivalent to the verbatim
      text. Only then update the writer prompt to emit lists/tables.
   b. Canonicalization and Slack routing must be deterministic claim *filtering*, not prompt
      wording: the planner (or a deterministic post-planner step) assigns each claim a
      primary page, and each writer call receives only that page's claim subset
      (today `_run_writer_and_reviewer` receives all accepted claims).
   c. Dedupe near-identical claims at extraction acceptance time.
   Touches: `agent/grounding.py`, `agent/prompts.py`, `agent/llm_pipeline.py`, prompt files.
4. **Protect the deterministic schema table on output pages** (F6).
  The full schema table already exists in the template (`okf/templates.py:776`); the bug is
   that LLM enrichment replaces the whole "Column Definitions" section. Split it into two
   *top-level* `##` sections — section parsing, planning, and replacement all operate only
   on `##`  headings (`okf/sections.py:23`, `llm_pipeline._headings`), so a `###` subsection
   would still be replaced with its parent. Keep the existing `## Column Definitions`
   heading as the deterministic, non-enrichable schema-table section (added to the
   enrichment deny-list) and add a new LLM-enrichable `## Column Meanings` section — this
   preserves the page contract: "Column Definitions" is a required Output section
   (`constants.py:82`) and the validator errors on missing required sections
   (`okf/validator.py:110`), so renaming it would break validation, snapshots, and tests.
   Keep the Description column strictly BQ-metadata — do not
   merge LLM prose into it (provenance must stay separable); LLM-derived meaning lives in
   the Column Meanings section (or a separate "Business Meaning" table) with `[src:]`
   citations. Auto-add a Known Gap listing columns with no documented meaning.
   The same migration caveat as plan 1 applies: the existing wiki-repo "Column Definitions"
   body is citation-bearing, so the restored deterministic table needs the plan-1 force path
   to land; without it the merge keeps the old LLM prose (`okf/sections.py:211`).
   Touches: `okf/templates.py`, `agent/llm_pipeline.py` (section deny-list),
   plan-1 migration path.
5. **Confine dated Slack evidence to the report page** (F7).
  Deterministic routing (see 3b): Slack-evidence claims are assigned to the report page only;
   the framework "Reports" section keeps the undated mapping. The dated quote lands in the
   report page's Slack Evidence section (ideally a dated log). Stops daily framework churn.
   Touches: `agent/llm_pipeline.py` (claim routing), `okf/templates.py`.
6. **Smaller fixes** (F5, F8).
  - Fix the extractor kind mismatch at every layer it lives in (F5): add `source_table` to
   `CLAIM_KINDS`, to the JSON schema in `extractor_prompt` (`agent/prompts.py:118`), and to
   the default/generated `extractor.md` stub — and add a sync test asserting the stub, the
   prompt schema, and `CLAIM_KINDS` agree, so they cannot drift again. Do NOT coerce
   unknown kinds to `fact` (bypasses the typed checks); keep rejecting unknown kinds.
   Account for prompt overrides: `load_prompts` prefers an existing
   `.lineage-wiki/prompts/*.md` over `PROMPT_STUBS` (`agent/prompts.py:44`) and `init`
   only writes missing files (`agent/runner.py:108`), so stub updates never reach repos
   with existing overrides (the wiki-repo's current `extractor.md` is one). The
   safety-critical contract (allowed kinds, JSON schema) must stay in the code-assembled
   prompt suffix — the stub is guidance only — and the generate run should warn when an
   override is stale (e.g. it names claim kinds or asks for extractions not in
   `CLAIM_KINDS`), with a documented migration step (delete or re-init the stub).
  - `source_table` validation must match the strength of the existing `column` check
  (`grounding.py:135`, which verifies the named column against schema evidence): require a
  structured table-identifier field on the claim that must itself appear — after
  normalization (case, backticks, project/dataset qualification) — in the cited
  quote/evidence content. Quote-in-evidence alone is insufficient: an invented table name
  could otherwise pass on an unrelated-but-true quote. The new field must be threaded
  through the whole data path — the `Claim` dataclass and `Claim.payload()` today carry
  only `id`/`kind`/`text`/`evidence_ids`/`quote` (`agent/grounding.py:41`), and
  `_run_extractor` parses only those — so the extractor JSON schema, `_run_extractor`
  parsing, `Claim`/`payload()`, the run transcript, and the writer/reviewer prompt
  payloads all need to carry it, or validation and routing can never see it. (The same
  threading requirement applies to the structured formula/definition fields in plan 3a.)
  - Auto-gap doc-mentioned output tables that are not in the chain config.
  - Emit the promised Known Gap bullets for undocumented grain/purpose, or drop the "see
  Known Gaps" pointer when no bullet exists.
  - Improve title casing for slug-derived titles; dedupe index tags.
  - Clean the example `bigquery_verification` block in `chains/gold_pnl.yml`.

## Second-pass review notes (2026-07-10)

An independent review of the first draft of this plan raised five points; all were verified
against the code and folded into the plan above:

1. Citation-exempt sections would create an ungrounded LLM path into agent instructions —
  dropped; plan 1 now uses exclusion + deterministic rendering only.
2. Coercing unknown claim kinds to `fact` would bypass type-specific checks — dropped;
  plan 6 now adds a typed `source_table` kind instead.
3. The output-page schema table already exists deterministically; the defect is LLM
  whole-section replacement — F6 and plan 4 corrected accordingly.
4. Prompt-only structure changes conflict with the verbatim-copy verifier, and per-page claim
  routing must be deterministic — plan 3 reordered (verifier/schema support first, prompts
   second, deterministic filtering for canonicalization).
5. Verification Status is preserved for verify-bq/human ownership — plan 2 now scopes LLM
  updates to scaffold-marked bodies or a separately labeled grounding status.

## Third-pass review notes (2026-07-10)

A follow-up review of the revised plan raised four implementation-level points; all verified
against the code and folded in:

1. Structured claim fields are model output and must be validated in `check_claim` against
  evidence, not only at section-body render time — plan 3a now requires claim-level
   validation before claims reach the writer.
2. Section parsing/planning/replacement operate only on top-level `##`  headings
  (`okf/sections.py:23`), so the schema/meanings split must be two `##` sections — a `###`
   subsection would still be replaced with its parent. Plan 4 updated.
3. Do not merge LLM meanings into the BQ-metadata Description cells (provenance blurring) —
  the "merge into Description" alternative was dropped; LLM meaning lives in a separate,
   cited section/column.
4. Verification Status updates must never go through `sections_written` / `force_by_rel`
  (forced sections bypass preservation, `agent/runner.py:795`) — plan 2 now states this as
   an explicit implementation rule: scaffold-only reconciliation in the merge path.

## Fourth-pass review notes (2026-07-10)

Three further points, all verified against the code and folded in:

1. No run dates in page-body status text — body dates defeat no-op detection across days
  (`okf/templates.py:360`) and would make every LLM run content-changing, forcing transcript
   writes (`agent/runner.py:737`). Plan 2's example wording is now date-free; dates stay in
   frontmatter `timestamp` and run metadata.
2. `source_table` validation was underspecified — quote-in-evidence plus source-type gating
  would let an invented table name pass on an unrelated true quote. Plan 6 now requires a
   structured, normalized table-identifier field verified against the cited evidence content,
   matching the strength of the existing `column` check (`grounding.py:135`).
3. F5 blamed the wrong prompt layer — the assembled extractor prompt already enumerates the
  allowed kinds (`agent/prompts.py:118`); the defect is the editable `extractor.md` stub
   asking for "source tables" with no matching kind. F5 corrected; plan 6 now updates
   `CLAIM_KINDS`, the prompt JSON schema, and the stub together, with a sync test to prevent
   drift.

## Fifth-pass review notes (2026-07-10)

Three further points, all verified against the code and folded in:

1. The schema/meanings split must respect the page contract — "Column Definitions" is a
  required Output section (`constants.py:82`) and the validator errors on missing required
   sections (`okf/validator.py:110`). Plan 4 now keeps `## Column Definitions` as the
   deterministic non-enrichable schema section and adds `## Column Meanings`, instead of
   renaming.
2. Prompt-stub updates never reach existing repos — `load_prompts` prefers repo overrides
  (`agent/prompts.py:44`) and `init` only writes missing files (`agent/runner.py:108`).
   Plan 6 now keeps the safety-critical kind contract in the code-assembled prompt suffix,
   treats the stub as guidance only, and adds a stale-override warning plus a documented
   migration step.
3. A free-standing "LLM Grounding Status" section would persist stale because existing-only
  sections are retained verbatim on rewrites (`okf/sections.py:230`). Plan 2 now names the
   scaffold-marked Verification Status reconciliation as the primary design and constrains
   the alternative to template-owned with explicit merge/invalidation semantics.

## Sixth-pass review notes (2026-07-10)

Two further points, both verified against the code and folded in:

1. Newly deterministic sections would never land on existing repos — the merge preserves
  existing citation-bearing bodies over uncited draft blocks when evidence is not stale
   (`okf/sections.py:211`), and deny-listed sections are never forced via `sections_written`
   / `force_by_rel` (`agent/runner.py:795`). The current wiki-repo "Column Definitions" and
   change-check trigger sections are in exactly this state. Plans 1 and 4 now include an
   explicit migration force path for template-owned sections, scoped to replace only
   citation-bearing (machine-written) bodies and warn on uncited (human) ones.
2. The `source_table` structured identifier must survive the whole data path — `Claim` and
  `Claim.payload()` carry only `id`/`kind`/`text`/`evidence_ids`/`quote`
   (`agent/grounding.py:41`) and `_run_extractor` parses only those fields. Plan 6 now lists
   every hop that must carry the new field (extractor JSON schema, parser, dataclass/payload,
   transcript, writer/reviewer payloads), and notes the same applies to plan 3a's structured
   formula/definition fields.

## Seventh-pass review notes (2026-07-10)

One point, verified and folded in:

1. `[src:]` is not proof a section is machine-written — the marker identifies
  evidence-written *style* (`okf/sections.py:46`), and a human can edit a generated section
   while keeping its citations, so the sixth-pass migration rule could have clobbered human
   edits. Plan 1's force path is now additionally gated on manifest drift: auto-replace only
   when the page still matches its last recorded tool snapshot in the manifest's per-file
   `file_snapshots` (`storage/manifest.py:52`); on any drift or an uncited body, warn instead
   of replacing.

## Implementation Goals for Coding Agent

This section converts the review findings above into implementation goals for Claude Code / Codex.

The coding agent must treat the review as the source of truth, but it must not implement all goals in one pass. Each run should implement only the explicitly requested goal.

### Execution Rule

For each coding-agent run:

- Read this full review first.
- Implement only the explicitly requested goal.
- Do not implement later goals unless explicitly asked.
- Keep grounding strict.
- Do not relax `check_section_body`.
- Do not add citation-exempt LLM write paths.
- Prefer deterministic controls over prompt-only fixes.
- Preserve human-owned and verify-bq-owned content.
- Add or update tests for the implemented behavior.
- Run the relevant test suite.
- Summarize changed files, behavior changes, and remaining risks.

Recommended execution order:

1. Goal 1 — Deterministic section ownership and enrichment deny-list
2. Goal 2 — LLM grounding status reconciliation
3. Goal 3 — Deterministic claim routing and dedupe
4. Goal 4 — Structured claims and `source_table` kind

---

### Goal 1 — Deterministic section ownership and enrichment deny-list

#### Objective

Fix the change-check enrichment dead-end and protect deterministic schema tables from LLM replacement.

This goal addresses primarily F4 and F6.

#### Implementation

Implement deterministic section ownership and an enrichment deny-list.

Required changes:

1. Add a per-page-type or per-page/section enrichment deny-list alongside the existing `_FORBIDDEN_SECTIONS` mechanism.
2. Deny-list procedural change-check sections that should never be LLM-enriched, including at least:
  - `How to Trigger a Review`
  - `Required Agent Behavior`
3. Keep these procedural sections deterministic. Do not create any citation-exempt LLM path for them.
4. Deny-list output-page `## Column Definitions` so LLM enrichment never replaces the deterministic full schema table.
5. Keep `## Column Definitions` as the required deterministic schema-table section. Do not rename it.
6. Add a new top-level LLM-enrichable section:
  - `## Column Meanings`
7. Ensure the schema/meanings split uses two top-level `##` sections. Do not implement `Column Meanings` as a `###` subsection, because section parsing, planning, and replacement operate on top-level `##` headings.
8. Keep the `Description` column in `Column Definitions` strictly based on BigQuery metadata. Do not merge LLM-derived business meaning into this deterministic schema table.
9. Put LLM-derived column meaning in `## Column Meanings`, with normal `[src:]` grounding requirements.
10. Improve deterministic rendering for change-check trigger sections so they do not paste raw docstrings, TOC anchors, or other low-quality scaffold artifacts.
11. Add a migration force path for newly template-owned sections, but scope it carefully:
  - Auto-replace only when the existing body carries `[src:]` and the file still matches its last recorded tool snapshot in the manifest `file_snapshots`.
  - If the page has manifest drift, or if the section body is uncited, do not replace it automatically.
  - Emit a warning naming the page and section so the owner can migrate manually.

#### Constraints

- Do not relax `check_section_body`.
- Do not add citation-exempt LLM write paths.
- Do not let LLM write agent-facing procedural instructions without grounding.
- Do not rename required Output sections.
- Preserve the Output page contract enforced by constants and validator.
- Do not treat `[src:]` alone as proof that a section is machine-written; also require no manifest drift.

#### Tests

Add or update tests proving:

1. Deny-listed sections are not passed to writer/reviewer.
2. Change-check procedural sections remain deterministic.
3. `Column Definitions` keeps the full BigQuery schema table after `generate --use-llm`.
4. `Column Meanings` can be enriched by LLM.
5. LLM enrichment cannot replace the deterministic schema table.
6. The migration force path replaces only unchanged machine-written sections.
7. Human-edited or drifted sections produce warnings instead of being clobbered.

---

### Goal 2 — LLM grounding status reconciliation

#### Objective

Make page status, footers, and Known Gaps reflect LLM grounding results without overwriting verify-bq or human-owned content.

This goal addresses primarily F3.

#### Implementation

After a successful LLM enrichment run, derive page status from the run transcript.

Required changes:

1. Derive deterministic LLM grounding status from transcript data, including where available:
  - accepted claim count
  - rejected claim count
  - divergence count
  - rejected formulas that became Known Gaps
  - missing evidence or undocumented mappings that became Known Gaps
2. Update stale scaffold wording in:
  - `Verification Status`
  - page footer
  - Known Gaps milestone wording
3. Update `Verification Status` only when the existing section still carries the scaffold marker.
4. Never update `Verification Status` via `sections_written` or `force_by_rel`.
5. Keep wording clearly separate from credentialed BigQuery verification. Use language such as:
  - `LLM grounding`
  - `claim grounding`
  - `accepted grounded claims`
  - `recorded divergence`
6. Do not imply that BigQuery verification has passed unless verify-bq actually ran and succeeded.
7. Keep page-body status text date-free. Dates belong only in frontmatter `timestamp` and run metadata.
8. Avoid a free-standing `LLM Grounding Status` section unless it is template-owned with explicit merge/invalidation semantics.
9. Remove or replace stale lines such as:
  - `extraction pending`
  - `cross-check pending`
  - `No fact on this page has been cross-checked`
  - `cross-checking lands in a later milestone`
   but only where the LLM transcript supports a better status.

#### Constraints

- Preserve verify-bq-owned status.
- Preserve human-edited status.
- Do not force `Verification Status`.
- Do not put run dates in page-body text.
- Do not create existing-only status sections that can persist stale indefinitely.

#### Tests

Add or update tests proving:

1. A scaffold-marked `Verification Status` is reconciled after an LLM run.
2. A human-edited `Verification Status` is preserved.
3. A verify-bq-owned `Verification Status` is preserved.
4. Footer text no longer says no facts were checked when grounded claims exist.
5. Known Gaps wording no longer refers to future milestones after LLM grounding has run.
6. Re-running on a later date does not change page body text solely because the date changed.

---

### Goal 3 — Deterministic claim routing and dedupe

#### Objective

Improve wiki readability by reducing duplicated claim text across pages and preventing dated Slack evidence from entering stable framework pages.

This goal addresses primarily F1, F2, and F7.

#### Implementation

Add deterministic claim routing before writer/reviewer calls.

Required changes:

1. Add a deterministic routing step for accepted claims.
2. Assign each accepted claim a primary page or page type.
3. Ensure each writer call receives only the claims relevant to the page being written.
4. Do not pass the full accepted claim list to every writer call.
5. Avoid copying the same claim sentence across framework, code-link, output, and report pages.
6. Route dated Slack evidence only to report pages.
7. Keep framework and methodology pages stable and undated.
8. Keep framework `Reports` section focused on undated report mapping or methodology, not point-in-time report figures.
9. Add dedupe for identical or near-identical accepted claims at extraction acceptance time or immediately after acceptance.
10. Make routing deterministic. Do not rely only on writer prompt wording.

#### Constraints

- Do not weaken grounding.
- Do not let prompt-only guidance carry the canonicalization behavior.
- Do not remove useful evidence from report pages.
- Do not route dated Slack figures to framework pages.
- Keep repeated runs stable when inputs are unchanged.

#### Tests

Add or update tests proving:

1. The same column claim does not appear verbatim on 3–4 pages.
2. Dated Slack PnL values do not appear on framework pages.
3. Dated Slack evidence still appears on the relevant report page.
4. Running the same inputs twice produces stable output.
5. Near-duplicate accepted claims are deduped or routed to one canonical location.
6. Writer payloads contain only page-relevant claims.

---

### Goal 4 — Structured claims and `source_table` kind

#### Objective

Support table-level claims and structured rendering without weakening validation.

This goal addresses primarily F5 and the structured-claim requirements in Plan 3a and Plan 6.

#### Implementation

Add typed `source_table` claims and structured claim fields.

Required changes:

1. Add `source_table` to `CLAIM_KINDS`.
2. Update the code-assembled extractor JSON schema to include `source_table`.
3. Update the default/generated `extractor.md` prompt stub so it agrees with the code-level allowed claim kinds.
4. Add a sync test proving these agree:
  - `CLAIM_KINDS`
  - extractor JSON schema
  - default extractor prompt stub guidance
5. Keep the safety-critical allowed-kind contract in the code-assembled prompt suffix. Treat repo prompt stubs as guidance only.
6. Add a stale prompt override warning:
  - `load_prompts` may prefer repo-level `.lineage-wiki/prompts/*.md`
  - existing overrides may mention outdated claim kinds
  - `generate` should warn when override guidance conflicts with code-level allowed kinds
7. Add structured fields to `Claim` where needed.
  For `source_table`, add a structured table identifier field.
   If implementing formula/definition structure in the same goal, add fields such as:
  - formula `expression`
  - formula `meaning`
  - definition `term`
  - definition `definition`
8. Thread structured fields through the full data path:
  - extractor JSON schema
  - `_run_extractor` parsing
  - `Claim` dataclass
  - `Claim.payload()`
  - run transcript
  - writer prompt payload
  - reviewer prompt payload
9. Validate structured fields in `check_claim` before claims reach the writer.
10. For `source_table`, require the structured table identifier to appear in the cited evidence after normalization.
11. Normalization should handle at least:
  - case differences
  - backticks
  - project/dataset qualification
  - equivalent fully qualified or partially qualified table references where safe
12. Do not rely on quote-in-evidence alone for `source_table`; an unrelated true quote must not validate an invented table identifier.
13. Keep unknown claim kinds rejected. Do not coerce unknown kinds to `fact`.
14. Update `check_section_body` to accept structured renderings only after claim-level validation supports those structured fields.

#### Constraints

- Do not bypass typed checks.
- Do not coerce unknown claim kinds to `fact`.
- Do not validate table claims using only quote-in-evidence.
- Do not update only the prompt stub while leaving the code schema unchanged.
- Preserve transcript compatibility where possible, or add explicit migration handling.

#### Tests

Add or update tests proving:

1. `source_table` claims are accepted only when cited evidence supports the normalized table identifier.
2. Invented table identifiers are rejected even if the quote itself appears in evidence.
3. Unknown claim kinds remain rejected.
4. Prompt override staleness produces a warning.
5. Structured fields survive into transcript and writer/reviewer payloads.
6. Structured formula/definition fields, if implemented, are validated against evidence before writing.
7. Structured section rendering passes only for validated structured fields.

---



