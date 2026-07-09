import os
import json
import re
import time
import asyncio
import tempfile
import requests
from flask import Flask, request, jsonify, Response
from markitdown import MarkItDown
from prompts import PROMPTS
import firebase_auth
import firestore_client
import tts

app = Flask(__name__)

_GEMINI_MODELS = {
    # gemini-2.5-flash-lite was tried and rejected for discovery — it
    # reliably dropped the "other" filler-block markers, reintroducing
    # the runaway-chunk bug. gemini-3.1-flash-lite (default below) does
    # not have that problem: verified via promptfoo/ across discovery
    # and all 12 parse section types (24/24 passing, ~48% cheaper than
    # 2.5 Flash on this workload). Empty for now — override a specific
    # section_type here if a future model swap needs one.
}
_DEFAULT_GEMINI_MODEL = 'gemini-3.1-flash-lite'


def _gemini_model(section_type: str) -> str:
    return _GEMINI_MODELS.get(section_type, _DEFAULT_GEMINI_MODEL)


def _gemini_url(model: str) -> str:
    return (
        f'https://generativelanguage.googleapis.com/v1beta/models/'
        f'{model}:generateContent'
    )


def _generation_config(model: str) -> dict:
    # Gemini 3.x renamed the thinking-budget knob: it's a coarse
    # thinkingLevel (MINIMAL/LOW/MEDIUM/HIGH), not a token budget.
    if model.startswith('gemini-3'):
        return {
            'temperature': 1,
            'thinkingConfig': {'thinkingLevel': 'MINIMAL'},
        }
    return {
        'temperature': 1,  # required when thinkingBudget=0
        'thinkingConfig': {'thinkingBudget': 0},
    }

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


# Generous enough that even several full PDF imports (discovery + tens of
# parse calls + per-variant-group cache lookups) comfortably fit in one
# window, while still putting a hard, known ceiling on what a single
# compromised/malicious account could cost — the problem the old single
# shared APP_SECRET (embedded in every APK, extractable, unlimited) had no
# answer for at all.
_RATE_LIMIT_PER_HOUR = 1000


def _rate_limit_ok(uid: str) -> bool:
    if not _UPSTASH_URL:
        return True  # rate limiting is a hardening layer, not a hard dependency
    window = int(time.time() // 3600)
    key = f'ratelimit|{uid}|{window}'
    resp = requests.post(
        f'{_UPSTASH_URL}/incr/{key}',
        headers={'Authorization': f'Bearer {_UPSTASH_TOKEN}'},
        timeout=10,
    )
    if resp.status_code != 200:
        return True  # fail open — a flaky rate limiter must not take the API down
    count = resp.json().get('result', 0)
    if count == 1:
        requests.post(
            f'{_UPSTASH_URL}/expire/{key}/3600',
            headers={'Authorization': f'Bearer {_UPSTASH_TOKEN}'},
            timeout=10,
        )
    return count <= _RATE_LIMIT_PER_HOUR


@app.after_request
def _cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
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
        result = MarkItDown().convert(tmp_path)
        return jsonify({'markdown': result.text_content})
    except Exception as e:
        # MarkItDown's raw exception can embed the local tmp file path —
        # keep it server-side only, never in the client-facing response.
        print(f'CONVERT_ERROR {type(e).__name__}: {e}')
        return jsonify({'error': 'Could not convert this PDF.'}), 500
    finally:
        os.unlink(tmp_path)


def _call_gemini(prompt: str, section_type: str = '', is_premium: bool = False) -> str:
    # Free-tier users always run against a separate, free Gemini API key —
    # its own quota is the actual cost ceiling, independent of anything a
    # client requests. Premium spend only ever hits the paid key.
    env_var = 'GEMINI_API_KEY' if is_premium else 'GEMINI_API_KEY_FREE'
    api_key = os.environ.get(env_var, '')
    model = _gemini_model(section_type)
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': _generation_config(model),
    }
    resp = requests.post(
        _gemini_url(model),
        params={'key': api_key},
        json=payload,
        # The structure-discovery call sends the whole document (~150K
        # tokens) — prefill of a context that large needs more room than
        # our usual small per-variant calls.
        timeout=100,
    )
    resp.raise_for_status()
    data = resp.json()
    return data['candidates'][0]['content']['parts'][0]['text']


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

    prompt = prompt_template.replace('{markdown}', markdown)

    text = ''
    try:
        text = _call_gemini(prompt, section_type, is_premium=premium).strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        # The discover prompt's numbered-line input ("00042: ...") sometimes
        # leaks zero-padded numbers straight into the JSON output
        # ("start_line": 00042), which isn't valid JSON (leading zeros are
        # illegal in JSON numbers) — strip them defensively.
        text = re.sub(r':\s*0+(\d+)(?=[,\s}\]])', r': \1', text)
        return jsonify(json.loads(text))
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
    if not text:
        return jsonify({'error': 'text is required'}), 400
    if len(text) > 2000:
        return jsonify({'error': 'text too long (max 2000 chars per line)'}), 400

    voice = tts.voice_for(speaker, text)
    try:
        audio_bytes = asyncio.run(tts.synthesize(text, voice))
        return Response(audio_bytes, mimetype='audio/mpeg')
    except Exception as e:
        print(f'TTS_ERROR {type(e).__name__}: {e}')
        return jsonify({'error': 'Could not generate audio for this line.'}), 500
