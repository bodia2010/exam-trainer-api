#!/usr/bin/env python3
"""check_answer_keys.py — deterministic answer-key audit against the PDF's
own visual marking (PRODUCT_PLAN.md 2.3, step 1).

The source PDF marks every correct answer with a yellow highlight (a
filled rect, fill color (1,1,0)) over the option line — including the
question types that carry no textual "– 100%" marker (beschwerde,
lesen_teil4). MarkItDown drops formatting, so the pipeline's Gemini calls
never see this and have to guess those keys; this script goes back to the
PDF and checks every extracted answer against the actual highlighting.
Unlike verify_content.py (an LLM auditor with run-to-run variance), this
is exact: rect geometry + text, no model involved.

Requires PyMuPDF, which is NOT in requirements.txt (server doesn't need
it) — run from a venv:
    <venv>/bin/python scripts/check_answer_keys.py \\
        --pdf "/path/to/source.pdf" \\
        --course-json /path/to/course.json

Method:
  1. Collect every yellow-highlighted text snippet in the PDF that looks
     like an option line ("a) ...", "b) ...", "c) ..."), normalized.
  2. For each multiple-choice question in the course JSON, look up which
     of ITS OWN option texts are in the highlighted set:
       - exactly one, and it's the marked answer  -> OK
       - exactly one, and it's a DIFFERENT option -> MISMATCH (real bug)
       - none                                     -> NO_MARK (that
         question may genuinely be unmarked in the source, or uses
         underline-only marking — needs eyes)
       - more than one                            -> AMBIGUOUS (same
         option text highlighted in several editions with different
         letters; needs eyes, not auto-decidable)
     Matching is by option TEXT, not letter — editions reshuffle letters
     for the same option texts, and text is what the highlight covers.

Scope: covers the universal-schema section types plus lesen_teil4 and
beschwerde style questions (anything shaped {text, options[], answer}).
hoeren_teil1's нested pairs and sprachbausteine letters use textual keys
that the pipeline already reads reliably; they're included where their
shape matches, skipped otherwise and counted in the summary.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF

_OPTION_RE = re.compile(r'^\s*([a-f])\)\s*(.+)$', re.DOTALL)
_YELLOW = (1.0, 1.0, 0.0)
_MIN_HIGHLIGHT_HEIGHT = 5  # points; thinner filled rects are underlines


def _normalize(text: str) -> str:
    """Whitespace/diacritics-stable comparison key for option text.
    PDF text extraction and Gemini output differ in hyphenation wraps,
    quote styles and trailing punctuation — strip all of it."""
    text = unicodedata.normalize('NFKC', text)
    text = text.replace('-\n', '').replace('\n', ' ')
    text = re.sub(r'[„“”"«»\'’]', '', text)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text.rstrip('.。 ').strip()


def highlighted_options(pdf_path: Path) -> dict[str, list[tuple[int, str]]]:
    """normalized option text -> [(page_no, raw_text), ...] for every
    yellow-highlighted option-shaped line in the PDF."""
    doc = fitz.open(pdf_path)
    found: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for pno in range(len(doc)):
        page = doc[pno]
        for dr in page.get_drawings():
            if dr.get('fill') != _YELLOW:
                continue
            rect = dr['rect']
            if rect.height <= _MIN_HIGHLIGHT_HEIGHT:
                continue
            text = page.get_text(clip=rect).strip()
            m = _OPTION_RE.match(text)
            if not m:
                continue
            found[_normalize(m.group(2))].append((pno, text.replace('\n', ' ')))
    return found


def iter_questions(sections: dict):
    """Yield (section_type, item_label, question_dict) for every
    {text, options[], answer}-shaped question in the course."""
    for section_type, items in sections.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            label = f'variant {item.get("variant_number", "?")}'
            # top-level questions, and versions[].questions for editioned types
            question_lists = []
            if isinstance(item.get('questions'), list):
                question_lists.append((label, item['questions']))
            for vi, v in enumerate(item.get('versions') or []):
                if isinstance(v, dict) and isinstance(v.get('questions'), list):
                    question_lists.append((f'{label} ed.{vi}', v['questions']))
            for qlabel, questions in question_lists:
                for q in questions:
                    if (isinstance(q, dict) and isinstance(q.get('options'), list)
                            and q.get('answer') is not None):
                        yield section_type, qlabel, q


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--course-json', required=True, type=Path)
    args = parser.parse_args()

    marks = highlighted_options(args.pdf)
    print(f'PDF: {len(marks)} distinct yellow-highlighted option texts\n',
          file=sys.stderr)

    course = json.loads(args.course_json.read_text(encoding='utf-8'))
    # Accept either the full course.json shape ({"sections": {...}, ...})
    # or the bare {section_type: [items]} dict — same convention as
    # check_verbatim_content.py / update_curated_content.py. Without this,
    # update_curated_content.py's --out-review (bare dict, by design —
    # see its build_review_course()) silently produced zero findings here:
    # course.get('sections', {}) returned {} for a dict with no top-level
    # "sections" key at all, despite the docstring above claiming this
    # exact file feeds straight into this script.
    sections = course.get('sections') if isinstance(course.get('sections'), dict) else course

    counts = defaultdict(int)
    for section_type, qlabel, q in iter_questions(sections):
        # Options come in two shapes across section types: plain strings,
        # or {letter, text} objects with the answer referencing `letter`.
        opts: list[tuple[str, str]] = []  # (letter-or-'', text)
        for o in q['options']:
            if isinstance(o, dict):
                opts.append((str(o.get('letter', '')).lower(), str(o.get('text', ''))))
            else:
                m = _OPTION_RE.match(str(o))
                opts.append((m.group(1), m.group(2)) if m else ('', str(o)))
        answer = str(q['answer']).strip()
        if len(answer) <= 2:  # letter form
            answer_text = next((t for l, t in opts if l == answer.lower()), '')
            if not answer_text and answer:  # positional fallback
                idx = ord(answer.lower()[0]) - ord('a')
                answer_text = opts[idx][1] if 0 <= idx < len(opts) else ''
        else:
            answer_text = answer
        hits = [t for _, t in opts if _normalize(t) in marks]
        qname = f'{section_type} / {qlabel} / Q{q.get("number", "?")} {str(q.get("text", ""))[:40]!r}'
        if len(hits) == 1:
            if _normalize(hits[0]) == _normalize(answer_text):
                counts['OK'] += 1
            else:
                counts['MISMATCH'] += 1
                print(f'MISMATCH  {qname}\n'
                      f'          extracted: {answer_text!r}\n'
                      f'          PDF marks: {hits[0]!r}')
        elif not hits:
            counts['NO_MARK'] += 1
        else:
            counts['AMBIGUOUS'] += 1
            print(f'AMBIGUOUS {qname}: {len(hits)} of its options highlighted '
                  f'somewhere in the PDF')

    print('\nSummary:', dict(counts))
    sys.exit(1 if counts['MISMATCH'] else 0)


if __name__ == '__main__':
    main()
