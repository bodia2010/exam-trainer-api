#!/usr/bin/env python3
"""E2E import verification (AGENT_PLAN.md Phase 3.3).

Drives convert -> discover -> parse against a REAL exam-trainer-api
deployment, for BOTH tariffs (free and premium), mirroring exactly how the
Flutter client sequences those calls — see:
  - exam_trainer/lib/services/parse_service.dart
      (convertPdf, discoverSections, groupChunksBySectionType,
       parseVariantGroups, _parseWithRetry, _validateGroup, _validateShape,
       _expandSentinels, _mergeByVariant)
  - exam_trainer/lib/screens/import_screen.dart
      (_sectionOrder, the free-tier "only the first variant's original
       chunk" trimming, the "take result.items.first" free-tier handling)
  - exam-trainer-api/main.py
      (the authoritative request/response contract for /api/convert and
       /api/parse — this script's HTTP shapes were read from these route
       handlers directly, not guessed)

USAGE (makes REAL network calls against --base-url; needs a real PDF and
two real Firebase ID tokens — nothing here is mocked or invented):

    python3 scripts/e2e_import.py \\
        --pdf /path/to/exam.pdf \\
        --free-token "$FREE_TIER_ID_TOKEN" \\
        --premium-token "$PREMIUM_TIER_ID_TOKEN" \\
        [--base-url https://exam-trainer-api-<preview>.vercel.app]

--base-url defaults to the production deployment
(https://exam-trainer-api.vercel.app), matching the project's own
`flutter build apk ... --dart-define=API_BASE_URL=...` convention — pass
--base-url explicitly to point at a preview deploy instead.

Exit code is non-zero if anything failed (HTTP error, malformed response,
a validation problem, or — free tier only — more/less than exactly one
variant coming back for a section).

VALIDATION-LOGIC DISCLAIMER
----------------------------
`expand_sentinels`, `validate_group`, `validate_shape`, the per-type
`validate_*` helpers, `_EXPECTED_QUESTION_COUNT`, and `merge_by_variant`
below are a manual, by-hand PORT of the corresponding Dart functions in
parse_service.dart (`_expandSentinels`, `_validateGroup`, `_validateShape`,
`_validateHoerenTeil1`, `_validateTelefonnotiz`, `_validateSprachbausteine1`,
`_validateUniversal`, `_expectedQuestionCount`, `_mergeByVariant`). There is
NO shared-schema mechanism between the Dart client and this Python script —
if parse_service.dart's validation rules change, this file must be updated
by hand to match, or it will silently drift out of sync with what the real
app actually enforces.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

# ─── constants mirrored from parse_service.dart ────────────────────────────

# Same Duration values as ParseService's _convertTimeout / _discoveryTimeout
# / _timeout (seconds).
TIMEOUT_CONVERT = 180
TIMEOUT_DISCOVER = 120
TIMEOUT_PARSE = 60

# Marker inserted between chunks of the same variant group — must match
# ParseService.itemDelimiter exactly, it's part of the request shape.
ITEM_DELIMITER = "<<<ITEM>>>"

# Sent by the parse prompt for a reworked edition's field that's identical
# to the original — must match ParseService._sameSentinel exactly.
SAME_SENTINEL = "<<SAME_AS_ORIGINAL>>"

# Mirrors ImportScreen._sectionOrder — the 12 real, parseable section types,
# in the client's fixed display order. Deliberately excludes 'discover' and
# 'other' (the discovery filler-block marker): groupChunksBySectionType can
# emit an 'other' group, but ImportScreen only ever iterates types that are
# in this list, so 'other' is silently dropped rather than parsed — this
# script mirrors that by only ever calling /api/parse for these 12 types.
SECTION_ORDER = [
    "lesen_teil1", "lesen_teil2", "lesen_teil3", "lesen_teil4",
    "beschwerde", "sprachbausteine_teil1", "sprachbausteine_teil2",
    "telefonnotiz", "hoeren_teil1", "hoeren_teil2", "hoeren_teil3",
    "hoeren_teil4",
]

# Mirrors ParseService._expectedQuestionCount.
_EXPECTED_QUESTION_COUNT = {
    "lesen_teil1": 5,
    "lesen_teil2": 2,
    "lesen_teil3": 4,
    "lesen_teil4": 5,
    "beschwerde": 2,
    "sprachbausteine_teil2": 6,
    "hoeren_teil2": 4,
    "hoeren_teil3": 4,
    "hoeren_teil4": 8,
}


# ─── HTTP plumbing ──────────────────────────────────────────────────────────


class ApiCallError(Exception):
    """Any failure talking to the API — network error, non-200 status, or
    a response shape that doesn't match what main.py's route handlers
    promise. Never carries the bearer token (the server never echoes it
    back to us, so nothing here can leak it)."""


@dataclass
class ApiResult:
    status_code: int | None
    json: Any
    raw_text: str | None
    error: str | None  # set only for network-level failures (no response at all)


def _do_request(url: str, token: str, timeout: int, *, data: bytes | None = None,
                 json_body: dict | None = None) -> ApiResult:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        if json_body is not None:
            resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
        else:
            headers["Content-Type"] = "application/octet-stream"
            resp = requests.post(url, headers=headers, data=data, timeout=timeout)
    except requests.RequestException as e:
        return ApiResult(None, None, None, f"{type(e).__name__}: {e}")

    raw_text = resp.text
    try:
        parsed = resp.json()
    except ValueError:
        parsed = None
    return ApiResult(resp.status_code, parsed, raw_text, None)


def _error_message(result: ApiResult) -> str:
    """Best-effort human-readable reason from a failed ApiResult — never
    trusts the body to be well-formed (that's exactly the "malformed JSON
    response" case this needs to survive)."""
    if isinstance(result.json, dict) and "error" in result.json:
        return str(result.json["error"])[:300]
    if result.raw_text:
        return result.raw_text[:300]
    return "(empty response body)"


def convert_pdf(base_url: str, token: str, pdf_bytes: bytes) -> str:
    result = _do_request(f"{base_url}/api/convert", token, TIMEOUT_CONVERT, data=pdf_bytes)
    if result.error:
        raise ApiCallError(f"network error calling /api/convert: {result.error}")
    if result.status_code != 200:
        raise ApiCallError(f"/api/convert HTTP {result.status_code}: {_error_message(result)}")
    markdown = result.json.get("markdown") if isinstance(result.json, dict) else None
    if not isinstance(markdown, str):
        raise ApiCallError(
            "/api/convert returned 200 but no string 'markdown' field "
            f"(malformed/unexpected response shape): {_error_message(result)}"
        )
    return markdown


def parse_section(base_url: str, token: str, markdown: str, section_type: str) -> list:
    """POSTs /api/parse — same request shape for BOTH 'discover' and every
    real section type, matching ParseService.parseSection exactly."""
    result = _do_request(
        f"{base_url}/api/parse", token, TIMEOUT_PARSE,
        json_body={"markdown": markdown, "section_type": section_type},
    )
    if result.error:
        raise ApiCallError(f"network error calling /api/parse ({section_type}): {result.error}")
    if result.status_code != 200:
        raise ApiCallError(
            f"/api/parse ({section_type}) HTTP {result.status_code}: {_error_message(result)}"
        )
    if not isinstance(result.json, list):
        raise ApiCallError(
            f"/api/parse ({section_type}) returned 200 but the body isn't a JSON "
            f"array (malformed response): {_error_message(result)}"
        )
    return result.json


def parse_with_retry(base_url: str, token: str, markdown: str, section_type: str) -> list:
    """Mirrors ParseService._parseWithRetry: 3 attempts, sleeping after
    EVERY failed attempt (including the last, before finally raising) —
    15s on a 429 (Gemini rate limit), else 2 + attempt*3s. Faithful port,
    including the (mildly wasteful) sleep-then-still-raise tail behavior,
    since this is exactly the retry policy the real client relies on for
    day-to-day transient 429/503s against the real backend."""
    last_error: ApiCallError | None = None
    for attempt in range(3):
        try:
            return parse_section(base_url, token, markdown, section_type)
        except ApiCallError as e:
            last_error = e
            seconds = 15 if "429" in str(e) else 2 + attempt * 3
            time.sleep(seconds)
    assert last_error is not None
    raise last_error


# ─── discovery + chunking (mirrors parse_service.dart) ─────────────────────


@dataclass
class DiscoveredItem:
    section_type: str
    variant_number: Any
    version_label: str | None
    start_line: int


@dataclass
class VariantGroup:
    variant_number: Any
    chunks: list

    def joined_text(self) -> str:
        return f"\n\n{ITEM_DELIMITER}\n\n".join(self.chunks)


def build_numbered_markdown(markdown: str) -> str:
    """Mirrors discoverSections' line-numbering: '00042: <line>' per line,
    Gemini reports an exact line index instead of quoting text verbatim."""
    lines = markdown.split("\n")
    out = [f"{i:05d}: {line}" for i, line in enumerate(lines)]
    # Dart's StringBuffer.writeln appends '\n' after every line, including
    # the last one — replicate that trailing newline exactly.
    return "\n".join(out) + "\n"


def discover_sections(base_url: str, token: str, markdown: str) -> list[DiscoveredItem]:
    numbered = build_numbered_markdown(markdown)
    raw = parse_with_retry(base_url, token, numbered, "discover")
    items = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        section_type = it.get("section_type") or ""
        if not section_type:
            continue
        items.append(DiscoveredItem(
            section_type=section_type,
            variant_number=it.get("variant_number") if it.get("variant_number") is not None else 0,
            version_label=it.get("version_label"),
            start_line=int(it.get("start_line") or 0),
        ))
    items.sort(key=lambda x: x.start_line)
    return items


def group_chunks_by_section_type(
    markdown: str, items: list[DiscoveredItem]
) -> dict[str, list[VariantGroup]]:
    """Mirrors groupChunksBySectionType: slices the RAW (non-numbered)
    markdown by line index — the numbering is 1:1 with raw line count, so
    the same start_line indices apply to either version of the text."""
    lines = markdown.split("\n")

    def clamp(n: int) -> int:
        return max(0, min(n, len(lines)))

    by_section: dict[str, dict[Any, list[str]]] = {}
    for i, item in enumerate(items):
        start = clamp(item.start_line)
        end = clamp(items[i + 1].start_line) if i + 1 < len(items) else len(lines)
        if end <= start:
            continue
        chunk = "\n".join(lines[start:end])
        by_section.setdefault(item.section_type, {}).setdefault(item.variant_number, []).append(chunk)

    result: dict[str, list[VariantGroup]] = {}
    for section_type, by_variant in by_section.items():
        result[section_type] = [
            VariantGroup(variant_number=v, chunks=c) for v, c in by_variant.items()
        ]
    return result


# ─── validation port (see module docstring disclaimer) ─────────────────────


def _find_sentinel_paths(value: Any, path: str) -> list[str]:
    hits: list[str] = []
    if value == SAME_SENTINEL:
        hits.append(path if path else "<root>")
    elif isinstance(value, dict):
        for k, v in value.items():
            hits.extend(_find_sentinel_paths(v, f"{path}.{k}"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            hits.extend(_find_sentinel_paths(v, f"{path}[{i}]"))
    return hits


def _contains_raw(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains_raw(v, needle) for v in value.values())
    if isinstance(value, list):
        return any(_contains_raw(v, needle) for v in value)
    return False


def expand_sentinels(group: list, section_type: str) -> list:
    """Port of ParseService._expandSentinels. Note: as of the responseSchema
    rollout (see response_schemas.py's module docstring), every section
    type now has a schema-enforced response and prompts.py's DEDUPLICATION
    rule says schema-covered types "drop that optimization and always emit
    full content per edition" — so SAME_SENTINEL should no longer actually
    appear in real responses. Ported anyway for exact parity with the
    client, which still runs this defensively on every result."""
    objects = [o for o in group if isinstance(o, dict)]
    base = next((o for o in objects if o.get("version") is None), None)
    if base is None:
        base = objects[0] if objects else {}
    if not base:
        return group
    for obj in objects:
        if obj is base:
            continue
        for field_name in ("texts", "option_pool", "letter_text", "all_options"):
            if obj.get(field_name) == SAME_SENTINEL and field_name in base:
                obj[field_name] = base[field_name]
        pairs = obj.get("question_pairs")
        base_pairs = base.get("question_pairs")
        if isinstance(pairs, list) and isinstance(base_pairs, list):
            for i in range(min(len(pairs), len(base_pairs))):
                if pairs[i] == SAME_SENTINEL:
                    pairs[i] = base_pairs[i]
    return group


def validate_group(expanded: list, section_type: str) -> list[str]:
    """Port of ParseService._validateGroup."""
    problems: list[str] = []
    if not expanded:
        return ["empty result — no variant object returned"]
    for item in expanded:
        if not isinstance(item, dict):
            problems.append(f"non-object entry: {item}")
            continue
        leaks = _find_sentinel_paths(item, "")
        if leaks:
            problems.append(f"unresolved {SAME_SENTINEL} at {', '.join(leaks)}")
        if _contains_raw(item, ITEM_DELIMITER):
            problems.append(f"leaked {ITEM_DELIMITER} delimiter")
        if item.get("variant_number") is None:
            problems.append("missing variant_number")
        problems.extend(validate_shape(section_type, item))
    return problems


def validate_shape(section_type: str, item: dict) -> list[str]:
    """Port of ParseService._validateShape."""
    if section_type == "hoeren_teil1":
        return _validate_hoeren_teil1(item)
    if section_type == "telefonnotiz":
        return _validate_telefonnotiz(item)
    if section_type == "sprachbausteine_teil1":
        return _validate_sprachbausteine1(item)
    return _validate_universal(section_type, item)


def _validate_hoeren_teil1(item: dict) -> list[str]:
    problems: list[str] = []
    pairs = item.get("question_pairs")
    if not isinstance(pairs, list) or len(pairs) != 3:
        got = len(pairs) if isinstance(pairs, list) else "none"
        problems.append(f"question_pairs must have exactly 3 entries, got {got}")
        return problems
    for i, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            problems.append(f"question_pairs[{i}] is not an object")
            continue
        dialogue = pair.get("dialogue")
        if not isinstance(dialogue, str) or not dialogue.strip():
            problems.append(f"question_pairs[{i}].dialogue is empty")
        rf = pair.get("richtig_falsch")
        if not isinstance(rf, dict) or not isinstance(rf.get("answer"), bool):
            problems.append(f"question_pairs[{i}].richtig_falsch missing/invalid answer")
        mc = pair.get("multiple_choice")
        if not isinstance(mc, dict):
            problems.append(f"question_pairs[{i}].multiple_choice missing")
            continue
        options = mc.get("options")
        correct = mc.get("correct_letter")
        if not isinstance(options, list) or not options:
            problems.append(f"question_pairs[{i}].multiple_choice.options empty")
        elif not isinstance(correct, str) or not any(
            isinstance(o, dict) and o.get("letter") == correct for o in options
        ):
            problems.append(
                f'question_pairs[{i}].multiple_choice.correct_letter "{correct}" '
                "not among its own options"
            )
    return problems


def _validate_telefonnotiz(item: dict) -> list[str]:
    problems: list[str] = []
    versions = item.get("versions")
    if not isinstance(versions, list) or not versions:
        problems.append("versions is empty")
        return problems
    for i, v in enumerate(versions):
        if not isinstance(v, dict):
            problems.append(f"versions[{i}] is not an object")
            continue
        monologue = v.get("monologue")
        if not isinstance(monologue, str) or not monologue.strip():
            problems.append(f"versions[{i}].monologue is empty")
        answer = v.get("answer")
        name = answer.get("name") if isinstance(answer, dict) else None
        if not isinstance(name, str) or not name.strip():
            problems.append(f"versions[{i}].answer.name is empty")
    return problems


def _validate_sprachbausteine1(item: dict) -> list[str]:
    problems: list[str] = []
    letter_text = item.get("letter_text")
    if not isinstance(letter_text, str) or not letter_text.strip():
        problems.append("letter_text is empty")
    answers = item.get("answers")
    if not isinstance(answers, list) or not answers:
        problems.append("answers is empty")
        return problems
    all_options = item.get("all_options")
    option_letters = (
        {o.get("letter") for o in all_options if isinstance(o, dict)}
        if isinstance(all_options, list) else set()
    )
    for a in answers:
        if not isinstance(a, dict):
            continue
        letter = a.get("letter")
        if letter not in option_letters:
            problems.append(
                f'answers letter "{letter}" (Q{a.get("question_number")}) not among all_options'
            )
        qnum = a.get("question_number")
        if isinstance(letter_text, str) and f"[{qnum}]" not in letter_text:
            problems.append(f"letter_text missing [{qnum}] marker")
    return problems


def _validate_universal(section_type: str, item: dict) -> list[str]:
    problems: list[str] = []
    texts = item.get("texts")
    if not isinstance(texts, list) or not texts:
        problems.append("texts is empty — reading passage/transcript is missing")
    questions = item.get("questions")
    if not isinstance(questions, list) or not questions:
        problems.append("questions is empty")
        return problems
    expected = _EXPECTED_QUESTION_COUNT.get(section_type)
    if expected is not None and len(questions) != expected:
        problems.append(f"expected {expected} questions for {section_type}, got {len(questions)}")
    option_pool = item.get("option_pool")
    pool_letters = (
        {o.get("letter") for o in option_pool if isinstance(o, dict)}
        if isinstance(option_pool, list) else set()
    )
    for q in questions:
        if not isinstance(q, dict):
            problems.append("a question entry is not an object")
            continue
        qtype = q.get("type")
        answer = q.get("answer")
        number = q.get("number")
        if qtype == "match":
            if answer not in pool_letters:
                problems.append(f'question {number}: match answer "{answer}" not in option_pool')
        elif qtype == "choice":
            options = q.get("options")
            letters = (
                {o.get("letter") for o in options if isinstance(o, dict)}
                if isinstance(options, list) else set()
            )
            if answer not in letters:
                problems.append(f'question {number}: choice answer "{answer}" not among its own options')
        elif qtype == "true_false":
            if answer not in ("richtig", "falsch"):
                problems.append(f'question {number}: true_false answer "{answer}" is not richtig/falsch')
        else:
            problems.append(f'question {number}: unknown type "{qtype}"')
    return problems


def _deduped_concat(a: list, b: list, key_field: str) -> list:
    seen = {e.get(key_field) for e in a if isinstance(e, dict)}
    return a + [e for e in b if isinstance(e, dict) and e.get(key_field) not in seen]


def merge_by_variant(raw: list) -> list:
    """Port of ParseService._mergeByVariant — combines several results
    sharing (variant_number, version) into one final entry."""
    by_num: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        n_raw = item.get("variant_number")
        n = int(n_raw) if isinstance(n_raw, (int, float)) else 0
        version = (item.get("version") or "").strip()
        key = f"{n}|{version.lower()}"
        existing = by_num.get(key)
        if existing is None:
            by_num[key] = dict(item)
            continue
        for k, v in item.items():
            if v is None:
                continue
            ev = existing.get(k)
            if ev is None or (isinstance(ev, str) and ev == ""):
                existing[k] = v
            elif isinstance(ev, list) and isinstance(v, list):
                if k == "questions":
                    existing[k] = _deduped_concat(ev, v, "number")
                elif k == "option_pool":
                    existing[k] = _deduped_concat(ev, v, "letter")
                elif k == "texts":
                    existing[k] = _deduped_concat(ev, v, "title")
                else:
                    existing[k] = ev + v
    result = list(by_num.values())
    result.sort(key=lambda a: ((a.get("variant_number") or 0), a.get("version") or ""))
    return result


# ─── report rows + driver ───────────────────────────────────────────────────


@dataclass
class Row:
    tariff: str
    section_type: str
    variant: str
    status: str  # 'OK' or 'FAIL'
    reason: str


def process_group(
    base_url: str, token: str, tariff: str, section_type: str, group: VariantGroup, rows: list[Row]
) -> None:
    """One row per variant group — mirrors the per-group unit
    parseVariantGroups validates/caches/retries at. 2 attempts on a
    validation (not network) failure, matching the Dart loop exactly."""
    text = group.joined_text()
    last_problems: list[str] = []
    for _attempt in range(2):
        try:
            parsed = parse_with_retry(base_url, token, text, section_type)
        except ApiCallError as e:
            rows.append(Row(tariff, section_type, str(group.variant_number), "FAIL", str(e)))
            return

        expanded = expand_sentinels(parsed, section_type)
        problems = validate_group(expanded, section_type)

        if tariff == "free":
            # Acceptance bar from AGENT_PLAN.md 3.3: free tier must return
            # exactly 1 variant per section. The client itself doesn't
            # enforce this — it silently takes result.items.first and
            # discards the rest — so this script checks the property the
            # client relies on implicitly holding, on the actual merged
            # result (what would really end up in the course).
            merged = merge_by_variant(expanded)
            if len(merged) != 1:
                problems = problems + [
                    f"free tier acceptance bar violated: expected exactly 1 "
                    f"variant, got {len(merged)}"
                ]

        if not problems:
            rows.append(Row(tariff, section_type, str(group.variant_number), "OK", f"{len(expanded)} item(s)"))
            return
        last_problems = problems

    rows.append(Row(tariff, section_type, str(group.variant_number), "FAIL", "; ".join(last_problems)))


def run_tier(base_url: str, tariff: str, token: str, pdf_bytes: bytes, rows: list[Row]) -> None:
    """Runs convert -> discover -> parse-all-sections for one tariff.
    Every step is wrapped so a failure records a row and returns instead of
    raising — a crash at any point must not lose rows already collected for
    this tier or the other one."""
    try:
        markdown = convert_pdf(base_url, token, pdf_bytes)
    except ApiCallError as e:
        rows.append(Row(tariff, "convert", "-", "FAIL", str(e)))
        return
    except Exception as e:  # noqa: BLE001 - last-resort net, see module docstring
        rows.append(Row(tariff, "convert", "-", "FAIL", f"unexpected error: {type(e).__name__}: {e}"))
        return

    try:
        items = discover_sections(base_url, token, markdown)
    except ApiCallError as e:
        rows.append(Row(tariff, "discover", "-", "FAIL", str(e)))
        return
    except Exception as e:  # noqa: BLE001
        rows.append(Row(tariff, "discover", "-", "FAIL", f"unexpected error: {type(e).__name__}: {e}"))
        return

    groups_by_type = group_chunks_by_section_type(markdown, items)
    present_types = [t for t in SECTION_ORDER if t in groups_by_type]

    if not present_types:
        rows.append(Row(tariff, "discover", "-", "FAIL", "no recognizable sections discovered"))
        return

    for section_type in present_types:
        all_groups = groups_by_type[section_type]
        if tariff == "free":
            # Mirrors ImportScreen: free tier sends only the FIRST
            # discovered variant's ORIGINAL chunk (no reworked editions).
            first = all_groups[0]
            groups = [VariantGroup(variant_number=first.variant_number, chunks=[first.chunks[0]])]
        else:
            groups = all_groups

        for group in groups:
            try:
                process_group(base_url, token, tariff, section_type, group, rows)
            except Exception as e:  # noqa: BLE001 - never let one group's bug kill the run
                rows.append(Row(
                    tariff, section_type, str(group.variant_number), "FAIL",
                    f"unexpected error: {type(e).__name__}: {e}",
                ))


def print_table(rows: list[Row]) -> None:
    if not rows:
        print("(no results collected)")
        return
    headers = ("TARIFF", "SECTION_TYPE", "VARIANT", "STATUS", "REASON")
    col_widths = [
        max(len(headers[i]), *(len(str(getattr(r, f))) for r in rows)) if rows else len(headers[i])
        for i, f in enumerate(("tariff", "section_type", "variant", "status", "reason"))
    ]
    # Reason can be very long — cap the column so the table stays readable;
    # full reason text is still the value used for pass/fail, only display
    # is truncated.
    col_widths[4] = min(col_widths[4], 100)

    def fmt_row(values: tuple) -> str:
        return "  ".join(
            str(v)[: col_widths[i]].ljust(col_widths[i]) for i, v in enumerate(values)
        )

    print(fmt_row(headers))
    print("  ".join("-" * w for w in col_widths))
    for r in rows:
        print(fmt_row((r.tariff, r.section_type, r.variant, r.status, r.reason)))

    ok = sum(1 for r in rows if r.status == "OK")
    fail = len(rows) - ok
    print(f"\n{ok} OK, {fail} FAIL, {len(rows)} total")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="E2E import verification: convert -> discover -> parse "
                    "against a real exam-trainer-api deployment, both tariffs."
    )
    parser.add_argument("--pdf", required=True, type=Path, help="Path to the reference exam PDF.")
    parser.add_argument("--free-token", required=True, help="Firebase ID token for a free-tier test account.")
    parser.add_argument("--premium-token", required=True, help="Firebase ID token for a premium-tier test account.")
    parser.add_argument(
        "--base-url",
        default="https://exam-trainer-api.vercel.app",
        help="API base URL (default: production — matches the project's "
             "`flutter build apk --dart-define=API_BASE_URL=...` convention). "
             "Override to target a preview deploy instead.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.pdf.is_file():
        print(f"error: PDF file not found: {args.pdf}", file=sys.stderr)
        return 2

    try:
        pdf_bytes = args.pdf.read_bytes()
    except OSError as e:
        print(f"error: could not read PDF file {args.pdf}: {e}", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    rows: list[Row] = []

    for tariff, token in (("free", args.free_token), ("premium", args.premium_token)):
        try:
            run_tier(base_url, tariff, token, pdf_bytes, rows)
        except Exception as e:  # noqa: BLE001
            # Absolute last resort: nothing above this should ever reach
            # here, but a crash mid-tier must not wipe out rows already
            # collected for the other tier.
            rows.append(Row(tariff, "<tier>", "-", "FAIL", f"unexpected top-level error: {type(e).__name__}: {e}"))

    print_table(rows)

    if not rows:
        return 1
    return 1 if any(r.status != "OK" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
