---
name: fluent-migration
description: >
  Use this skill when a patch or local changes rename, restructure, move, or
  replace Fluent (.ftl) strings - or migrate legacy .properties strings to Fluent
  - and you need a migration recipe in python/l10n/fluent_migrations so existing
  translations carry over. It detects the changes, then generates, verifies, and
  validates the recipe with the in-tree `fluent.migrate` library (offline) and
  writes the file. Triggers: "write/generate a fluent migration", "migrate these
  strings", renamed/bumped l10n IDs (foo -> foo2), moving a value to/from an
  attribute, moving a string between files, .properties -> Fluent. Also reports
  changes that cannot be migrated.
---

## What this does

A **migration recipe** (a Python module in `python/l10n/fluent_migrations/`,
shipped in the same patch) tells l10n tooling to copy existing translations to a
renamed/moved/restructured string, so locales don't fall back to English. It's
only worthwhile when the English text is **reusable** - identical, or
capitalization-only different; if the wording genuinely changed, the string is
translated fresh instead.

The helper detects the changes itself (renames/bumps, restructures, cross-file
moves, seeds, wholly-different-id renames, multi-source assembly, and legacy
`.properties` -> Fluent), builds the transforms with `fluent.migrate`, verifies
them offline against the pre-change strings, validates the recipe with the
library's `Validator`, and writes the file - **only if every check passes**.
Whatever it can't generate cleanly is listed under NEEDS ATTENTION.

## Workflow

**1. Bug number** - passed via `--bug` (never read from git, so it works on
uncommitted changes); use the bug you're working on. Omitting it leaves a
`Bug <NUMBER>` placeholder to fill in.

**2. Run it** through `./mach python`:

```bash
# committed change:
./mach python .claude/skills/fluent-migration/scripts/generate_migration.py \
    --rev HEAD --bug 2043735 --description "Update the containers panel"
# local-only change (omit --rev) and write the file:
./mach python .claude/skills/fluent-migration/scripts/generate_migration.py \
    --bug 2043735 --description "Update the containers panel" \
    --output python/l10n/fluent_migrations/bug_2043735_containers_panel.py
```
`--rev` only selects the diff (omit for the working tree). `--output` must be
under `python/l10n/fluent_migrations/` named `bug_<number>_<slug>.py`.

**3. Read the output**
- *Verifying transforms* - `OK`/`FAIL` per message; `FAIL` = a reference didn't
  resolve (wrong key/`from_path`).
- *Validating recipe* - the library's static check.
- *NEEDS ATTENTION* - everything not auto-generated; relay it (see below).
- The file is written only if all checks pass.

**4. Test** before submitting (this exercises real locales):
```bash
./mach fluent-migration-test python/l10n/fluent_migrations/bug_<NUMBER>_<desc>.py
```
Expected diffs: non-migrated new strings show as removals; a capitalization-only
migration shows an en-US-only diff (the test migrates en-US onto itself) - benign,
other locales keep their translation. Any other diff means wrong references.

## NEEDS ATTENTION (what to relay)

Cardinal rule: **a changed string must get a new identifier** (unique, with a
stable meaning across files) - otherwise locales keep the stale translation
against the new English. The only exception is an *unchanged* cross-file move
(keeps its id; auto-handled).

- **WARNING - changed but kept its id** -> needs a new id, translated fresh (no
  `.style`/"cosmetic" exception).
- **SUGGESTED rename** -> only attributes dropped/added with reused text; rename
  it and the translations carry (the helper prints the id + `COPY_PATTERN` refs).
- **RENAMED/MOVED but text changed** -> not migratable; translate fresh.
- **AMBIGUOUS** -> text matches several strings; pick the source by hand.
- **LEGACY .properties** -> hand-write `COPY`/`REPLACE`/`PLURALS`/`CONCAT` (see
  below), or scaffold with `properties-to-ftl`
  (https://github.com/mozilla/properties-to-ftl).
- Confirm any `[capitalization changed - verify]` / `[matched by content]` items.

## Recipe shape & hand-writing

The helper emits `COPY_PATTERN` for FTL sources and `COPY` for `.properties`
keys. When editing the output or hand-writing the rest:
- Recipe paths drop `locales/en-US/` (`browser/locales/en-US/browser/foo.ftl` ->
  `browser/browser/foo.ftl`). `from_path` = the *old* file, `target` = the new
  file; one `add_transforms` block per (target, `from_path`).
- `COPY_PATTERN`: `"id"` copies the value, `"id.attr"` an attribute (list each).
- `.properties` -> Fluent uses **`COPY`** with the flat key. For placeholders,
  brand, plurals, or markup, drop to the raw AST:
  ```python
  import fluent.syntax.ast as FTL
  from fluent.migrate.transforms import COPY, REPLACE, PLURALS, REPLACE_IN_TEXT, CONCAT
  from fluent.migrate.helpers import VARIABLE_REFERENCE, TERM_REFERENCE, MESSAGE_REFERENCE
  ```
  - `%S`/`%1$S`/brand -> `REPLACE(path, key, {"%1$S": VARIABLE_REFERENCE("name"),
    "Firefox": TERM_REFERENCE("brand-short-name")})` (`normalize_printf=True` is
    the default for `.properties`).
  - `a;b` plural with `#1` -> `PLURALS(path, key, VARIABLE_REFERENCE("count"),
    lambda t: REPLACE_IN_TEXT(t, {"#1": VARIABLE_REFERENCE("count")}))`.
  - markup / joined strings -> `CONCAT(...)`; never add your own spaces/punctuation.
- FTL->FTL transforms (strip `…`, remove a `<span>`, rename a `{ $var }`) need a
  custom `TransformPattern` subclass. Never bake English literals into a template.

Authoritative docs: `intl/l10n/docs/migrations/{overview,fluent,legacy,testing}.rst`.
For recent examples grep `python/l10n/fluent_migrations/` (pruned each cycle).

## Quick reference

| Change | Migratable? | How |
| --- | --- | --- |
| Rename / restructure, same text (`foo`->`foo2`, `.label`->`.title`, ...) | Yes | `COPY_PATTERN(from_path, "old[.attr]")` |
| Only capitalization differs | Yes (flag) | migrate; locales keep their own caps |
| Same id moved to another file | Yes | `add_transforms(newFile, newFile, …, from_path=oldFile)` |
| Seed: new id reuses a still-present string | Yes | `COPY_PATTERN(from_path, "kept-id")` |
| Wholly different id, same text | Yes (verify) | paired by coverage |
| Assembled from several same-file strings | Yes (verify) | per-pattern `COPY_PATTERN` |
| Same id but text changed, or any reword | No | new id; translate fresh |
| `.properties` plain string | Yes | `COPY(from_path, "key")` |
| `.properties` with `%S`/`#1`/brand/markup | Yes (raw AST) | `REPLACE` / `PLURALS` / `CONCAT` |
| Across-file assembly, `CONCAT` into one pattern, rule-transform | By hand | write manually |
