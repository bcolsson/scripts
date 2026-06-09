---
name: fluent-migration
description: >
  Use this skill when a patch or local changes rename, restructure, move, or
  replace Fluent (.ftl) strings - or migrate legacy .properties strings to Fluent
  - and you need a migration recipe in python/l10n/fluent_migrations so existing
  translations carry over. You read the diff, classify each changed string, write
  the recipe by hand, and validate it with the in-tree `./mach
  fluent-migration-test`. Triggers: "write/generate a fluent migration", "migrate
  these strings", renamed/bumped l10n IDs (foo -> foo2), moving a value to/from an
  attribute, moving a string between files, .properties -> Fluent. Also covers
  changes that must NOT be migrated.
---

## What this does

A **migration recipe** is a Python module in `python/l10n/fluent_migrations/`,
shipped in the same patch. It tells l10n tooling to copy existing translations
over to a renamed, moved, or restructured string, so locales don't fall back to
English. It only helps when the English text can be **reused** - identical, or
differing only in capitalization. If the wording really changed, the string gets
translated fresh instead (give it a new id and leave it out of the recipe).

You build the recipe by hand: read the diff, decide per string whether it's
migratable, write the `add_transforms` blocks, then let `./mach
fluent-migration-test` check them authoritatively against real l10n. There is no
generator - the test is the source of truth.

**Two hard rules - never break them when writing a migration file:**
- **No partial migrations.** Migration is all-or-nothing per message. Every
  translatable part of the target message (its value and *each* attribute) must
  be rebuildable from reused source content via `COPY`/`COPY_PATTERN`. If any part
  is new or changed - a new attribute like an added `.description`, a changed
  value, or an attribute with reworded text - you can't migrate just the reused
  parts. Leave the **whole message** out of the recipe and let it translate fresh.
  Bumping the id does **not** rescue such a message: a message that changed one
  attribute (say `.style = ...45em` -> `...32em`) still needs a new id *and* still
  stays out of the recipe, because migrating its unchanged `.title` while the
  `.style` changed is exactly a partial migration. Never suggest "give it a new id
  and migrate the reused part" - the new id and migratability are separate
  questions, and a message only becomes migratable when *every* part is reused.
- **No hardcoded strings.** Never put a literal in the recipe to fill in content
  the migration can't produce from a source string (for example, copying `.title`
  via `COPY_PATTERN` while hardcoding a changed `.style = ...`). Mixing copied
  translations with hardcoded literals is just a partial migration in disguise.
  (Some landed patches do this as a deliberate human exception - but it's an
  exception, never something this skill should produce or suggest.)

## Workflow

### 1. Get the diff of the string changes

These commands work on uncommitted changes too. Look only at `.ftl` /
`.properties` files:

```bash
# working tree (local-only change):
git diff -- '*.ftl' '*.properties'
# committed change (single commit):
git diff 'HEAD^!' -- '*.ftl' '*.properties'
# committed change spanning several commits <base>..<tip>:
git diff <base>~1 <tip> -- '*.ftl' '*.properties'
```

To confirm a string's text is reused, read its *old* value from the pre-change
file. `<rev>` is the commit *before* the change (e.g. `HEAD^`, or `<base>~1`):

```bash
git show <rev>:browser/locales/en-US/browser/preferences/containers.ftl
```

### 2. Classify each changed string

Decide, per message, what the recipe should do. This table is the core of the
job:

| What changed | Migrate? | How |
| --- | --- | --- |
| id renamed/bumped, text identical (`foo` -> `foo2`) | Yes | `COPY_PATTERN(from_path, "foo")` for the value; `"foo.attr"` for each attribute |
| capitalization-only difference | Yes (still reusable) | same as above; the test flags it `WARNING` - confirm only the casing changed |
| wording genuinely changed | **No** | new id, translated fresh; leave out of the recipe entirely |
| moved to another file, text unchanged | Yes | `target` = new file, `from_path` = old file; a pure move may even keep its id |
| value <-> attribute restructure, **all** text reused | Yes | map every reused piece with `COPY_PATTERN` |
| restructure that adds/changes any text (new `.description`, changed `.style`, ...) | **No** (no partial) | leave the whole message out |
| legacy `.properties` key -> Fluent | Yes | `COPY` / `REPLACE` / `PLURALS` / `CONCAT` (see below) |

Cardinal rule: **a changed string must get a new identifier** (unique, with a
meaning that stays stable across files) - otherwise locales keep showing the old
translation next to the new English. "Changed" means any non-capitalization change
to the value *or to any attribute* - including non-prose attributes like `.style`,
`.accesskey`, or `.key`. A message whose only edit is `.style = ...45em` ->
`...32em` still needs a new id. The only exception is an *unchanged* cross-file
move, which keeps its id. Brand-new strings (no predecessor) are never migrated -
they're translated from scratch.

If text matches several candidate source strings (AMBIGUOUS), pick the source by
hand. For legacy `.properties`, you can also scaffold with `properties-to-ftl`
(https://github.com/mozilla/properties-to-ftl).

When a single target message draws its parts from **more than one source
message** (e.g. a restructure whose value comes from one string and whose
`.title`/`.header` come from others), the test only proves the *English text*
matches - it cannot tell you the borrowed translation belongs in the new context.
A translation that is correct in its original message may be wrong once reused
elsewhere. For every such cross-message reuse, surface it to the user and have
them **independently confirm the source string's context matches the target's**
before relying on the migration; if the contexts don't line up, leave that part
(and therefore the whole message - no partial) out and let it translate fresh.

### 3. Write the recipe file

Path: `python/l10n/fluent_migrations/bug_<number>_<slug>.py`. The docstring must
contain the bug number and the literal `part {index}`. Use one `add_transforms`
block per `(target, from_path)` pair. See "Recipe shape" below for the template.

The bug number comes from the work you're doing - look in the relevant commit
message (`git log`, e.g. `git log -1 --format=%s`, or the bug reference on the
commit you diffed) and the conversation/task. If you can't find it, **use a
numeric placeholder** - `bug_0000000_<slug>.py` with `Bug 0000000` in the
docstring - rather than stalling; it keeps the test passing. Then tell the user
plainly that the bug number is a placeholder they must replace (rename the file
and update the docstring to match) before landing.

### 4. Validate with the in-tree test (authoritative)

```bash
./mach fluent-migration-test python/l10n/fluent_migrations/bug_<number>_<slug>.py
```

It checks out the pre-change strings, runs the recipe, and exits non-zero on any
error. Read the **"Fluent migration test summary"** (the diff above it is just a
visual aid), which sorts every finding into three levels:

- **ERROR** (must fix; the test exits non-zero): a recipe string that wasn't
  migrated, a migrated message differing from the reference by more than
  capitalization, a same-id/same-file ("migrated from itself") migration, a
  non-normalized reference path, a recipe that couldn't be inspected or loaded
  ("Could not inspect declared targets"), or a bad bug number / a commit missing
  `part {index}`.
- **WARNING** (surface every one): a migrated message differing only in
  capitalization (confirm only the casing changed - then it's fine); a migrated
  message "not present in the reference" (the target id doesn't exist in the new
  en-US, usually a wrong or mistyped target id - fix it); or "No migration applied"
  (the recipe produced no changes at all - almost always a mistake - fix it).
- **INFO**: strings that differ but aren't in the recipe. Surface these to the
  user for review rather than silently ignoring them: each changed string needs
  the cardinal-rule treatment (a **new id**, counting *every* attribute - see step
  2). Flagging that a string "needs a new id" is **not** an invitation to then
  migrate its unchanged parts: if any part changed, the whole message stays out (no
  partial - see the hard rules). Genuinely new strings and quarantined strings are
  fine to ignore.

Relay **every** ERROR and WARNING line the summary prints to the user - never drop
a finding just because it isn't described above. Use the summary to correct
yourself too: an ERROR on a string you left out means it *was* fully reusable - add
it; an ERROR on a string you included means the text wasn't reusable - remove it,
and re-check against the two hard rules (you may be attempting a partial or
hardcoded migration).

## Recipe shape & hand-writing

Template - `COPY_PATTERN` for FTL sources, `COPY` for `.properties` keys:

```python
# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/

from fluent.migrate.helpers import transforms_from


def migrate(ctx):
    """Bug <number> - <description>, part {index}."""

    source = "browser/browser/preferences/containers.ftl"
    target = "toolkit/toolkit/global/contextual-identity.ftl"
    ctx.add_transforms(
        target,
        target,
        transforms_from(
            """
user-context-color-blue =
    .label = {COPY_PATTERN(from_path, "containers-color-blue.label")}
""",
            from_path=source,
        ),
    )
```

- Recipe paths drop `locales/en-US/` (`browser/locales/en-US/browser/foo.ftl` ->
  `browser/browser/foo.ftl`). `from_path` is the *old* file, `target` is the new
  file; use one `add_transforms` block per (target, `from_path`) pair.
- Inside the `transforms_from` string, always reference `from_path` (the keyword
  passed to `transforms_from`), regardless of the local variable's name.
- `COPY_PATTERN`: `"id"` copies the value, `"id.attr"` copies an attribute. List
  every attribute you migrate - and per the no-partial rule, migrate all of a
  message's translatable parts or none.
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
  custom `TransformPattern` subclass. Never bake English literals into a template -
  that is the no-hardcoding rule.

Authoritative docs: `intl/l10n/docs/migrations/{overview,fluent,legacy,testing}.rst`.
For recent examples grep `python/l10n/fluent_migrations/` (pruned each cycle).
