#!/usr/bin/env python3
"""Fail-closed, offline comparison of a fresh course with a trusted course.

This tool deliberately makes no network calls and has no dependency on a PDF,
LLM, cache, or deployment. It answers the prerequisite question before a
paid reparse can be accepted: did the newly parsed course preserve the exact
exercise identities and content of the trusted course?

Both input files may be either a full course object with a ``sections`` field
or a bare ``{section_type: [items]}`` mapping.  By default *every* difference
fails the gate.  ``--allow-metadata-drift`` is intentionally explicit and is
only useful while diagnosing generated TTS voice hints: it does not permit
an identity or exercise-payload difference.

Example:
    python3 scripts/offline_semantic_gate.py \
      --trusted trusted.json --fresh fresh.json --report semantic-report.json
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


# This is the only field intentionally ignored for the metadata-only
# comparison. It contains generated voice hints, not exercise content.
# Keeping this list narrow is important: a newly introduced field must fail
# until someone consciously classifies it.
_VOICE_HINT_KEYS = frozenset({'voice_gender', 'speaker_voice_genders'})


def sections_from_course(value: object) -> dict[str, list[dict[str, Any]]]:
    """Validate and return the supported course ``sections`` representation."""
    if not isinstance(value, dict):
        raise ValueError('course JSON must be an object')
    sections = value.get('sections') if isinstance(value.get('sections'), dict) else value
    result: dict[str, list[dict[str, Any]]] = {}
    for section_type, items in sections.items():
        if not isinstance(section_type, str) or not section_type:
            raise ValueError('every section type must be a non-empty string')
        if not isinstance(items, list):
            raise ValueError(f'{section_type}: section must be a list')
        if any(not isinstance(item, dict) for item in items):
            raise ValueError(f'{section_type}: every item must be an object')
        result[section_type] = items
    if not result or not any(result.values()):
        raise ValueError('course must contain at least one item')
    return result


def item_identity(section_type: str, item: dict[str, Any]) -> tuple[str, int, str | None] | None:
    """Return the stable item identity, or ``None`` if it cannot be trusted.

    ``version`` is legitimately nullable for most exercise types.  A missing
    ``variant_number`` is never a usable identity: treating it as zero or
    merging it with another missing value would hide discovery/parser drift.
    """
    variant = item.get('variant_number')
    if isinstance(variant, bool) or not isinstance(variant, int):
        return None
    version = item.get('version')
    if version is not None and not isinstance(version, str):
        return None
    return section_type, variant, version


def _identity_record(identity: tuple[str, int, str | None]) -> dict[str, Any]:
    section_type, variant_number, version = identity
    return {
        'section_type': section_type,
        'variant_number': variant_number,
        'version': version,
    }


def _identity_sort_key(identity: tuple[str, int, str | None]) -> tuple[str, int, int, str]:
    """Sort identities without comparing ``None`` to a version string."""
    section_type, variant_number, version = identity
    return section_type, variant_number, 0 if version is None else 1, version or ''


def _sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _without_voice_metadata(value: Any) -> Any:
    """Recursively remove only known generated voice-hint metadata fields."""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key == 'metadata' and isinstance(child, dict):
                remaining = {
                    metadata_key: _without_voice_metadata(metadata_value)
                    for metadata_key, metadata_value in child.items()
                    if metadata_key not in _VOICE_HINT_KEYS
                }
                if remaining:
                    result[key] = remaining
                continue
            result[key] = _without_voice_metadata(child)
        return result
    if isinstance(value, list):
        return [_without_voice_metadata(child) for child in value]
    return copy.deepcopy(value)


def _index_items(
    sections: dict[str, list[dict[str, Any]]],
    side: str,
) -> tuple[
    dict[tuple[str, int, str | None], dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    indexed: dict[tuple[str, int, str | None], dict[str, Any]] = {}
    missing: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    positions: dict[tuple[str, int, str | None], list[int]] = defaultdict(list)

    for section_type in sorted(sections):
        for item_index, item in enumerate(sections[section_type]):
            identity = item_identity(section_type, item)
            if identity is None:
                missing.append({
                    'side': side,
                    'section_type': section_type,
                    'item_index': item_index,
                    'reason': 'variant_number must be an integer and version must be string or null',
                })
                continue
            positions[identity].append(item_index)
            # Do not overwrite first item: we report duplicates and never use
            # their later content for a potentially misleading comparison.
            indexed.setdefault(identity, item)

    for identity, item_positions in positions.items():
        if len(item_positions) > 1:
            duplicates.append({
                'side': side,
                **_identity_record(identity),
                'item_indexes': item_positions,
            })
    return indexed, missing, duplicates


def compare_courses(trusted: object, fresh: object) -> dict[str, Any]:
    """Compare two course payloads and return a JSON-serialisable report.

    The report contains hashes only, never full exercise text, so it can be
    retained as a diagnostic artifact without copying the source course.
    """
    trusted_sections = sections_from_course(trusted)
    fresh_sections = sections_from_course(fresh)
    trusted_index, trusted_missing, trusted_duplicates = _index_items(trusted_sections, 'trusted')
    fresh_index, fresh_missing, fresh_duplicates = _index_items(fresh_sections, 'fresh')

    trusted_ids = set(trusted_index)
    fresh_ids = set(fresh_index)
    exact: list[dict[str, Any]] = []
    metadata_only: list[dict[str, Any]] = []
    payload_changed: list[dict[str, Any]] = []

    for identity in sorted(trusted_ids & fresh_ids, key=_identity_sort_key):
        trusted_item = trusted_index[identity]
        fresh_item = fresh_index[identity]
        trusted_hash = _sha256(trusted_item)
        fresh_hash = _sha256(fresh_item)
        record = {
            **_identity_record(identity),
            'trusted_sha256': trusted_hash,
            'fresh_sha256': fresh_hash,
        }
        if trusted_hash == fresh_hash:
            exact.append(record)
        elif _sha256(_without_voice_metadata(trusted_item)) == _sha256(_without_voice_metadata(fresh_item)):
            metadata_only.append(record)
        else:
            payload_changed.append(record)

    fresh_only = [
        _identity_record(identity)
        for identity in sorted(fresh_ids - trusted_ids, key=_identity_sort_key)
    ]
    trusted_only = [
        _identity_record(identity)
        for identity in sorted(trusted_ids - fresh_ids, key=_identity_sort_key)
    ]
    missing_identity = trusted_missing + fresh_missing
    duplicate_identities = trusted_duplicates + fresh_duplicates
    metrics = {
        'trusted_items': sum(len(items) for items in trusted_sections.values()),
        'fresh_items': sum(len(items) for items in fresh_sections.values()),
        'exact': len(exact),
        'metadata_only': len(metadata_only),
        'payload_changed': len(payload_changed),
        'fresh_only': len(fresh_only),
        'trusted_only': len(trusted_only),
        'duplicate_identities': len(duplicate_identities),
        'missing_identity': len(missing_identity),
        'identity_set_equal': trusted_ids == fresh_ids,
        'fresh_only_sections': len(set(fresh_sections) - set(trusted_sections)),
        'trusted_only_sections': len(set(trusted_sections) - set(fresh_sections)),
    }
    return {
        'schema_version': 1,
        'metrics': metrics,
        'exact': exact,
        'metadata_only': metadata_only,
        'payload_changed': payload_changed,
        'fresh_only': fresh_only,
        'trusted_only': trusted_only,
        'duplicate_identities': duplicate_identities,
        'missing_identity': missing_identity,
        'fresh_only_sections': sorted(set(fresh_sections) - set(trusted_sections)),
        'trusted_only_sections': sorted(set(trusted_sections) - set(fresh_sections)),
    }


def evaluate_gate(report: dict[str, Any], *, allow_metadata_drift: bool = False) -> list[str]:
    """Return stable fail reasons for a comparison report."""
    metrics = report['metrics']
    failures: list[str] = []
    for metric, label in (
        ('duplicate_identities', 'duplicate identity'),
        ('missing_identity', 'missing/invalid identity'),
        ('fresh_only_sections', 'fresh-only section'),
        ('trusted_only_sections', 'trusted-only section'),
        ('fresh_only', 'fresh-only identity'),
        ('trusted_only', 'trusted-only identity'),
        ('payload_changed', 'exercise payload drift'),
    ):
        count = metrics[metric]
        if count:
            plural = 'identities' if label == 'duplicate identity' and count != 1 else label
            failures.append(f'{count} {plural}')
    if metrics['metadata_only'] and not allow_metadata_drift:
        failures.append(f"{metrics['metadata_only']} voice metadata drift")
    return failures


def _load_course(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise ValueError(f'cannot read {path}: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f'invalid JSON in {path}: {exc}') from exc


def _write_error_report(path: Path, message: str, *, allow_metadata_drift: bool) -> None:
    """Best-effort report for malformed input, preserving fail-closed exit 2."""
    report = {
        'schema_version': 1,
        'policy': {'allow_metadata_drift': allow_metadata_drift},
        'gate': {'passed': False, 'failures': ['invalid input']},
        'error': message,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trusted', required=True, type=Path, help='trusted curated course JSON')
    parser.add_argument('--fresh', required=True, type=Path, help='freshly parsed course JSON')
    parser.add_argument('--report', required=True, type=Path, help='path for the JSON diagnostic report')
    parser.add_argument(
        '--allow-metadata-drift', action='store_true',
        help='diagnostic policy: do not fail only generated TTS voice-hint differences',
    )
    args = parser.parse_args(argv)
    try:
        report = compare_courses(_load_course(args.trusted), _load_course(args.fresh))
    except ValueError as exc:
        try:
            _write_error_report(
                args.report, str(exc), allow_metadata_drift=args.allow_metadata_drift)
        except OSError as report_error:
            print(f'ERROR: {exc}; also could not write report: {report_error}', file=sys.stderr)
        else:
            print(f'ERROR: {exc}; wrote fail-closed report to {args.report}', file=sys.stderr)
        return 2

    failures = evaluate_gate(report, allow_metadata_drift=args.allow_metadata_drift)
    report['policy'] = {'allow_metadata_drift': args.allow_metadata_drift}
    report['gate'] = {'passed': not failures, 'failures': failures}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    summary = report['metrics']
    print(
        'offline semantic gate: '
        f"exact={summary['exact']} metadata_only={summary['metadata_only']} "
        f"payload_changed={summary['payload_changed']} fresh_only={summary['fresh_only']} "
        f"trusted_only={summary['trusted_only']} duplicates={summary['duplicate_identities']} "
        f"missing_identity={summary['missing_identity']}"
    )
    if failures:
        print('FAIL: ' + '; '.join(failures), file=sys.stderr)
        return 1
    print('PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
