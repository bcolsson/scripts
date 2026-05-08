#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TERM="-thunderbird-brand-short-name"
VALUE="-thunderbird-brand-short-name = Thunderbird"
ADDED=0
SKIPPED=0
MISSING=0

for locale_dir in "$SCRIPT_DIR"/*/; do
    locale="$(basename "$locale_dir")"
    ftl="$locale_dir/toolkit/toolkit/branding/brandings.ftl"

    if [[ ! -f "$ftl" ]]; then
        ((MISSING++)) || true
        continue
    fi

    if grep -qF -- "$TERM" "$ftl"; then
        ((SKIPPED++)) || true
    else
        printf '\n%s\n' "$VALUE" >> "$ftl"
        echo "Added to: $locale"
        ((ADDED++)) || true
    fi
done

echo ""
echo "Done — added: $ADDED, already present: $SKIPPED, file missing: $MISSING"
