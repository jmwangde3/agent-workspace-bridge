import codecs
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
SCRIPT = Path(__file__).resolve().parents[1] / "sync-agent-memory.ps1"


@unittest.skipUnless(POWERSHELL, "Requires PowerShell")
class RuleDistributionTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "snippet.md"
        self.target = self.root / "中文规则.md"
        self.source.write_bytes(b"<!-- awb -->\nnew rule\n<!-- /awb -->\n")

    def run_script(self, *options):
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(SCRIPT), "-Snippet", str(self.source), "-TargetsOnly",
                               "-ExtraTargets", str(self.target), *options],
                              capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_check_distinguishes_drift_and_preserves_bom_and_nonmanaged_bytes(self):
        before = codecs.BOM_UTF8 + b"Top\r\n<!-- note -->\n<!-- awb -->\r\nold rule\r\n<!-- /awb -->\r\nEnd  "
        self.target.write_bytes(before)
        self.assertEqual(self.run_script("-Check").returncode, 1)
        self.assertEqual(self.run_script("-DryRun", "-Update").returncode, 0)
        self.assertEqual(self.target.read_bytes(), before)
        updated = self.run_script("-Update")
        self.assertEqual(updated.returncode, 0, (updated.stdout, updated.stderr))
        self.assertEqual(self.target.read_bytes(), before.replace(b"old rule", b"new rule"))
        self.assertEqual(next(self.root.glob("*.bak-*")).read_bytes(), before)
        self.assertEqual(self.run_script("-Check").returncode, 0)
        self.assertEqual(self.run_script("-Update").returncode, 0)
        self.assertEqual(len(list(self.root.glob("*.bak-*"))), 1)

    def test_invalid_markers_and_encoding_cannot_change_target(self):
        malformed = (
            b"<!-- awb -->\n",
            b"<!-- /awb -->\n<!-- awb -->\n",
            b"<!-- awb -->\n<!-- awb -->\n<!-- /awb -->\n",
            b"<!-- awb -->\n<!-- /awb -->\n<!-- awb -->\n<!-- /awb -->\n",
            b"text <!-- awb -->\n<!-- /awb -->\n",
            b"\xff unsupported",
        )
        for data in malformed:
            with self.subTest(data=data):
                self.target.write_bytes(data)
                for action in ("-Check", "-Update"):
                    result = self.run_script(action)
                    self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                    self.assertEqual(self.target.read_bytes(), data)
        self.assertFalse(list(self.root.glob("*.bak-*")))

    def test_missing_target_and_empty_source_block(self):
        self.source.write_bytes(b"<!-- awb -->\n<!-- /awb -->\n")
        self.assertEqual(self.run_script("-Check").returncode, 1)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.run_script("-Update").returncode, 0)
        self.assertEqual(self.target.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.run_script("-Check").returncode, 0)

    def test_default_and_all_read_only_modes_leave_test_home_unchanged(self):
        profile = self.root / "profile"
        folder = profile / ".workbuddy"
        folder.mkdir(parents=True)
        snippet = folder / "agent-memory-snippet.md"
        snippet.write_bytes(self.source.read_bytes())
        target = folder / "AGENTS.md"
        target.write_bytes(b"<!-- awb -->\nold\n<!-- /awb -->\n")
        command = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)]
        def rule_files():
            return {p.relative_to(profile): p.read_bytes() for p in profile.rglob("*")
                    if p.relative_to(profile).parts[0] != "AppData" and p.is_file()}

        before = rule_files()
        for flags, expected in (([], 0), (["-Status"], 0), (["-Check"], 1), (["-Lint"], 0),
                                (["-DryRun"], 0), (["-DryRun", "-Update"], 0),
                                (["-Check", "-Update"], 2)):
            with self.subTest(flags=flags):
                result = subprocess.run(command + flags, env=dict(os.environ, USERPROFILE=str(profile)),
                                        capture_output=True, timeout=20)
                self.assertEqual(result.returncode, expected, (result.stdout, result.stderr))
                self.assertEqual(rule_files(), before)

    @unittest.skipUnless(os.name == "nt", "Hard link protection is Windows-specific")
    def test_update_refuses_to_break_hard_link(self):
        original = b"legacy content"
        self.target.write_bytes(original)
        alias = self.root / "alias.md"
        os.link(self.target, alias)
        result = self.run_script("-Update")
        self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
        self.assertEqual(self.target.read_bytes(), original)
        self.assertTrue(self.target.samefile(alias))

    @unittest.skipUnless(os.name == "nt", "Windows prevents replacement of an open file")
    def test_replace_failure_preserves_original_and_reports_staged_file(self):
        original = b"existing rule outside managed blocks"
        self.target.write_bytes(original)
        with self.target.open("rb"):
            result = self.run_script("-Update")
            self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
            self.assertIn(b"STAGED_FILE=", result.stdout)
            self.assertEqual(self.target.read_bytes(), original)
