"""检查发行版本、暂存内容及当前行内链接/HTML 图片的本地文件。

不解析引用式 Markdown，也不验证锚点；这些由文档审查另行负责。
不读真实配置或访问外部服务。
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DOCS = (
    'README.md', 'AGENTS.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'THIRD_PARTY.md',
    'docs/README.md', 'docs/architecture.md', 'docs/development.md',
    'docs/troubleshooting.md', 'docs/installation.md', 'docs/releasing.md',
    'docs/batch-project-import.md', 'scripts/docs/README.md', 'docs/validation-v1.0.0.md',
)
LINK = re.compile(r'!?\[[^\]\n]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+["\'][^\n]*["\'])?\)')
IMAGE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', re.I)
SECRETS = (
    ('密钥组合', re.compile(r'[0-9a-f]{32}\.[A-Za-z0-9]{8}')),
    ('API 密钥', re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}')),
    ('GitHub token', re.compile(r'\bghp_[A-Za-z0-9]{36,}')),
    ('GitHub fine-grained token', re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}')),
    ('B站 Cookie', re.compile(r'''(?i)\b(?:SESSDATA|bili_jct)\b(?:['"]?\s*[:=]\s*['"]?|\t+)(?P<secret>[A-Za-z0-9%_.~+/=-]{16,})''')),
    ('私钥', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
)
# 仅保留已核对的固定人工泄漏测试值；改一个字符、换文件或加长都会重新阻断。
# 存完整匹配值的摘要，不把可复用的前缀或整个测试目录加入白名单。
TEST_PLACEHOLDER_SHA256 = {
    'backend/tests/test_login_state.py': frozenset({
        '8aac4f23489a5381bd8f0f60b95fd40c23ec0581d08ac4d00e93197ea587c814',
    }),
    'backend/tests/test_stale_explanation.py': frozenset({
        'e71facd8e6a884222334d17e4f9ed6cf38061f5056ce12ac23f07daefcd490f7',
        'ad363f2dbe47232e975436bb42e36593b60364a849c9ebe23ed6988adff5e3eb',
    }),
    'backend/tests/test_llm_conversations.py': frozenset({
        'c6df88096144a70d61f4b1a910477a79b9d5b96183e058c2efd6b699c3f63af9',
    }),
    'workers/bilibili/login.test.mjs': frozenset({
        '03ae59eb6345258e488e855ff87cb0e4b29555ef18ea79a8ef28e759f7708b08',
        '3a2c49751858c55e171f4e757859806e82accafdb669c517ee5129a3e0fd6020',
        'd9870d07c393da409965381e8fa16f050b98caabf291bae35ba36a43a7df7d86',
    }),
}
BLOCKED_DIRECTORIES = frozenset({
    'data', 'cache', '.cache', '.tmp', '.venv', 'node_modules',
    '.test-vite-cache', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.vite',
})


def git(*args: str) -> str:
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True, encoding='utf-8')


def docs_errors() -> tuple[list[str], int]:
    errors, checked = [], 0
    for name in CURRENT_DOCS:
        source = ROOT / name
        if not source.is_file():
            errors.append(f'缺少当前文档：{name}')
            continue
        text = source.read_text(encoding='utf-8-sig')
        # 代码示例中的链接不作为实际文档导航。
        text = re.sub(r'(?ms)^(```|~~~).*?^\1\s*$', '', text)
        urls = [a or b for a, b in LINK.findall(text)] + IMAGE.findall(text)
        for value in urls:
            url = urlsplit(value)
            if url.scheme or url.netloc or not url.path:
                continue
            checked += 1
            target = (source.parent / unquote(url.path)).resolve()
            if not target.is_relative_to(ROOT) or not target.exists():
                errors.append(f'{name} 的本地链接不存在或越界：{value}')
    return errors, checked


def version_errors() -> tuple[list[str], str]:
    errors = []
    tree = ast.parse((ROOT / 'backend/dubforge/__init__.py').read_text(encoding='utf-8-sig'))
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets)
              and isinstance(node.value, ast.Constant)]
    if len(values) != 1 or not isinstance(values[0], str) or not values[0].strip():
        return ['后端缺少唯一字符串 __version__'], ''
    version = values[0]
    package = json.loads((ROOT / 'frontend/package.json').read_text(encoding='utf-8'))
    lock = json.loads((ROOT / 'frontend/package-lock.json').read_text(encoding='utf-8'))
    pyproject = tomllib.loads((ROOT / 'backend/pyproject.toml').read_text(encoding='utf-8'))
    uv = tomllib.loads((ROOT / 'backend/uv.lock').read_text(encoding='utf-8'))
    versions = [package['version'], lock['version'], lock['packages']['']['version']]
    locked = [p for p in uv.get('package', []) if p.get('name') == 'dubforge']
    if len(locked) != 1:
        errors.append('uv 锁文件必须恰有一个 DubForge 项目条目')
    else:
        local = locked[0]
        if local.get('source') != {'editable': '.'}:
            errors.append('uv 的 DubForge 条目必须是当前目录的 editable 项目')
        # uv 对本地动态版本可省略 version；有显式版本时仍必须核对。
        if 'version' in local:
            versions.append(local['version'])
    if any(value != version for value in versions):
        errors.append('前后端或锁文件版本不一致')
    project = pyproject.get('project', {})
    dynamic = project.get('dynamic', [])
    if ('version' in project or not isinstance(dynamic, list) or 'version' not in dynamic
            or pyproject.get('tool', {}).get('hatch', {}).get('version', {}).get('path') != 'dubforge/__init__.py'):
        errors.append('Python 打包未使用统一版本来源')
    if f'## {version} ' not in (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'):
        errors.append('变更记录缺少当前版本')
    return errors, version


def blocked_staged_path(name: str) -> bool:
    path = PurePosixPath(name.replace('\\', '/'))
    return path.name.casefold() == 'config.yaml' or any(
        part.casefold() in BLOCKED_DIRECTORIES for part in path.parts[:-1])


def secret_errors(name: str, data: bytes) -> list[str]:
    errors = []
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        return errors
    allowed = TEST_PLACEHOLDER_SHA256.get(name, ())
    for line, content in enumerate(text.splitlines(), 1):
        for kind, pattern in SECRETS:
            for match in pattern.finditer(content):
                value = match.groupdict().get('secret', match.group())
                if hashlib.sha256(value.encode()).hexdigest() not in allowed:
                    errors.append(f'需人工核对{kind}：{name}:{line}（未输出值）')
    return errors


def staged_errors() -> tuple[list[str], int]:
    errors, checked = [], 0
    for name in git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z').split('\0'):
        if not name:
            continue
        checked += 1
        if blocked_staged_path(name):
            errors.append(f'本地配置、数据或缓存不能发布：{name}')
            continue
        errors.extend(secret_errors(name, subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)))
    return errors, checked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docs-only', action='store_true', help='仅检查当前行内链接/HTML图片本地文件，不核验锚点')
    parser.add_argument('--staged', action='store_true', help='额外检查实际暂存内容及潜在密钥')
    args = parser.parse_args()
    errors, links = docs_errors()
    version, staged = '', 0
    if not args.docs_only:
        more, version = version_errors()
        errors.extend(more)
    if args.staged:
        more, staged = staged_errors()
        errors.extend(more)
    print(json.dumps({'passed': not errors, 'version': version, 'local_links_checked': links,
                      'docs_scope': '当前行内链接和HTML图片的本地文件；不解析引用式链接或核验锚点',
                      'staged_files_checked': staged, 'errors': errors}, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
