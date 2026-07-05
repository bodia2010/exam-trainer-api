import os
import json
import tempfile
from flask import Flask, request, jsonify
from markitdown import MarkItDown
import google.generativeai as genai
from prompts import PROMPTS

app = Flask(__name__)

genai.configure(api_key=os.environ.get('GEMINI_API_KEY', ''))
_model = genai.GenerativeModel(
    'gemini-2.5-flash',
    generation_config=genai.GenerationConfig(temperature=0),
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

    try:
        response = _model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return jsonify(json.loads(text))
    except json.JSONDecodeError as e:
        return jsonify({'error': f'Invalid JSON from Gemini: {e}', 'raw': text[:500]}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500
