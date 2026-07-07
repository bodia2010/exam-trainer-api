#!/usr/bin/env python3
"""Generates the local test fixtures promptfoo evals read from.

Not committed to git (see ../.gitignore) — the source PDF is copyrighted
exam-prep material, so neither it nor its extracted text belongs in a
public repo. Run this once locally whenever you need fresh fixtures:

    APP_SECRET=... python3 make_fixtures.py /path/to/your.pdf
"""
import json
import os
import sys

import requests

API_BASE = os.environ.get('API_BASE_URL', 'https://exam-trainer-api.vercel.app')
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')


def call(path, **kwargs):
    secret = os.environ['APP_SECRET']
    headers = kwargs.pop('headers', {})
    headers['X-App-Secret'] = secret
    resp = requests.post(f'{API_BASE}{path}', headers=headers, timeout=150, **kwargs)
    resp.raise_for_status()
    return resp.json()


def main():
    if len(sys.argv) != 2:
        sys.exit('usage: make_fixtures.py /path/to/exam.pdf')
    pdf_path = sys.argv[1]
    os.makedirs(FIXTURES_DIR, exist_ok=True)

    print('Converting PDF -> Markdown...')
    with open(pdf_path, 'rb') as f:
        pdf_bytes = f.read()
    markdown = call('/api/convert', data=pdf_bytes,
                     headers={'Content-Type': 'application/octet-stream'})['markdown']

    lines = markdown.split('\n')
    numbered = '\n'.join(f'{i:05d}: {l}' for i, l in enumerate(lines))
    discover_fixture = os.path.join(FIXTURES_DIR, 'discover_input.txt')
    with open(discover_fixture, 'w') as f:
        f.write(numbered)
    print(f'Wrote {discover_fixture} ({len(numbered)} chars)')

    print('Running discovery to locate a multi-edition hoeren_teil1 group...')
    items = call('/api/parse', json={'markdown': numbered, 'section_type': 'discover'})
    items.sort(key=lambda x: x['start_line'])

    by_variant = {}
    for i, it in enumerate(items):
        if it['section_type'] != 'hoeren_teil1':
            continue
        start = max(0, min(it['start_line'], len(lines)))
        end = len(lines) if i + 1 >= len(items) else max(0, min(items[i + 1]['start_line'], len(lines)))
        if end <= start:
            continue
        chunk = '\n'.join(lines[start:end])
        by_variant.setdefault(it['variant_number'], []).append(chunk)

    if not by_variant:
        sys.exit('No hoeren_teil1 items found in this PDF — cannot build the parse fixture.')
    heaviest = max(by_variant.items(), key=lambda kv: len(kv[1]))
    variant_number, chunks = heaviest
    text = '\n\n<<<ITEM>>>\n\n'.join(chunks)
    parse_fixture = os.path.join(FIXTURES_DIR, 'hoeren_teil1_variant3.txt')
    with open(parse_fixture, 'w') as f:
        f.write(text)
    print(f'Wrote {parse_fixture} (variant {variant_number}, {len(chunks)} editions, {len(text)} chars)')


if __name__ == '__main__':
    main()
