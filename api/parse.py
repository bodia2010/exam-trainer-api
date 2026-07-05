import os
import json
import google.generativeai as genai
from http.server import BaseHTTPRequestHandler
from prompts import PROMPTS

genai.configure(api_key=os.environ.get('GEMINI_API_KEY', ''))
model = genai.GenerativeModel(
    'gemini-2.5-flash',
    generation_config=genai.GenerationConfig(temperature=0),
)


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.headers.get('X-App-Secret') != os.environ.get('APP_SECRET'):
            self._send(401, {'error': 'Unauthorized'})
            return

        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length))
        markdown = body.get('markdown', '')
        section_type = body.get('section_type', '')

        prompt_template = PROMPTS.get(section_type)
        if not prompt_template:
            self._send(400, {'error': f'Unknown section_type: {section_type}'})
            return

        prompt = prompt_template.replace('{markdown}', markdown)

        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            # Strip ```json ... ``` wrapper if Gemini added it
            if text.startswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
            result = json.loads(text)
            self._send(200, result)
        except json.JSONDecodeError as e:
            self._send(500, {'error': f'Invalid JSON from Gemini: {e}', 'raw': text[:500]})
        except Exception as e:
            self._send(500, {'error': str(e)})

    def _cors(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-App-Secret')

    def _send(self, code, data):
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
