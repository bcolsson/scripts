---
name: fluent-migration
description: >
  Use this skill when a patch or local changes rename, restructure, move, or
  replace Fluent (.ftl) strings - or migrate legacy .properties strings to Fluent
  - and you need a migration recipe in python/l10n/fluent_migrations so existing
  translations carry over. It detects the changes, then generates, verifies, and
  validates the recipe with the in-tree `fluent.migrate` library (offline, no
  firefox-l10n-source clone) and writes the file. Triggers: "write/generate a
  fluent migration", "migrate these strings", renamed/bumped l10n string IDs
  (foo -> foo2), moving a value to/from an attribute, moving a string between
  files, .properties -> Fluent. Also reports the changes that cannot be migrated.
---

## Overview

Firefox ships its UI strings as Fluent messages in `.ftl` files; the reference
(English) strings live under `<component>/locales/en-US/...`. When a patch
changes where a string's content lives, the localized translations keyed to the
old location are otherwise lost and every locale falls back to English until
re-translated. A **migration recipe** - a small Python module in
`python/l10n/fluent_migrations/` - tells the l10n tooling to copy the existing
translations to the new identifier/location. It ships in the same patch.

A migration is needed in more cases than a simple rename:
- **Rename**: a version bump (`foo` -> `foo2`, `foo-2`, `foo_2`, `title2` ->
  `title3`) or a `-`/`_`-separated segment added/dropped (`foo` <-> `foo-descriptor`).
- **Restructure**: a value moved to/from an attribute, or an attribute renamed.
- **Move across files**: the *same* identifier moved to another `.ftl`
  (translations are keyed per file, so the recipe copies from the old file via a
  distinct `from_path`).
- **Seed / split**: a *new* identifier reuses the text of a message that is
  **still present** (the old string is not removed).
- **Rename to a wholly different identifier**: `panel-old-label` ->
  `sidebar-new-heading` - detected only by matching identical text.
- **Assembled from several strings**: one new message whose patterns come from
  multiple old messages (e.g. `.label`/`.accesskey` from one, `.description` from
  another). Auto-handled when all sources are in the **same file**; assembling
  across files, or stitching strings into one pattern with `CONCAT`, is left for a
  hand-written recipe.
- **Legacy `.properties` -> Fluent**: a string moved out of a `.properties`
  file. (`.dtd`/XUL legacy migration is out of scope for this skill.)

This skill is **self-contained** and library-driven: a single helper detects the
changes, then for the clean cases **builds** the transforms with the real
`fluent.migrate.helpers.transforms_from`, **verifies** them by evaluating against
the pre-change strings with a `fluent.migrate` `InternalContext` (offline),
**validates** the finished recipe with `fluent.migrate.validator.Validator`, and
**writes** the file - only if every check passes. Anything it cannot generate
cleanly is reported under "Needs attention" for you to handle by hand.

Key facts:
- A migration is only worthwhile when the **English text is reusable** - either
  identical, capitalization-only different (see below), or transformable by a
  known rule. If the wording genuinely changed, do **not** migrate it; let it be
  translated fresh.
- Recipes are disposable: l10n-drivers delete them after ~2 release cycles, so
  do not update or worry about old recipes referencing now-removed strings.

In-tree docs (read for edge cases):
- `intl/l10n/docs/migrations/overview.rst` - recipe structure and lifecycle
- `intl/l10n/docs/migrations/fluent.rst` - `COPY_PATTERN` and `TransformPattern`
- `intl/l10n/docs/migrations/legacy.rst` - `.properties` -> Fluent: `COPY`,
  `REPLACE`, `PLURALS`, `CONCAT`, references (the authoritative legacy reference)
- `intl/l10n/docs/migrations/testing.rst` - `./mach fluent-migration-test`

For more real recipes, grep the in-tree `python/l10n/fluent_migrations/`
directory (pruned each cycle, but usually has recent examples). The
`properties-to-ftl` tool (https://github.com/mozilla/properties-to-ftl) can
scaffold a `.properties` -> Fluent migration and refactor the calling code.

## Workflow

### 1. Find the bug number

The bug number is just the `--bug` argument; the tool never reads it from git, so
you do **not** need a commit. Use the bug you're working on (from Bugzilla / your
notes). If the change is already committed it's convenient to read it from there
(`git log -1 --format=%s HEAD` -> `Bug <NNNNNNN> - ...`), but for local-only work
just supply it directly. If you omit `--bug`, the recipe is still generated with a
`Bug <NUMBER>` placeholder in the docstring and a `bug_NUMBER_*.py` default name -
fill those in before landing (`./mach fluent-migration-test` checks the filename's
bug number against the commit, so the placeholder will flag it).

### 2. Generate (and verify + validate) the recipe

Run the helper through `./mach python` so `fluent.migrate` is importable:

```bash
# Committed change - analyze the commit:
./mach python .claude/skills/fluent-migration/scripts/generate_migration.py \
    --rev HEAD --bug 2043735 --description "Update the containers panel"

# Local-only change - omit --rev to analyze the working tree against HEAD, and
# write the file (only happens if all checks pass):
./mach python .claude/skills/fluent-migration/scripts/generate_migration.py \
    --bug 2043735 --description "Update the containers panel" \
    --output python/l10n/fluent_migrations/bug_2043735_containers_panel.py
```

- `--rev` only selects which diff to analyze (commit-vs-parent); omit it to use
  uncommitted working-tree changes. It does not affect the bug number.
- The `--output` path must live under `python/l10n/fluent_migrations/` with the
  `bug_<number>_<slug>.py` convention so `./mach fluent-migration-test` accepts it.

### 3. Interpret the output

- **Verifying transforms** - one `OK`/`FAIL` per generated message. The helper
  builds each transform with `transforms_from` and evaluates it against the
  pre-change source; `FAIL` means a reference did not resolve (a wrong key or
  `from_path`) - fix it before trusting the recipe.
- **Validating recipe** - the library's static `Validator` check of the file.
- **NEEDS ATTENTION** - everything *not* auto-generated (see "Things to point
  out"); relay these to the user.
- If any check fails, the file is **not** written.

### 4. The recipe / how transforms work

The generated recipe looks like this (clean FTL -> FTL case):

```python
# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/

from fluent.migrate.helpers import transforms_from


def migrate(ctx):
    """Bug 2043735 - Update the containers panel, part {index}."""

    source = "browser/browser/preferences/containers.ftl"
    target = source
    ctx.add_transforms(
        target,
        target,
        transforms_from(
            """
containers-color-blue2 = {COPY_PATTERN(from_path, "containers-color-blue.label")}
containers-settings-button2 =
    .title = {COPY_PATTERN(from_path, "containers-settings-button.label")}
""",
            from_path=source,
        ),
    )
```

Rules to know (for editing the output or hand-writing the rest):
- **Docstring** `"""Bug <NUMBER> - <description>, part {index}."""` - keep the
  literal `part {index}` (the tooling uses it for the per-author commit message).
- **Recipe paths drop `locales/en-US/`**: `browser/locales/en-US/browser/foo.ftl`
  is referenced as `browser/browser/foo.ftl`.
- **`COPY_PATTERN` reference syntax** (second arg is a *pattern*, not a message):
  `"old-id"` copies the value; `"old-id.attr"` copies that attribute. To carry a
  value and attributes, list each one.
- **`from_path` is where the *old* string lives; `target`/`reference` is the new
  file.** Same file for a normal rename. For a **move across files** they differ;
  the helper emits a separate `add_transforms` block per (target, `from_path`):
  ```python
  source = "toolkit/toolkit/global/browser-utils.ftl"   # old location
  target = "browser/browser/preferences/preferences.ftl"  # new location
  ctx.add_transforms(target, target, transforms_from(
      """home-prefs-homepage-extension-option =
      .label = {COPY_PATTERN(from_path, "browser-utils-url-extension")}""",
      from_path=source))
  ```

### 5. Rules and special cases

- **A message is migrated as a whole.** Migratable only when *every* pattern (the
  value and all attributes) reuses existing text. If any one pattern's text
  changed, the whole message - including unchanged attributes - is translated
  fresh and must **not** be migrated.
- **Capitalization-only changes are migratable, but flag them.** When the English
  differs only in case ("Add New Container" -> "Add new container"), still
  migrate it: locales keep their own capitalization, so the existing translations
  stay correct. The helper tags these `[capitalization changed - verify]`; always
  ask the user to confirm it really was only casing.
- **Moves and seeds need a migration even with an unchanged id.** The helper finds
  both (a move emits a cross-file block; a seed copies from a still-present
  string). It detects a **wholly different identifier** by pairing the new message
  with the *removed* string that covers the most of it - by number of matched
  patterns, then by matched text length - and only when that's the mutual best on
  both sides (ties are reported AMBIGUOUS, so it never guesses). Identical English
  does not guarantee identical meaning, so these are flagged for you to confirm.
- **A message assembled from several strings.** When a new message's patterns
  come from more than one old message (e.g. `.label`/`.accesskey` from one string,
  `.description` from another), the helper anchors on the source covering most
  patterns, then sources each remaining pattern from a co-located sibling or a
  *uniquely* matching string, emitting one block (`.label =
  {COPY_PATTERN(from_path, "a.label")}`, `.description = {COPY_PATTERN(from_path,
  "b")}`, ...) marked "assembled from a, b - verify". This only triggers when all
  sources are in the **same file** (so a single `from_path` holds). Assembling
  across files, or stitching strings into one pattern with `CONCAT`, is not
  auto-generated - write those by hand.
- **Legacy `.properties` -> Fluent.** The transform is **`COPY`** (not
  `COPY_PATTERN`), and the reference is the flat `.properties` **key** (no
  `.attr`); `from_path` is the `.properties` file. The helper auto-emits `COPY`
  for any key whose text matches verbatim. Strings with `%S`/`#1` placeholders, a
  brand reference, or built from several keys land in "Needs attention" - write
  them by hand with the raw AST (below). For a larger `.properties` -> Fluent
  migration, Mozilla's **`properties-to-ftl`** tool
  (https://github.com/mozilla/properties-to-ftl) can scaffold the FTL strings,
  the recipe (including `REPLACE`/`PLURALS` with `# FIXME` variable names), and
  the calling-code changes for you - then use this skill to verify/validate the
  generated recipe. Imports for hand-writing:
  ```python
  import fluent.syntax.ast as FTL
  from fluent.migrate.transforms import COPY, REPLACE, PLURALS, REPLACE_IN_TEXT, CONCAT
  from fluent.migrate.helpers import (
      transforms_from, VARIABLE_REFERENCE, TERM_REFERENCE, MESSAGE_REFERENCE,
  )
  ```
  - **Placeholders / brand (`REPLACE`)** - `normalize_printf=True` is the default
    for `.properties` (rewrites `%S` -> `%1$S` for reliable positional replace):
    ```python
    FTL.Message(id=FTL.Identifier("update-full-name"),
        value=REPLACE("toolkit/.../updates.properties", "updateFullName", {
            "%1$S": VARIABLE_REFERENCE("name"),
            "%2$S": VARIABLE_REFERENCE("buildID"),
        }))
    # brand: {"Firefox": TERM_REFERENCE("brand-short-name")}
    # other message: {"%1$S": MESSAGE_REFERENCE("some-other-id")}
    ```
  - **Plurals (`PLURALS` + `REPLACE_IN_TEXT`)** - a semicolon-separated string
    with `#1`:
    ```python
    FTL.Message(id=FTL.Identifier("containers-disable-alert-ok-button"),
        value=PLURALS("browser/.../preferences.properties", "disableContainersOkButton",
            VARIABLE_REFERENCE("tabCount"),
            lambda text: REPLACE_IN_TEXT(text, {"#1": VARIABLE_REFERENCE("tabCount")})))
    ```
  - **Combine strings / markup (`CONCAT`)** - stitch `FTL.TextElement`s and
    `COPY`/`REPLACE` results; never add spaces/punctuation of your own.
  - **Whitespace**: `COPY`/`REPLACE`/`PLURALS` trim each line by default; pass
    `trim=False` to keep it.
- **Text transformed by a rule (FTL -> FTL)** - e.g. trailing `…` stripped, an
  HTML `<span>` removed, a `{ $var }` reference renamed: `transforms_from` can't
  express it; use an explicit `FTL.Message` list with a custom `TransformPattern`
  subclass (see `fluent.rst`).
- **Never hard-code English** into a transform template (e.g. `new = { old }
  (Persistent)`) - it bakes English into every locale.
- **Limits.** Only *changed* files are inspected, so a seed/move whose source is
  in an untouched file is not detected - add that `COPY`/`COPY_PATTERN` yourself.

### 6. Things to point out ("Needs attention")

The cardinal rule: **a string identifier must be unique and keep a stable
meaning - across files, not just within one.** If a string's content changes it
must get a new identifier; otherwise every locale keeps the stale translation
against the new English (and a dropped attribute is orphaned). The sole exception
is moving a string *unchanged* to another file (it keeps its id, auto-handled).

The helper prints these; relay each to the user:
- **!! WARNING - changed but kept its id**: a value/attribute changed, was added,
  or removed. Must get a new id and be translated fresh. No `.style`/"cosmetic"
  exception. (If only attributes were dropped or added *with reused text*, it
  appears as **SUGGESTED rename** instead - rename it and the translations carry.)
- **SUGGESTED rename**: relay the suggested id and the `COPY_PATTERN` refs.
- **RENAMED/MOVED but text changed**: correctly not migratable; translate fresh.
- **AMBIGUOUS content match**: pick the source and add the transform by hand.
- **LEGACY .properties**: write the `COPY`/`REPLACE`/`PLURALS`/`CONCAT` by hand
  (see step 5), or scaffold it with `properties-to-ftl`
  (https://github.com/mozilla/properties-to-ftl).
- Confirm any **`[capitalization changed - verify]`** and content-matched items.

### 7. Test

Even though the recipe is verified offline, run the official test before
submitting (it exercises real localizations):

```bash
./mach fluent-migration-test python/l10n/fluent_migrations/bug_<NUMBER>_<desc>.py
```

A clean run prints only the migration commits. Expected diffs: new non-migrated
strings show as removals; a **capitalization-only** migration shows a diff on
that pattern *for en-US only* (the test migrates en-US onto itself, so it keeps
the old casing) - benign, every real locale keeps its own translation; the
message's unchanged patterns must still show no diff. Any other diff means the
recipe produces the wrong content - fix the references. Redirect long output to
`artifacts/` rather than piping through `tail`/`grep`.

## Quick reference

| Change | Migratable? | How |
| --- | --- | --- |
| Rename, same text (`foo`->`foo2`/`foo-2`/`foo-descriptor`) | Yes | `foo2 = {COPY_PATTERN(from_path, "foo")}` |
| `.label` value -> message value, same text | Yes | `foo2 = {COPY_PATTERN(from_path, "foo.label")}` |
| `.label` -> `.title`, same text | Yes | `foo2 =`<br>`    .title = {COPY_PATTERN(from_path, "foo.label")}` |
| Same id moved to another file | Yes | `add_transforms(newFile, newFile, …, from_path=oldFile)` |
| New id reuses a still-present string (seed/split) | Yes | `new = {COPY_PATTERN(from_path, "kept-id")}` |
| Wholly different id, same text (old removed) | Yes (verify) | paired by text; `new = {COPY_PATTERN(from_path, "old")}` |
| New message assembled from several same-file strings | Yes (verify) | per-pattern `COPY_PATTERN` from each source id (one block) |
| Assembled across files, or `CONCAT` into one pattern | No (by hand) | write the recipe manually |
| Same text on several strings | Ambiguous | pick the source, add by hand |
| Only capitalization differs | Yes (flag it) | migrate; locales keep their own caps |
| Same id, text/value changed | No, needs new id | give a new id; translate fresh |
| Same id, only attrs dropped / added-but-reused | Needs new id, then yes | rename, then `COPY_PATTERN` the carried patterns |
| Any attr/value reworded (beyond caps) | No | translate fresh |
| `.properties` -> FTL, plain string | Yes | `new = { COPY(from_path, "legacyKey") }` |
| `.properties` -> FTL, `%S`/`#1`/brand | Yes (raw AST) | `REPLACE(... {"%1$S": VARIABLE_REFERENCE("x")}, normalize_printf=True)` |
| `.properties` -> FTL, `a;b` plural | Yes (raw AST) | `PLURALS(...)` + `REPLACE_IN_TEXT` on `#1` |
| `.properties` strings combined / with markup | Yes (raw AST) | `CONCAT(COPY/REPLACE, FTL.TextElement(...))` |
| Text transformed by a rule (`…`, span, `{ $var }`) | Yes (custom) | explicit `FTL.Message` + `TransformPattern` |
