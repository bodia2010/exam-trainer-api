"""Shared helpers for line-span extraction: instead of asking Gemini to
retype a verbatim field (source of paraphrasing/truncation bugs — see
DISCOVERY_BUG_ANALYSIS.md and yesterday's check_verbatim_content.py
findings), the prompt asks for a {start_line, end_line} pointer into a
line-numbered copy of the chunk, and the actual text is sliced out by
code afterward. First consumer: telefonnotiz's weitere_informationen
bullets (main.py).

Numbering format matches ParseService.discoverSections' own convention
client-side ("00042: <line text>", 5-digit zero-padded, 0-indexed) — same
shape Gemini already sees for discover, not a new pattern to learn.
"""
from __future__ import annotations

import re

_BULLET_PREFIX_RE = re.compile(r'^[•▪◦\-*]\s+')


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
    """Slices raw_lines[start_line:end_line+1] (inclusive, clamped to
    bounds) and joins them into one clean string:
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
    n = len(raw_lines)
    start = max(0, min(start_line, n - 1)) if n else 0
    end = max(0, min(end_line, n - 1)) if n else 0
    if end < start:
        start, end = end, start

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
