#!/usr/bin/env python3
"""Generates the local test fixtures promptfoo evals read from.

For every section type, finds the variant_number with the MOST discovered
editions in the given PDF (the heaviest SEGMENTATION/DEDUPLICATION stress
case for that type) and saves it as fixtures/<type>.txt.

Not committed to git (see ../.gitignore) — the source PDF is copyrighted
exam-prep material, so neither it nor its extracted text belongs in a
public repo. Run this once locally whenever you need fresh fixtures:

    FIREBASE_ID_TOKEN=... python3 make_fixtures.py /path/to/your.pdf

The backend authenticates every request with a Firebase ID token now
(APP_SECRET was retired — see the 'replace shared APP_SECRET with
Firebase Auth' commit), not the old shared secret this script used to
send as X-App-Secret. Get a token by signing in through the app itself
and grabbing it, or via curl against the Identity Toolkit REST API:

    curl -s -X POST \\
      "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=<WEB_API_KEY>" \\
      -d '{"email":"you@example.com","password":"...","returnSecureToken":true}' \\
      | python3 -c "import json,sys; print(json.load(sys.stdin)['idToken'])"

A token is valid for about an hour — re-run that if this script starts
getting 401s partway through.
"""
import json
import os
import sys

import requests

API_BASE = os.environ.get('API_BASE_URL', 'https://exam-trainer-api.vercel.app')
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')

SECTION_TYPES = [
    'lesen_teil1', 'lesen_teil2', 'lesen_teil3', 'lesen_teil4',
    'beschwerde', 'sprachbausteine_teil1', 'sprachbausteine_teil2',
    'telefonnotiz', 'hoeren_teil1', 'hoeren_teil2', 'hoeren_teil3', 'hoeren_teil4',
]


def call(path, **kwargs):
    token = os.environ['FIREBASE_ID_TOKEN']
    headers = kwargs.pop('headers', {})
    headers['Authorization'] = f'Bearer {token}'
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

    print('Running discovery...')
    items = call('/api/parse', json={'markdown': numbered, 'section_type': 'discover'})
    items.sort(key=lambda x: x['start_line'])

    for section_type in SECTION_TYPES:
        by_variant = {}
        for i, it in enumerate(items):
            if it['section_type'] != section_type:
                continue
            start = max(0, min(it['start_line'], len(lines)))
            end = len(lines) if i + 1 >= len(items) else max(0, min(items[i + 1]['start_line'], len(lines)))
            if end <= start:
                continue
            chunk = '\n'.join(lines[start:end])
            by_variant.setdefault(it['variant_number'], []).append(chunk)

        if not by_variant:
            print(f'  {section_type}: no items found — skipping')
            continue

        variant_number, chunks = max(by_variant.items(), key=lambda kv: len(kv[1]))
        text = '\n\n<<<ITEM>>>\n\n'.join(chunks)
        fixture_path = os.path.join(FIXTURES_DIR, f'{section_type}.txt')
        with open(fixture_path, 'w') as f:
            f.write(text)
        print(f'  {section_type}: wrote {fixture_path} '
              f'(variant {variant_number}, {len(chunks)} editions, {len(text)} chars)')


if __name__ == '__main__':
    main()
