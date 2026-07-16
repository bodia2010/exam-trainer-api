"""Pure-Python resolvers for line-span backed parse responses.

The Gemini schema reports line pointers for selected fields; this module
turns those pointers into the legacy text shapes consumed downstream. It
intentionally contains no Flask or network dependencies.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import line_extraction

NO_ANSWER_SENTINEL = '(nicht angegeben)'

_LOGGER = logging.getLogger(__name__)
_INTEGER_STRING_RE = re.compile(r'^[+-]?\d+$')
_VALID_VOICE_GENDERS = {'female', 'male', 'unknown'}
_H4_TITLE_RE = re.compile(r'^\s*Nummer\s+(?P<number>\d+)\s+(?P<label>.+?)\s*$', re.I)
_H1_VARIANT_HEADER_RE = re.compile(
    r'Hören\s+Teil\s+1\s*\(\s*вариант\s*№\s*(\d+)',
    re.I,
)


def _warn_invalid(kind: str, reason: str) -> None:
    _LOGGER.warning('SPAN_RESOLUTION_INVALID kind=%s reason=%s', kind, reason)


def _coerce_span_index(value: Any) -> int:
    """Accept ints and integer strings, but never bool-as-int."""
    if isinstance(value, bool):
        raise TypeError('span indices must not be bool')
    if type(value) is int:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if _INTEGER_STRING_RE.match(stripped):
            return int(stripped)
    raise TypeError('span indices must be integers or integer strings')


def _coerce_optional_slash_index(value: Any) -> int | None:
    if value is None:
        return None
    return _coerce_span_index(value)


def _text_item_sentinel() -> dict[str, Any]:
    return {'title': NO_ANSWER_SENTINEL, 'content': NO_ANSWER_SENTINEL}


def _legacy_text_item(title: Any, content: str) -> dict[str, Any]:
    safe_title = title if isinstance(title, str) and title else NO_ANSWER_SENTINEL
    return {'title': safe_title, 'content': content or NO_ANSWER_SENTINEL}


def _sanitize_metadata_value(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    metadata: dict[str, Any] = {}
    voice_gender = value.get('voice_gender')
    if voice_gender in _VALID_VOICE_GENDERS:
        metadata['voice_gender'] = voice_gender

    raw_speaker_hints = value.get('speaker_voice_genders')
    if isinstance(raw_speaker_hints, list):
        speaker_hints = []
        for hint in raw_speaker_hints:
            if not isinstance(hint, dict):
                continue
            speaker = hint.get('speaker')
            hint_gender = hint.get('voice_gender')
            if (
                    isinstance(speaker, str)
                    and speaker.strip()
                    and hint_gender in _VALID_VOICE_GENDERS):
                speaker_hints.append({
                    'speaker': speaker.strip(),
                    'voice_gender': hint_gender,
                })
        if speaker_hints:
            metadata['speaker_voice_genders'] = speaker_hints

    return metadata or None


def _copy_metadata(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    metadata = _sanitize_metadata_value(source.get('metadata'))
    if metadata is not None:
        target['metadata'] = metadata
    return target


def sanitize_parser_metadata(parsed: Any) -> Any:
    """Keep only valid optional TTS metadata in a parsed JSON tree.

    This is intentionally tolerant: old cache entries have no metadata,
    and malformed model output loses only the bad hint instead of making
    the whole exercise invalid for legacy clients.
    """
    if isinstance(parsed, list):
        for item in parsed:
            sanitize_parser_metadata(item)
        return parsed
    if not isinstance(parsed, dict):
        return parsed

    if 'metadata' in parsed:
        metadata = _sanitize_metadata_value(parsed.get('metadata'))
        if metadata is None:
            parsed.pop('metadata', None)
        else:
            parsed['metadata'] = metadata

    for key, value in list(parsed.items()):
        if key != 'metadata':
            sanitize_parser_metadata(value)
    return parsed


def _last_name(value: str) -> str:
    tokens = re.findall(r'[A-Za-zÄÖÜäöüß]+', value.casefold())
    return tokens[-1] if tokens else ''


def _self_identified_speaker(lines: list[str]) -> str:
    text = ' '.join(line.strip() for line in lines if line.strip())
    patterns = (
        r'\bhier\s+(?:ist|spricht)\s+([A-ZÄÖÜ][\wÄÖÜäöüß-]*(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]*)?)',
        r'^\s*Hallo,\s+([A-ZÄÖÜ][\wÄÖÜäöüß-]*(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]*)?)\s+von\b',
        r'^\s*([A-ZÄÖÜ][\wÄÖÜäöüß-]*(?:\s+[A-ZÄÖÜ][\wÄÖÜäöüß-]*)?)\s*,',
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ''


def apply_contextual_voice_metadata(
    parsed: Any,
    markdown: str,
    section_type: str,
) -> Any:
    """Correct Hören Teil 4 gender only from explicit same-speaker evidence.

    A task title can name an absent person while somebody else leaves the
    message. Therefore a matching ``Frau/Herr`` question label is authoritative
    only when the transcript self-identifies the same surname. This keeps the
    rule generic and avoids hardcoded person-name lists.
    """
    if section_type != 'hoeren_teil4' or not isinstance(parsed, list):
        return parsed

    raw_lines = markdown.split('\n')
    for obj in parsed:
        if not isinstance(obj, dict):
            continue
        texts = obj.get('texts')
        if not isinstance(texts, list):
            continue
        for text_item in texts:
            if not isinstance(text_item, dict):
                continue
            title = text_item.get('title')
            if not isinstance(title, str):
                continue
            title_match = _H4_TITLE_RE.match(title)
            if not title_match:
                continue
            try:
                start = _coerce_span_index(text_item.get('start_line'))
                end = _coerce_span_index(text_item.get('end_line'))
                line_extraction.validate_inclusive_span(raw_lines, start, end)
            except (TypeError, ValueError):
                continue

            number = title_match.group('number')
            question_match = None
            question_re = re.compile(
                rf'^\s*{re.escape(number)}\.\s*(Frau|Herr)\s+(.+?)\s*$',
                re.I,
            )
            for line in raw_lines[end + 1:min(len(raw_lines), end + 13)]:
                question_match = question_re.match(line)
                if question_match:
                    break
            if question_match is None:
                continue

            speaker_lines = raw_lines[start:end + 1]
            # A valid span may start either at the printed Nummer label or at
            # the first transcript line. Skip only an actual label, never the
            # transcript's self-identification.
            if speaker_lines and re.match(
                    rf'^\s*Nummer\s+{re.escape(number)}\b',
                    speaker_lines[0],
                    re.I):
                speaker_lines = speaker_lines[1:]
            speaker = _self_identified_speaker(speaker_lines)
            if not speaker:
                continue
            question_person = question_match.group(2)
            if _last_name(speaker) != _last_name(question_person):
                continue

            metadata = text_item.get('metadata')
            if not isinstance(metadata, dict):
                metadata = {}
                text_item['metadata'] = metadata
            metadata['voice_gender'] = (
                'female' if question_match.group(1).casefold() == 'frau' else 'male'
            )
    return parsed


def normalize_h1_variant_numbers(parsed: Any, markdown: str) -> Any:
    """Use the sole explicit H1 header for headerless continuation fragments.

    Discovery may split later edition fragments after the printed variant
    header. When the complete input contains exactly one explicit H1 variant
    number, inventing a different number is impossible: every emitted edition
    inherits that sole source value.
    """
    if not isinstance(parsed, list):
        return parsed
    explicit_numbers = {
        int(match.group(1))
        for match in _H1_VARIANT_HEADER_RE.finditer(markdown)
    }
    if len(explicit_numbers) != 1:
        return parsed
    variant_number = next(iter(explicit_numbers))
    for obj in parsed:
        if isinstance(obj, dict):
            obj['variant_number'] = variant_number
    return parsed


def _coerce_heading_lines(text_item: dict[str, Any]) -> list[int]:
    if 'heading_lines' not in text_item:
        return []
    raw_heading_lines = text_item['heading_lines']
    if raw_heading_lines is None:
        return []
    if not isinstance(raw_heading_lines, list):
        raise TypeError('heading_lines must be a list when present')

    return [_coerce_span_index(line) for line in raw_heading_lines]


def resolve_telefonnotiz_spans(parsed: list, markdown: str) -> list:
    """Resolve telefonnotiz answer.weitere_informationen line spans.

    Valid entries become strings extracted from markdown with
    strip_bullet=True and the existing slash_index behavior. Bad span
    entries degrade to ``(nicht angegeben)`` without leaking source text
    into warnings.
    """
    raw_lines = markdown.split('\n')
    for item in parsed:
        if not isinstance(item, dict):
            continue
        for version in item.get('versions') or []:
            if not isinstance(version, dict):
                continue
            answer = version.get('answer')
            if not isinstance(answer, dict):
                continue
            spans = answer.get('weitere_informationen')
            if not isinstance(spans, list):
                continue

            resolved: list[str] = []
            for span in spans:
                try:
                    start = _coerce_span_index(span['start_line'])
                    end = _coerce_span_index(span['end_line'])
                    if line_extraction.is_missing_span_sentinel(start, end):
                        resolved.append(NO_ANSWER_SENTINEL)
                        continue
                    slash_index = _coerce_optional_slash_index(
                        span.get('slash_index') if isinstance(span, dict) else None
                    )
                    text = line_extraction.extract_span(
                        raw_lines,
                        start,
                        end,
                        strip_bullet=True,
                        slash_index=slash_index,
                    )
                except (KeyError, TypeError, ValueError):
                    _warn_invalid('telefonnotiz', 'invalid_span')
                    text = ''
                resolved.append(text or NO_ANSWER_SENTINEL)
            answer['weitere_informationen'] = resolved
    return sanitize_parser_metadata(parsed)


def resolve_universal_text_spans(
    parsed: list,
    markdown: str,
    section_type: str = '',
) -> list:
    """Resolve universal-schema texts[] spans to legacy title/content items."""
    parsed = apply_contextual_voice_metadata(parsed, markdown, section_type)
    raw_lines = markdown.split('\n')
    for item in parsed:
        if not isinstance(item, dict):
            continue
        texts = item.get('texts')
        if not isinstance(texts, list):
            continue

        resolved: list[dict[str, Any]] = []
        for text_item in texts:
            if not isinstance(text_item, dict):
                _warn_invalid('universal_text', 'invalid_text_item')
                resolved.append(_text_item_sentinel())
                continue

            title = text_item.get('title')
            try:
                start = _coerce_span_index(text_item['start_line'])
                end = _coerce_span_index(text_item['end_line'])
            except (KeyError, TypeError, ValueError):
                _warn_invalid('universal_text', 'invalid_span')
                resolved.append(_copy_metadata(
                    text_item,
                    _legacy_text_item(title, NO_ANSWER_SENTINEL),
                ))
                continue

            if line_extraction.is_missing_span_sentinel(start, end):
                resolved.append(_copy_metadata(
                    text_item,
                    _legacy_text_item(title, NO_ANSWER_SENTINEL),
                ))
                continue

            try:
                line_extraction.validate_inclusive_span(raw_lines, start, end)
            except (TypeError, ValueError):
                _warn_invalid('universal_text', 'invalid_span')
                resolved.append(_copy_metadata(
                    text_item,
                    _legacy_text_item(title, NO_ANSWER_SENTINEL),
                ))
                continue

            try:
                heading_lines = _coerce_heading_lines(text_item)
                start, end = line_extraction.normalize_span_for_adjacent_headings(
                    raw_lines,
                    start,
                    end,
                    heading_lines,
                )
                line_extraction.validate_heading_lines(heading_lines, start, end)
            except (TypeError, ValueError):
                _warn_invalid('universal_text', 'invalid_heading_lines')
                resolved.append(_copy_metadata(text_item, _text_item_sentinel()))
                continue

            try:
                content = line_extraction.extract_block(
                    raw_lines,
                    start,
                    end,
                    heading_lines=heading_lines,
                )
            except (TypeError, ValueError):
                _warn_invalid('universal_text', 'invalid_span')
                content = ''
            resolved.append(_copy_metadata(text_item, _legacy_text_item(title, content)))
        item['texts'] = resolved
    return sanitize_parser_metadata(parsed)
