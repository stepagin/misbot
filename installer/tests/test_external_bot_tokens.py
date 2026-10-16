"""External MAS validates authoritative saved sessions with managed bot UIDs."""

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lifecycle
import state_store


class ExternalBotTokenTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('config', 'secrets', 'data/referral-control/data'):
            (self.root / name).mkdir(parents=True, mode=0o700)
        self.metadata = {'domain': 'chat.example.org', 'mas_mode': 'external'}
        self.configs = {}
        self.journals = {}
        self.sessions = {}
        self.config_uids = {}
        original_lstat, original_fstat = Path.lstat, os.fstat

        def owned_info(info, path):
            if path in self.config_uids:
                fields = list(info)
                fields[4] = self.config_uids[path]
                return os.stat_result(fields)
            return info

        def lstat(path, *args, **kwargs):
            return owned_info(original_lstat(path, *args, **kwargs), path)

        def fstat(fd):
            return owned_info(original_fstat(fd), Path(os.readlink('/proc/self/fd/' + str(fd))))

        for replacement in (
            patch.object(lifecycle, 'ROOT', self.root, create=True),
            patch.object(Path, 'lstat', lstat),
            patch.object(os, 'fstat', fstat),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        dc_patch = patch.object(lifecycle, 'dc', return_value='{"passed": true}')
        self.dc = dc_patch.start()
        self.addCleanup(dc_patch.stop)
        emit_patch = patch.object(lifecycle, 'emit')
        self.emit = emit_patch.start()
        self.addCleanup(emit_patch.stop)

    def seed(self, role='referral'):
        tag = {'referral': 'referral-bot', 'federation': 'my-admin', 'welcome': 'server-guide'}[
            role
        ]
        device = {
            'referral': 'MATRIX-REFERRAL-01',
            'federation': 'MATRIX-FEDERATION-01',
            'welcome': 'MATRIX-WELCOME-01',
        }[role]
        config = {
            'user_id': '@' + tag + ':chat.example.org',
            'device_id': device,
            'access_token': 'FICTIONAL_PRIVATE_TOKEN_' + role,
        }
        runtime = self.root / 'config' / (role + '-bot.json')
        journal = (
            self.root
            / 'data/referral-control'
            / ('session.json' if role == 'referral' else role + '-session.json')
        )
        session = self.root / 'secrets' / (role + '-session.json')
        state_store.write_json(runtime, config)
        state_store.write_json(journal, {'config': config})
        state_store.write_json(session, {key: config[key] for key in ('user_id', 'device_id')})
        self.config_uids[runtime] = 10002 if role == 'referral' else 10003
        self.metadata[role + '_provisioned'] = True
        state_store.write_json(self.root / 'installation.json', self.metadata)
        self.configs[role], self.journals[role], self.sessions[role] = runtime, journal, session
        if role == 'referral':
            with sqlite3.connect(
                self.root / 'data/referral-control/data/bot-control.sqlite3'
            ) as db:
                db.execute('CREATE TABLE fixture_identity (id TEXT)')
        elif role == 'federation':
            state_store.write_json(
                self.root / 'config/admin-panel-identity.json',
                {
                    'user_id': config['user_id'],
                    'administrator_mxid': '@matrix-admin:chat.example.org',
                    'administrator_mxids': ['@matrix-admin:chat.example.org'],
                },
            )
        return config

    def passed(self, configs):
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        lifecycle.verify_external_bot_tokens()
        self.assertEqual(json.loads(self.dc.call_args.kwargs['stdin']), configs)
        self.emit.assert_called_once_with('external_mas_bot_tokens', passed=True)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def refused(self, message):
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with self.assertRaisesRegex(RuntimeError, message):
            lifecycle.verify_external_bot_tokens()
        self.dc.assert_not_called()
        self.emit.assert_not_called()
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_actual_provisioning_uids_accept_authoritative_controller_tokens(self):
        configs = [self.seed(role) for role in ('referral', 'federation', 'welcome')]
        self.passed(configs)

    def test_unprovisioned_bot_needs_no_checkpoint_and_sends_no_token(self):
        state_store.write_json(self.root / 'installation.json', self.metadata)
        lifecycle.verify_external_bot_tokens()
        self.dc.assert_not_called()
        self.emit.assert_not_called()

    def test_root_private_loader_still_rejects_container_owned_runtime_config(self):
        self.seed()
        import configuration

        with self.assertRaisesRegex(RuntimeError, 'mode 0600'):
            configuration.read_private_json(self.configs['referral'])
        self.passed([json.loads(self.journals['referral'].read_text())['config']])

    def test_runtime_token_mismatch_refused_before_whoami(self):
        config = self.seed()
        state_store.write_json(
            self.configs['referral'], dict(config, access_token='FICTIONAL_CHANGED')
        )
        self.refused('Saved referral session does not match')

    def test_controller_identity_must_match_separate_saved_session(self):
        config = self.seed()
        foreign = dict(config, device_id='UNSAVED_DEVICE')
        state_store.write_json(self.configs['referral'], foreign)
        state_store.write_json(self.journals['referral'], {'config': foreign})
        self.refused('identity differs')

    def test_controller_world_readable_profile_refused_before_whoami(self):
        self.seed()
        self.journals['referral'].chmod(0o644)
        self.refused('mode 0600')

    def test_controller_container_owned_profile_refused_before_whoami(self):
        self.seed()
        self.config_uids[self.journals['referral']] = 10002
        self.refused('mode 0600')

    def test_separate_saved_session_world_readable_refused_before_whoami(self):
        self.seed()
        self.sessions['referral'].chmod(0o644)
        self.refused('mode 0600')

    def test_runtime_world_readable_profile_refused_before_whoami(self):
        self.seed()
        self.configs['referral'].chmod(0o644)
        self.refused('private managed owner and permissions')

    def test_runtime_foreign_container_owner_refused_before_whoami(self):
        self.seed()
        self.config_uids[self.configs['referral']] = 14000
        self.refused('private managed owner and permissions')

    def test_checkpoint_symlink_refused_before_whoami(self):
        self.seed()
        path = self.journals['referral']
        saved = path.with_name('retained.json')
        path.rename(saved)
        path.symlink_to(saved.name)
        self.refused('symbolic links')

    def test_runtime_symlink_refused_before_whoami(self):
        self.seed()
        path = self.configs['referral']
        saved = path.with_name('retained.json')
        path.rename(saved)
        path.symlink_to(saved.name)
        self.refused('symbolic links')

    def test_controller_parent_symlink_refused_before_whoami(self):
        self.seed()
        folder = self.root / 'data/referral-control'
        target = folder.with_name('retained-controller')
        folder.rename(target)
        folder.symlink_to(target.name, target_is_directory=True)
        self.refused('symbolic links')

    def test_runtime_hardlink_refused_before_whoami(self):
        self.seed()
        os.link(self.configs['referral'], self.root / 'config/other.json')
        self.refused('private managed owner and permissions')

    def test_provider_error_never_replays_private_details(self):
        self.seed()
        self.dc.side_effect = RuntimeError('FICTIONAL_PRIVATE_PROVIDER_DETAIL')
        with self.assertRaisesRegex(RuntimeError, 'Synapse') as caught:
            lifecycle.verify_external_bot_tokens()
        self.assertNotIn('FICTIONAL_PRIVATE_PROVIDER_DETAIL', str(caught.exception))
        self.emit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
