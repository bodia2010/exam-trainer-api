"""Shared helpers for line-span extraction: instead of asking Gemini to
retype a verbatim field (source of paraphrasing/truncation bugs — see
PRODUCT_PLAN.md and the check_verbatim_content.py audit
findings), the prompt asks for a {start_line, end_line} pointer into a
line-numbered copy of the chunk, and the actual text is sliced out by
code afterward. Consumers include telefonnotiz's weitere_informationen
bullets and multi-line universal texts (span_resolution.py).

Numbering format matches ParseService.discoverSections' own convention
client-side ("00042: <line text>", 5-digit zero-padded, 0-indexed) — same
shape Gemini already sees for discover, not a new pattern to learn.
"""
from __future__ import annotations

import re

_BULLET_PREFIX_RE = re.compile(r'^[•▪◦\-*]\s+')
MISSING_SPAN_SENTINEL = (-1, -1)


def is_missing_span_sentinel(start_line: int, end_line: int) -> bool:
    return (start_line, end_line) == MISSING_SPAN_SENTINEL


def validate_inclusive_span(
    raw_lines: list[str],
    start_line: int,
    end_line: int,
) -> tuple[int, int]:
    """Validate an inclusive, 0-based line span.

    The (-1, -1) missing-content sentinel is intentionally not a valid span;
    callers must detect it with is_missing_span_sentinel before extraction.
    """
    if is_missing_span_sentinel(start_line, end_line):
        raise ValueError(
            '(-1, -1) is a missing-span sentinel, not a valid line span'
        )
    if (
        not isinstance(start_line, int)
        or isinstance(start_line, bool)
        or not isinstance(end_line, int)
        or isinstance(end_line, bool)
    ):
        raise TypeError('start_line and end_line must be integers')
    if not (0 <= start_line <= end_line < len(raw_lines)):
        raise ValueError(
            'invalid line span: expected 0 <= start_line <= end_line < '
            f'len(raw_lines), got start_line={start_line}, '
            f'end_line={end_line}, len(raw_lines)={len(raw_lines)}'
        )
    return start_line, end_line


def validate_heading_lines(
    heading_lines: list[int] | None,
    start_line: int,
    end_line: int,
) -> set[int]:
    if heading_lines is None:
        return set()
    if not isinstance(heading_lines, list):
        raise TypeError('heading_lines must be a list of integers')

    headings: set[int] = set()
    for line in heading_lines:
        if not isinstance(line, int) or isinstance(line, bool):
            raise TypeError('heading_lines must be a list of integers')
        if not start_line <= line <= end_line:
            raise ValueError(
                'heading_lines must be inside the extracted span: '
                f'got {line}, span={start_line}..{end_line}'
            )
        headings.add(line)
    return headings


def normalize_span_for_adjacent_headings(
    raw_lines: list[str],
    start_line: int,
    end_line: int,
    heading_lines: list[int] | None,
    *,
    max_distance: int = 2,
) -> tuple[int, int]:
    """Expand a valid span to include an immediately preceding heading.

    Gemini occasionally points ``heading_lines`` at a genuine standalone
    heading just outside the body span. Repair only the unambiguous PDF
    layout: the heading is at most ``max_distance`` source lines from the
    boundary, its own line is non-blank, and every intervening line is blank.
    Distant headings and headings separated by any source content remain
    invalid instead of widening a span into an unrelated block.
    """
    start, end = validate_inclusive_span(raw_lines, start_line, end_line)
    if heading_lines is None:
        return start, end
    if not isinstance(heading_lines, list):
        raise TypeError('heading_lines must be a list of integers')
    if not isinstance(max_distance, int) or isinstance(max_distance, bool):
        raise TypeError('max_distance must be an integer')
    if max_distance < 1:
        raise ValueError('max_distance must be positive')

    normalized_start = start
    normalized_end = end
    for heading in heading_lines:
        if not isinstance(heading, int) or isinstance(heading, bool):
            raise TypeError('heading_lines must be a list of integers')
        if not 0 <= heading < len(raw_lines):
            raise ValueError('heading_lines must name existing source lines')
        if start <= heading <= end:
            continue
        if not raw_lines[heading].strip():
            raise ValueError('heading_lines must not point at blank source lines')

        if heading < start:
            if start - heading > max_distance:
                raise ValueError('heading before span is not adjacent')
            if any(line.strip() for line in raw_lines[heading + 1:start]):
                raise ValueError('heading before span crosses non-blank content')
            normalized_start = min(normalized_start, heading)
            continue

        # A heading after the body is ambiguous: it is normally the heading
        # of the next passage. Never widen the current span forward.
        raise ValueError('heading after span is not part of the current text')

    return normalized_start, normalized_end


def number_markdown(markdown: str) -> str:
    lines = markdown.split('\n')
    return '\n'.join(f'{i:05d}: {line}' for i, line in enumerate(lines))


def extract_span(
    raw_lines: list[str],
    start_line: int,
    end_line: int,
    *,
    strip_bullet: bool = False,
    slash_index: int | None = None,
) -> str:
    """Slices raw_lines[start_line:end_line+1] (inclusive, 0-based) and
    joins them into one clean string:
      - a line ending in '-' is a hard-wrapped word split across the
        PDF's print line break — joined directly to the next line, no
        space, hyphen dropped (matches prompts.py's own de-hyphenation
        rule for LLM-generated text, applied mechanically here instead).
      - every other line boundary joins with a single space.
      - strip_bullet=True additionally strips one leading bullet marker
        ('• ', '- ', etc.) from the very first line only — source bullets
        are stored without their marker (see prompts.py's telefonnotiz
        weitere_informationen examples), and stripping it from every line
        of a range would corrupt genuine mid-sentence punctuation.
      - slash_index=N (0-based): confirmed live that this source
        sometimes prints ONE answer-key block shared by several
        editions, with each field's value joined by "/" in printed
        order (e.g. "Name: Mayer/ Meyer / Azrael" covering 3 editions).
        When set, splits the extracted text on "/" and returns only
        segment N (stripped) — out-of-range or None returns the full
        text unsplit (the common case: a single edition's own bullet
        that just happens to contain "/" as two alternate readings of
        the SAME fact, which must stay together, not be split).
    Same join rules as check_verbatim_content.py's SourceIndex blob —
    that logic was hardened through five rounds of live debugging
    (splitlines() page-break artifacts, blank-line double-spacing, etc.)
    on this exact document; kept consistent rather than re-derived.
    """
    start, end = validate_inclusive_span(raw_lines, start_line, end_line)

    pieces: list[str] = []
    for i in range(start, end + 1):
        line = raw_lines[i].strip()
        if strip_bullet and i == start:
            line = _BULLET_PREFIX_RE.sub('', line)
        if not line:
            continue
        if pieces and pieces[-1].endswith('-'):
            pieces[-1] = pieces[-1][:-1] + line
        else:
            pieces.append(line)
    full = ' '.join(pieces).strip()

    if slash_index is not None and slash_index >= 0:
        parts = [p.strip() for p in full.split('/')]
        if 0 <= slash_index < len(parts) and parts[slash_index]:
            return parts[slash_index]
    return full


def extract_block(
    raw_lines: list[str],
    start_line: int,
    end_line: int,
    *,
    heading_lines: list[int] | None = None,
) -> str:
    """Multi-line variant of extract_span for texts[].content (lesen_teil2 /
    hoeren_teil4 — see SPAN_TEXT_SECTION_TYPES in response_schemas.py).

    Unlike extract_span's single-string join, line breaks are PRESERVED:
    the client renders '\\n' as a hard break and '\\n\\n' as a paragraph gap
    (buildContentSpan in universal_exercise_screen.dart), and the Hören
    audio path re-segments the transcript by speaker turns from the same
    line structure (TtsService.parseLines). Flattening to one line the way
    extract_span does would destroy both.

    Rules:
      - lines are rstripped; runs of 2+ blank lines collapse to ONE blank
        line (a paragraph gap), leading/trailing blank lines are dropped.
      - a line ending in '-' is a hard-wrapped word split across the PDF's
        print line break — joined directly to the next line, no separator
        (same de-hyphenation rule as extract_span / prompts.py).
      - heading_lines: ABSOLUTE line numbers (same numbering the spans use)
        whose text is a standalone sub-heading — those lines are wrapped in
        '**...**', mechanically reproducing the HEADINGS annotation the
        model used to add while retyping (the client renders it bold; the
        source itself never contains '**').
    """
    start, end = validate_inclusive_span(raw_lines, start_line, end_line)
    headings = validate_heading_lines(heading_lines, start, end)

    out: list[str] = []
    pending_hyphen = False
    for i in range(start, end + 1):
        line = raw_lines[i].rstrip()
        if pending_hyphen:
            out[-1] = out[-1][:-1] + line.lstrip()
            pending_hyphen = out[-1].endswith('-') and bool(line)
            continue
        if not line:
            if out and out[-1] != '':
                out.append('')
            continue
        if i in headings:
            line = f'**{line.strip()}**'
        out.append(line)
        pending_hyphen = line.endswith('-') and i not in headings
    while out and out[-1] == '':
        out.pop()
    return '\n'.join(out)
