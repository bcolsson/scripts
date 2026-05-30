#!/usr/bin/env python3
# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/
"""Generate a Fluent migration recipe with the in-tree ``fluent.migrate`` library.

This detects the string changes itself, then for the clean (auto-migratable)
cases it:

  1. builds the transforms with ``fluent.migrate.helpers.transforms_from``
     (``COPY_PATTERN`` for FTL sources, ``COPY`` for legacy ``.properties``);
  2. VERIFIES them offline by evaluating each against the pre-change strings with a
     ``fluent.migrate`` ``InternalContext`` (no firefox-l10n-source clone, no
     network) - catching a mistyped key/path the template alone would not;
  3. VALIDATES the finished recipe with ``fluent.migrate.validator.Validator``;
  4. writes (or prints) the recipe file - only if every check passed.

Run it through mach so ``fluent.migrate`` is importable:

    ./mach python .claude/skills/fluent-migration/scripts/generate_migration.py \\
        --rev HEAD --bug 2043735 --description "Update the containers panel" \\
        --output python/l10n/fluent_migrations/bug_2043735_containers_panel.py

Only clean cases are generated. Renames whose text changed, same-id changes that
need a new id, ambiguous matches, and legacy ``.properties`` strings needing
``REPLACE``/``PLURALS`` are reported under "needs attention" and handled by hand.
``.dtd``/XUL migration is out of scope.
"""

import argparse
import os
import re
import subprocess
import sys
from collections import OrderedDict

import fluent.syntax.ast as FTL
from compare_locales.parser import PropertiesParser
from fluent.migrate._context import InternalContext
from fluent.migrate.helpers import transforms_from
from fluent.migrate.validator import Validator
from fluent.syntax import FluentParser
from fluent.syntax.serializer import serialize_pattern

PARSER = FluentParser()


def git(*args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8", check=False
    )


def file_at_rev(rev, path):
    res = git("show", f"{rev}:{path}")
    return res.stdout if res.returncode == 0 else None


def working_tree_file(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def changed_paths(rev):
    """Repo-relative changed reference files: .ftl (targets/sources) and
    .properties (legacy sources). .dtd/XUL is out of scope."""
    if rev:
        res = git("diff", "--name-only", f"{rev}^!", "--")
    else:
        res = git("diff", "--name-only", "HEAD", "--")
    return [
        p
        for p in res.stdout.splitlines()
        if (p.endswith(".ftl") or p.endswith(".properties")) and "/locales/en-US/" in p
    ]


def migration_path(repo_path):
    """Map a repo path to the path used inside a recipe (drop locales/en-US)."""
    return repo_path.replace("/locales/en-US/", "/", 1)


def parse_properties(content):
    """key -> value for a legacy .properties file, using the canonical
    compare-locales parser (the same one fluent.migrate reads legacy sources
    with). It unescapes \\uXXXX / \\n / \\t / \\\\, handles `=` and `:`
    separators and odd/even-backslash line continuations, and trims trailing
    whitespace - so matching and the offline verification see the same values
    the real migration will. Junk (malformed) lines are skipped."""
    out = OrderedDict()
    if content is None:
        return out
    parser = PropertiesParser()
    parser.readUnicode(content)
    for entity in parser:
        if getattr(entity, "localized", False):  # an Entity, not Junk
            out[entity.key] = entity.val
    return out


def parse_messages(content):
    """id -> {'value': text|None, 'attrs': OrderedDict(name->text)}."""
    out = OrderedDict()
    if content is None:
        return out
    resource = PARSER.parse(content)
    for entry in resource.body:
        if not isinstance(entry, (FTL.Message, FTL.Term)):
            continue
        mid = entry.id.name
        if isinstance(entry, FTL.Term):
            mid = "-" + mid
        value = serialize_pattern(entry.value).strip() if entry.value else None
        attrs = OrderedDict(
            (a.id.name, serialize_pattern(a.value).strip()) for a in entry.attributes
        )
        out[mid] = {"value": value, "attrs": attrs}
    return out


def strip_version(mid):
    """Drop a trailing version bump from an identifier: a final run of digits,
    optionally preceded by a `-`, `_`, or `.` separator. So foo2, foo-2 and foo_2
    all reduce to foo, and title2/title3 share the stem title."""
    return re.sub(r"[-_.]?\d+$", "", mid)


def rename_rank(old_id, new_id):
    """How strongly new_id looks like a rename of old_id - lower is a stronger
    signal, None if they look unrelated. Identifiers get updated in several shapes:
      0  version bump: same stem, only a trailing number added/changed
         (foo -> foo2, foo -> foo-2, title2 -> title3)
      1  a `-`/`_`-separated segment added or dropped (foo <-> foo-descriptor)
    A wholly different identifier (foo -> bar) scores None here and is left to
    content matching (identical text against a removed string)."""
    if old_id == new_id:
        return None
    if strip_version(old_id) == strip_version(new_id):
        return 0
    longer, shorter = sorted((old_id, new_id), key=len, reverse=True)
    if any(longer.startswith(shorter + sep) for sep in ("-", "_")):
        return 1
    return None


def source_patterns(msg):
    """Yield (ref_suffix, label, text) for every pattern of a source message."""
    if msg["value"] is not None:
        yield "", "(value)", msg["value"]
    for name, text in msg["attrs"].items():
        yield f".{name}", f".{name}", text


def find_source(old_id, old_msg, want_name, text):
    """Find the source pattern in old_msg whose text matches `text`. Prefers an
    exact match over a capitalization-only one, and the same slot (value->value or
    the same attribute) over a cross-slot match. Returns (ref, label, kind,
    src_text) - kind is "exact" or "caps" - or None when nothing matches."""
    folded = text.casefold()
    best_rank = best = None
    for ref, label, src_text in source_patterns(old_msg):
        if src_text == text:
            kind = "exact"
        elif src_text.casefold() == folded:
            kind = "caps"
        else:
            continue
        same_slot = ref == "" if want_name is None else ref == f".{want_name}"
        rank = (kind == "caps", not same_slot)  # exact before caps, same before cross
        if best_rank is None or rank < best_rank:
            best_rank, best = rank, (f"{old_id}{ref}", label, kind, src_text)
    return best


def build_targets(src_id, src_msg, new_msg):
    """Classify each pattern of new_msg against a candidate source message."""
    targets = []
    patterns = []
    if new_msg["value"] is not None:
        patterns.append((None, new_msg["value"]))
    for name, text in new_msg["attrs"].items():
        patterns.append((name, text))
    for name, text in patterns:
        found = find_source(src_id, src_msg, name, text)
        target = {"attr": name, "text": text, "migratable": found is not None}
        if found:
            (
                target["source_ref"],
                target["source_label"],
                target["match"],
                target["source_text"],
            ) = found
        targets.append(target)
    return targets


def make_migration(
    target_path, new_id, src_path, src_id, targets, match_by="name", source_kind="ftl"
):
    status = "clean" if targets and all(t["migratable"] for t in targets) else "non-migratable"
    return {
        "new_id": new_id,
        "src_id": src_id,
        "src_path": src_path,
        "target_path": target_path,
        "moved": src_path != target_path,
        "renamed": src_id != new_id,
        "caps_only": any(t.get("match") == "caps" for t in targets),
        "match_by": match_by,  # name | content | legacy
        "source_kind": source_kind,  # ftl -> COPY_PATTERN; properties -> COPY
        "status": status,
        "targets": targets,
    }


def primary_text(msg):
    """The most distinctive piece of a message: its value, else its longest attr."""
    if msg["value"] is not None:
        return msg["value"]
    if msg["attrs"]:
        return max(msg["attrs"].values(), key=len)
    return None


def suggest_new_id(mid):
    """Suggest a bumped identifier: increment a trailing number, else append 2."""
    match = re.search(r"(\d+)$", mid)
    if match:
        return mid[: match.start()] + str(int(match.group(1)) + 1)
    return mid + "2"


def coverage_score(src_id, src_msg, new_msg):
    """How much of new_msg a single source covers, as (matched pattern count,
    total matched text length). Count is the primary signal - the source that
    supplies the most patterns is the best primary; length breaks ties toward
    substantive matches (a long value over a short accesskey)."""
    matched = [t for t in build_targets(src_id, src_msg, new_msg) if t["migratable"]]
    return len(matched), sum(len(t["text"]) for t in matched)


def strict_best(scored):
    """The key with the strictly-largest value in a {key: score} dict, or None when
    it is empty or tied at the top - so we never guess between equal candidates."""
    if not scored:
        return None
    ranked = sorted(scored.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def match_target(target_path, new_id, new_msg, old_index, removed_keys):
    """Find the best old FTL source for an added message: a kept id in another
    file (move), or an identifier-rename (version bump or a separated segment
    added/dropped - see rename_rank)."""

    def rank(key):
        src_path, src_id = key
        relation = -1 if src_id == new_id else rename_rank(src_id, new_id)
        if relation is None:
            return None
        exact_bump = bool(re.fullmatch(re.escape(src_id) + r"[-_.]?\d+", new_id))
        return (
            relation,  # -1 move (same id), 0 version bump, 1 extended segment
            0 if src_path == target_path else 1,  # same file beats cross-file
            0 if exact_bump else 1,  # prefer an exact version bump of this id
            src_path,  # deterministic tie-break
            src_id,
        )

    ranked = sorted((k for k in old_index if rank(k) is not None), key=rank)
    if not ranked:
        return None
    for key in ranked:
        src_path, src_id = key
        targets = build_targets(src_id, old_index[key], new_msg)
        if any(t["migratable"] for t in targets):
            return make_migration(target_path, new_id, src_path, src_id, targets)
    src_path, src_id = ranked[0]
    strong = (src_id == new_id) or ((src_path, src_id) in removed_keys)
    if not strong:
        return None
    targets = build_targets(src_id, old_index[(src_path, src_id)], new_msg)
    return make_migration(target_path, new_id, src_path, src_id, targets)


def diff_same_id(old_msg, new_msg):
    """Changes for a message whose identifier is unchanged (all need a new id)."""
    changes = []
    if old_msg["value"] != new_msg["value"]:
        changes.append({"attr": None, "kind": "value changed"})
    old_attrs, new_attrs = old_msg["attrs"], new_msg["attrs"]
    for name in old_attrs:
        if name not in new_attrs:
            changes.append({"attr": name, "kind": "attribute removed"})
        elif old_attrs[name] != new_attrs[name]:
            changes.append({"attr": name, "kind": "attribute changed"})
    for name in new_attrs:
        if name not in old_attrs:
            changes.append({"attr": name, "kind": "attribute added"})
    return changes


def complete_multi_source(migration, tpath, old_in_file):
    """Try to finish a partially-matched migration by sourcing its still-unmatched
    patterns from OTHER old messages in the SAME file - the data-collection shape,
    where one new message is assembled from several old strings (e.g. .label and
    .accesskey from one, .description from another).

    It anchors on the primary source, then for each leftover pattern pulls it from
    any already-contributing source (no uniqueness needed - it travels with its
    siblings), else accepts a match only if it is UNIQUE among the file's other old
    messages. Promotes the migration to a clean multi-source one (all sources in
    this file, so a single `from_path` still works) and returns True, or leaves it
    untouched and returns False. Restricted to same-file sources to keep the
    generated/verified recipe to one `from_path`; cross-file assembly is left for a
    hand-written recipe."""
    if migration["src_path"] != tpath:
        return False  # primary lives in another file; don't mix from_paths
    targets = migration["targets"]
    migratable = [t for t in targets if t["migratable"]]
    # Only EXTEND a genuine partial match; never assemble a message from scratch.
    if not migratable or len(migratable) == len(targets):
        return False

    contributing = OrderedDict()
    if migration["src_id"] in old_in_file:
        contributing[migration["src_id"]] = old_in_file[migration["src_id"]]
    # Also anchor on the same-file source with the best coverage, so all of its
    # patterns arrive via tier 1 even when none of them is individually unique.
    new_msg = {"value": None, "attrs": OrderedDict()}
    for t in targets:
        if t["attr"] is None:
            new_msg["value"] = t["text"]
        else:
            new_msg["attrs"][t["attr"]] = t["text"]
    best = max(
        old_in_file,
        key=lambda sid: coverage_score(sid, old_in_file[sid], new_msg),
        default=None,
    )
    if best is not None and coverage_score(best, old_in_file[best], new_msg)[0] > 0:
        contributing.setdefault(best, old_in_file[best])

    progressed = True
    while progressed:
        progressed = False
        for target in targets:
            if target["migratable"]:
                continue
            found = None
            # 1) Pull from a source we already draw from (anchored - no uniqueness).
            for sid, smsg in contributing.items():
                found = find_source(sid, smsg, target["attr"], target["text"])
                if found:
                    break
            # 2) Otherwise accept a match only if it is unique across the file.
            if not found:
                hits = []
                for sid, smsg in old_in_file.items():
                    if sid in contributing:
                        continue
                    hit = find_source(sid, smsg, target["attr"], target["text"])
                    if hit:
                        hits.append((sid, hit))
                if len(hits) == 1:
                    sid, found = hits[0]
                    contributing[sid] = old_in_file[sid]
            if found:
                (
                    target["source_ref"],
                    target["source_label"],
                    target["match"],
                    target["source_text"],
                ) = found
                target["migratable"] = True
                progressed = True

    if all(t["migratable"] for t in targets) and len(contributing) > 1:
        migration["status"] = "clean"
        migration["caps_only"] = any(t.get("match") == "caps" for t in targets)
        migration["multi_source"] = sorted(contributing)
        return True
    return False


def analyze(files):
    """Cross-file analysis. Returns (reports, legacy_pending)."""
    ftl_files = [f for f in files if f["kind"] == "ftl"]
    prop_files = [f for f in files if f["kind"] == "properties"]

    old_index = {}
    removed_keys = set()
    for f in ftl_files:
        for mid, msg in f["old"].items():
            old_index[(f["recipe_path"], mid)] = msg
            if mid not in f["new"]:
                removed_keys.add((f["recipe_path"], mid))

    consumed = set()
    per_file = OrderedDict()

    # Phase 1: identifier-based matching (move, or rename - see rename_rank).
    for f in ftl_files:
        old, new = f["old"], f["new"]
        tpath = f["recipe_path"]
        migrations, broken, unmatched = [], [], []
        for mid in new:
            if mid in old:
                continue
            migration = match_target(tpath, mid, new[mid], old_index, removed_keys)
            if migration is None:
                unmatched.append(mid)
                continue
            consumed.add((migration["src_path"], migration["src_id"]))
            (migrations if migration["status"] == "clean" else broken).append(migration)

        same_id_warn, rename_suggested = [], []
        for mid in new:
            if mid not in old:
                continue
            changes = diff_same_id(old[mid], new[mid])
            if not changes:
                continue
            targets = build_targets(mid, old[mid], new[mid])
            if all(t["migratable"] for t in targets):
                rename_suggested.append(
                    {
                        "id": mid,
                        "suggested_id": suggest_new_id(mid),
                        "changes": changes,
                        "targets": targets,
                    }
                )
            else:
                same_id_warn.append({"id": mid, "changes": changes})

        per_file[tpath] = {
            "repo_path": f["repo_path"],
            "recipe_path": tpath,
            "new": new,
            "migrations": migrations,
            "broken": broken,
            "unmatched": unmatched,
            "same_id_changes": same_id_warn,
            "rename_suggested": rename_suggested,
            "removed_raw": [mid for mid in old if mid not in new],
        }

    # Phase 2: a wholly different identifier. Pair each still-unmatched new message
    # with the REMOVED source that covers the most of it (coverage_score: matched
    # pattern count, then matched text length). Take a pairing only when it is the
    # mutual best on both sides; ties on either side are reported AMBIGUOUS so we
    # never guess. Content still gates the actual migration in build_targets.
    available = [k for k in removed_keys if k not in consumed]
    scores = {}  # (tpath, mid) -> {removed_key: (count, length)}
    for tpath, data in per_file.items():
        for mid in data["unmatched"]:
            new_msg = data["new"][mid]
            row = {}
            for key in available:
                score = coverage_score(key[1], old_index[key], new_msg)
                if score[0] > 0:
                    row[key] = score
            if row:
                scores[(tpath, mid)] = row

    per_source = {}  # removed_key -> {(tpath, mid): score}
    for added, row in scores.items():
        for key, score in row.items():
            per_source.setdefault(key, {})[added] = score

    ambiguous = {}
    paired_added = set()
    for added, row in scores.items():
        tpath, mid = added
        best = strict_best(row)
        if best is None or strict_best(per_source[best]) != added:
            ambiguous.setdefault(tpath, []).append(mid)
            paired_added.add(added)
            continue
        src_path, src_id = best
        targets = build_targets(src_id, old_index[best], per_file[tpath]["new"][mid])
        migration = make_migration(tpath, mid, src_path, src_id, targets, match_by="content")
        bucket = "migrations" if migration["status"] == "clean" else "broken"
        per_file[tpath][bucket].append(migration)
        consumed.add(best)
        paired_added.add(added)

    # Phase 3: legacy .properties -> FTL (verbatim text -> COPY; rest left for hand).
    legacy_index = {}
    for f in prop_files:
        for key, value in f["old"].items():
            if key not in f["new"]:
                legacy_index[(f["recipe_path"], key)] = value
    legacy_by_text = {}
    for (lpath, lkey), value in legacy_index.items():
        legacy_by_text.setdefault(value.casefold(), []).append((lpath, lkey))

    consumed_legacy = set()
    legacy_paired = set()
    for tpath, data in per_file.items():
        for mid in data["unmatched"]:
            if (tpath, mid) in paired_added:
                continue
            text = primary_text(data["new"][mid])
            if not text:
                continue
            cands = [c for c in legacy_by_text.get(text.casefold(), []) if c not in consumed_legacy]
            if len(cands) != 1:
                continue
            lpath, lkey = cands[0]
            pseudo = {"value": legacy_index[(lpath, lkey)], "attrs": {}}
            targets = build_targets(lkey, pseudo, data["new"][mid])
            if not all(t["migratable"] for t in targets):
                continue
            data["migrations"].append(
                make_migration(tpath, mid, lpath, lkey, targets, match_by="legacy", source_kind="properties")
            )
            consumed_legacy.add((lpath, lkey))
            legacy_paired.add((tpath, mid))

    # Phase 4: a new message assembled from several old messages in the same file
    # (e.g. .label/.accesskey from one string, .description from another). Try to
    # complete partially-matched (broken) migrations from same-file siblings.
    for tpath, data in per_file.items():
        old_in_file = {mid: msg for (p, mid), msg in old_index.items() if p == tpath}
        still_broken = []
        for migration in data["broken"]:
            if complete_multi_source(migration, tpath, old_in_file):
                data["migrations"].append(migration)
            else:
                still_broken.append(migration)
        data["broken"] = still_broken

    ftl_added = any(m not in f["old"] for f in ftl_files for m in f["new"])
    legacy_pending = []
    if ftl_added:
        for (lpath, lkey), value in legacy_index.items():
            if (lpath, lkey) not in consumed_legacy:
                legacy_pending.append({"path": lpath, "key": lkey, "value": value})

    reports = []
    for tpath, data in per_file.items():
        reports.append(
            {
                "repo_path": data["repo_path"],
                "recipe_path": tpath,
                "migrations": data["migrations"],
                "broken": data["broken"],
                "same_id_changes": data["same_id_changes"],
                "rename_suggested": data["rename_suggested"],
                "ambiguous": ambiguous.get(tpath, []),
                "new_strings": sorted(
                    mid
                    for mid in data["unmatched"]
                    if (tpath, mid) not in paired_added and (tpath, mid) not in legacy_paired
                ),
            }
        )
    return reports, legacy_pending


def collect_files(rev):
    files = []
    for path in changed_paths(rev):
        if rev:
            before = file_at_rev(f"{rev}^", path)
            after = file_at_rev(rev, path)
        else:
            before = file_at_rev("HEAD", path)
            after = working_tree_file(path)
        is_ftl = path.endswith(".ftl")
        parse = parse_messages if is_ftl else parse_properties
        files.append(
            {
                "repo_path": path,
                "recipe_path": migration_path(path),
                "kind": "ftl" if is_ftl else "properties",
                "old": parse(before),
                "new": parse(after),
            }
        )
    return files


def emit_recipe_body(file_reports, bug=None, description=None):
    """Build the recipe source for clean migrations, one add_transforms block per
    (target file, source file) pair."""
    blocks = []
    for report in file_reports:
        tpath = report["recipe_path"]
        groups = OrderedDict()
        for migration in report["migrations"]:
            groups.setdefault(migration["src_path"], []).append(migration)
        for from_path, migrations in groups.items():
            lines, notes = [], []
            for migration in migrations:
                if migration.get("multi_source"):
                    notes.append(
                        f"    # {migration['new_id']} assembled from "
                        f"{', '.join(migration['multi_source'])} - verify each source."
                    )
                if migration["match_by"] == "content":
                    notes.append(
                        f"    # Matched by content (identifiers differ): verify "
                        f"{migration['new_id']} replaces {migration['src_id']}."
                    )
                if migration["match_by"] == "legacy":
                    notes.append(
                        f"    # Migrated from legacy .properties ({from_path}); "
                        f"verify {migration['new_id']} matches {migration['src_id']}."
                    )
                if migration["moved"] and migration["match_by"] == "name":
                    notes.append(f"    # {migration['new_id']} moved from {from_path}.")
                if migration["caps_only"]:
                    changed = [
                        (t["attr"] or "value") for t in migration["targets"] if t.get("match") == "caps"
                    ]
                    notes.append(
                        f"    # Capitalization-only change on {migration['new_id']} "
                        f"({', '.join(changed)}); translations kept - verify this is intended."
                    )
                copy = "COPY" if migration["source_kind"] == "properties" else "COPY_PATTERN"
                targets = migration["targets"]
                if len(targets) == 1 and targets[0]["attr"] is None:
                    lines.append(f'{migration["new_id"]} = {{{copy}(from_path, "{targets[0]["source_ref"]}")}}')
                else:
                    lines.append(f"{migration['new_id']} =")
                    for target in targets:
                        lines.append(f'    .{target["attr"]} = {{{copy}(from_path, "{target["source_ref"]}")}}')
            body = "\n".join(lines)
            note_block = ("\n".join(notes) + "\n") if notes else ""
            if from_path == tpath:
                paths = f'    source = "{from_path}"\n    target = source'
            else:
                paths = f'    source = "{from_path}"\n    target = "{tpath}"'
            blocks.append(
                f'''{note_block}{paths}
    ctx.add_transforms(
        target,
        target,
        transforms_from(
            """
{body}
""",
            from_path=source,
        ),
    )'''
            )
    if not blocks:
        return None
    docstring = (
        f'"""Bug {bug} - {description or "Migrate strings to Fluent"}, part {{index}}."""'
        if bug
        else '"""Bug <NUMBER> - <description>, part {index}."""'
    )
    joined = "\n\n".join(blocks)
    return f'''# Any copyright is dedicated to the Public Domain.
# http://creativecommons.org/publicdomain/zero/1.0/

from fluent.migrate.helpers import transforms_from


def migrate(ctx):
    {docstring}

{joined}
'''


# ----------------------------------------------------------------------------
# Library-driven build / verification / validation.
# ----------------------------------------------------------------------------
def migration_template(migration):
    """The FTL body for one migration's message (COPY for legacy, else COPY_PATTERN)."""
    copy = "COPY" if migration["source_kind"] == "properties" else "COPY_PATTERN"
    targets = migration["targets"]
    if len(targets) == 1 and targets[0]["attr"] is None:
        return f'{migration["new_id"]} = {{ {copy}(from_path, "{targets[0]["source_ref"]}") }}\n'
    lines = [f'{migration["new_id"]} =']
    for target in targets:
        lines.append(f'    .{target["attr"]} = {{ {copy}(from_path, "{target["source_ref"]}") }}')
    return "\n".join(lines) + "\n"


def build_localization(files, rev):
    """recipe_path -> pre-change source the transforms read from: a parsed FTL
    Resource for .ftl, a {key: value} dict for legacy .properties."""
    resources = {}
    for f in files:
        raw = (
            file_at_rev(f"{rev}^", f["repo_path"])
            if rev
            else file_at_rev("HEAD", f["repo_path"])
        )
        if f["kind"] == "ftl":
            resources[f["recipe_path"]] = PARSER.parse(raw or "")
        else:
            resources[f["recipe_path"]] = parse_properties(raw)
    return resources


def verify_migration(ctx, migration):
    """Build the transform with transforms_from and evaluate it against the
    pre-change source. Returns (ok, detail)."""
    try:
        nodes = transforms_from(migration_template(migration), from_path=migration["src_path"])
    except Exception as exc:
        return False, f"transforms_from failed: {exc}"
    try:
        for node in nodes:
            evaluated = ctx.evaluate(node)
            patterns = ([evaluated.value] if evaluated.value else []) + [
                attr.value for attr in evaluated.attributes
            ]
            if not patterns:
                return False, "produced no value/attributes"
            for pattern in patterns:
                if not serialize_pattern(pattern).strip():
                    return False, "a reference did not resolve (empty result)"
    except Exception as exc:
        return False, f"evaluation failed: {exc}"
    return True, "reproduced from source"


def print_needs_attention(reports, legacy_pending):
    """Everything NOT auto-generated, so nothing is dropped silently. The cardinal
    rule: a string whose content changes must get a NEW identifier (the exception
    is an unchanged move to another file, which is auto-handled)."""
    warnings = [(r, e) for r in reports for e in r["same_id_changes"]]
    suggested = [(r, e) for r in reports for e in r["rename_suggested"]]
    broken = [(r, m) for r in reports for m in r["broken"]]
    ambiguous = [(r, nid) for r in reports for nid in r["ambiguous"]]
    if not (warnings or suggested or broken or ambiguous or legacy_pending):
        return

    print("\n" + "-" * 72)
    print("NEEDS ATTENTION (not auto-generated)")
    print("-" * 72)

    if warnings:
        print("\n!! WARNING - changed but kept its id; give it a NEW id, translate fresh:")
        print("   (otherwise every locale keeps the stale translation against the new English)")
        for r, e in warnings:
            kinds = ", ".join(f"{c['kind']} {c['attr'] or '(value)'}" for c in e["changes"])
            print(f"   {e['id']}  ({r['recipe_path']}) - {kinds}")

    if suggested:
        print("\nSUGGESTED rename - only attributes dropped/added with reused text;")
        print("rename it (the translations can then be carried) and add the COPY_PATTERN:")
        for _r, e in suggested:
            refs = ", ".join(f"{(t['attr'] or 'value')} <- {t['source_ref']}" for t in e["targets"])
            print(f"   {e['id']} -> {e['suggested_id']} (suggested): {refs}")

    if broken:
        print("\nRENAMED/MOVED but text changed - not migratable; translate fresh:")
        for _r, m in broken:
            print(f"   {m['src_id']} -> {m['new_id']}")

    if ambiguous:
        print("\nAMBIGUOUS content match - same text on several strings; resolve by hand:")
        for _r, nid in ambiguous:
            print(f"   {nid}")

    if legacy_pending:
        print("\nLEGACY .properties -> FTL - write COPY/REPLACE/PLURALS/CONCAT by hand")
        print("(%S/#1 placeholders, brand reference, or several keys combined):")
        for e in legacy_pending:
            print(f'   {e["key"]}  ({e["path"]})')
            print(f'       value: "{e["value"]}"')


def main():
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument("--rev", help="Analyze this revision against its parent (e.g. HEAD).")
    arg_parser.add_argument("--bug", help="Bug number for the docstring/filename.")
    arg_parser.add_argument("--description", help="Short description for the docstring.")
    arg_parser.add_argument("--output", help="Write the recipe to this path (otherwise print).")
    args = arg_parser.parse_args()

    files = collect_files(args.rev)
    if not files:
        print("No changed reference (.ftl/.properties under locales/en-US) files found.", file=sys.stderr)
        return 1
    reports, legacy_pending = analyze(files)

    migrations = [m for r in reports for m in r["migrations"]]
    if not migrations:
        print("No auto-migratable strings found.")
        print_needs_attention(reports, legacy_pending)
        return 0

    # Verify every generated transform offline, using the library's own evaluator.
    ctx = InternalContext("en-US")
    ctx.localization_resources = build_localization(files, args.rev)
    print("=" * 72)
    print("Verifying transforms against pre-change strings (fluent.migrate, offline)")
    print("=" * 72)
    all_ok = True
    for migration in migrations:
        ok, detail = verify_migration(ctx, migration)
        all_ok = all_ok and ok
        kind = "COPY" if migration["source_kind"] == "properties" else "COPY_PATTERN"
        src = ", ".join(migration["multi_source"]) if migration.get("multi_source") else migration["src_id"]
        print(f"  [{'OK  ' if ok else 'FAIL'}] {migration['new_id']:<40} {kind:<12} <- {src}")
        if not ok:
            print(f"         {detail}")
    if any(m.get("multi_source") for m in migrations):
        print("\n  Note: messages marked '<- a, b' are assembled from multiple source")
        print("  strings - confirm each pattern is sourced from the intended string.")

    recipe = emit_recipe_body(reports, bug=args.bug, description=args.description)

    out_path = args.output or f"bug_{args.bug or 'NUMBER'}_migration.py"
    print("\n" + "=" * 72)
    print("Validating recipe (fluent.migrate.validator.Validator)")
    print("=" * 72)
    try:
        Validator.validate(out_path, recipe)
        print("  [OK] recipe is structurally valid")
    except Exception as exc:
        all_ok = False
        print(f"  [FAIL] {type(exc).__name__}: {exc}")

    print_needs_attention(reports, legacy_pending)

    print("\n" + "=" * 72)
    if args.output and all_ok:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(recipe)
        print(f"Wrote {args.output}")
        print("Run `./mach fluent-migration-test` on it before submitting.")
    elif args.output:
        print("NOT written: a check failed above. Fix the references and re-run.")
    else:
        print("Generated recipe (pass --output PATH to write it):")
        print("=" * 72)
        print(recipe)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
