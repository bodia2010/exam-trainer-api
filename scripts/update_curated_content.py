#!/usr/bin/env python3
"""update_curated_content.py — orchestrator for re-curating a document
after its source PDF changes (new variants added, existing ones edited).

Design choice, and why: an earlier version of this idea worked at the
chunk-hash level (recompute discover + chunking on the new PDF, compare
each chunk's hash against existing `v32|group|*` Redis keys — a hit means
"byte-identical to a chunk we've already curated, skip it"). That's the
architecturally "correct" approach (see PRODUCT_PLAN.md Phase 1), but
reproducing it here means re-deriving markitdown output, discover
boundaries, and anchor-correction exactly — three separate places today's
session found real, subtle bugs in (markitdown version drift, discover's
own "N/M" numbering inconsistency, line-mapping arithmetic). Diffing at
the ITEM level instead — comparing a freshly parsed course.json against
the previously curated one, matched by (section_type, variant_number,
version label) — needs none of that machinery, is far more robust, and
answers the actual question ("what's new or changed since we last
reviewed this document") just as well. The trade-off: it can't tell you
"this chunk's raw text is identical" for content that got reparsed into a
differently-shaped item (rare) — acceptable given the robustness gain.

USAGE:
    python3 scripts/update_curated_content.py \\
        --old-course /path/to/previously-curated-course.json \\
        --new-course /path/to/freshly-parsed-course.json \\
        --pdf /path/to/updated.pdf \\
        --source-md /path/to/updated-source.md \\
        --out-review /tmp/review_subset.json \\
        [--out-report /tmp/update_report.txt]

`--new-course` is produced however the document was actually (re-)parsed
— e2e_import.py against a real deployment, or the app itself; this script
doesn't parse anything or spend any Gemini budget itself.

Both course JSONs accept either the bare {section_type: [items]} shape
(as pulled directly from Redis, see PRODUCT_PLAN.md Phase 1) or the full
course.json shape with a top-level "sections" key — same convention as
check_answer_keys.py / check_verbatim_content.py.

Output: `--out-review` is a course.json-shaped file containing ONLY the
NEW or CHANGED items — feed it straight into check_answer_keys.py and
check_verbatim_content.py (same --course-json flag both already accept)
to get findings scoped to exactly what actually needs a human/LLM look,
without re-flagging the whole document's already-reviewed content every
time. A summary (reused vs. needs-review counts, per-item detail) goes
to --out-report / stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _sections(course: dict) -> dict:
    return course.get('sections') if isinstance(course.get('sections'), dict) else course


def _item_identity(section_type: str, item: dict) -> tuple:
    """(section_type, variant_number, version-or-label) — the same triple
    a human would use to say "this is the same exercise instance" across
    two parses of the same document. telefonnotiz nests editions under
    versions[] with their own 'label' instead of a top-level 'version'
    field (see response_schemas.py) — either one, if present, joins the
    identity so two DIFFERENT editions of the same variant_number aren't
    collapsed into one."""
    variant = item.get('variant_number')
    version = item.get('version')
    return (section_type, variant, version)


def _content_hash(item: dict) -> str:
    """Stable hash of an item's own content, independent of key order —
    two parses of identical source text should hash identically even if
    Gemini emitted the JSON's keys in a different order."""
    canonical = json.dumps(item, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def diff_courses(old_sections: dict, new_sections: dict):
    """Returns (reused, new_or_changed) — both lists of
    (section_type, item) tuples from new_sections."""
    old_by_identity: dict[tuple, dict] = {}
    for section_type, items in old_sections.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                old_by_identity[_item_identity(section_type, item)] = item

    reused = []
    changed = []
    for section_type, items in new_sections.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            identity = _item_identity(section_type, item)
            old_item = old_by_identity.get(identity)
            if old_item is not None and _content_hash(old_item) == _content_hash(item):
                reused.append((section_type, item))
            else:
                changed.append((section_type, item))
    return reused, changed


def build_review_course(changed: list[tuple[str, dict]]) -> dict:
    sections: dict[str, list] = {}
    for section_type, item in changed:
        sections.setdefault(section_type, []).append(item)
    return sections


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--old-course', required=True, type=Path)
    parser.add_argument('--new-course', required=True, type=Path)
    parser.add_argument('--out-review', required=True, type=Path)
    parser.add_argument('--out-report', type=Path, default=None)
    args = parser.parse_args()

    old_sections = _sections(json.loads(args.old_course.read_text(encoding='utf-8')))
    new_sections = _sections(json.loads(args.new_course.read_text(encoding='utf-8')))

    reused, changed = diff_courses(old_sections, new_sections)

    review = build_review_course(changed)
    args.out_review.write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')

    lines = [
        f'REUSED (byte-identical to a previously curated item): {len(reused)}',
        f'NEW OR CHANGED (needs review): {len(changed)}',
        '',
    ]
    by_type: dict[str, list] = {}
    for section_type, item in changed:
        by_type.setdefault(section_type, []).append(item)
    for section_type in sorted(by_type):
        items = by_type[section_type]
        labels = [f'variant {i.get("variant_number")}'
                  + (f' ({i["version"]})' if i.get('version') else '')
                  for i in items]
        lines.append(f'  {section_type}: {len(items)} item(s) — {", ".join(labels)}')

    lines.append('')
    lines.append(f'Review subset written to {args.out_review}')
    lines.append(
        'Next steps: run check_answer_keys.py and check_verbatim_content.py '
        'with --course-json pointed at that file (and the updated PDF / '
        'source.md) to get findings scoped to only this new/changed content. '
        'Patch and re-inject only the affected group-cache keys — everything '
        'in REUSED already has a valid v32|group|* entry from before and '
        'needs no action.')

    report = '\n'.join(lines)
    if args.out_report:
        args.out_report.write_text(report, encoding='utf-8')
    print(report, file=sys.stderr)


if __name__ == '__main__':
    main()
