"""Verify environment-file integration using Docker Compose's real parser."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nginx_config import base_from_env_file, render_locations

ROOT = Path(__file__).resolve().parent
TEMP_ROOT = ROOT / '.build/env-tests'
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


class EnvironmentIntegration(unittest.TestCase):
    def test_compose_dotenv_syntax_and_nested_prefixes(self):
        cases = [('APP_BASE_PATH=/\n', '/'),
                 ('APP_BASE_PATH="/s2a/" # comment\n', '/s2a/'),
                 ("APP_BASE_PATH='/tools/ai'\n", '/tools/ai/'),
                 ('PREFIX=/nested\nAPP_BASE_PATH=${PREFIX}/app/\n', '/nested/app/')]
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder, patch.dict(os.environ):
            self.assertTrue(Path(folder).resolve().is_relative_to(ROOT))
            os.environ.pop('APP_BASE_PATH', None)
            os.environ.pop('PREFIX', None)
            path = Path(folder) / '.env'
            for content, expected in cases:
                with self.subTest(expected=expected):
                    path.write_text(content, encoding='utf8')
                    actual = base_from_env_file(path)
                    self.assertEqual(actual, expected)
                    self.assertIn('location ^~ ' + expected + ' {', render_locations(actual))

    def test_missing_or_invalid_prefix_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder, patch.dict(os.environ):
            self.assertTrue(Path(folder).resolve().is_relative_to(ROOT))
            os.environ.pop('APP_BASE_PATH', None)
            path = Path(folder) / '.env'
            for content in ['UNRELATED=value\n', 'APP_BASE_PATH=//other.example/\n', 'APP_BASE_PATH=/a/../b\n']:
                path.write_text(content, encoding='utf8')
                with self.subTest(content=content), self.assertRaises(ValueError):
                    base_from_env_file(path)

    def test_follows_compose_shell_override_precedence(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder, patch.dict(os.environ, APP_BASE_PATH='/override/'):
            self.assertTrue(Path(folder).resolve().is_relative_to(ROOT))
            path = Path(folder) / '.env'
            path.write_text('APP_BASE_PATH=/file/\n', encoding='utf8')
            self.assertEqual(base_from_env_file(path), '/override/')


if __name__ == '__main__':
    unittest.main()
