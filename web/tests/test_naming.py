import re, sys, unittest
from pathlib import Path
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
VISIBLE = ['static/index.html', 'static/app.js', 'static/workbench.js', 'psi_sync_center.py',
           'psi_sync/state.py', 'psi_sync/handoff.py', 'psi_sync/http_server.py']
RETIRED = re.compile(r'同步中心|\bAGI\b|\bSI\b|\bPAM\b|Personal AI Monitor')

class MergedNamingTest(unittest.TestCase):
    def test_user_visible_text_has_no_retired_names(self):
        for rel in VISIBLE:
            self.assertIsNone(RETIRED.search((ROOT / rel).read_text(encoding='utf-8')), rel)

    def test_title(self):
        self.assertIn('<title>PSI · 问题与进展</title>', (ROOT / 'static/index.html').read_text(encoding='utf-8'))

    def test_public_entry_is_a_browser_workbench(self):
        self.assertTrue((ROOT / 'psi_sync_center.py').is_file())
        self.assertFalse((REPO / 'apps/PersonalAIMonitorMenuBar').exists())
        self.assertFalse((REPO / 'scripts/build-macos-app.sh').exists())
        self.assertFalse(list(ROOT.glob('*.command')))
        self.assertFalse((ROOT / 'web_agent.py').exists())
        self.assertNotIn('swift build', (REPO / '.github/workflows/ci.yml').read_text())


if __name__ == '__main__': unittest.main()
