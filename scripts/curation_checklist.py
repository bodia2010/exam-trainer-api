#!/usr/bin/env python3
"""curation_checklist.py — single-command release checklist for updating
curated content after a source PDF changes.

Orchestrates the four tools built for this workflow, in order, WITHOUT
re-implementing any of their logic — each is imported as a module and its
actual functions/`main()` are called in-process (no subprocess):

  1. update_curated_content.py — diff old vs. new course.json (matched by
     section_type + variant_number + version/label, content-hashed) to
     find exactly what's new or changed. Produces the review-subset that
     every later step is scoped to, so already-reviewed content never
     gets re-flagged.
  2. check_answer_keys.py — deterministic: review subset's marked answers
     vs. the PDF's own yellow-highlight geometry. Needs PyMuPDF (fitz),
     which is NOT in requirements.txt — if it's not importable in the
     interpreter running this script, step 2 is SKIPPED with a note
     rather than aborting the whole checklist (steps 1/3/4 are still
     useful on their own). Run with a venv that has PyMuPDF to include it
     (see check_answer_keys.py's own docstring).
  3. check_verbatim_content.py — deterministic: review subset's verbatim
     fields vs. source.md, checked for hallucination / truncated-at-slash.
     Run with --context-course-json pointed at the FULL new course, not
     the narrow review subset — a value's sibling edition (needed to tell
     a genuine truncation from an expected "edition A / edition B" split)
     can easily live outside the subset if it wasn't itself changed. See
     check_verbatim_content.py's own docstring for the confirmed false
     positive this avoids.
  4. verify_content.py --runs 3 — LLM audit of the review subset, with
     verify_content.py's own multi-run cross-run consensus (a single LLM
     audit run is NOT reliable — see that script's docstring for the
     confirmed same-input-different-output case that motivated this).

Consolidated output: one readable text report with a top-level summary
(reused vs. review-scope counts, plus total content questions split by
confidence — DETERMINISTIC finding = a real, geometry/text-match-checked
bug with no model involved; LLM-only finding = needs a human look before
acting on it, consensus or not) followed by each tool's own findings in
full, under its own section.

Deliberately does NOT patch, re-inject, or write back anything — read-
only from the API's/course's point of view, produces a report for a
human to act on. This is intentional, not a missing feature: yesterday's
curation session found that even the "deterministic" checks here caught
real bugs IN THEMSELVES five separate times while being built (see each
script's own docstring/comments for the specific false positives) —
trusting any of this enough to auto-patch cached content unattended
would be a mistake.

USAGE:
    GEMINI_API_KEY=... python3 scripts/curation_checklist.py \\
        --old-course /path/to/previously-curated-course.json \\
        --new-course /path/to/freshly-parsed-course.json \\
        --pdf /path/to/updated.pdf \\
        --source-md /path/to/updated-source.md \\
        [--out /tmp/checklist_report.txt] \\
        [--out-review /tmp/review_subset.json] \\
        [--runs 3]

Both course JSONs accept either the bare {section_type: [items]} shape or
the full course.json shape with a top-level "sections" key — same
convention as the four tools this wraps.

If GEMINI_API_KEY isn't set, step 4 is skipped with a note (steps 1-3
still run and still produce a useful report — they're free/local).

SELF-TESTING without a real "new" PDF/document: same approach as
update_curated_content.py's own self-test convention — take a real
--old-course, make a copy as --new-course with 1-2 items deliberately
edited or added, and point --pdf/--source-md at the SAME source used to
build --old-course. The review scope should come out narrow (just the
deliberately-changed items), and steps 2-4 should run against exactly
that narrow scope, not the whole course.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import os
import sys
import tempfile
from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


update_curated_content = _load_module('_ucc_curation_checklist', 'update_curated_content.py')
check_verbatim_content = _load_module('_cvc_curation_checklist', 'check_verbatim_content.py')
verify_content = _load_module('_vc_curation_checklist', 'verify_content.py')

try:
    check_answer_keys = _load_module('_cak_curation_checklist', 'check_answer_keys.py')
    _FITZ_IMPORT_ERROR = None
except ImportError as e:  # PyMuPDF not installed in this interpreter
    check_answer_keys = None
    _FITZ_IMPORT_ERROR = f'{type(e).__name__}: {e}'


def _run_module_main(module, argv: list[str]) -> tuple[int, str, str]:
    """Call `module.main()` in-process with a synthetic sys.argv, capturing
    everything it prints (findings + summary) and its sys.exit() code.
    Reuses each tool's own logic completely unmodified — same interpreter,
    same imported module, no subprocess, no re-implementation."""
    old_argv = sys.argv
    out, err = io.StringIO(), io.StringIO()
    code = 0
    sys.argv = [getattr(module, '__name__', 'tool')] + argv
    try:
        with redirect_stdout(out), redirect_stderr(err):
            module.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    finally:
        sys.argv = old_argv
    return code, out.getvalue(), err.getvalue()


def _parse_summary_dict(text: str) -> dict:
    """check_answer_keys.py / check_verbatim_content.py both end with
    `print('\\nSummary:', dict(counts))` — pull that dict back out of the
    captured stdout instead of re-deriving the counts ourselves."""
    import re
    m = re.search(r'^Summary: (\{.*\})\s*$', text, re.MULTILINE)
    if not m:
        return {}
    try:
        return ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return {}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--old-course', required=True, type=Path)
    parser.add_argument('--new-course', required=True, type=Path)
    parser.add_argument('--pdf', required=True, type=Path)
    parser.add_argument('--source-md', required=True, type=Path)
    parser.add_argument('--out', type=Path, default=None,
                         help='Write the consolidated report here instead of stdout.')
    parser.add_argument('--out-review', type=Path, default=None,
                         help='Keep the review-subset JSON here instead of a throwaway temp file.')
    parser.add_argument('--runs', type=int, default=3,
                         help='verify_content.py --runs for the step 4 LLM audit (default 3).')
    args = parser.parse_args()
    if args.runs < 1:
        parser.error('--runs must be >= 1')

    blocks: list[str] = []

    # -------------------------------------------------------------------
    # Step 1: update_curated_content — diff old vs new course -> review
    # subset. Calling its functions directly (not main()) since we need
    # the review dict itself in-memory, not just its printed report.
    # -------------------------------------------------------------------
    old_sections = update_curated_content._sections(
        json.loads(args.old_course.read_text(encoding='utf-8')))
    new_sections = update_curated_content._sections(
        json.loads(args.new_course.read_text(encoding='utf-8')))
    reused, changed = update_curated_content.diff_courses(old_sections, new_sections)
    review = update_curated_content.build_review_course(changed)

    if args.out_review:
        review_path = args.out_review
    else:
        fd, tmp_name = tempfile.mkstemp(prefix='curation_review_', suffix='.json')
        os.close(fd)
        review_path = Path(tmp_name)
    review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8')

    by_type: dict[str, list] = defaultdict(list)
    for section_type, item in changed:
        by_type[section_type].append(item)

    step1 = [
        'STEP 1 — update_curated_content: diff old vs new course',
        '=' * 72,
        f'REUSED (byte-identical to a previously curated item, not re-reviewed): {len(reused)}',
        f'NEW OR CHANGED (in review scope below): {len(changed)}',
    ]
    for section_type in sorted(by_type):
        items = by_type[section_type]
        labels = [f'variant {i.get("variant_number")}'
                  + (f' ({i["version"]})' if i.get('version') else '') for i in items]
        step1.append(f'  {section_type}: {len(items)} item(s) — {", ".join(labels)}')
    step1.append(f'Review subset written to: {review_path}')
    blocks.append('\n'.join(step1))

    if not changed:
        blocks.append('Review scope is empty — nothing changed, skipping steps 2-4.')
        _emit(blocks, args.out, header=args.new_course.name)
        return

    # -------------------------------------------------------------------
    # Step 2: check_answer_keys — deterministic, review subset vs PDF.
    # -------------------------------------------------------------------
    step2 = ['', 'STEP 2 — check_answer_keys: deterministic answer-key audit '
             '(PDF highlight geometry vs extracted answers)', '=' * 72]
    answer_key_summary: dict = {}
    if check_answer_keys is None:
        step2.append(f'SKIPPED — PyMuPDF (fitz) not importable in this interpreter: '
                     f'{_FITZ_IMPORT_ERROR}')
        step2.append('Rerun this checklist with a venv that has PyMuPDF installed to '
                     'include this check (see check_answer_keys.py\'s own docstring).')
    else:
        code, out, err = _run_module_main(
            check_answer_keys, ['--pdf', str(args.pdf), '--course-json', str(review_path)])
        answer_key_summary = _parse_summary_dict(out)
        step2.append(out.strip() or '(no output)')
        if err.strip():
            step2.append('--- stderr ---')
            step2.append(err.strip())
        step2.append(f'(exit code {code})')
    blocks.append('\n'.join(step2))

    # -------------------------------------------------------------------
    # Step 3: check_verbatim_content — deterministic, review subset vs
    # source.md, with the FULL new course as --context-course-json (not
    # the narrow subset — see module docstring for why).
    # -------------------------------------------------------------------
    step3 = ['', 'STEP 3 — check_verbatim_content: deterministic verbatim/'
             'hallucination audit', '=' * 72]
    code, out, err = _run_module_main(check_verbatim_content, [
        '--course-json', str(review_path),
        '--context-course-json', str(args.new_course),
        '--source-md', str(args.source_md),
    ])
    verbatim_summary = _parse_summary_dict(out)
    step3.append(out.strip() or '(no output)')
    if err.strip():
        step3.append('--- stderr ---')
        step3.append(err.strip())
    step3.append(f'(exit code {code})')
    blocks.append('\n'.join(step3))

    # -------------------------------------------------------------------
    # Step 4: verify_content — LLM audit, --runs N, cross-run consensus.
    # Calls audit_section()/aggregate_findings()/format_consensus_report()
    # directly (same functions main() itself uses) so we get both the
    # structured counts AND the human-readable text without re-deriving
    # either.
    # -------------------------------------------------------------------
    step4 = ['', f'STEP 4 — verify_content: LLM audit, {args.runs} run(s) per '
             'section type, cross-run consensus', '=' * 72]
    api_key = os.environ.get('GEMINI_API_KEY', '')
    llm_high = llm_low = 0
    if not api_key:
        step4.append('SKIPPED — GEMINI_API_KEY not set in the environment.')
    else:
        markdown = args.source_md.read_text(encoding='utf-8')
        for section_type in sorted(review.keys()):
            section_json = review[section_type]
            print(f'[curation_checklist] auditing {section_type} '
                  f'({len(section_json)} item(s)) — {args.runs} run(s)...', file=sys.stderr)
            run_results = [
                verify_content.audit_section(markdown, section_type, section_json, api_key)
                for _ in range(args.runs)
            ]
            if args.runs == 1:
                text = run_results[0]
                groups: list[dict] = []
            else:
                text = verify_content.format_consensus_report(run_results, args.runs)
                valid = [r for r in run_results if not r.strip().startswith('ERROR:')]
                groups = verify_content.aggregate_findings(valid)
            llm_high += sum(1 for g in groups if g['count'] >= 2)
            llm_low += sum(1 for g in groups if g['count'] == 1)
            step4.append(f'## {section_type}\n\n{text}\n')
    blocks.append('\n'.join(step4))

    # -------------------------------------------------------------------
    # Consolidated summary.
    # -------------------------------------------------------------------
    mismatch = answer_key_summary.get('MISMATCH', 0)
    ambiguous = answer_key_summary.get('AMBIGUOUS', 0)
    halluc = verbatim_summary.get('HALLUCINATED', 0)
    trunc = verbatim_summary.get('TRUNCATED_AT_SLASH', 0)
    deterministic_total = mismatch + halluc + trunc
    llm_total = llm_high + llm_low

    summary = [
        'SUMMARY', '=' * 72,
        f'Reused (untouched, not re-reviewed): {len(reused)} item(s)',
        f'In review scope (new/changed): {len(changed)} item(s)',
        '',
        'Confirmed by DETERMINISTIC checks (geometry/text-match, no model involved):',
        f'  answer-key MISMATCH: {mismatch}'
        + (f'  (+ {ambiguous} AMBIGUOUS, needs eyes)' if ambiguous else ''),
        f'  verbatim HALLUCINATED: {halluc}',
        f'  verbatim TRUNCATED_AT_SLASH: {trunc}',
        f'  subtotal: {deterministic_total}',
        '',
        f'LLM-suspected only (verify_content.py, {args.runs}-run consensus):',
        f'  consensus findings (>=2/{args.runs} runs agree): {llm_high}',
        f'  low-confidence (1 run only): {llm_low}',
        f'  subtotal: {llm_total}',
        '',
        f'TOTAL open content questions: {deterministic_total + llm_total}',
        '',
        'Nothing was patched automatically. Deterministic findings above are '
        'real, checked bugs and safe to act on directly; LLM findings (step '
        '4) — consensus or not — need a human look before touching any '
        'cached content.',
    ]
    blocks.insert(0, '\n'.join(summary))
    _emit(blocks, args.out, header=args.new_course.name)


def _emit(blocks: list[str], out_path: Path | None, header: str) -> None:
    text = f'# Curation checklist — {header}\n\n' + '\n\n'.join(blocks)
    if out_path:
        out_path.write_text(text, encoding='utf-8')
        print(f'Report written to {out_path}', file=sys.stderr)
    else:
        print(text)


if __name__ == '__main__':
    main()
