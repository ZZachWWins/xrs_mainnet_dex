"""Isolated proxy installer checks; no root changes, live HTTP, or service calls."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parent / 'deploy' / 'install-paused-proxy.py'


def load_installer():
    specification = importlib.util.spec_from_file_location('isolated_proxy_installer', SOURCE)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class ProxyInstallerTests(unittest.TestCase):
    def setUp(self):
        self.module = load_installer()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.stage = self.directory / 'stage'
        self.stage.mkdir(mode=0o700)
        self.snippets = self.directory / 'snippets'
        self.snippets.mkdir()
        self.config = self.directory / 'mainnetrpc.conf'
        self.common = self.snippets / 'common.conf'
        self.original = 'server {\n    listen 80;\n}\nserver {\n    listen 443 ssl;\n    location / { return 404; }\n}\n'
        self.config.write_text(self.original)
        self.common.write_text('# public common fixture\n')
        for name in self.module.FILES:
            (self.stage / name).write_text('# public fixture ' + name + '\n')
        manifest = {name: self.module.sha(self.stage / name) for name in self.module.FILES}
        (self.stage / 'proxy-manifest.json').write_text(json.dumps(manifest))
        self.calls = []
        self.reload_failures = 0

    def run_command(self, *args):
        self.calls.append(args)
        if args[:2] == ('systemctl', 'show'):
            return '4321'
        if args[:2] == ('systemctl', 'is-active'):
            return 'active'
        if args == ('systemctl', 'reload', 'nginx.service') and self.reload_failures:
            self.reload_failures -= 1
            raise RuntimeError('Synthetic reload failure')
        return ''

    def context(self, paused=None):
        stack = ExitStack()
        settings = {'CONFIG': self.config, 'COMMON': self.common, 'SNIPPETS': self.snippets,
            'BACKUP_DIRECTORY': self.directory, 'EXPECTED_CONFIG': self.module.sha(self.config),
            'EXPECTED_COMMON': self.module.sha(self.common), 'run': self.run_command,
            'paused': paused or (lambda: None)}
        for name, value in settings.items():
            stack.enter_context(patch.object(self.module, name, value))
        stack.enter_context(patch.object(sys, 'argv', [str(SOURCE), str(self.stage)]))
        stack.enter_context(patch.object(self.module.os, 'getuid', return_value=0))
        # The fixture only pretends these temporary public files are root-owned.
        original_stat = Path.stat
        def root_stat(path, *args, **kwargs):
            result = list(original_stat(path, *args, **kwargs))
            result[4] = 0
            return os.stat_result(result)
        stack.enter_context(patch.object(Path, 'stat', root_stat))
        stack.enter_context(patch.object(self.module.time, 'strftime', side_effect=['test-first', 'test-retry']))
        stack.enter_context(patch.object(self.module.urllib.request, 'urlopen', side_effect=AssertionError('No live HTTP')))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def test_optimized_python_still_enforces_privilege_and_paused_guards(self):
        code = f'''
import importlib.util, json, sys
spec=importlib.util.spec_from_file_location('reviewed_installer',{str(SOURCE)!r})
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
module.os.getuid=lambda:123
sys.argv=['installer','unused-staging-path']
try:
    module.main()
except RuntimeError as error:
    if str(error)!='Run as root with exactly one reviewed staging directory':raise
else:raise RuntimeError('Privilege guard disappeared under optimization')
class Response:
    status=200
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return json.dumps({{'schema_version':1,'can_accept_funds':True}}).encode()
module.urllib.request.urlopen=lambda *args,**kwargs:Response()
try:
    module.paused()
except RuntimeError as error:
    if str(error)!='Settlement must be explicitly paused':raise
else:raise RuntimeError('Paused guard disappeared under optimization')
print('optimized guards retained')
'''
        result = subprocess.run([sys.executable, '-O', '-c', code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'optimized guards retained')

    def test_failed_reload_restores_config_removes_only_created_snippets_and_allows_retry(self):
        self.reload_failures = 1
        with self.context():
            with self.assertRaisesRegex(RuntimeError, 'Synthetic reload failure'):
                self.module.main()
            self.assertEqual(self.config.read_text(), self.original)
            for name in self.module.FILES:
                self.assertFalse((self.snippets / ('xeris-' + name)).exists())
            self.assertEqual((self.directory / 'xeris-settlement-backup-test-first' / 'mainnetrpc.before.conf').read_text(), self.original)
            self.module.main()
            self.assertIn('xeris-settlement-location.conf', self.config.read_text())
            self.assertTrue(all((self.snippets / ('xeris-' + name)).is_file() for name in self.module.FILES))
            report = json.loads((self.directory / 'xeris-settlement-backup-test-retry' / 'deployment.json').read_text())
            self.assertFalse(report['funds_enabled'])
            self.assertEqual(report['node_pid_unchanged'], '4321')
        self.assertEqual(self.calls.count(('systemctl', 'reload', 'nginx.service')), 3)
        self.assertFalse(any('restart' in call for call in self.calls))

    def test_creation_race_preserves_other_attempts_file_and_cleans_own_partial_snippet(self):
        other = self.snippets / ('xeris-' + self.module.FILES[1])
        def paused_with_competing_creation():
            other.write_text('Another attempt owns this public file')
        with self.context(paused_with_competing_creation):
            with self.assertRaises(FileExistsError):
                self.module.main()
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(other.read_text(), 'Another attempt owns this public file')
        self.assertFalse((self.snippets / ('xeris-' + self.module.FILES[0])).exists())
        self.assertNotIn(('systemctl', 'reload', 'nginx.service'), self.calls)

    def test_changed_reviewed_payload_aborts_before_any_service_call(self):
        (self.stage / self.module.FILES[0]).write_text('Changed after review')
        with self.context():
            with self.assertRaisesRegex(RuntimeError, 'reviewed manifest'):
                self.module.main()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.config.read_text(), self.original)


if __name__ == '__main__':
    unittest.main()
