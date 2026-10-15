import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1]
          / 'scripts' / 'portfolio_html.py')
spec = importlib.util.spec_from_file_location('portfolio_html', SCRIPT)
portfolio_html = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portfolio_html)


def item(ref, tag, file, caption=''):
    return {'ref': ref, 'date': '2024-03-02T10:00:00+00:00', 'tag': tag, 'kind': 'photo', 'file': file, 'caption': caption}


def write_project(root, folder, project_id, editorial, before=(), after=(), progress=()):
    directory = root / folder
    for entry in (*before, *after, *progress):
        path = directory / entry['file']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    directory.mkdir(parents=True, exist_ok=True)
    data = {'schema': 1,
            'generated': {'project_id': project_id, 'name': folder, 'before': list(before), 'after': list(after),
                          'progress': list(progress)},
            'editorial': {'title': '', 'date': '', 'event': '', 'summary': '', 'collaborators': [], 'before': [],
                          'after': [], **editorial}}
    (directory / 'portfolio.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return directory


def run(*argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = portfolio_html.main([str(a) for a in argv])
    return code, stdout.getvalue(), stderr.getvalue()


class PortfolioHtmlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'Projects'
        self.flat = write_project(
            self.root, 'Квартира_на_Лесной', 'flat',
            {'title': 'Лесная <120 м²>', 'date': '2024-03', 'event': 'Сдача объекта', 'summary': 'Первая строка\nвторая & "кавычки"',
             'collaborators': ['Студия А', 'Bob'], 'before': ['1:2', '1:3'], 'after': ['1:5']},
            before=[item('1:2', 'render', 'media/2024-03-02_1_2_render 1.jpg', 'Рендер <b>гостиной</b>'),
                    item('1:3', 'drawing', 'media/2024-03-03_1_3_plan.pdf')],
            after=[item('1:5', 'finished', 'media/2024-03-09_1_5_done.png')],
            progress=[item('1:4', 'site_photo', 'media/2024-03-05_1_4_site.jpg', 'НЕ-ВЫБРАНО')])
        self.loft = write_project(
            self.root, 'Loft', 'loft',
            {'title': 'Loft', 'date': '2025-01', 'event': 'Монтаж', 'summary': 'Текст', 'before': ['2:1'], 'after': ['2:2']},
            before=[item('2:1', 'render', 'media/a.jpg')], after=[item('2:2', 'finished', 'media/b.jpg')])
        self.draft = write_project(self.root, 'Draft', 'draft', {'title': 'Draft'},
                                   before=[item('3:1', 'render', 'media/c.jpg')])
        (self.root / 'NoPortfolio').mkdir()

    def test_renders_ready_projects_and_a_combined_page(self):
        code, stdout, _ = run('--output-dir', self.root)
        summary = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(summary['rendered'], ['loft', 'flat'])
        self.assertEqual(summary['skipped'], {'draft': ['no date', 'no event', 'no summary', 'no before images chosen', 'no after images chosen']})
        self.assertFalse((self.draft / 'portfolio.html').exists())
        page = (self.flat / 'portfolio.html').read_text(encoding='utf-8')
        self.assertIn('<h2>Лесная &lt;120 м²&gt;</h2>', page)
        self.assertIn('2024-03 · Сдача объекта', page)
        self.assertIn('вторая &amp; &quot;кавычки&quot;', page)
        self.assertIn('Рендер &lt;b&gt;гостиной&lt;/b&gt;', page)
        self.assertNotIn('<b>гостиной', page)
        self.assertIn('<img src="media/2024-03-02_1_2_render%201.jpg"', page)
        self.assertIn('<a class="file" href="media/2024-03-03_1_3_plan.pdf">2024-03-03_1_3_plan.pdf</a>', page)
        self.assertIn('Студия А, Bob', page)
        self.assertLess(page.index('Было'), page.index('Стало'))
        self.assertNotIn('НЕ-ВЫБРАНО', page)
        self.assertNotIn(str(self.root), page)
        combined = (self.root / 'portfolio.html').read_text(encoding='utf-8')
        self.assertLess(combined.index('<h2>Loft</h2>'), combined.index('<h2>Лесная'))
        self.assertIn('src="Loft/media/a.jpg"', combined)
        self.assertIn('src="%D0%9A%D0%B2%D0%B0%D1%80%D1%82%D0%B8%D1%80%D0%B0_%D0%BD%D0%B0_%D0%9B%D0%B5%D1%81%D0%BD%D0%BE%D0%B9/media/2024-03-09_1_5_done.png"', combined)
        self.assertIn('<h1>Archive Projects</h1>', combined)
        self.assertEqual(combined.count('class="page project"'), 2)
        self.assertNotIn('http', combined.replace('http-equiv', ''))

    def test_theme_project_filter_and_drafts(self):
        theme = Path(self.tmp.name) / 'theme.json'
        theme.write_text(json.dumps({'company': 'Acme & Co', 'accent': '#123456', 'before_label': 'Before',
                                     'tagline': 'Интерьеры', 'logo': 'logo.png'}), encoding='utf-8')
        code, stdout, _ = run('--output-dir', self.root, '--theme', theme, '--project', 'draft', '--project', 'loft',
                              '--include-incomplete')
        self.assertEqual(json.loads(stdout)['rendered'], ['draft', 'loft'])
        combined = (self.root / 'portfolio.html').read_text(encoding='utf-8')
        self.assertIn('<h1>Acme &amp; Co</h1>', combined)
        self.assertIn('#123456', combined)
        self.assertIn('<h3>Before</h3>', combined)
        self.assertIn('src="logo.png"', combined)
        self.assertNotIn('Лесная', combined)
        self.assertTrue((self.draft / 'portfolio.html').exists())
        self.assertFalse((self.flat / 'portfolio.html').exists())

    def test_bad_input_is_refused(self):
        theme = Path(self.tmp.name) / 'theme.json'
        for content in ('{"colour": "red"}', '{"accent": "red; } body { display:none"}', 'nope', '[]'):
            theme.write_text(content, encoding='utf-8')
            with self.subTest(content), self.assertRaises(SystemExit):
                run('--output-dir', self.root, '--theme', theme)
        with self.assertRaises(SystemExit):
            run('--output-dir', self.root, '--project', 'missing')
        with self.assertRaises(SystemExit):
            run('--output-dir', self.root / 'absent')
        self.assertFalse((self.root / 'portfolio.html').exists())

    def test_missing_files_and_broken_json_are_skipped_with_reasons(self):
        (self.flat / 'media' / '2024-03-09_1_5_done.png').unlink()
        (self.loft / 'portfolio.json').write_text('{broken', encoding='utf-8')
        code, stdout, _ = run('--output-dir', self.root)
        summary = json.loads(stdout)
        self.assertEqual((code, summary['rendered']), (0, []))
        self.assertEqual(summary['skipped']['flat'], ['after: 1 chosen file(s) unknown or missing'])
        self.assertEqual(summary['skipped']['Loft'], ['unreadable portfolio.json: JSONDecodeError'])
        self.assertFalse((self.root / 'portfolio.html').exists())

    def test_pdf_reports_when_no_browser_is_available(self):
        original = portfolio_html.CHROME_CANDIDATES
        portfolio_html.CHROME_CANDIDATES = ('definitely-not-a-browser',)
        try:
            code, stdout, _ = run('--output-dir', self.root, '--pdf')
        finally:
            portfolio_html.CHROME_CANDIDATES = original
        self.assertEqual(code, 0)
        self.assertIn('print the page to PDF from a browser', json.loads(stdout)['pdf'])
        self.assertEqual(list(self.root.glob('*.pdf')), [])


if __name__ == '__main__':
    unittest.main()
