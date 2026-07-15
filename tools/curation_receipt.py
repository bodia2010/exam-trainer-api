"""Machine-readable proof that a curated course ran through the checklist.

This is an operational safety binding, not a cryptographic signature.  It
prevents accidentally injecting a different course/PDF than the pair that was
reviewed by ``scripts/curation_checklist.py``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


SCHEMA_VERSION = 1


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def build_receipt(
    *,
    course_value: str,
    pdf_bytes: bytes,
    source_markdown_bytes: bytes,
    report_text: str,
    review_items: int,
    deterministic_findings: int,
    llm_findings: int,
    checks: dict[str, str],
) -> dict:
    return {
        'schema_version': SCHEMA_VERSION,
        'course_value_sha256': _sha256(course_value.encode('utf-8')),
        'pdf_sha256': _sha256(pdf_bytes),
        'source_markdown_sha256': _sha256(source_markdown_bytes),
        'report_sha256': _sha256(report_text.encode('utf-8')),
        'review_items': review_items,
        'deterministic_findings': deterministic_findings,
        'llm_findings': llm_findings,
        'checks': checks,
    }


def write_receipt(path: Path, receipt: dict) -> None:
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def verify_receipt(
    path: Path,
    *,
    course_value: str,
    pdf_bytes: bytes,
    source_markdown_bytes: bytes,
    report_text: str,
) -> dict:
    try:
        receipt = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f'cannot read checklist receipt {path}: {exc}') from exc
    if not isinstance(receipt, dict) or receipt.get('schema_version') != SCHEMA_VERSION:
        raise ValueError(f'unsupported checklist receipt schema in {path}')
    required = {
        'course_value_sha256', 'pdf_sha256', 'source_markdown_sha256',
        'report_sha256', 'review_items', 'deterministic_findings',
        'llm_findings', 'checks',
    }
    missing = sorted(required - receipt.keys())
    if missing:
        raise ValueError(f'checklist receipt is missing fields: {missing}')

    course_hash = _sha256(course_value.encode('utf-8'))
    if receipt['course_value_sha256'] != course_hash:
        raise ValueError('checklist receipt course hash does not match --course')
    pdf_hash = _sha256(pdf_bytes)
    if receipt['pdf_sha256'] != pdf_hash:
        raise ValueError('checklist receipt PDF hash does not match --pdf')
    source_hash = _sha256(source_markdown_bytes)
    if receipt['source_markdown_sha256'] != source_hash:
        raise ValueError('checklist receipt source markdown hash does not match')
    report_hash = _sha256(report_text.encode('utf-8'))
    if receipt['report_sha256'] != report_hash:
        raise ValueError('checklist receipt report hash does not match')
    if not isinstance(receipt['checks'], dict):
        raise ValueError('checklist receipt checks must be an object')
    checks = receipt['checks']
    required_checks = {'diff', 'answer_keys', 'verbatim', 'llm'}
    missing_checks = sorted(required_checks - checks.keys())
    if missing_checks:
        raise ValueError(f'checklist receipt is missing checks: {missing_checks}')
    if checks['diff'] != 'completed':
        raise ValueError('checklist diff step did not complete')
    for name in ('answer_keys', 'verbatim'):
        if checks[name] not in ('completed', 'not-needed'):
            raise ValueError(f'checklist deterministic step {name} did not complete')
    return receipt
