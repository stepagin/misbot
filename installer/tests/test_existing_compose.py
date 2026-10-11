"""Existing installations keep identity and reject unsafe native operations."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_crypto
import command_wrapper
import existing_compose as existing
import misbot
import state_store

BUNDLE = Path(__file__).resolve().parents[1]


class ExistingTests(unittest.TestCase):
    def fixture(self, base):
        root = base / "installation"
        root.mkdir(mode=0o700)
        for name in command_wrapper.PUBLIC_FILES:
            (root / name).write_bytes((BUNDLE / name).read_bytes())
        for name in ("install.sh", "generate.py", "lifecycle.py", "existing_compose.py"):
            (root / name).write_text("# fictional backend\n")
        (root / "synapse").mkdir()
        (root / "mas").mkdir()
        (root / "synapse/config.yaml").write_text("server_name: fictional.example.org\npassword: FICTIONAL_SECRET\n")
        (root / "mas/config.yaml").write_text("http:\n  issuer: https://fictional.example.org/\npassword: FICTIONAL_SECRET\n")
        (root / "synapse/signing.key").write_text("FICTIONAL_SIGNING_KEY")
        control = root / ".misbot"
        (control / "pins").mkdir(parents=True)
        (root / "docker-compose.yml").write_text("# fictional compose")
        (control / "pins/fictional.json").write_text("{}")
        image = {"id": "sha256:" + "1" * 64, "digest": "example.invalid/image@sha256:" + "2" * 64}
        model = {
            "format": 1, "projects": [{"name": "fictional", "directory": ".",
                "services": {"synapse": "matrix-synapse", "mas": "matrix-mas", "postgres": "matrix-postgres"}}],
            "writers": ["matrix-mas", "matrix-synapse"], "postgres_container": "matrix-postgres",
            "databases": ["synapse", "mas"], "backup_directory": str(base / "backups"),
            "backup_key": str(base / "recovery.key"), "raw_database_paths": ["postgres/data"],
            "critical_paths": ["synapse/signing.key"], "identity": {"synapse_config": "synapse/config.yaml",
                "mas_config": "mas/config.yaml", "signing_key": "synapse/signing.key",
                "signing_sha256": existing.digest(root / "synapse/signing.key"), "issuer": "https://fictional.example.org/"},
            "images": {name: image.copy() for name in ("matrix-synapse", "matrix-mas", "matrix-postgres")}}
        state = {"domain": "fictional.example.org", "backend": "existing-compose", "config_format_version": 1, "existing": model}
        (root / "installation.json").write_text(json.dumps(state))
        backup_crypto.create_key(base / "recovery.key")
        return root, model

    def installation(self, root):
        def directory(path):
            path = Path(path)
            path.mkdir(parents=True, exist_ok=True)
            return path
        with patch.object(existing, "private_directory", side_effect=directory):
            return existing.Installation(root)

    def test_native_init_generation_update_and_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, model = self.fixture(Path(folder))
            for arguments in (["config", "init", "--domain", "other.invalid"],
                    ["config", "generate"], ["config", "apply"],
                    ["credentials", "database", "rotate"]):
                with self.subTest(arguments=arguments):
                    command, options, values = misbot.parse([*arguments, "--directory", str(root)])
                    with self.assertRaises(misbot.InputError):
                        misbot.plan(command, options, values, source=BUNDLE)
            for arguments in (["status"], ["ui"], ["backup", "create"], ["backup", "verify"]):
                command, options, values = misbot.parse([*arguments, "--directory", str(root)])
                planned = misbot.plan(command, options, values, source=BUNDLE)
                self.assertEqual(planned[1], str(root / "existing_compose.py"))
            self.assertIn("FICTIONAL_SECRET", (root / "synapse/config.yaml").read_text())

    def test_public_help_never_reads_private_installation(self):
        output = io.StringIO()
        with patch.object(command_wrapper, "default_backend", return_value="existing-compose"), \
                patch.object(command_wrapper, "default_directory", side_effect=AssertionError("private read")), \
                contextlib.redirect_stdout(output):
            misbot.help_text()
        self.assertIn("misbot backup verify", output.getvalue())
        self.assertNotIn("config init", output.getvalue())
        self.assertIn("update apply", output.getvalue())

    def test_technical_entry_points_cannot_take_portable_operation_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root, model = self.fixture(Path(folder))
            before = (root / "installation.json").read_bytes()
            with self.assertRaisesRegex(RuntimeError, "Existing Compose"):
                with state_store.operation_guard(root):
                    self.fail("Portable operation entered")
            with self.assertRaisesRegex(RuntimeError, "Existing Compose"):
                state_store.assert_update_allowed(root)
            self.assertEqual((root / "installation.json").read_bytes(), before)

    def test_identity_changes_are_rejected_before_docker(self):
        with tempfile.TemporaryDirectory() as folder:
            root, model = self.fixture(Path(folder))
            installation = self.installation(root)
            (root / "synapse/signing.key").write_text("FICTIONAL_REPLACED_KEY")
            with patch.object(installation, "run", side_effect=AssertionError("Docker called")):
                with self.assertRaises(existing.Error):
                    installation.validate()

    def test_foreign_container_is_never_started(self):
        with tempfile.TemporaryDirectory() as folder:
            root, model = self.fixture(Path(folder))
            installation = self.installation(root)
            with patch.object(installation, "run", return_value=(0, b"running healthy sha256:x another-project synapse")) as run:
                with self.assertRaises(existing.Error):
                    installation.container("matrix-synapse")
            self.assertEqual(run.call_args.args[0][:2], ["docker", "inspect"])

    def test_backup_failure_resumes_only_original_writers(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, model = self.fixture(base)
            installation = self.installation(root)
            running = {"matrix-mas": True, "matrix-synapse": False}
            calls = []
            def run(arguments, **kwargs):
                calls.append(arguments)
                if arguments[0] == "du":
                    return 0, b"100\tinstallation"
                if arguments[:2] == ["docker", "stop"]:
                    running["matrix-mas"] = False
                if arguments[:2] == ["docker", "start"]:
                    for name in arguments[2:]:
                        running[name] = True
                return 0, b""
            def directory(path):
                path = Path(path)
                path.mkdir(parents=True, exist_ok=True)
                return path
            with patch.object(installation, "validate"), patch.object(installation, "run", side_effect=run), \
                    patch.object(installation, "container", side_effect=lambda name: {"name": name, "exists": True, "running": running[name], "health": "healthy"}), \
                    patch.object(installation, "pg", side_effect=existing.Error("fictional dump failure")), \
                    patch.object(existing, "private_directory", side_effect=directory), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(existing.Error):
                    installation.backup()
            self.assertTrue(running["matrix-mas"])
            self.assertFalse(running["matrix-synapse"])
            self.assertIn(["docker", "start", "matrix-mas"], calls)
            self.assertFalse(installation.journal.exists())

    def test_recovery_rejects_a_foreign_container_in_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            root, model = self.fixture(Path(folder))
            installation = self.installation(root)
            installation.journal.write_text(json.dumps({"format": 1, "running_writers": ["foreign"]}))
            with patch.object(installation, "run", side_effect=AssertionError("mutation")):
                with self.assertRaises(existing.Error):
                    installation.recover()
            self.assertTrue(installation.journal.exists())

    def test_failed_restore_drops_only_the_new_database(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, model = self.fixture(base)
            installation = self.installation(root)
            destination = base / "backups" / "owned"
            destination.mkdir(parents=True)
            manifest = {"format": "misbot-existing-compose-backup-v1", "server_name": "fictional.example.org",
                "database_sha256": {name + ".dump": hashlib.sha256(b"FICTIONAL_DUMP").hexdigest() for name in model["databases"]}}
            source = base / "input.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                for name, value in {"manifest.json": json.dumps(manifest).encode(),
                        "database/synapse.dump": b"FICTIONAL_DUMP", "database/mas.dump": b"FICTIONAL_DUMP"}.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(value)
                    archive.addfile(info, io.BytesIO(value))
            encrypted = destination / "backup.tar.gz.aesgcm"
            backup_crypto.encrypt(source, encrypted, backup_crypto.read_key(base / "recovery.key"))
            (installation.control / "last-backup.json").write_text(json.dumps({"directory": str(destination), "sha256": existing.digest(encrypted)}))
            created, dropped = [], []
            def pg(tool, *arguments, **kwargs):
                if tool == "createdb":
                    created.append(arguments[-1])
                elif tool == "dropdb":
                    dropped.append(arguments[0])
                elif tool == "pg_restore":
                    raise existing.Error("fictional restore failure")
                return 0, b""
            with patch.object(installation, "validate"), patch.object(installation, "pg", side_effect=pg):
                with self.assertRaises(existing.Error):
                    installation.verify()
            self.assertEqual(created, dropped)
            self.assertEqual(len(created), 1)
            self.assertTrue(created[0].startswith("misbot_verify_"))
            self.assertNotIn("synapse", dropped)
            self.assertNotIn("mas", dropped)

    def test_streaming_decryption_authenticates_final_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            key = os.urandom(32)
            source = base / "source"
            data = os.urandom(backup_crypto.CHUNK + 7)
            source.write_bytes(data)
            archive = base / "archive"
            backup_crypto.encrypt(source, archive, key)
            with io.BufferedReader(existing.Frames(archive, key)) as reader:
                self.assertEqual(reader.read(), data)
            damaged = base / "damaged"
            damaged.write_bytes(archive.read_bytes()[:-1])
            with io.BufferedReader(existing.Frames(damaged, key)) as reader:
                with self.assertRaises(RuntimeError):
                    reader.read()

    def test_backup_keeps_private_theme_source_and_rejects_unsafe_theme(self):
        with tempfile.TemporaryDirectory() as folder:
            root,_=self.fixture(Path(folder));installation=self.installation(root)
            theme=installation.control/'features/site-template';theme.mkdir(mode=0o700,parents=True)
            for name in ('index.html','site.css'):
                (theme/name).write_text('fictional theme');(theme/name).chmod(0o600)
            original=Path.lstat
            def info(path,*args,**kwargs):
                value=list(original(path,*args,**kwargs));value[4]=0;return os.stat_result(value)
            with patch.object(Path,'lstat',info):
                paths=installation.backup_control_sources()
                self.assertIn(theme/'index.html',paths);self.assertIn(theme/'site.css',paths)
                (theme/'site.css').chmod(0o644)
                with self.assertRaises(existing.Error):installation.backup_control_sources()
                (theme/'site.css').chmod(0o600)
                (theme/'index.html').unlink();(theme/'index.html').symlink_to(theme/'site.css')
                with self.assertRaises(RuntimeError):installation.backup_control_sources()

    def test_verify_accepts_optional_theme_and_rejects_unknown_control_paths(self):
        for theme_path,accepted in (('features/site-template/index.html',True),
                                    ('features/site-template/private.json',False)):
            with self.subTest(path=theme_path), tempfile.TemporaryDirectory() as folder:
                base=Path(folder);root,model=self.fixture(base);installation=self.installation(root)
                destination=base/'backups/owned';destination.mkdir(parents=True)
                theme=b'<h1>@@NAME@@</h1>'
                member='misbot-control/'+theme_path
                payload={member:theme, 'database/synapse.dump':b'FICTIONAL_DUMP','database/mas.dump':b'FICTIONAL_DUMP'}
                manifest={'format':'misbot-existing-compose-backup-v1','server_name':'fictional.example.org',
                    'database_sha256':{name+'.dump':hashlib.sha256(b'FICTIONAL_DUMP').hexdigest() for name in model['databases']},
                    'control_sha256':{member:hashlib.sha256(theme).hexdigest()}}
                payload['manifest.json']=json.dumps(manifest).encode()
                source=base/'input.tar.gz'
                with tarfile.open(source,'w:gz') as archive:
                    for name,value in payload.items():
                        entry=tarfile.TarInfo(name);entry.size=len(value);archive.addfile(entry,io.BytesIO(value))
                encrypted=destination/'backup.tar.gz.aesgcm'
                backup_crypto.encrypt(source,encrypted,backup_crypto.read_key(base/'recovery.key'))
                selected={'directory':str(destination),'sha256':existing.digest(encrypted)}
                with patch.object(installation,'validate'),patch.object(installation,'pg',return_value=(0,b'0')) as pg:
                    if accepted:installation.verify(selected)
                    else:
                        with self.assertRaises(existing.Error):installation.verify(selected)
                        pg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
