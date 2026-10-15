import importlib.util
import py_compile
import subprocess
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[3]
ENGINE = MODULE / 'assets' / 'engine'


class TelegramArchiveModuleTests(unittest.TestCase):
    def test_python_sources_compile(self):
        sources = sorted(MODULE.rglob('*.py'))
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source.relative_to(MODULE)):
                py_compile.compile(str(source), doraise=True)

    def test_install_script_syntax(self):
        subprocess.run(['bash', '-n', str(MODULE / 'scripts' / 'install.sh')], check=True)

    def test_no_private_state_in_module(self):
        for pattern in ('.env', '*.session', '*.sqlite3'):
            self.assertEqual(list(MODULE.rglob(pattern)), [], pattern)



if __name__ == '__main__':
    unittest.main()
