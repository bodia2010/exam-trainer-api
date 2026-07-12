#!/usr/bin/env python3
"""verify_content.py — audit an already-parsed course's content against its
source PDF's markdown, one section type at a time.

This is NOT the same check as e2e_import.py (structural validity — right
shape, right counts, no empty required fields) or promptfoo (prompt
regression testing on fixtures). This checks CONTENT FIDELITY: is what's
sitting in a real user's saved course actually a faithful, accurate
extraction of the real source, or did something get paraphrased, dropped,
mismatched, or hallucinated despite passing structural validation? That
class of bug (e.g. a paraphrased zu_erledigen, or Hören Teil 2 dialogue
turns attributed to the wrong speaker) is invisible to shape-only checks —
catching it previously meant a human comparing screenshots by eye.

Runs one Gemini call per section type present in the course JSON, giving
it the FULL source markdown plus that section type's extracted JSON, and
asking it to act as an independent auditor and list concrete discrepancies
(or confirm none). Deliberately does not reuse the app's own parse prompt
or model choice — an auditor that shares the same prompt/model as the
original extraction would tend to repeat the same blind spots.

Usage:
    GEMINI_API_KEY=... python3 scripts/verify_content.py \\
        --markdown /path/to/source.md \\
        --course-json /path/to/course.json \\
        --section-type telefonnotiz \\
        [--out report.md]

Omit --section-type to audit every section type present in the course
(one call each) — costs roughly (markdown tokens + that section's JSON
tokens) x input rate, x12 for a full course. Printed per-call before each
request so cost isn't a surprise mid-run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import generation_config  # noqa: E402

_GEMINI_URL_TMPL = (
    'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
)

# Independent of generation_config.model_for('discover') on purpose — see
# module docstring. Large context, needs to actually read the whole
# document reliably, so not the parse-side flash-lite either.
_AUDIT_MODEL = 'gemini-3.5-flash'

_AUDIT_PROMPT = """You are an independent fact-checker for a German B2 Beruf \
(telc) exam-prep app. Below is (1) the FULL source document the app was \
given, converted from PDF to Markdown, and (2) what the app's own \
extraction pipeline produced for ONE section type, as JSON.

Your job: verify the JSON is a faithful, accurate representation of what's \
actually printed in the source for this section type. Find every \
discrepancy — do not assume the extraction is correct.

Check specifically for:
- Paraphrased or reworded text where the source should have been copied \
verbatim (monologues, letter/passage text, answer-key fields).
- Wrong numbers: question counts, phone numbers, prices, dates, variant \
numbers.
- Content attributed to the wrong speaker, variant, or edition.
- Missing content: a variant/edition/field present in the source but \
absent from the JSON.
- Hallucinated content: anything in the JSON that isn't actually in the \
source at all.
- Wrong answers: a marked-correct option that doesn't match the source's \
own answer key.

A field containing the literal string "(nicht angegeben)" is NOT a bug — \
it's the app's own marker for a field that's genuinely blank in the \
source (e.g. an unfilled "Telefonnummer:" line). Only flag it as wrong if \
the source actually DOES contain a value there that was missed.

Report format: for each discrepancy found, one line: \
"<variant/item identifier>: <what's wrong, quoting source vs. extracted>". \
If you find nothing wrong after a careful check, respond with exactly: \
NO DISCREPANCIES FOUND

Do not comment on formatting, field ordering, or anything not listed \
above. Be concrete and quote exact text — "seems off" is not useful.

SECTION TYPE: {section_type}

SOURCE DOCUMENT (Markdown):
{markdown}

EXTRACTED JSON for this section type:
{section_json}
"""


def audit_section(
    markdown: str, section_type: str, section_json: list, api_key: str,
) -> str:
    prompt = _AUDIT_PROMPT.format(
        section_type=section_type,
        markdown=markdown,
        section_json=json.dumps(section_json, ensure_ascii=False, indent=2),
    )
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 0,
            'thinkingConfig': {'thinkingLevel': 'MEDIUM'},
        },
    }
    url = _GEMINI_URL_TMPL.format(model=_AUDIT_MODEL)
    resp = requests.post(url, params={'key': api_key}, json=payload, timeout=180)
    if resp.status_code != 200:
        return f'ERROR: Gemini request failed (HTTP {resp.status_code}): {resp.text[:500]}'
    data = resp.json()
    usage = data.get('usageMetadata') or {}
    print(
        f'    tokens: prompt={usage.get("promptTokenCount", 0)} '
        f'output={usage.get("candidatesTokenCount", 0)} '
        f'thoughts={usage.get("thoughtsTokenCount", 0)}',
        file=sys.stderr,
    )
    try:
        return data['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError):
        return f'ERROR: unexpected response shape: {json.dumps(data)[:500]}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--markdown', required=True, type=Path)
    parser.add_argument('--course-json', required=True, type=Path)
    parser.add_argument('--section-type', default=None,
                         help='Audit only this section type; omit to audit all present.')
    parser.add_argument('--out', type=Path, default=None,
                         help='Write the report here instead of stdout.')
    args = parser.parse_args()

    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        print('ERROR: set GEMINI_API_KEY in the environment.', file=sys.stderr)
        sys.exit(2)

    markdown = args.markdown.read_text(encoding='utf-8')
    course = json.loads(args.course_json.read_text(encoding='utf-8'))
    sections = course.get('sections', {})

    types_to_check = [args.section_type] if args.section_type else list(sections.keys())

    report_lines = [f'# Content verification — {course.get("title", "?")}', '']
    for section_type in types_to_check:
        if section_type not in sections:
            report_lines.append(f'## {section_type}\n\nSKIPPED — not present in course JSON.\n')
            continue
        section_json = sections[section_type]
        print(f'auditing {section_type} ({len(section_json)} items)...', file=sys.stderr)
        t0 = time.time()
        result = audit_section(markdown, section_type, section_json, api_key)
        print(f'  done in {time.time() - t0:.1f}s', file=sys.stderr)
        report_lines.append(f'## {section_type}\n\n{result}\n')

    report = '\n'.join(report_lines)
    if args.out:
        args.out.write_text(report, encoding='utf-8')
        print(f'Report written to {args.out}', file=sys.stderr)
    else:
        print(report)


if __name__ == '__main__':
    main()
