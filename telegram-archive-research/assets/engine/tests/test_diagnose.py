import contextlib
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('archive_diagnose', Path(__file__).resolve().parents[1] / 'scripts/diagnose.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class DiagnoseTests(unittest.TestCase):
    def test_offline_report_never_connects_or_prints_credentials(self):
        secrets = {'TG_API_ID': '12345', 'TG_API_HASH': 'private-hash', 'TG_PHONE': 'private-phone',
                   'HTTPS_PROXY': 'http://private-user:private-password@localhost:9999'}
        output = io.StringIO()
        with patch.dict(os.environ, secrets, clear=True), patch.object(app, 'load_project_env'), \
             patch.object(app.socket, 'create_connection', side_effect=AssertionError('network forbidden')), \
             patch.object(app.subprocess, 'run', side_effect=AssertionError('subprocess forbidden')), \
             contextlib.redirect_stdout(output):
            self.assertEqual(app.main(['--offline', '--json']), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report['installation_ok'])
        self.assertTrue(report['telegram_configured'])
        self.assertFalse(report['network_checked'])
        for value in secrets.values():
            self.assertNotIn(value, output.getvalue())

    def test_unconfigured_profile_is_installable_but_not_authenticated(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(app, 'load_project_env'), contextlib.redirect_stdout(output):
            self.assertEqual(app.main(['--offline', '--json']), 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report['telegram_configured'])
        self.assertEqual(report['missing_config'], ['TG_API_ID', 'TG_API_HASH', 'TG_PHONE'])

    def test_missing_dependency_fails_installation_check(self):
        with patch.object(app.importlib.util, 'find_spec', return_value=None), patch.object(app, 'load_project_env'), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(['--offline']), 1)
