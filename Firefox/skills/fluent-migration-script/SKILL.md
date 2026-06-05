---
name: fluent-migration
description: >
  Use this skill when a patch or local changes rename, restructure, move, or
  replace Fluent (.ftl) strings - or migrate legacy .properties strings to Fluent
  - and you need a migration recipe in python/l10n/fluent_migrations so existing
  translations carry over. It finds the changes, then builds, checks, and
  validates the recipe with the in-tree `fluent.migrate` library (offline) and
  writes the file. Triggers: "write/generate a fluent migration", "migrate these
  strings", renamed/bumped l10n IDs (foo -> foo2), moving a value to/from an
  attribute, moving a string between files, .properties -> Fluent. Also reports
  changes that can't be migrated.
---

## What this does

A **migration recipe** is a Python module in `python/l10n/fluent_migrations/`,
shipped in the same patch. It tells l10n tooling to copy existing translations
over to a renamed, moved, or restructured string, so locales don't fall back to
English. It only helps when the English text can be **reused** - identical, or
differing only in capitalization. If the wording really changed, the string gets
translated fresh instead.

**Two hard rules - never break them when generating a migration file:**
- **No partial migrations.** Migration is all-or-nothing per message. Every
  translatable part of the target message (its value and *each* attribute) must
  be rebuildable from reused source content via `COPY`/`COPY_PATTERN`. If any part
  is new or changed - a new attribute like an added `.description`, a changed
  value, or an attribute with reworded text - you can't migrate just the reused
  parts. Leave the **whole message** out of the recipe and let it translate fresh.
- **No hardcoded strings.** Never put a literal in the recipe to fill in content
  the migration can't produce from a source string (for example, copying `.title`
  via `COPY_PATTERN` while hardcoding a changed `.style = ...`). Mixing copied
  translations with hardcoded literals is just a partial migration in disguise.
  (Some landed patches do this as a deliberate human exception - but it's an
  exception, never something this skill should produce or suggest.)

The helper detects the changes on its own (renames/bumps, restructures,
cross-file moves, seeds, wholly-different-id renames, multi-source assembly, and
legacy `.properties` -> Fluent), builds the transforms with `fluent.migrate`,
checks them offline against the pre-change strings, validates the recipe with the
library's `Validator`, and writes the file - **only if every check passes**.
Anything it can't generate cleanly is listed under NEEDS ATTENTION.

## Workflow

**1. Bug number** - pass it via `--bug` (it's never read from git, so this works
on uncommitted changes); use the bug you're working on. If you omit it, the file
gets a `Bug <NUMBER>` placeholder to fill in.

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
`--rev` only picks the diff (omit it for the working tree). `--output` must be
under `python/l10n/fluent_migrations/` and named `bug_<number>_<slug>.py`.

**3. Read the output**
- *Verifying transforms* / *Validating recipe* - offline pre-checks; the file is
  written only if both pass (step 4's test re-checks them for real).
- *Review before landing* - a table of per-migration items to confirm
  (capitalization-only changes with their before/after text, content/legacy
  matches, cross-file moves, multi-source assembly). Relay these to the user.
- *NEEDS ATTENTION* - changes it couldn't auto-generate. The test reports these
  only as ignorable INFO, so acting on them (see below) is your call, not the
  test's.

**4. Test** - the real check (runs the recipe against actual l10n; exits
non-zero on any error):
```bash
./mach fluent-migration-test python/l10n/fluent_migrations/bug_<NUMBER>_<desc>.py
```
Read its **"Fluent migration test summary"**, which sorts every finding into
three levels (the diff above it is just a visual aid):
- **ERROR** (must fix): a recipe string that wasn't migrated, a migrated value
  differing by more than capitalization, a same-id/same-file migration, or a bad
  bug number / missing `part {index}`.
- **WARNING**: a migrated message that differs only in capitalization - review it.
- **INFO**: strings that differ but aren't in the recipe (new strings in the
  patch, or quarantined strings) - safe to ignore.

## NEEDS ATTENTION (what to relay)

Cardinal rule: **a changed string must get a new identifier** (unique, with a
meaning that stays stable across files) - otherwise locales keep showing the old
translation next to the new English. The only exception is an *unchanged*
cross-file move (it keeps its id, and the helper handles it automatically).

- **WARNING - changed but kept its id** -> needs a new id, translated fresh (no
  `.style`/"cosmetic" exception).
- **WARNING - rename** -> kept its id but was only restructured. Give it a new id,
  then add it to the migration **only if the whole message is still reusable** -
  that is, its value and every attribute map 1:1 onto reused source content via
  `COPY_PATTERN`. If the restructure added new text or changed any wording (a new
  `.description`, a changed `.style`), the no-partial-migration rule applies:
  leave the whole message out and let it translate fresh. The helper prints the
  suggested id and `COPY_PATTERN` refs when the message is fully reusable.
- **AMBIGUOUS** -> the text matches several strings; pick the source by hand.
- **LEGACY .properties** -> hand-write `COPY`/`REPLACE`/`PLURALS`/`CONCAT` (see
  below), or scaffold with `properties-to-ftl`
  (https://github.com/mozilla/properties-to-ftl).

## Recipe shape & hand-writing

The helper emits `COPY_PATTERN` for FTL sources and `COPY` for `.properties`
keys. When editing the output or hand-writing the rest:
- Recipe paths drop `locales/en-US/` (`browser/locales/en-US/browser/foo.ftl` ->
  `browser/browser/foo.ftl`). `from_path` is the *old* file, `target` is the new
  file; use one `add_transforms` block per (target, `from_path`) pair.
- `COPY_PATTERN`: `"id"` copies the value, `"id.attr"` copies an attribute (list
  each one).
- `.properties` -> Fluent uses **`COPY`** with the flat key. For placeholders,
  brand, plurals, or markup, drop down to the raw AST:
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
