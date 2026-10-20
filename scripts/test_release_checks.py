"""发行门禁的标准库回归；只使用内存假暂存区和临时目录。"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import check_release as release


class SecretChecks(unittest.TestCase):
    def test_all_supported_secret_types_are_blocked_without_printing_values(self):
        samples = [
            'a' * 32 + '.' + 'B' * 8,
            'sk-' + 'Z9_' * 12,
            'sk-' + 'proj-' + 'A7b_' * 12,
            'ghp_' + 'A8b' * 12,
            'github_' + 'pat_' + 'A8b_' * 22,
            'SESSDATA=' + 'A9b%2C' * 8,
            '"bili_jct": "' + 'a1' * 16 + '"',
            '.bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\t' + 'A9b' * 12,
            '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----',
        ]
        for sample in samples:
            with self.subTest(kind=sample.split('=')[0][:8]):
                errors = release.secret_errors('notes.txt', sample.encode())
                self.assertTrue(errors)
                self.assertNotIn(sample, '\n'.join(errors))
                self.assertIn('notes.txt:1', errors[0])

    def test_test_prefix_does_not_bypass_secret_checks(self):
        for prefix in ('test', 'fake', 'demo', 'x'):
            sample = 'sk-' + prefix + 'A1b2C3d4' * 8
            self.assertTrue(release.secret_errors('backend/tests/test_any.py', sample.encode()))

    def test_fixed_placeholder_is_bound_to_exact_file_and_complete_value(self):
        name = 'backend/tests/test_known.py'
        sample = 'sk-' + 'synthetic-known-' + 'x' * 24
        digest = hashlib.sha256(sample.encode()).hexdigest()
        with patch.object(release, 'TEST_PLACEHOLDER_SHA256', {name: frozenset({digest})}):
            self.assertEqual(release.secret_errors(name, sample.encode()), [])
            self.assertTrue(release.secret_errors(name, (sample + 'q').encode()))
            self.assertTrue(release.secret_errors('frontend/tests/test_known.ts', sample.encode()))

    def test_paths_block_directory_segments_before_reading_content(self):
        for name in ('config.yaml', 'nested/Config.yaml', 'node_modules/p/index.js',
                     'backend/.venv/lib/file.py', 'data/private.json', 'nested/cache/private.txt',
                     'x/.CACHE/private.txt', 'scripts/docs/node_modules/a.js', '__pycache__/a.pyc'):
            with self.subTest(path=name):
                self.assertTrue(release.blocked_staged_path(name))
                with patch.object(release, 'git', return_value=name + '\0'), \
                        patch.object(release.subprocess, 'check_output') as read:
                    errors, count = release.staged_errors()
                self.assertTrue(errors)
                self.assertEqual(count, 1)
                read.assert_not_called()
        for name in ('config.example.yaml', 'backend/dubforge/config.py', 'docs/images/cache-guide.webp'):
            self.assertFalse(release.blocked_staged_path(name))

    def test_reviewed_journal_redaction_fixture_is_exact_and_file_bound(self):
        name = 'backend/tests/test_llm_conversations.py'
        source = (release.ROOT / name).read_text(encoding='utf-8-sig')
        sample = release.SECRETS[1][1].search(source).group()
        self.assertEqual(release.secret_errors(name, sample.encode()), [])
        self.assertTrue(release.secret_errors(name, (sample + 'x').encode()))
        self.assertTrue(release.secret_errors('backend/tests/test_other.py', sample.encode()))

    def test_staged_scans_blob_not_worktree_and_preserves_nul_names(self):
        name = 'notes/演示 file.txt'
        payload = ('github_' + 'pat_' + 'A7b_' * 22).encode()
        with patch.object(release, 'git', return_value=name + '\0'), \
                patch.object(release.subprocess, 'check_output', return_value=payload) as read:
            errors, count = release.staged_errors()
        self.assertEqual(count, 1)
        self.assertTrue(errors)
        self.assertEqual(read.call_args.args[0], ['git', 'show', ':' + name])

    def test_short_documentation_markers_are_not_real_credentials(self):
        text = 'API 前缀 sk-、ghp_、github_pat_；SESSDATA=<会话>；bili_jct=示例'
        self.assertEqual(release.secret_errors('guide.md', text.encode()), [])


class VersionChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        for folder in ('backend/dubforge', 'frontend'):
            (self.root / folder).mkdir(parents=True)
        (self.root / 'backend/dubforge/__init__.py').write_text("__version__ = '1.0.0'\n", encoding='utf-8')
        self.write_json('frontend/package.json', {'version': '1.0.0'})
        self.write_json('frontend/package-lock.json', {'version': '1.0.0', 'packages': {'': {'version': '1.0.0'}}})
        self.write_python_project()
        self.write_lock(['1.0.0'])
        (self.root / 'CHANGELOG.md').write_text('## 1.0.0 (synthetic)\n', encoding='utf-8')
        self.scope = patch.object(release, 'ROOT', self.root)
        self.scope.start()
        self.addCleanup(self.scope.stop)
        self.addCleanup(self.temporary.cleanup)

    def write_json(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding='utf-8')

    def write_python_project(self, *, static='', dynamic='["version"]', source='dubforge/__init__.py'):
        (self.root / 'backend/pyproject.toml').write_text(
            '[project]\nname="dubforge"\n' + static + '\ndynamic=' + dynamic
            + '\n[tool.hatch.version]\npath="' + source + '"\n', encoding='utf-8')

    def write_lock(self, versions, source='{ editable = "." }'):
        text = '\n'.join('[[package]]\nname="dubforge"\n'
                         + ('' if version is None else 'version="' + version + '"\n')
                         + ('' if source is None else 'source=' + source + '\n') for version in versions)
        (self.root / 'backend/uv.lock').write_text(text or 'package=[]\n', encoding='utf-8')

    def test_matching_dynamic_version_passes(self):
        self.assertEqual(release.version_errors(), ([], '1.0.0'))

    def test_local_editable_dynamic_lock_can_omit_version(self):
        self.write_lock([None])
        self.assertEqual(release.version_errors(), ([], '1.0.0'))

    def test_dynamic_lock_requires_exact_current_editable_source(self):
        for source in (None, '{ editable = "../other" }', '{ registry = "https://example.invalid" }'):
            with self.subTest(source=source):
                self.write_lock([None], source)
                self.assertTrue(release.version_errors()[0])

    def test_missing_or_duplicate_uv_project_is_rejected(self):
        for values in ([], ['1.0.0', '1.0.0'], [None, None]):
            self.write_lock(values)
            self.assertTrue(release.version_errors()[0])

    def test_uv_version_mismatch_is_rejected(self):
        self.write_lock(['9.9.9'])
        self.assertTrue(release.version_errors()[0])

    def test_python_static_or_missing_dynamic_source_is_rejected(self):
        for kwargs in ({'static': 'version="9.9.9"'}, {'dynamic': '[]'},
                       {'dynamic': '"version"'}, {'source': 'another.py'}):
            with self.subTest(settings=kwargs):
                self.write_python_project(**kwargs)
                self.assertTrue(release.version_errors()[0])

    def test_frontend_lock_and_changelog_must_match(self):
        self.write_json('frontend/package-lock.json', {'version': '1.0.0', 'packages': {'': {'version': '9.9.9'}}})
        (self.root / 'CHANGELOG.md').write_text('## 0.1.0 (synthetic)\n', encoding='utf-8')
        errors, _ = release.version_errors()
        self.assertEqual(len(errors), 2)


class DocumentationChecks(unittest.TestCase):
    def test_supported_local_links_images_and_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'image file.webp').write_bytes(b'fixture')
            (root / 'README.md').write_text(
                '[local](image%20file.webp)\n<img src="image file.webp">\n'
                '[external](https://example.invalid/anything)\n[escape](../outside.md)\n'
                '```md\n[example](not-a-real-file)\n```\n', encoding='utf-8')
            with patch.object(release, 'ROOT', root), patch.object(release, 'CURRENT_DOCS', ('README.md',)):
                errors, count = release.docs_errors()
            self.assertEqual(count, 3)
            self.assertEqual(len(errors), 1)
            self.assertIn('outside.md', errors[0])

    def test_anchor_and_reference_links_are_explicitly_out_of_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'README.md').write_text('[heading](#not-verified)\n[reference][missing]\n', encoding='utf-8')
            with patch.object(release, 'ROOT', root), patch.object(release, 'CURRENT_DOCS', ('README.md',)):
                self.assertEqual(release.docs_errors(), ([], 0))

    def test_main_reports_scope_and_nonzero_exit_for_actual_block(self):
        sample = 'sk-' + 'A9b_' * 12
        with patch.object(release, 'docs_errors', return_value=([], 3)), \
                patch.object(release, 'version_errors', return_value=([], '1.0.0')), \
                patch.object(release, 'git', return_value='notes.txt\0'), \
                patch.object(release.subprocess, 'check_output', return_value=sample.encode()), \
                patch('sys.argv', ['check_release.py', '--staged']), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(release.main(), 1)
        data = json.loads(output.getvalue())
        self.assertFalse(data['passed'])
        self.assertIn('不解析引用式链接', data['docs_scope'])
        self.assertNotIn(sample, output.getvalue())


if __name__ == '__main__':
    unittest.main()
