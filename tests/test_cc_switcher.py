import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'bin/cc-switcher'

class SwitcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root/'store'
        bindir = self.root/'bin'
        bindir.mkdir()
        fake = bindir/'claude'
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
if sys.argv[1:3] == ['auth', 'login']:
    if os.environ.get('FAIL_LOGIN'): sys.exit(7)
    (p/'.credentials.json').write_text('{"fake":true}')
elif sys.argv[1:3] == ['auth', 'status']:
    print(json.dumps({'loggedIn': (p/'.credentials.json').exists()}))
else:
    print(json.dumps({'config':str(p), 'args':sys.argv[1:]}))
    sys.exit(int(os.environ.get('RUN_EXIT', '0')))
''')
        fake.chmod(0o700)
        self.env = {**os.environ, 'CC_SWITCHER_DIR':str(self.store), 'PATH':str(bindir)+os.pathsep+os.environ['PATH']}

    def call(self, *args, ok=True, env=None):
        result = subprocess.run([str(SCRIPT), *args], env=env or self.env, capture_output=True, text=True)
        if ok: self.assertEqual(result.returncode, 0, result.stderr)
        else: self.assertNotEqual(result.returncode, 0)
        return result

    def test_accounts_remain_separate_and_arguments_pass_through(self):
        self.call('login', 'Work Account')
        self.call('login', 'Personal')
        self.call('switch', 'work-account')
        result = json.loads(self.call('run', '--', '--model', 'sonnet', 'hello world').stdout)
        self.assertTrue(result['config'].endswith('/accounts/work-account/config'))
        self.assertEqual(result['args'], ['--model', 'sonnet', 'hello world'])
        self.call('switch', 'personal')
        self.assertIn('personal/config', self.call('run').stdout)
        self.assertIn('yes\tPersonal\tpersonal', self.call('list').stdout)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.store/'selected').stat().st_mode & 0o777, 0o600)

    def test_failed_login_preserves_selection(self):
        self.call('login', 'Work')
        self.call('switch', 'work')
        result = self.call('login', 'Other', ok=False, env={**self.env, 'FAIL_LOGIN':'1'})
        self.assertEqual(result.returncode, 7)
        self.assertEqual((self.store/'selected').read_text(), 'work\n')
        self.assertFalse((self.store/'accounts/other').exists())
        self.assertEqual(len(list((self.store/'backups').glob('failed.other.*'))), 1)

    def test_no_overwrite_traversal_or_symlink(self):
        self.call('login', 'Work')
        self.call('login', 'Work', ok=False)
        self.call('switch', '../work', ok=False)
        (self.store/'accounts/evil').symlink_to(self.store/'accounts/work')
        self.call('switch', 'evil', ok=False)

    def test_delete_retains_backup_and_protects_selected(self):
        self.call('login', 'Work')
        self.call('login', 'Personal')
        self.call('switch', 'work')
        self.call('delete', 'work', ok=False)
        self.call('delete', 'personal')
        backups = list((self.store/'backups').glob('deleted.personal.*'))
        self.assertTrue((backups[0]/'config/.credentials.json').exists())

    def test_no_selection_and_exit_code(self):
        self.call('run', ok=False)
        self.call('login', 'Work')
        self.call('switch', 'work')
        result = self.call('run', ok=False, env={**self.env, 'RUN_EXIT':'12'})
        self.assertEqual(result.returncode, 12)

    def test_delete_running_profile_is_rejected(self):
        import fcntl
        self.call('login', 'Work')
        with (self.store/'accounts/work/.run-lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            self.call('delete', 'work', ok=False)

if __name__ == '__main__':
    unittest.main()
