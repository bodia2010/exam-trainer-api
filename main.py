import os
import json
import re
import time
import asyncio
import tempfile
import requests
from flask import Flask, request, jsonify, Response
import pdfminer.high_level
from prompts import PROMPTS
from response_schemas import SPAN_TEXT_SECTION_TYPES
from answer_markers import _inject_answer_markers
import line_extraction
import span_resolution
import generation_config
import firebase_auth
import firestore_client
import tts

app = Flask(__name__)
_COURSE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')

# Model choice lives in generation_config.py next to the rest of the
# generation settings — single source of truth shared with the promptfoo
# eval provider, so an eval can never silently test a different model
# than what's deployed (discovery notably runs on a stronger model than
# the parse calls; see the MODELS comment there for the evidence).
_gemini_model = generation_config.model_for


def _gemini_url(model: str) -> str:
    return (
        f'https://generativelanguage.googleapis.com/v1beta/models/'
        f'{model}:generateContent'
    )


_UPSTASH_URL = os.environ.get('UPSTASH_REDIS_REST_URL', '').rstrip('/')
_UPSTASH_TOKEN = os.environ.get('UPSTASH_REDIS_REST_TOKEN', '')


def _cache_get(key: str):
    if not _UPSTASH_URL:
        return None
    resp = requests.get(
        f'{_UPSTASH_URL}/get/{key}',
        headers={'Authorization': f'Bearer {_UPSTASH_TOKEN}'},
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    return resp.json().get('result')


def _cache_set(key: str, value: str):
    if not _UPSTASH_URL:
        return
    requests.post(
        f'{_UPSTASH_URL}/set/{key}',
        headers={'Authorization': f'Bearer {_UPSTASH_TOKEN}'},
        data=value.encode('utf-8'),
        timeout=10,
    )


def _cache_key_type(key: str) -> str:
    """Cache keys are rolling out a new `v14|<type>|<hash>` format (type is
    one of doc/group/discover) but during the rollout some callers may still
    send the old bare-hash keys with no '|' at all — those log as 'legacy'
    rather than crashing on a missing segment."""
    if '|' not in key:
        return 'legacy'
    parts = key.split('|')
    return parts[1] if len(parts) >= 2 and parts[1] else 'unknown'


def _authenticate():
    """Returns the caller's Firebase UID, or None if unauthenticated."""
    return firebase_auth.authenticate_request(request.headers)


def _expected_revision(value):
    """Parses the additive sync CAS token without accepting bool as an int."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError
    try:
        revision = int(value)
    except (TypeError, ValueError):
        raise ValueError from None
    if revision < 0 or str(value).strip() != str(revision):
        raise ValueError
    return revision


def _valid_course_id(value) -> bool:
    return isinstance(value, str) and _COURSE_ID_RE.fullmatch(value) is not None


def _mutation_json(result, *, field: str):
    """Normalizes new typed results and old bool mocks during rollout."""
    if isinstance(result, bool):
        return {field: result}, 200 if result else 503
    if result.status == 'success':
        payload = {field: True}
        if result.revision is not None:
            payload['revision'] = result.revision
        return payload, 200
    if result.status == 'conflict':
        return {field: False, 'conflict': True}, 409
    return {field: False}, 503


def _incr_with_ttl(key: str, ttl_seconds: int) -> int | None:
    """INCRs `key`, setting a TTL on it the moment it's first created (so a
    window's counter always expires instead of accumulating forever) —
    shared by every counter below (hourly rate limit, daily import caps).
    Returns the post-increment count, or None if Redis is unreachable/not
    configured, which every caller treats as fail-open: a flaky or absent
    counter must not take the API down."""
    if not _UPSTASH_URL:
        return None
    resp = requests.post(
        f'{_UPSTASH_URL}/incr/{key}',
        headers={'Authorization': f'Bearer {_UPSTASH_TOKEN}'},
        timeout=10,
    )
    if resp.status_code != 200:
        return None
    count = resp.json().get('result', 0)
    if count == 1:
        requests.post(
            f'{_UPSTASH_URL}/expire/{key}/{ttl_seconds}',
            headers={'Authorization': f'Bearer {_UPSTASH_TOKEN}'},
            timeout=10,
        )
    return count


# Generous enough that even several full PDF imports (discovery + tens of
# parse calls + per-variant-group cache lookups) comfortably fit in one
# window, while still putting a hard, known ceiling on what a single
# compromised/malicious account could cost — the problem the old single
# shared APP_SECRET (embedded in every APK, extractable, unlimited) had no
# answer for at all.
_RATE_LIMIT_PER_HOUR = 1000


def _rate_limit_ok(uid: str) -> bool:
    window = int(time.time() // 3600)
    count = _incr_with_ttl(f'ratelimit|{uid}|{window}', 3600)
    return count is None or count <= _RATE_LIMIT_PER_HOUR


# Both Gemini keys are paid now (see generation_config.py's model split —
# discover alone runs ~$0.35/document on gemini-3.5-flash), so "free tier"
# no longer means "on a free quota" — it needs its own hard ceiling on the
# one call whose cost scales with document size instead of being a few
# cents per chunk. A single new-document discover call is the single most
# expensive thing this API ever does; these three caps bound it from three
# angles (per-account daily, and a service-wide daily circuit breaker) —
# see PRODUCT_PLAN.md Phase 0 for the reasoning and the free-tier policy
# this pairs with (free never reaches Gemini for discover at all, only
# ever reads the shared cache — enforced in parse() below).
_PREMIUM_DAILY_IMPORT_LIMIT = 5
_GLOBAL_DAILY_DISCOVER_LIMIT = 100


def _premium_import_cap_ok(uid: str) -> bool:
    day = time.strftime('%Y%m%d', time.gmtime())
    count = _incr_with_ttl(f'importcap|{uid}|{day}', 86400)
    return count is None or count <= _PREMIUM_DAILY_IMPORT_LIMIT


def _global_discover_cap_ok() -> bool:
    day = time.strftime('%Y%m%d', time.gmtime())
    count = _incr_with_ttl(f'discovercap|{day}', 86400)
    return count is None or count <= _GLOBAL_DAILY_DISCOVER_LIMIT


@app.after_request
def _cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


@app.route('/api/me', methods=['GET', 'OPTIONS'])
def me():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    return jsonify({'isPremium': firestore_client.is_premium(uid)})


@app.route('/api/device', methods=['POST', 'OPTIONS'])
def device():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    body = request.get_json(force=True)
    device_id = (body.get('deviceId') or '').strip()
    device_name = (body.get('deviceName') or 'Unknown Device').strip()
    if not device_id:
        return jsonify({'error': 'deviceId is required'}), 400

    allowed = firestore_client.check_and_register_device(uid, device_id, device_name)
    return jsonify({'allowed': allowed})


@app.route('/api/device/force', methods=['POST', 'OPTIONS'])
def device_force():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    body = request.get_json(force=True)
    device_id = (body.get('deviceId') or '').strip()
    device_name = (body.get('deviceName') or 'Unknown Device').strip()
    if not device_id:
        return jsonify({'error': 'deviceId is required'}), 400

    ok = firestore_client.force_register_device(uid, device_id, device_name)
    return jsonify({'ok': ok}), 200 if ok else 503


@app.route('/api/courses', methods=['GET', 'POST', 'OPTIONS'])
def courses():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        records = firestore_client.list_course_records(uid)
        if records is None:
            # Unlike the old fail-empty response, callers can now preserve
            # their last known local library and retry instead of treating an
            # outage as a remote deletion.
            return jsonify({'error': 'Course sync unavailable'}), 503
        parsed = []
        sync = []
        for record in records:
            if not _valid_course_id(record.course_id):
                continue
            sync.append({
                'id': record.course_id,
                'revision': record.revision,
                'deleted': record.deleted,
                'updatedAt': record.updated_at,
            })
            if record.deleted or not record.course_json:
                continue
            try:
                course = json.loads(record.course_json)
            except json.JSONDecodeError:
                continue
            # Historic/corrupt Firestore data must not smuggle a different or
            # path-like id into a client's local filename namespace.
            if (not isinstance(course, dict) or
                    course.get('id') != record.course_id or
                    not _valid_course_id(course.get('id'))):
                continue
            parsed.append(course)
        return jsonify({'courses': parsed, 'sync': sync})

    body = request.get_json(force=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'JSON object is required'}), 400
    course = body.get('course')
    if not isinstance(course, dict) or not _valid_course_id(course.get('id')):
        return jsonify({'error': 'course with an id is required'}), 400
    try:
        expected_revision = _expected_revision(body.get('expectedRevision'))
    except ValueError:
        return jsonify({'error': 'expectedRevision must be a non-negative integer'}), 400
    result = firestore_client.save_course(
        uid, course['id'], json.dumps(course), expected_revision)
    payload, status = _mutation_json(result, field='saved')
    return jsonify(payload), status


@app.route('/api/courses/<course_id>', methods=['DELETE', 'OPTIONS'])
def course_delete(course_id):
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    if not _valid_course_id(course_id):
        return jsonify({'error': 'invalid course id'}), 400
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return jsonify({'error': 'JSON object is required'}), 400
    raw_revision = request.args.get('expectedRevision')
    if raw_revision is None:
        raw_revision = body.get('expectedRevision')
    try:
        expected_revision = _expected_revision(raw_revision)
    except ValueError:
        return jsonify({'error': 'expectedRevision must be a non-negative integer'}), 400
    result = firestore_client.delete_course(uid, course_id, expected_revision)
    payload, status = _mutation_json(result, field='ok')
    return jsonify(payload), status


@app.route('/api/account', methods=['DELETE', 'OPTIONS'])
def account_delete():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401

    # Order matters: Firestore data first, then the Auth account. If we did
    # it the other way round and the Firestore step then failed, the user
    # would be locked out (Auth account gone) with their data still sitting
    # in Firestore forever — unrecoverable by them, and undiscoverable by
    # us since there's no account left to retry from. Deleting data first
    # means a failure here is still fully retryable (this call is
    # idempotent — a second attempt just finds already-empty
    # subcollections and a missing root doc, both of which count as
    # success).
    if not firestore_client.delete_user_data(uid):
        print(f'ACCOUNT_DELETE_ERROR uid={uid} stage=firestore')
        return jsonify({
            'error': 'Could not delete your data. Please try again or contact support.',
        }), 500

    if not firebase_auth.delete_user(uid):
        # The harder failure mode: data is already gone but the login
        # itself survives. Flagged distinctly (dataDeleted=True) so the
        # client can tell the user their data really is gone and only the
        # empty account shell needs a retry/support contact — not leave
        # them thinking nothing happened.
        print(f'ACCOUNT_DELETE_ERROR uid={uid} stage=auth')
        return jsonify({
            'error': 'Your data was deleted but the account could not be fully '
                     'removed. Please try again or contact support.',
            'dataDeleted': True,
        }), 500

    return jsonify({'ok': True})


@app.route('/api/convert', methods=['POST', 'OPTIONS'])
def convert():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    if not _rate_limit_ok(uid):
        return jsonify({'error': 'Rate limit exceeded'}), 429

    pdf_bytes = request.data
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
        f.write(pdf_bytes)
        tmp_path = f.name

    try:
        # Was MarkItDown().convert(tmp_path) — that call is, for a PDF,
        # nothing but pdfminer.high_level.extract_text() plus the two
        # normalization lines below (markitdown._markitdown._convert's
        # post-processing); everything else MarkItDown does before that
        # (magika-based file-type sniffing, pulling in onnxruntime+numpy,
        # ~126MB) is guessing a file type we already know — this file was
        # just written with suffix='.pdf' specifically for this call.
        # Calling pdfminer directly produces byte-identical output
        # (verified against the old code path on a real PDF) while
        # freeing enough of the function's size budget to fit PyMuPDF
        # (answer_markers.py) in the same deployment.
        with open(tmp_path, 'rb') as f:
            raw_text = pdfminer.high_level.extract_text(f)
        text = '\n'.join(line.rstrip() for line in re.split(r'\r?\n', raw_text))
        text = re.sub(r'\n{3,}', '\n\n', text)
        markdown = _inject_answer_markers(tmp_path, text)
        return jsonify({'markdown': markdown})
    except Exception as e:
        # The raw exception can embed the local tmp file path —
        # keep it server-side only, never in the client-facing response.
        print(f'CONVERT_ERROR {type(e).__name__}: {e}')
        return jsonify({'error': 'Could not convert this PDF.'}), 500
    finally:
        os.unlink(tmp_path)


class GeminiError(Exception):
    """Carries only a status code + a message safe to show a client.
    requests' HTTPError.__str__ embeds the full request URL — which
    includes '?key=<api_key>' since the key is passed as a query param —
    so it must never be allowed to propagate to jsonify({'error': str(e)})
    verbatim, or the API key leaks straight into the app's error UI."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _call_gemini(prompt: str, section_type: str = '', is_premium: bool = False) -> str:
    # Free-tier users always run against a separate, free Gemini API key —
    # its own quota is the actual cost ceiling, independent of anything a
    # client requests. Premium spend only ever hits the paid key.
    env_var = 'GEMINI_API_KEY' if is_premium else 'GEMINI_API_KEY_FREE'
    api_key = os.environ.get(env_var, '')
    model = _gemini_model(section_type)
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': generation_config.build(model, section_type),
    }

    # 503 (model momentarily overloaded, unrelated to any per-key quota) is
    # usually a several-second blip — worth one short retry here. 429
    # (quota) is a different story: Gemini's own error tells us to wait
    # ~15-20s, far longer than makes sense to hold a serverless function
    # open for — the client already retries with exactly that kind of
    # delay (see ParseService._parseWithRetry), so 429 fails fast here and
    # lets the client's longer-horizon retry handle it instead of two
    # short, ineffective waits stacking on top of each other.
    last_status = 500
    for attempt in range(3):
        try:
            resp = requests.post(
                _gemini_url(model),
                params={'key': api_key},
                json=payload,
                # The structure-discovery call sends the whole document
                # (~150K tokens) — prefill of a context that large needs
                # more room than our usual small per-variant calls.
                timeout=100,
            )
        except requests.RequestException as e:
            raise GeminiError(502, f'Could not reach Gemini: {type(e).__name__}') from e

        if resp.status_code == 200:
            data = resp.json()
            # Structured single-line log for scripts/cost_report.py (and
            # `vercel logs | grep GEMINI_USAGE`) to parse — keep the tag and
            # key=value shape in sync with that script if either changes.
            # usageMetadata (and any of its three fields) can be absent from
            # the response; default everything to 0 rather than let a
            # missing key blow up a successful parse.
            usage = data.get('usageMetadata') or {}
            print(
                'GEMINI_USAGE '
                f'section_type={section_type or "unknown"} '
                f'tariff={"premium" if is_premium else "free"} '
                f'prompt_tokens={usage.get("promptTokenCount", 0)} '
                f'candidates_tokens={usage.get("candidatesTokenCount", 0)} '
                f'thoughts_tokens={usage.get("thoughtsTokenCount", 0)}'
            )
            return data['candidates'][0]['content']['parts'][0]['text']

        last_status = resp.status_code
        if resp.status_code == 503 and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        break

    if last_status == 429:
        raise GeminiError(429, 'Gemini rate limit reached — please try again in a moment.')
    raise GeminiError(502, f'Gemini request failed (HTTP {last_status}).')


@app.route('/api/parse', methods=['POST', 'OPTIONS'])
def parse():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    if not _rate_limit_ok(uid):
        return jsonify({'error': 'Rate limit exceeded'}), 429

    body = request.get_json(force=True)
    markdown = body.get('markdown', '')
    section_type = body.get('section_type', '')

    prompt_template = PROMPTS.get(section_type)
    if not prompt_template:
        return jsonify({'error': f'Unknown section_type: {section_type}'}), 400

    premium = firestore_client.is_premium(uid)

    # Discovery is the single most expensive call this API makes (whole
    # document, pricier model — see generation_config.py) and, unlike
    # every other call, its cost scales with document size instead of
    # being a few cents per chunk. The client already checks the shared
    # `/api/cache` doc/discover cache before ever calling this endpoint
    # (see ParseService.discoverSections), so reaching here for a
    # 'discover' request always means a real cache miss — a genuinely new
    # document. Free tier never gets to trigger that Gemini call at all:
    # it can only ever benefit from a document some premium import (or a
    # curated cache pre-warm, see PRODUCT_PLAN.md Phase 1) already paid
    # for. Premium still gets a hard daily ceiling per account, plus a
    # service-wide daily circuit breaker, so a single (or many
    # coordinated) compromised account(s) can't run up an unbounded bill.
    if section_type == 'discover':
        if not premium:
            print(f'DISCOVER_FREE_REJECTED uid={uid[:8]}')
            return jsonify({
                'error': 'This document has not been processed before — new '
                         'documents require Premium. Free tier can open any '
                         'document a Premium import has already used.'
            }), 403
        if not _premium_import_cap_ok(uid):
            print(f'DISCOVER_IMPORT_CAP_REJECTED uid={uid[:8]}')
            return jsonify({
                'error': 'Daily limit for new documents reached — try again '
                         'tomorrow.'
            }), 429
        if not _global_discover_cap_ok():
            print('DISCOVER_GLOBAL_CAP_REJECTED')
            return jsonify({
                'error': 'Service is busy processing new documents right now '
                         '— please try again later.'
            }), 503

    # The client only sends one variant group per section for free users,
    # but that's a courtesy, not a boundary — a modified client, or a PDF
    # deliberately relabeled so many/all real exercises in a section claim
    # to be "variant 1, edition <N>", could send arbitrarily more. Two
    # independent checks, since either alone is gameable: enough small
    # relabeled editions stay under the char cap, and padding one edition's
    # text stays under the count cap. Both together track our real observed
    # data — the largest legitimate single-variant group we've measured is
    # ~10.3K chars across 6 editions (hoeren_teil1) — with real headroom
    # above it, not against it.
    _FREE_TIER_MAX_CHARS = 12000
    _FREE_TIER_MAX_EDITIONS = 8
    if not premium and section_type != 'discover':
        edition_count = markdown.count('<<<ITEM>>>') + 1
        if len(markdown) > _FREE_TIER_MAX_CHARS or edition_count > _FREE_TIER_MAX_EDITIONS:
            return jsonify({
                'error': 'Free tier content limit exceeded — upgrade to premium '
                         'for full documents.'
            }), 403

    # Span-backed fields are extracted as line pointers instead of retyped
    # text (telefonnotiz bullets plus selected universal texts). Their
    # prompts need the same numbered-line format discovery already uses;
    # `markdown` stays raw so the resolver can slice it after generation.
    prompt_markdown = markdown
    if section_type == 'telefonnotiz' or section_type in SPAN_TEXT_SECTION_TYPES:
        prompt_markdown = line_extraction.number_markdown(markdown)

    prompt = prompt_template.replace('{markdown}', prompt_markdown)

    text = ''
    try:
        text = _call_gemini(prompt, section_type, is_premium=premium).strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        if section_type == 'discover':
            # The discover prompt's numbered-line input ("00042: ...")
            # sometimes leaked zero-padded numbers straight into the JSON
            # output ("start_line": 00042), which isn't valid JSON
            # (leading zeros are illegal in JSON numbers) — strip them
            # defensively. Scoped to discover ONLY: this used to run
            # unconditionally on every section_type's response, which
            # meant a parsed dialogue/letter containing a clock time like
            # "16:05," (colon, leading zero, delimiter — the same shape
            # this regex targets) would get silently mangled to "16:5,".
            # responseSchema on the discover call (see response_schemas.py)
            # should make this unreachable going forward — kept as a
            # harmless fallback rather than removed outright.
            text = re.sub(r':\s*0+(\d+)(?=[,\s}\]])', r': \1', text)
            return jsonify(json.loads(text))

        parsed = json.loads(text)
        if section_type == 'hoeren_teil1':
            parsed = span_resolution.normalize_h1_variant_numbers(parsed, markdown)
        if section_type == 'telefonnotiz':
            parsed = span_resolution.resolve_telefonnotiz_spans(parsed, markdown)
        if section_type in SPAN_TEXT_SECTION_TYPES:
            parsed = span_resolution.resolve_universal_text_spans(
                parsed,
                markdown,
                section_type=section_type,
            )
        parsed = span_resolution.sanitize_parser_metadata(parsed)
        return jsonify(parsed)
    except GeminiError as e:
        return jsonify({'error': str(e)}), e.status_code
    except json.JSONDecodeError as e:
        print(f'PARSE_JSON_ERROR {e}: raw={text[:500]!r}')
        return jsonify({'error': 'Gemini returned malformed data — please retry.'}), 500
    except Exception as e:
        print(f'PARSE_ERROR {type(e).__name__}: {e}')
        return jsonify({'error': 'Could not parse this section.'}), 500


@app.route('/api/cache', methods=['GET', 'POST', 'OPTIONS'])
def cache_endpoint():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    if not _rate_limit_ok(uid):
        return jsonify({'error': 'Rate limit exceeded'}), 429

    if request.method == 'GET':
        content_hash = request.args.get('hash', '')
        if not content_hash:
            return jsonify({'error': 'hash is required'}), 400
        cached = _cache_get(content_hash)
        hit = cached is not None
        # Structured single-line log for scripts/cost_report.py / grepping
        # `vercel logs` — key format matches GEMINI_USAGE's key=value shape.
        print(f'CACHE_LOOKUP hit={hit} key_type={_cache_key_type(content_hash)}')
        if not hit:
            return jsonify({'hit': False})
        return jsonify({'hit': True, 'value': json.loads(cached)})

    # Generic hash -> JSON value store, used both for whole-course results
    # (keyed by a hash of the full document) and per-variant-group parse
    # results (keyed by a hash of just that group's text) — same store,
    # different granularity of what's being cached.
    body = request.get_json(force=True)
    content_hash = body.get('hash', '')
    value = body.get('value')
    if not content_hash or value is None:
        return jsonify({'error': 'hash and value are required'}), 400
    # Parsed content never changes for the same input text — cache
    # permanently rather than picking an arbitrary TTL.
    _cache_set(content_hash, json.dumps(value))
    return jsonify({'ok': True})


@app.route('/api/tts', methods=['POST', 'OPTIONS'])
def tts_endpoint():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    uid = _authenticate()
    if not uid:
        return jsonify({'error': 'Unauthorized'}), 401
    if not _rate_limit_ok(uid):
        return jsonify({'error': 'Rate limit exceeded'}), 429

    body = request.get_json(force=True)
    text = (body.get('text') or '').strip()
    speaker = body.get('speaker') or ''
    voice_gender = body.get('voice_gender') if 'voice_gender' in body else None
    if 'voice_gender' in body and voice_gender not in {'female', 'male', 'unknown'}:
        return jsonify({
            'error': 'voice_gender must be one of: female, male, unknown',
        }), 400
    if not text:
        return jsonify({'error': 'text is required'}), 400
    if len(text) > 2000:
        return jsonify({'error': 'text too long (max 2000 chars per line)'}), 400

    voice = tts.voice_for(speaker, text, voice_gender)
    try:
        audio_bytes = asyncio.run(tts.synthesize(text, voice))
        return Response(audio_bytes, mimetype='audio/mpeg')
    except Exception as e:
        print(f'TTS_ERROR {type(e).__name__}: {e}')
        return jsonify({'error': 'Could not generate audio for this line.'}), 500
