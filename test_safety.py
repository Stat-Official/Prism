"""Regression tests for offline export and clean on a fresh checkout."""
import shutil
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prism_core.publishing import standalone, cleanup
from prism_core.common import locking

ROOT = Path(__file__).resolve().parent


class PortalSafetyTests(unittest.TestCase):
    def test_export_keeps_existing_standalone_and_includes_both_styles(self):
        original = ROOT / 'Prism.html'
        initial_hash = hashlib.sha256(original.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(ROOT / 'web', root / 'web')
            existing = root / 'Prism.html'
            existing.write_bytes(original.read_bytes())
            with patch.object(standalone, 'BASE', root):
                with self.assertRaises(FileExistsError):
                    standalone.export_html()
                self.assertEqual(hashlib.sha256(existing.read_bytes()).hexdigest(), initial_hash)
                exported = standalone.export_html(output='Prism.preview.html')
                html = exported.read_text(encoding='utf-8')
                self.assertIn('.editorial-hero', html)
                self.assertIn('.site-footer', html)
                self.assertNotIn('href="assets/css/editorial.css', html)
                self.assertIn('id="prism-data"', html)
                match = re.search(r'<script id="prism-data" type="application/json">(.*?)</script>', html, re.S)
                self.assertEqual(len(json.loads(match.group(1))['articles']), 4)
                with self.assertRaises(ValueError):
                    standalone.export_html(output='../outside.html')
            self.assertEqual(hashlib.sha256(existing.read_bytes()).hexdigest(), initial_hash)

    def test_clean_fresh_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'web').mkdir()
            with patch.object(cleanup, 'BASE', root), patch.object(locking, 'BASE', root):
                output = cleanup.clean_snapshots()
                self.assertEqual(output['kept'], [])
                self.assertEqual(output['candidates'], [])
                self.assertFalse(output['applied'])
                self.assertEqual(cleanup.clean_snapshots(apply=True)['removed'], [])


if __name__ == '__main__':
    unittest.main()
