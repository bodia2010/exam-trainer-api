import os
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_DIR = Path(__file__).resolve().parents[1]
PROMPTFOO_DIR = PROJECT_DIR / 'promptfoo'
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROMPTFOO_DIR))

import fixture_loader  # noqa: E402
import prompt_discover  # noqa: E402
import prompt_parse  # noqa: E402


class RawFixtureLoaderTest(unittest.TestCase):
    def _fixture(self, content: bytes) -> str:
        fd, name = tempfile.mkstemp(prefix='.raw-loader-test-', dir=PROMPTFOO_DIR)
        os.close(fd)
        path = Path(name)
        path.write_bytes(content)
        self.addCleanup(path.unlink, missing_ok=True)
        return path.relative_to(PROMPTFOO_DIR).as_posix()

    def test_preserves_leading_form_feed_and_all_surrounding_whitespace(self):
        raw = b'\x0cHoren Teil 1\r\nsecond page\x0c\n\n'
        path = self._fixture(raw)

        loaded = fixture_loader.load_markdown({'vars': {'markdown_path': path}})

        self.assertEqual(loaded.encode('utf-8'), raw)

    def test_inline_markdown_remains_supported_for_unit_tests(self):
        self.assertEqual(
            fixture_loader.load_markdown({'vars': {'markdown': '\x0cinline\n'}}),
            '\x0cinline\n',
        )

    def test_rejects_paths_outside_promptfoo_directory(self):
        with self.assertRaisesRegex(ValueError, 'inside the promptfoo directory'):
            fixture_loader.load_markdown({'vars': {'markdown_path': '../prompts.py'}})

    def test_parse_prompt_receives_untrimmed_fixture(self):
        path = self._fixture(b'\x0cHoren raw parse fixture\n')

        prompt = prompt_parse.get_prompt({
            'vars': {'section_type': 'hoeren_teil1', 'markdown_path': path},
        })

        self.assertIn('\x0cHoren raw parse fixture\n', prompt)

    def test_discover_prompt_receives_untrimmed_fixture(self):
        path = self._fixture(b'\x0cRaw discover fixture\n')

        prompt = prompt_discover.get_prompt({
            'vars': {'section_type': 'discover', 'markdown_path': path},
        })

        self.assertIn('\x0cRaw discover fixture\n', prompt)


if __name__ == '__main__':
    unittest.main()
