#!/usr/bin/env python3
"""Inspect or migrate one curated whole-document cache entry.

The script deliberately defaults to a read-only dry run.  With no
``--course`` it copies the already-curated value from the source cache
version; this is the safest cache-version bump because the document hash and
the client-facing ``sections`` contract are unchanged.  A write requires the
explicit ``--apply`` flag and is verified by reading the target back.

Examples::

    python3 tools/inject_curated.py --pdf /path/to/source.pdf
    python3 tools/inject_curated.py --pdf /path/to/source.pdf --apply
    python3 tools/inject_curated.py --pdf /path/to/source.pdf \
        --course /path/to/course.json --apply

Redis credentials are read only from ``UPSTASH_REDIS_REST_URL`` and
``UPSTASH_REDIS_REST_TOKEN``.  Never pass credentials on the command line.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import pdfminer.high_level
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from answer_markers import _inject_answer_markers  # noqa: E402


DEFAULT_DISCOVER_VERSION = 'v30'
DEFAULT_SOURCE_PARSE_VERSION = 'v34'
DEFAULT_TARGET_PARSE_VERSION = 'v35'


def convert_pdf_to_markdown(pdf_path: Path) -> str:
    """Mirror production ``/api/convert`` byte for byte.

    Keep these three operations in sync with ``main.convert``: pdfminer
    extraction, MarkItDown-compatible whitespace normalization, then answer
    marker injection from the PDF's highlight/underline annotations.
    """
    # answer_markers intentionally fails open in the user-facing conversion
    # route so one optional enhancement cannot make PDF import unavailable.
    # A cache migration must do the opposite: silently omitting markers would
    # compute a different document hash and migrate the wrong key.
    try:
        import fitz  # noqa: F401
    except ImportError as error:
        raise RuntimeError(
            'PyMuPDF is required to reproduce production markdown exactly; '
            'install pymupdf==1.28.0') from error

    with pdf_path.open('rb') as pdf_file:
        raw_text = pdfminer.high_level.extract_text(pdf_file)
    text = '\n'.join(line.rstrip() for line in re.split(r'\r?\n', raw_text))
    text = re.sub(r'\n{3,}', '\n\n', text)
    return _inject_answer_markers(str(pdf_path), text)


def document_digest(markdown: str) -> str:
    return hashlib.sha256(f'doc|{markdown}'.encode('utf-8')).hexdigest()


def document_key(discover_version: str, parse_version: str, digest: str) -> str:
    return f'{discover_version}.{parse_version}|doc|{digest}'


def _sections(course: object) -> dict:
    if not isinstance(course, dict):
        raise ValueError('course JSON must be an object')
    sections = course.get('sections')
    if isinstance(sections, dict):
        return sections
    return course


def validate_sections(value: object) -> dict:
    sections = _sections(value)
    if not sections:
        raise ValueError('sections must not be empty')
    for section_type, items in sections.items():
        if not isinstance(section_type, str) or not section_type:
            raise ValueError('every section key must be a non-empty string')
        if not isinstance(items, list):
            raise ValueError(f'{section_type}: section value must be a list')
        if any(not isinstance(item, dict) for item in items):
            raise ValueError(f'{section_type}: every item must be an object')
    return sections


def serialized_sections(course_path: Path) -> str:
    course = json.loads(course_path.read_text(encoding='utf-8'))
    sections = validate_sections(course)
    return json.dumps(sections, ensure_ascii=False, separators=(',', ':'))


class UpstashRedis:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip('/')
        self.headers = {'Authorization': f'Bearer {token}'}

    def get(self, key: str) -> str | None:
        response = requests.get(
            f'{self.url}/get/{quote(key, safe="")}',
            headers=self.headers,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('error'):
            raise RuntimeError(f'Redis GET failed: {payload["error"]}')
        return payload.get('result')

    def set(self, key: str, value: str) -> None:
        response = requests.post(
            f'{self.url}/set/{quote(key, safe="")}',
            headers=self.headers,
            data=value.encode('utf-8'),
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('result') != 'OK':
            raise RuntimeError(f'Redis SET failed: {payload!r}')


def _value_summary(value: str | None) -> str:
    if value is None:
        return 'MISS'
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
    return f'HIT bytes={len(value.encode("utf-8"))} sha256={digest}'


def migrate(
    redis: UpstashRedis,
    source_key: str,
    target_key: str,
    *,
    supplied_value: str | None,
    apply: bool,
) -> str:
    source_value = supplied_value if supplied_value is not None else redis.get(source_key)
    if source_value is None:
        raise RuntimeError(f'source cache entry is missing: {source_key}')
    validate_sections(json.loads(source_value))

    target_value = redis.get(target_key)
    print(f'source: {_value_summary(source_value)}')
    print(f'target: {_value_summary(target_value)}')

    if target_value is not None:
        validate_sections(json.loads(target_value))
        if target_value != source_value:
            raise RuntimeError(
                'target exists with different content; refusing to overwrite it')
        print('target already matches source; no write needed')
        return 'already-current'

    if not apply:
        print('dry-run: target is absent and would be created')
        return 'dry-run'

    redis.set(target_key, source_value)
    read_back = redis.get(target_key)
    if read_back != source_value:
        raise RuntimeError('target read-back did not match the written value')
    validate_sections(json.loads(read_back))
    print(f'write verified: {_value_summary(read_back)}')
    return 'written'


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--course', type=Path, help='optional course JSON; otherwise copy source Redis value')
    parser.add_argument('--discover-version', default=DEFAULT_DISCOVER_VERSION)
    parser.add_argument('--source-parse-version', default=DEFAULT_SOURCE_PARSE_VERSION)
    parser.add_argument('--target-parse-version', default=DEFAULT_TARGET_PARSE_VERSION)
    parser.add_argument(
        '--source-key',
        help='exact legacy Redis key when the production conversion hash changed',
    )
    parser.add_argument('--expected-pdf-sha256', help='refuse a different source PDF')
    parser.add_argument('--apply', action='store_true', help='write and verify the target key')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.pdf.is_file():
        raise SystemExit(f'PDF not found: {args.pdf}')
    pdf_hash = hashlib.sha256(args.pdf.read_bytes()).hexdigest()
    if args.expected_pdf_sha256 and pdf_hash != args.expected_pdf_sha256.lower():
        raise SystemExit(
            f'PDF sha256 mismatch: expected {args.expected_pdf_sha256}, got {pdf_hash}')

    markdown = convert_pdf_to_markdown(args.pdf)
    digest = document_digest(markdown)
    source_key = args.source_key or document_key(
        args.discover_version, args.source_parse_version, digest)
    target_key = document_key(args.discover_version, args.target_parse_version, digest)
    print(f'pdf_sha256: {pdf_hash}')
    print(f'markdown_chars: {len(markdown)}')
    print(f'source_key: {source_key}')
    print(f'target_key: {target_key}')

    supplied_value = serialized_sections(args.course) if args.course else None
    url = os.environ.get('UPSTASH_REDIS_REST_URL', '')
    token = os.environ.get('UPSTASH_REDIS_REST_TOKEN', '')
    if not url or not token:
        if args.apply:
            raise SystemExit('Redis credentials are required for --apply')
        print('offline dry-run: Redis credentials are unavailable; keys computed only')
        return 0

    migrate(
        UpstashRedis(url, token),
        source_key,
        target_key,
        supplied_value=supplied_value,
        apply=args.apply,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
