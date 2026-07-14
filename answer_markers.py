"""answer_markers.py — inject the PDF's own yellow-highlight answer marking
into the MarkItDown-converted markdown, as a textual "– 100%" marker, so the
existing prompts.py rule ("Correct answers are marked with '– 100%', '(100%)',
a letter written after the item, or similar markers") picks them up for
free — no prompt change needed.

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

# Marker forms the parse prompt already recognizes (prompts.py, "Correct
# answers are marked with '– 100%', '(100%)', a letter written after the
# item, or similar markers") — used to detect a line/span that's ALREADY
# marked, so we never double the marker on sections (e.g. lesen_teil1) that
# print it in the source text itself.
_MARKER_RE = re.compile(r'[-–—]\s*100\s*%|\(\s*100\s*%\s*\)')

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
# Injection — new logic tying the two halves above together.
# ---------------------------------------------------------------------------

def _inject_answer_markers(pdf_path: str, markdown: str) -> str:
    """Find every yellow-highlighted option in the PDF at `pdf_path` and, for
    each one that can be located in `markdown` on a genuine option-shaped
    line ("a) ...", "b) ...", ...) that doesn't already carry a "– 100%" /
    "(100%)" style marker, append " – 100%" to that markdown line.

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
            # Only inject onto genuine option-shaped markdown lines ("a) ...")
            # — the same shape check the PDF side was filtered through — so
            # a coincidental prose match elsewhere in the document is never
            # mistaken for an answer option.
            if not _OPTION_RE.match(lines[l0].strip()):
                continue
            span_text = '\n'.join(lines[l0:l1 + 1])
            if _MARKER_RE.search(span_text):
                continue  # already marked (e.g. lesen_teil1 prints "(100%)"
                          # in the source itself) — never double it
            lines[l1] = lines[l1].rstrip() + ' – 100%'
            injected += 1
            marked_here += 1

    if injected:
        print(f'ANSWER_MARKERS injected={injected} distinct_highlighted_options={len(marks)}')

    return '\n'.join(lines)
