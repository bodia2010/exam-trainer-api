"""answer_markers.py — preserve the PDF's own yellow-highlight answer keys.

The source document's physical yellow highlight is more reliable than text
such as ``– 100%``: flattened columns and later corrections can put a textual
marker beside the wrong option.  Conversion therefore adds an explicit,
machine-readable ``[[PDF_CORRECT:<normalised option text>]]`` provenance
marker.  The parser prompt can use it, and the server deterministically repairs
a returned answer when exactly one of that question's options matches it.

Why this exists (PRODUCT_PLAN.md 2.3, step 2): the source PDF marks every
correct answer with a yellow highlight (a filled rect, fill color (1,1,0))
over the option line. MarkItDown drops all formatting on PDF->markdown
conversion, so for section types that carry no OTHER textual marker in the
source (beschwerde, lesen_teil4 — reading-comprehension questions with no
"– 100%" printed anywhere) the Gemini parse call never sees the answer and
has to guess — confirmed on a real document to get ~8% of those keys wrong
(scripts/check_answer_keys.py, 21/257 MISMATCH). This module runs the same
geometry extraction that script uses, but at convert()-time, and rewrites
the markdown itself rather than just auditing an already-parsed course.

This is NOT a reimplementation of scripts/check_answer_keys.py or
scripts/check_verbatim_content.py's matching logic — the geometry
extraction (`highlighted_options`, `_OPTION_RE`, `_YELLOW`,
`_MIN_HIGHLIGHT_HEIGHT`) is copied verbatim from check_answer_keys.py, and
the markdown-normalization / line-indexing (`_normalize`, `SourceIndex`) is
copied verbatim from check_verbatim_content.py, where both were already
debugged against this exact document's MarkItDown output (hyphenation
wraps, \\f page-break artifacts inflating split('\\n') vs splitlines(),
blank-line paragraph spacing — see each function's own docstring/comments
below for why). Copied rather than imported so this module has no
dependency on the scripts/ directory (offline-only tooling, not meant to
ship) and main.py's import stays a flat, single-file dependency PyMuPDF
aside.

Requires PyMuPDF (`pymupdf` — imported as `fitz`), added to
requirements.txt for this to work in the deployed function. Fits under
Vercel's package size limit alongside pdfminer.six only because main.py
no longer goes through the full markitdown package for PDF conversion —
markitdown's own onnxruntime+numpy weight (~126MB, pulled in solely for
ML-based file-type detection the pipeline never needed, since /api/convert
already knows its input is a PDF) was dropped in favor of calling
pdfminer.six directly for the exact same output (see main.py's convert()
route for the byte-identical-output reasoning).
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Geometry extraction — copied verbatim from scripts/check_answer_keys.py.
# ---------------------------------------------------------------------------

_OPTION_RE = re.compile(r'^\s*([a-f])\)\s*(.+)$', re.DOTALL)
_YELLOW = (1.0, 1.0, 0.0)
_MIN_HIGHLIGHT_HEIGHT = 5  # points; thinner filled rects are underlines

# A physical PDF highlight is represented separately from the source's own
# textual ``– 100%`` labels.  The latter can belong to another column or a
# later correction, so it must never suppress this authoritative provenance.
_PDF_CORRECT_PREFIX = '[[PDF_CORRECT:'
_PDF_CORRECT_RE = re.compile(r'\[\[PDF_CORRECT:\s*([^\]\r\n]+?)\s*\]\]')
_LEGACY_MARKER_RE = re.compile(r'[-–—]\s*100\s*%|\(\s*100\s*%\s*\)')
_INLINE_ANSWER_RE = re.compile(
    r'(?<!\d)(\d{1,3})\s*\(\s*([a-fA-F])\s*[-–—]\s*([^\)\r\n]+?)\s*\)'
)

# Below this many normalized characters, an option's text is too short/
# generic (e.g. a bare "Ja." or "Nein.") to safely locate in the markdown
# without risking a match against unrelated text elsewhere in the document —
# skip injecting a marker for it rather than risk marking the wrong line.
_MIN_MATCH_LEN = 6


def highlighted_options(pdf_path) -> dict[str, list[tuple[int, str]]]:
    """normalized option text -> [(page_no, raw_text), ...] for every
    yellow-highlighted option-shaped line in the PDF.

    Imports fitz lazily so importing this module (e.g. from main.py at
    process start) never fails just because PyMuPDF isn't installed in a
    given environment — only calling this function does.
    """
    import fitz  # PyMuPDF

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


# ---------------------------------------------------------------------------
# Normalization + line index — copied verbatim from
# scripts/check_verbatim_content.py (_normalize, SourceIndex), already
# debugged (5 rounds, per PRODUCT_PLAN.md) against this exact MarkItDown
# output's quirks. Do not "simplify" this — see each comment for the
# specific bug it fixes.
# ---------------------------------------------------------------------------

def _normalize(text: str, strip_trailing: bool = True) -> str:
    """Whitespace/diacritics-stable comparison key. PDF->markdown export and
    Gemini output differ in hyphenation wraps, quote styles and trailing
    punctuation — strip all of it so comparisons aren't thrown off by them.

    strip_trailing=False keeps a trailing '.'/'。' — needed when normalizing
    source.md ONE LINE AT A TIME before joining lines into the search blob
    (see SourceIndex): stripping each line's own sentence-ending period
    individually, then joining with single spaces, silently deletes every
    internal sentence boundary in reconstructed multi-line dialogue/
    monologue content — confirmed live, this alone made a genuine,
    verbatim-present dialogue register as HALLUCINATED, because the value
    being checked (normalized whole, trailing-stripped only ONCE at its
    own end) still had its internal periods while the blob it was being
    searched in had lost every one of them.

    Also strips "**" — prompts.py's own HEADINGS rule deliberately wraps
    a verbatim sub-heading in double asterisks as a structural annotation
    ON TOP OF the real text (see buildContentSpan client-side), which
    source.md itself never contains. Left in, every text starting with a
    bolded heading probed as HALLUCINATED purely because of "**", not
    because the actual words weren't found."""
    text = unicodedata.normalize('NFKC', text)
    text = text.replace('-\n', '').replace('\n', ' ')
    text = text.replace('**', '')
    text = re.sub(r'[„“”"«»\'’]', '', text)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text.rstrip('.。 ').strip() if strip_trailing else text


class SourceIndex:
    def __init__(self, source_md_text: str):
        # str.splitlines() also breaks on \r, \v, \f, \x1c-\x1e, \x85,
        # U+2028/U+2029 — MarkItDown's PDF extraction embeds literal form
        # feeds (\f, page-break artifacts) in real documents, which
        # silently inflated the line count (12771 vs. the real 12566) and
        # made every reported line number drift further off with depth
        # into the document. split('\n') matches every other line-based
        # convention already established in this codebase (main.py,
        # parse_service.dart) for exactly this reason.
        self.source_lines = source_md_text.split('\n')
        self.norm_lines = [_normalize(line, strip_trailing=False) for line in self.source_lines]
        offsets = []
        pos = 0
        blob_parts = []
        # Exactly one space between any two non-empty pieces, however many
        # blank lines (paragraph breaks) separate them in the source — a
        # naive "one line, one trailing space" join put TWO spaces around
        # a blank-line paragraph break (one from the heading line's own
        # slot, one from the blank line's), while a value's own "\n\n"
        # collapses to a single space under _normalize()'s \s+ -> ' '
        # rule. A heading immediately followed by a blank line before its
        # body paragraph — extremely common — mismatched on this alone.
        pending_space = False
        for i, nl in enumerate(self.norm_lines):
            offsets.append(pos)
            is_last = i == len(self.norm_lines) - 1
            # A line ending in '-' is a hard-wrapped word split across the
            # PDF's print line break (e.g. "Fir-" / "menausweis") — the
            # pipeline's own de-hyphenation rule (prompts.py Common rules)
            # already joins these into one word in every extracted field,
            # so building the search blob with a literal space in between
            # ("fir- menausweis") made every value spanning one of these
            # wraps mismatch the source it's actually taken from.
            hyphen_wrap = nl.endswith('-') and not is_last
            piece = nl[:-1] if hyphen_wrap else nl
            if piece:
                if pending_space:
                    blob_parts.append(' ')
                    pos += 1
                    pending_space = False
                blob_parts.append(piece)
                pos += len(piece)
            if not hyphen_wrap:
                pending_space = True
        self.offsets = offsets
        self.blob = ''.join(blob_parts)

    def line_for_offset(self, offset: int) -> int:
        """0-based source line index containing normalized blob offset."""
        return max(0, bisect.bisect_right(self.offsets, offset) - 1)


# ---------------------------------------------------------------------------
# Injection and deterministic post-parse repair.
# ---------------------------------------------------------------------------


def _pdf_correct_marker(normalized_option_text: str) -> str:
    return f'{_PDF_CORRECT_PREFIX}{normalized_option_text}]]'


def authoritative_option_texts(markdown: str) -> set[str]:
    """Return valid option texts marked by the PDF's physical highlight.

    A malformed/too-short marker is deliberately ignored.  This makes a bad
    conversion fail open to the model result rather than guessing a key from a
    partial technical annotation.
    """
    return {
        normalized
        for raw in _PDF_CORRECT_RE.findall(markdown)
        if len(normalized := _normalize(raw)) >= _MIN_MATCH_LEN
    }


def repair_answers_from_pdf_markers(parsed: object, markdown: str) -> int:
    """Correct choice answers only where one option has PDF provenance.

    The API response shape is intentionally unchanged: this mutates only an
    existing ``answer`` field in universal-schema ``questions``.  If marker
    matching is absent, malformed, ambiguous, or the model returned a shape
    outside that contract, the value is left untouched.
    """
    marked_texts = authoritative_option_texts(markdown)
    if not marked_texts or not isinstance(parsed, list):
        return 0

    # A text-only marker has no question/edition identifier.  It is safe for
    # deterministic repair only when that normalized option text occurs in
    # exactly one returned question.  The prompt may still use colocated
    # markers in ambiguous cases, but the server must not mutate another
    # question merely because it happens to reuse the same wording.
    option_occurrences: dict[str, int] = defaultdict(int)
    for item in parsed:
        if not isinstance(item, dict) or not isinstance(item.get('questions'), list):
            continue
        for question in item['questions']:
            if not isinstance(question, dict) or not isinstance(question.get('options'), list):
                continue
            seen_in_question: set[str] = set()
            for option in question['options']:
                if not isinstance(option, dict) or not isinstance(option.get('text'), str):
                    continue
                normalized = _normalize(option['text'])
                if normalized and normalized not in seen_in_question:
                    option_occurrences[normalized] += 1
                    seen_in_question.add(normalized)

    repaired = 0
    for item in parsed:
        if not isinstance(item, dict) or not isinstance(item.get('questions'), list):
            continue
        for question in item['questions']:
            if not isinstance(question, dict) or not isinstance(question.get('options'), list):
                continue
            matches: list[str] = []
            for option in question['options']:
                if not isinstance(option, dict):
                    continue
                letter = option.get('letter')
                text = option.get('text')
                if not isinstance(letter, str) or not isinstance(text, str):
                    continue
                normalized = _normalize(text)
                if normalized in marked_texts and option_occurrences[normalized] == 1:
                    matches.append(letter)
            if len(matches) == 1 and question.get('answer') != matches[0]:
                question['answer'] = matches[0]
                repaired += 1
    return repaired


def repair_sprachbausteine_inline_answers(parsed: object, markdown: str) -> int:
    """Apply explicit Teil-2 inline keys only when every edition agrees.

    Sprachbausteine Teil 2 embeds keys in its source text as
    ``55 (a - Mittlerweile befinden)``.  This fallback is intentionally
    called only by that section's endpoint path.  A question is repairable
    when each chunk contains at most one key for its number, all chunks that
    mention the number agree on the exact letter/text pair, and the returned
    question contains that same option.  Any conflict or malformed shape is
    a no-op.
    """
    if not isinstance(parsed, list):
        return 0

    per_number: dict[int, set[tuple[str, str]]] = defaultdict(set)
    conflicted_numbers: set[int] = set()
    for chunk in markdown.split('<<<ITEM>>>'):
        chunk_matches: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for raw_number, raw_letter, raw_text in _INLINE_ANSWER_RE.findall(chunk):
            normalized = _normalize(raw_text)
            if len(normalized) < _MIN_MATCH_LEN:
                continue
            chunk_matches[int(raw_number)].append((raw_letter.lower(), normalized))
        for number, matches in chunk_matches.items():
            if len(matches) != 1:
                conflicted_numbers.add(number)
                continue
            per_number[number].add(matches[0])

    authoritative = {
        number: next(iter(keys))
        for number, keys in per_number.items()
        if number not in conflicted_numbers and len(keys) == 1
    }
    if not authoritative:
        return 0

    repaired = 0
    for item in parsed:
        questions = item.get('questions') if isinstance(item, dict) else None
        if not isinstance(questions, list):
            continue
        number_counts: dict[int, int] = defaultdict(int)
        for question in questions:
            if isinstance(question, dict) and isinstance(question.get('number'), int):
                number_counts[question['number']] += 1
        for question in questions:
            if not isinstance(question, dict):
                continue
            number = question.get('number')
            if not isinstance(number, int) or number_counts[number] != 1:
                continue
            key = authoritative.get(number)
            options = question.get('options')
            if key is None or not isinstance(options, list):
                continue
            expected_letter, expected_text = key
            matches = [
                option
                for option in options
                if isinstance(option, dict)
                and option.get('letter') == expected_letter
                and isinstance(option.get('text'), str)
                and _normalize(option['text']) == expected_text
            ]
            if len(matches) == 1 and question.get('answer') != expected_letter:
                question['answer'] = expected_letter
                repaired += 1
    return repaired


def strip_pdf_correct_markers(value: object) -> object:
    """Remove technical provenance if Gemini echoed it into a JSON field."""
    if isinstance(value, str):
        cleaned = _PDF_CORRECT_RE.sub('', value)
        if cleaned == value:
            return value
        return re.sub(r'[ \t]{2,}', ' ', cleaned).strip()
    if isinstance(value, list):
        return [strip_pdf_correct_markers(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_pdf_correct_markers(item) for key, item in value.items()}
    return value


def _inject_legacy_answer_markers(
    pdf_path: str,
    markdown: str,
    *,
    strict: bool = False,
) -> str:
    """Reproduce the deployed v37 ``– 100%`` conversion byte for byte.

    Do not tighten this historical matching algorithm: its exact output is
    part of the legacy document-cache digest contract.  Safety improvements
    belong in the opt-in v38 format below.
    """
    try:
        marks = highlighted_options(pdf_path)
    except Exception as error:
        if strict:
            raise RuntimeError('legacy answer-marker extraction failed') from error
        print(f'ANSWER_MARKER_ERROR extract {type(error).__name__}: {error}')
        return markdown
    if not marks:
        return markdown

    idx = SourceIndex(markdown)
    lines = idx.source_lines
    injected = 0
    for norm_text, hits in marks.items():
        if len(norm_text) < _MIN_MATCH_LEN:
            continue
        budget = len(hits)
        marked_here = 0
        search_from = 0
        while marked_here < budget:
            pos = idx.blob.find(norm_text, search_from)
            if pos == -1:
                break
            end = pos + len(norm_text)
            search_from = pos + 1
            l0 = idx.line_for_offset(pos)
            l1 = idx.line_for_offset(max(pos, end - 1))
            if not _OPTION_RE.match(lines[l0].strip()):
                continue
            span_text = '\n'.join(lines[l0:l1 + 1])
            if _LEGACY_MARKER_RE.search(span_text):
                continue
            lines[l1] = lines[l1].rstrip() + ' – 100%'
            injected += 1
            marked_here += 1
    if injected:
        print(
            f'ANSWER_MARKERS injected={injected} '
            f'distinct_highlighted_options={len(marks)}'
        )
    return '\n'.join(lines)

def _inject_answer_markers(
    pdf_path: str,
    markdown: str,
    *,
    strict: bool = False,
) -> str:
    """Find every yellow-highlighted option in the PDF at `pdf_path` and, for
    each one that can be located in `markdown` on a genuine option-shaped
    line ("a) ...", "b) ...", ...), append an authoritative provenance
    marker carrying its exact normalized option text.

    Best-effort by design: PDF text extraction and MarkItDown's markdown
    are different representations of the same document, so not every
    highlight is expected to find a clean match (see the module docstring).
    Any failure to extract from the PDF at all (corrupt file, PyMuPDF
    missing) degrades to a no-op returning the markdown unchanged — this
    step is a quality improvement on top of the existing text-marker path,
    never a hard requirement for /api/convert to succeed.
    """
    try:
        marks = highlighted_options(pdf_path)
    except Exception as e:
        if strict:
            raise RuntimeError('v38 answer-marker extraction failed') from e
        print(f'ANSWER_MARKER_ERROR extract {type(e).__name__}: {e}')
        return markdown

    if not marks:
        return markdown

    idx = SourceIndex(markdown)
    lines = idx.source_lines  # mutated in place, then rejoined below
    injected = 0

    for norm_text, hits in marks.items():
        if len(norm_text) < _MIN_MATCH_LEN:
            continue
        # Cap how many markdown occurrences we mark to how many times this
        # exact option text was actually highlighted in the PDF (usually 1,
        # more when the same option wording repeats verbatim across several
        # editions of a variant) — bounds the risk of a short/generic option
        # text coincidentally matching an unrelated line elsewhere in the
        # document and getting over-marked.
        budget = len(hits)
        candidates: list[tuple[int, int]] = []
        search_from = 0
        while True:
            pos = idx.blob.find(norm_text, search_from)
            if pos == -1:
                break
            end = pos + len(norm_text)
            search_from = pos + 1
            l0 = idx.line_for_offset(pos)
            l1 = idx.line_for_offset(max(pos, end - 1))
            # Only inject onto genuine option-shaped markdown lines ("a) ...")
            # — the same shape check the PDF side was filtered through — so
            # a coincidental prose match elsewhere in the document is never
            # mistaken for an answer option.
            if not _OPTION_RE.match(lines[l0].strip()):
                continue
            candidates.append((l0, l1))

        # A source option repeated on several genuine option lines cannot be
        # safely matched back to only one physical highlight with our
        # text-only bridge.  Leave all of them untagged rather than risking
        # a deterministic but wrong repair.  Matching counts are safe: one
        # highlight per candidate, ordered as in the source document.
        if len(candidates) != budget:
            continue

        for l0, l1 in candidates:
            marker = _pdf_correct_marker(norm_text)
            span_text = '\n'.join(lines[l0:l1 + 1])
            if marker in span_text:
                continue
            # Do NOT use a generic "already has – 100%" guard here.  In a
            # two-column PDF extraction that marker can belong to the second
            # column; in a corrected exercise it can explicitly contradict
            # the physical highlight.  The provenance marker is distinct and
            # safe to append even in both cases.
            lines[l1] = lines[l1].rstrip() + f' {marker}'
            injected += 1

    if injected:
        print(f'ANSWER_MARKERS injected={injected} distinct_highlighted_options={len(marks)}')

    return '\n'.join(lines)
