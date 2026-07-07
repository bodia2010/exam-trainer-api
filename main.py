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

_GEMINI_URL = (
    'https://generativelanguage.googleapis.com/v1beta/models/'
    'gemini-2.5-flash:generateContent'
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


def _call_gemini(prompt: str) -> str:
    api_key = os.environ.get('GEMINI_API_KEY', '')
    payload = {
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {
            'temperature': 1,  # required when thinkingBudget=0
            'thinkingConfig': {'thinkingBudget': 0},
        },
    }
    resp = requests.post(
        _GEMINI_URL,
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
        text = _call_gemini(prompt).strip()
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
