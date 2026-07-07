import os
import json
import re
import asyncio
import tempfile
import requests
from flask import Flask, request, jsonify, Response
from markitdown import MarkItDown
from prompts import PROMPTS
import tts

app = Flask(__name__)

_GEMINI_MODELS = {
    # Tried gemini-2.5-flash-lite for discovery (cheaper, faster) but it
    # reliably dropped the "other" filler-block markers even though it
    # found a comparable number of real items — that reintroduces the
    # runaway-chunk bug (an exercise's boundary swallowing a filler
    # section into its own text). Not worth the ~7% cost saving. Keeping
    # this dict so a future, better-tuned attempt is a one-line change.
}
_DEFAULT_GEMINI_MODEL = 'gemini-2.5-flash'


def _gemini_url(section_type: str) -> str:
    model = _GEMINI_MODELS.get(section_type, _DEFAULT_GEMINI_MODEL)
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


def _check_auth():
    return request.headers.get('X-App-Secret') == os.environ.get('APP_SECRET')


@app.after_request
def _cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-App-Secret'
    return response


@app.route('/api/convert', methods=['POST', 'OPTIONS'])
def convert():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    if not _check_auth():
        return jsonify({'error': 'Unauthorized'}), 401

    pdf_bytes = request.data
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
        f.write(pdf_bytes)
        tmp_path = f.name

    try:
        result = MarkItDown().convert(tmp_path)
        return jsonify({'markdown': result.text_content})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        os.unlink(tmp_path)


def _call_gemini(prompt: str, section_type: str = '') -> str:
    api_key = os.environ.get('GEMINI_API_KEY', '')
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 1,  # required when thinkingBudget=0
            'thinkingConfig': {'thinkingBudget': 0},
        },
    }
    resp = requests.post(
        _gemini_url(section_type),
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
    if not _check_auth():
        return jsonify({'error': 'Unauthorized'}), 401

    body = request.get_json(force=True)
    markdown = body.get('markdown', '')
    section_type = body.get('section_type', '')

    prompt_template = PROMPTS.get(section_type)
    if not prompt_template:
        return jsonify({'error': f'Unknown section_type: {section_type}'}), 400

    prompt = prompt_template.replace('{markdown}', markdown)

    text = ''
    try:
        text = _call_gemini(prompt, section_type).strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        # The discover prompt's numbered-line input ("00042: ...") sometimes
        # leaks zero-padded numbers straight into the JSON output
        # ("start_line": 00042), which isn't valid JSON (leading zeros are
        # illegal in JSON numbers) — strip them defensively.
        text = re.sub(r':\s*0+(\d+)(?=[,\s}\]])', r': \1', text)
        return jsonify(json.loads(text))
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Invalid JSON from Gemini: {e}', 'raw': text[:500]}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cache', methods=['GET', 'POST', 'OPTIONS'])
def cache_endpoint():
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    if not _check_auth():
        return jsonify({'error': 'Unauthorized'}), 401

    if request.method == 'GET':
        content_hash = request.args.get('hash', '')
        if not content_hash:
            return jsonify({'error': 'hash is required'}), 400
        cached = _cache_get(content_hash)
        if cached is None:
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
    if not _check_auth():
        return jsonify({'error': 'Unauthorized'}), 401

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
        return jsonify({'error': str(e)}), 500
