#!/usr/bin/env python3
"""Check Pontoon translation completion for a set of strings across locales.

For a given project, a set of human-readable string identifiers and a set of
locale codes, report — per locale — whether every requested string has an
approved translation. A locale is "complete" only when every requested string
has an approved translation; otherwise it is "incomplete" and the missing
identifiers are listed.

Strings are identified as ``<resource-path>/<entity-key>`` — the resource path
within the project followed by the entity key, e.g.::

    browser/browser/ipProtection.ftl/ipprotection-feature-introduction-link-text-privacy-1

Uses Pontoon's public REST API:
``GET /api/v2/entities/<project>/<resource>/<entity>/?include_translations=true``
which returns the entity's approved translations along with their locale codes.

Example:
    python check_translation_status.py \\
        --host https://pontoon.mozilla.org \\
        --project firefox \\
        --locales de fr ja \\
        --strings browser/browser/ipProtection.ftl/ipprotection-feature-introduction-link-text-privacy-1

Exit status: 0 if every locale is complete, 1 if any locale is incomplete,
2 on input error, 3 on transport error.
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable
from urllib.parse import quote

import requests


DEFAULT_HOST = "https://pontoon.mozilla.org"


def split_identifier(identifier: str) -> tuple[str, str]:
    """Split ``<resource-path>/<entity-key>`` into (resource_path, entity_key).

    The last slash-delimited segment is the entity key; everything before it is
    the resource path. Pontoon's REST API uses the same convention.
    """
    cleaned = identifier.strip().strip("/")
    if "/" not in cleaned:
        raise ValueError(
            f"identifier {identifier!r} must contain at least one '/' "
            "separating the resource path from the entity key"
        )
    resource, _, key = cleaned.rpartition("/")
    if not resource or not key:
        raise ValueError(f"identifier {identifier!r} has an empty resource or key")
    return resource, key


def fetch_entity(
    session: requests.Session,
    host: str,
    project_slug: str,
    resource_path: str,
    entity_key: str,
) -> dict:
    url = (
        f"{host.rstrip('/')}/api/v2/entities/"
        f"{quote(project_slug, safe='')}/"
        f"{quote(resource_path, safe='/')}/"
        f"{quote(entity_key, safe='')}/"
    )
    response = session.get(
        url,
        params={"include_translations": "true"},
        timeout=60,
    )
    if response.status_code == 404:
        raise LookupError(
            f"No entity at {resource_path}/{entity_key} in project "
            f"{project_slug!r} (HTTP 404 from {url})"
        )
    if response.status_code != 200:
        raise RuntimeError(
            f"{url} returned HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {exc}") from exc


def approved_locales(entity: dict) -> set[str]:
    """Return the set of locale codes that have an approved translation."""
    codes: set[str] = set()
    for translation in entity.get("translations") or []:
        locale = translation.get("locale") or {}
        code = locale.get("code")
        if code:
            codes.add(code)
    return codes


def read_lines(path: str) -> list[str]:
    out: list[str] = []
    with open(path) as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Pontoon base URL (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project slug, e.g. 'firefox'.",
    )

    loc = parser.add_mutually_exclusive_group(required=True)
    loc.add_argument(
        "--locales",
        nargs="+",
        help="One or more locale codes (e.g. de fr ja)",
    )
    loc.add_argument(
        "--locales-file",
        help="Path to a file with one locale code per line",
    )

    strs = parser.add_mutually_exclusive_group(required=True)
    strs.add_argument(
        "--strings",
        nargs="+",
        help="One or more string identifiers of the form '<resource>/<key>'",
    )
    strs.add_argument(
        "--strings-file",
        help="Path to a file with one '<resource>/<key>' identifier per line",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the unapproved string identifiers for incomplete locales",
    )
    return parser.parse_args(list(argv))


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)

    raw_ids = list(args.strings) if args.strings else read_lines(args.strings_file)
    if not raw_ids:
        print("No string identifiers provided.", file=sys.stderr)
        return 2

    locales = args.locales if args.locales else read_lines(args.locales_file)
    if not locales:
        print("No locales provided.", file=sys.stderr)
        return 2

    try:
        parsed = [(identifier, *split_identifier(identifier)) for identifier in raw_ids]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    session = requests.Session()

    # approved_by_id[identifier] = set of locale codes with an approved translation.
    approved_by_id: dict[str, set[str]] = {}
    for identifier, resource, key in parsed:
        try:
            entity = fetch_entity(session, args.host, args.project, resource, key)
        except LookupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except (requests.RequestException, RuntimeError) as exc:
            print(f"error fetching {identifier!r}: {exc}", file=sys.stderr)
            return 3
        approved_by_id[identifier] = approved_locales(entity)

    any_incomplete = False
    total = len(parsed)
    for locale in locales:
        missing = [
            identifier
            for identifier, _, _ in parsed
            if locale not in approved_by_id[identifier]
        ]
        done = total - len(missing)
        if not missing:
            print(f"{locale}: complete ({done}/{total})")
        else:
            any_incomplete = True
            print(f"{locale}: incomplete ({done}/{total} approved)")
            if args.verbose:
                for identifier in missing:
                    print(f"  missing: {identifier}")

    return 1 if any_incomplete else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
