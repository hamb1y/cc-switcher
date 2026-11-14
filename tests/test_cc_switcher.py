import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1]/'bin/cc-switcher'
loader = importlib.machinery.SourceFileLoader('switcher', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
switcher = importlib.util.module_from_spec(spec)
loader.exec_module(switcher)


class SwitcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = self.root/'store'
        self.active = self.root/'active'
        self.active.mkdir()
        bindir = self.root/'bin'
        bindir.mkdir()
        fake = bindir/'claude'
        fake.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR'])
if sys.argv[1:3] == ['auth', 'login']:
    if os.environ.get('FAIL_LOGIN'): sys.exit(7)
    (p/'.credentials.json').write_text(json.dumps({'claudeAiOauth':{'accessToken':'new-token','refreshToken':'new-refresh'}}))
    (p/'.claude.json').write_text(json.dumps({'oauthAccount':{'accountUuid':'new-account'}}))
elif sys.argv[1:3] == ['auth', 'status']:
    d=json.loads((p/'.credentials.json').read_text())
    print(json.dumps({'loggedIn':bool(d.get('claudeAiOauth',{}).get('accessToken'))}))
''')
        fake.chmod(0o700)
        self.env = {**os.environ, 'HOME':str(self.root), 'CC_SWITCHER_DIR':str(self.store), 'CLAUDE_CONFIG_DIR':str(self.active), 'PATH':str(bindir)+os.pathsep+os.environ['PATH']}
        self.env.pop('CLAUDE_SECURESTORAGE_CONFIG_DIR', None)
        self.credentials = self.active/'.credentials.json'
        self.config = self.active/'.claude.json'
        self.write_active('work')

    def write_active(self, name):
        self.credentials.write_text(json.dumps({'claudeAiOauth':{'accessToken':name+'-token','refreshToken':name+'-refresh'}, 'mcpOAuth':{'keep':'mcp-secret'}}))
        self.config.write_text(json.dumps({'oauthAccount':{'accountUuid':name}, 'projects':{'keep':'trust'}, 'hasCompletedOnboarding':True}))

    def call(self, *args, ok=True, env=None):
        result = subprocess.run([str(SCRIPT), *args], env=env or self.env, capture_output=True, text=True)
        if ok: self.assertEqual(result.returncode, 0, result.stderr)
        else: self.assertNotEqual(result.returncode, 0)
        return result

    @unittest.skipIf(sys.platform == 'darwin', 'file integration on Linux; Keychain tested separately')
    def test_switch_changes_existing_claude_and_preserves_settings_and_mcp(self):
        self.call('save', 'Work')
        self.write_active('personal')
        self.call('save', 'Personal')
        self.call('switch', 'work')
        credentials = json.loads(self.credentials.read_text())
        config = json.loads(self.config.read_text())
        self.assertEqual(credentials['claudeAiOauth']['accessToken'], 'work-token')
        self.assertEqual(credentials['mcpOAuth'], {'keep':'mcp-secret'})
        self.assertEqual(config['oauthAccount']['accountUuid'], 'work')
        self.assertEqual(config['projects'], {'keep':'trust'})
        result = subprocess.run(['claude','auth','status'], env=self.env, capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)['loggedIn'])
        self.assertIn('yes\tWork\twork', self.call('list').stdout)
        self.assertEqual(self.credentials.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertTrue(list((self.store/'backups').glob('active.*.json')))

    @unittest.skipIf(sys.platform == 'darwin', 'isolated file login is Linux only')
    def test_login_success_failure_and_duplicate_never_touch_active(self):
        before = (self.credentials.read_bytes(), self.config.read_bytes())
        self.call('login', 'Other')
        self.call('login', 'Other', ok=False)
        result = self.call('login', 'Failed', ok=False, env={**self.env,'FAIL_LOGIN':'1'})
        self.assertEqual(result.returncode, 7)
        self.assertEqual((self.credentials.read_bytes(),self.config.read_bytes()), before)
        self.assertFalse(list(self.store.glob('login.*')))
        self.assertFalse((self.store/'accounts/failed').exists())
        self.call('switch', 'other')
        self.assertEqual(json.loads(self.credentials.read_text())['claudeAiOauth']['accessToken'], 'new-token')

    @unittest.skipIf(sys.platform == 'darwin', 'file integration on Linux')
    def test_replace_delete_migration_and_paths(self):
        self.call('save','Work')
        self.call('save','Work',ok=False)
        self.write_active('refreshed')
        self.call('save','Work','--replace')
        self.assertTrue(list((self.store/'backups').glob('replaced.*')))
        self.call('switch','../work',ok=False)
        (self.store/'accounts/evil').symlink_to(self.store/'accounts/work')
        self.call('switch','evil',ok=False)
        old = self.store/'accounts/old'
        (old/'config').mkdir(parents=True)
        (old/'label').write_text('Old\n')
        (old/'config/.credentials.json').write_text(json.dumps({'claudeAiOauth':{'accessToken':'old-token'}}))
        self.call('switch','old')
        self.assertEqual(json.loads(self.credentials.read_text())['claudeAiOauth']['accessToken'], 'old-token')
        self.call('delete','old')
        self.assertTrue(list((self.store/'backups').glob('deleted.old.*')))

    @unittest.skipIf(sys.platform == 'darwin', 'file integration on Linux')
    def test_invalid_credentials_and_symlink_config_do_not_change_active(self):
        self.call('save','Work')
        before = self.credentials.read_bytes()
        (self.store/'accounts/work/auth.json').write_text('{"claudeAiOauth":{"accessToken":""}}')
        self.call('switch','work',ok=False)
        self.assertEqual(self.credentials.read_bytes(), before)
        (self.store/'accounts/work/auth.json').write_text('{secret-invalid-json')
        result = self.call('switch','work',ok=False)
        self.assertNotIn('secret-invalid-json', result.stderr)
        self.assertEqual(self.credentials.read_bytes(), before)
        self.config.rename(self.active/'real-config')
        self.config.symlink_to(self.active/'real-config')
        self.call('save','Symlink',ok=False)
        self.assertEqual(self.credentials.read_bytes(), before)

    def test_config_rollback_on_credential_write_failure(self):
        with mock.patch.dict(os.environ,self.env,clear=True):
            active = switcher.Active()
            before = self.config.read_bytes()
            backups = self.root/'backups'
            backups.mkdir()
            with mock.patch.object(active,'credentials_read',return_value={'mcpOAuth':{'keep':1}}), mock.patch.object(active,'credentials_write',side_effect=OSError('write failed')):
                with self.assertRaises(OSError):
                    active.activate({'claudeAiOauth':{'accessToken':'other'},'oauthAccount':{'accountUuid':'other'}},backups)
            self.assertEqual(self.config.read_bytes(),before)

    def test_keychain_credentials_use_stdin_and_preserve_other_secrets(self):
        with mock.patch.dict(os.environ,self.env,clear=True), mock.patch.object(switcher.sys,'platform','darwin'):
            active = switcher.Active()
            self.assertTrue(active.service.startswith('Claude Code-credentials-'))
            with mock.patch.object(switcher.subprocess,'run') as run:
                run.return_value = subprocess.CompletedProcess([],0,'{"claudeAiOauth":{"accessToken":"secret"},"mcpOAuth":{"keep":1}}','')
                self.assertIn('mcpOAuth',active.credentials_read())
                active.credentials_write({'claudeAiOauth':{'accessToken':'secret'}})
                argv = run.call_args.args[0]
                self.assertEqual(argv,['security','-i'])
                self.assertNotIn('secret',repr(argv))
                self.assertIn('-X',run.call_args.kwargs['input'])

    def test_secure_storage_override_and_existing_directory_permissions(self):
        secure = self.root/'secure'
        with mock.patch.dict(os.environ,{**self.env,'CLAUDE_SECURESTORAGE_CONFIG_DIR':str(secure)},clear=True):
            self.assertEqual(switcher.Active().credentials,secure/'.credentials.json')
        self.active.chmod(0o755)
        switcher.write_json(self.config,{'keep':True})
        self.assertEqual(self.active.stat().st_mode & 0o777,0o755)

if __name__ == '__main__':
    unittest.main()
