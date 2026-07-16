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


def _coerce_heading_lines(text_item: dict[str, Any], start: int, end: int) -> list[int]:
    if 'heading_lines' not in text_item:
        return []
    raw_heading_lines = text_item['heading_lines']
    if raw_heading_lines is None:
        return []
    if not isinstance(raw_heading_lines, list):
        raise TypeError('heading_lines must be a list when present')

    heading_lines = [_coerce_span_index(line) for line in raw_heading_lines]
    line_extraction.validate_heading_lines(heading_lines, start, end)
    return heading_lines


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


def resolve_universal_text_spans(parsed: list, markdown: str) -> list:
    """Resolve universal-schema texts[] spans to legacy title/content items."""
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
                heading_lines = _coerce_heading_lines(text_item, start, end)
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
