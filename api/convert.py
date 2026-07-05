import os
import tempfile
import json
from markitdown import MarkItDown
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.headers.get('X-App-Secret') != os.environ.get('APP_SECRET'):
            self._send(401, {'error': 'Unauthorized'})
            return

        length = int(self.headers.get('Content-Length', 0))
        pdf_bytes = self.rfile.read(length)

        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        try:
            md = MarkItDown()
            result = md.convert(tmp_path)
            self._send(200, {'markdown': result.text_content})
        except Exception as e:
            self._send(500, {'error': str(e)})
        finally:
            os.unlink(tmp_path)

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
