"""安装脚本回归：Windows PowerShell 5.1、临时伪仓库和命令替身，无真实安装。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / 'scripts' / 'install.ps1'
EXAMPLE = ROOT / 'config.example.yaml'
POWERSHELL = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'


@unittest.skipUnless(os.name == 'nt', '需要 Windows PowerShell 5.1')
class InstallTests(unittest.TestCase):
    def run_fake_install(self, switches=(), existing_config=None, failure=None):
        # 新目录名刻意含空格，脚本、配置及所有输出都限定在该目录内。
        with tempfile.TemporaryDirectory(prefix='DubForge-install-test-') as temporary:
            temporary = Path(temporary)
            repo = temporary / 'fake repo with spaces'
            for directory in ('scripts', 'backend', 'frontend', 'workers/bilibili'):
                (repo / directory).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(INSTALL, repo / 'scripts/install.ps1')
            shutil.copyfile(EXAMPLE, repo / 'config.example.yaml')
            if existing_config is not None:
                (repo / 'config.yaml').write_bytes(existing_config)
            invocations = temporary / 'commands.jsonl'
            result_file = temporary / 'result.json'
            harness = temporary / 'invoke-install.ps1'
            harness.write_text(r'''
param([string]$RepoRoot, [string]$LogFile, [string]$ResultFile,
      [string]$SwitchesJson, [string]$FailureJson)
$ErrorActionPreference = 'Stop'
$global:InstallTestLog = $LogFile
$global:InstallTestFailure = ConvertFrom-Json $FailureJson
$global:InstallTestFailed = $false
function global:Record-InstallCommand {
    param([string]$Tool, [object[]]$Arguments)
    $entry = [ordered]@{ tool = $Tool; args = @($Arguments); cwd = (Get-Location).Path }
    $line = ConvertTo-Json -InputObject $entry -Compress -Depth 5
    [IO.File]::AppendAllText($global:InstallTestLog, $line + "`n", [Text.UTF8Encoding]::new($false))
    $shouldFail = $false
    if ($global:InstallTestFailure -and -not $global:InstallTestFailed) {
        $actual = @($Arguments) -join '|'
        $wanted = @($global:InstallTestFailure.args) -join '|'
        $shouldFail = $Tool -eq $global:InstallTestFailure.tool -and (
            $null -eq $global:InstallTestFailure.args -or $actual -eq $wanted)
    }
    $global:LASTEXITCODE = if ($shouldFail) { 23 } else { 0 }
    if ($shouldFail) { $global:InstallTestFailed = $true }
}
function global:uv { Record-InstallCommand -Tool 'uv' -Arguments @($args) }
function global:npm { Record-InstallCommand -Tool 'npm' -Arguments @($args) }
function global:ffmpeg { Record-InstallCommand -Tool 'ffmpeg' -Arguments @($args) }
function global:ffprobe { Record-InstallCommand -Tool 'ffprobe' -Arguments @($args) }
foreach ($tool in @('uv', 'npm', 'ffmpeg', 'ffprobe')) {
    if ((Get-Command $tool).CommandType -ne 'Function') { throw "命令替身未接管：$tool" }
}
Set-Location -LiteralPath $RepoRoot
$initialCwd = (Get-Location).Path
$status = 0
$message = ''
try {
    $chosen = @{}
    $requestedSwitches = ConvertFrom-Json $SwitchesJson
    foreach ($switch in $requestedSwitches) { $chosen[$switch] = $true }
    & (Join-Path $RepoRoot 'scripts/install.ps1') @chosen
} catch {
    $status = 1
    $message = $_.Exception.Message
}
$result = [ordered]@{ status = $status; message = $message; initial_cwd = $initialCwd;
    final_cwd = (Get-Location).Path; powershell_major = $PSVersionTable.PSVersion.Major;
    powershell_minor = $PSVersionTable.PSVersion.Minor; failure_used = $global:InstallTestFailed;
    chosen_switches = @($chosen.Keys) }
[IO.File]::WriteAllText($ResultFile, (ConvertTo-Json $result), [Text.UTF8Encoding]::new($false))
exit $status
''', encoding='utf-8-sig')
            environment = os.environ.copy()
            for key in ('DUBFORGE_CONFIG', 'DUBFORGE_DATA_ROOT', 'DUBFORGE_DEBUG_DATADIR',
                        'PYTHONPATH', 'PYTHONHOME'):
                environment.pop(key, None)
            environment.update(TEMP=str(temporary), TMP=str(temporary), PATH='')
            # 只允许系统自带模块自动载入，既不读用户 profile，也无法退回真实 uv/npm。
            environment['PSModulePath'] = str(Path(os.environ['SystemRoot']) /
                'System32/WindowsPowerShell/v1.0/Modules')
            command = [POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(harness),
                '-RepoRoot', str(repo), '-LogFile', str(invocations), '-ResultFile', str(result_file),
                '-SwitchesJson', json.dumps(list(switches)), '-FailureJson', json.dumps(failure)]
            process = subprocess.run(command, cwd=temporary, env=environment,
                capture_output=True, timeout=30)
            self.assertTrue(result_file.is_file(), process.stdout.decode(errors='replace') +
                process.stderr.decode(errors='replace'))
            result = json.loads(result_file.read_text(encoding='utf-8'))
            result['exit_code'] = process.returncode
            result['calls'] = [json.loads(line) for line in invocations.read_text(encoding='utf-8').splitlines()] \
                if invocations.exists() else []
            result['config'] = (repo / 'config.yaml').read_bytes() if (repo / 'config.yaml').exists() else None
            result['example'] = (repo / 'config.example.yaml').read_bytes()
            result['config_tmp_exists'] = (repo / 'config.yaml.tmp').exists()
            result['repo'] = str(repo)
            return result

    def assert_success(self, result):
        self.assertEqual(result['exit_code'], 0, result['message'])
        self.assertEqual(result['status'], 0, result['message'])
        self.assertEqual((result['powershell_major'], result['powershell_minor']), (5, 1))
        self.assertEqual(result['initial_cwd'], result['final_cwd'])
        self.assertFalse(result['config_tmp_exists'])
        self.assertTrue(all(call['tool'] in ('uv', 'npm') for call in result['calls']))

    def test_utf8_bom_and_actual_windows_powershell_51_parser(self):
        self.assertTrue(INSTALL.read_bytes().startswith(b'\xef\xbb\xbf'))
        script = r'''
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$tokens = $null; $errors = $null
[Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$tokens, [ref]$errors) | Out-Null
[pscustomobject]@{ major = $PSVersionTable.PSVersion.Major; minor = $PSVersionTable.PSVersion.Minor;
    errors = @($errors | ForEach-Object { $_.Message }) } | ConvertTo-Json -Compress
if ($errors.Count) { exit 1 }
'''
        # 用独立脚本接收字面路径参数，不将仓库路径拼接进 PowerShell 代码。
        with tempfile.TemporaryDirectory(prefix='DubForge-install-parse-') as temporary:
            parser = Path(temporary) / 'parse.ps1'
            parser.write_text(script, encoding='utf-8-sig')
            process = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(parser), str(INSTALL)],
                capture_output=True, timeout=15)
            self.assertEqual(process.returncode, 0, process.stderr.decode(errors='replace'))
            result = json.loads(process.stdout.decode('utf-8-sig'))
            no_bom = Path(temporary) / 'install-no-bom.ps1'
            no_bom.write_bytes(INSTALL.read_bytes().removeprefix(b'\xef\xbb\xbf'))
            negative = subprocess.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'Bypass', '-File', str(parser), str(no_bom)],
                capture_output=True, timeout=15)
            self.assertEqual(negative.returncode, 1, '去掉 BOM 后旧解析缺陷应能复现')
            self.assertTrue(json.loads(negative.stdout.decode('utf-8-sig'))['errors'])
        self.assertEqual((result['major'], result['minor']), (5, 1))
        self.assertEqual(result['errors'], [])
        source = INSTALL.read_text(encoding='utf-8-sig')
        self.assertIn('设置 → 模型与服务', source)
        self.assertNotIn('环境与服务', source)

    def test_base_install_copies_missing_configuration_and_builds_frontend(self):
        result = self.run_fake_install()
        self.assert_success(result)
        self.assertEqual(result['config'], result['example'])
        calls = result['calls']
        self.assertEqual([(call['tool'], call['args']) for call in calls], [
            ('uv', ['sync', '--locked', '--inexact', '--project', str(Path(result['repo']) / 'backend')]),
            ('npm', ['ci']), ('npm', ['run', 'build'])])
        self.assertEqual([Path(call['cwd']).name for call in calls], ['fake repo with spaces', 'frontend', 'frontend'])

    def test_all_extras_and_publishing_are_explicit_and_in_expected_directories(self):
        result = self.run_fake_install(('Asr', 'Separation', 'Publishing'))
        self.assert_success(result)
        calls = result['calls']
        self.assertEqual(calls[0]['args'][-4:], ['--extra', 'asr', '--extra', 'separation'])
        self.assertEqual([call['args'] for call in calls[1:]], [['ci'], ['run', 'build'], ['ci', '--ignore-scripts']])
        self.assertEqual(Path(calls[-1]['cwd']), Path(result['repo']) / 'workers/bilibili')

    def test_skip_frontend_without_publishing_invokes_only_backend(self):
        result = self.run_fake_install(('SkipFrontend',))
        self.assert_success(result)
        self.assertEqual([call['tool'] for call in result['calls']], ['uv'])
        self.assertEqual(result['config'], result['example'])

    def test_skip_frontend_with_publishing_still_installs_only_worker(self):
        result = self.run_fake_install(('SkipFrontend', 'Publishing'))
        self.assert_success(result)
        self.assertEqual([(call['tool'], call['args']) for call in result['calls'][1:]], [('npm', ['ci', '--ignore-scripts'])])
        self.assertEqual(Path(result['calls'][-1]['cwd']), Path(result['repo']) / 'workers/bilibili')

    def test_existing_configuration_is_preserved_byte_for_byte(self):
        existing = b'\xef\xbb\xbf# synthetic existing configuration\r\nserver:\r\n  port: 9999\r\n'
        result = self.run_fake_install(existing_config=existing)
        self.assert_success(result)
        self.assertEqual(result['config'], existing)
        self.assertNotEqual(result['config'], result['example'])

    def test_backend_failure_stops_before_config_and_frontend(self):
        result = self.run_fake_install(('Publishing',), failure={'tool': 'uv', 'args': None})
        self.assertEqual(result['exit_code'], 1)
        self.assertTrue(result['failure_used'])
        self.assertEqual([call['tool'] for call in result['calls']], ['uv'])
        self.assertIn('后端依赖安装失败', result['message'])
        self.assertIsNone(result['config'])
        self.assertFalse(result['config_tmp_exists'])
        self.assertEqual(result['initial_cwd'], result['final_cwd'])

    def test_dependency_failure_never_continues_and_restores_working_directory(self):
        for arguments, expected_calls, message in [
            (['ci'], 2, '前端依赖安装失败'),
            (['run', 'build'], 3, '前端构建失败'),
            (['ci', '--ignore-scripts'], 4, 'B站发布组件安装失败'),
        ]:
            with self.subTest(arguments=arguments):
                result = self.run_fake_install(('Publishing',), failure={'tool': 'npm', 'args': arguments})
                self.assertEqual(result['exit_code'], 1)
                self.assertTrue(result['failure_used'])
                self.assertEqual(len(result['calls']), expected_calls)
                self.assertIn(message, result['message'])
                self.assertEqual(result['initial_cwd'], result['final_cwd'])
                self.assertEqual(result['config'], result['example'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
