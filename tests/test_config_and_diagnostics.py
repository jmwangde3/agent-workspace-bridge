import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import awb_config as C


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.explicit = self.root / "explicit.json"
        self.user = self.root / "user"
        self.user.mkdir()
        for item in (patch.object(C, "HERE", str(self.root)), patch.object(C, "DATA_DIR", str(self.user)),
                     patch.dict(os.environ)):
            item.start()
            self.addCleanup(item.stop)
        os.environ.pop("AWB_CONFIG", None)

    def test_explicit_invalid_config_never_falls_back(self):
        (self.root / "config.json").write_text("{}", encoding="utf-8")
        for value, message in ((" ", "whitespace"), (str(self.root), "directory"),
                               (str(self.explicit), "does not exist")):
            with self.subTest(value=value), patch.dict(os.environ, AWB_CONFIG=value):
                with self.assertRaisesRegex(C.ConfigError, message):
                    C.load()
        with patch.dict(os.environ, AWB_CONFIG=str(self.explicit)):
            for payload, message in (("{", "Invalid JSON"), ("[]", "JSON object"),
                                     ('{"snippet": 12}', "snippet"),
                                     ('{"workspace_root": ""}', "workspace_root"),
                                     ('{"config_dir": "<config_dir>/more"}', "cannot reference itself")):
                self.explicit.write_text(payload, encoding="utf-8")
                with self.subTest(payload=payload), self.assertRaisesRegex(C.ConfigError, message):
                    C.load()

    def test_defaults_relative_paths_and_source_precedence(self):
        self.assertEqual(C.load()["workspace_root"], os.path.expanduser("~/workspaces").replace("/", os.sep))
        user = self.user / "config.json"
        user.write_text('{"workspace_root":"user"}', encoding="utf-8")
        self.assertEqual(C.load()["workspace_root"], str(self.root / "user"))
        (self.root / "config.json").write_text('{"workspace_root":"portable"}', encoding="utf-8")
        self.assertEqual(C.load()["workspace_root"], str(self.root / "portable"))
        self.explicit.write_text(json.dumps({"config_dir": "settings", "workspace_root": "projects",
                                             "snippet": "<config_dir>/rules.md", "obsolete": 123}), encoding="utf-8")
        with patch.dict(os.environ, AWB_CONFIG=str(self.explicit)):
            config = C.load()
        self.assertEqual(config, {"config_dir": str(self.root / "settings"),
                                  "workspace_root": str(self.root / "projects"),
                                  "snippet": str(self.root / "settings" / "rules.md")})

    def test_selftest_report_uses_user_directory_and_explicit_path(self):
        source = Path(__file__).resolve().parents[1]
        self.explicit.write_text(json.dumps({"workspace_root": str(self.root),
                                             "snippet": str(source / "configs" / "agent-memory-snippet.md")}),
                                 encoding="utf-8")
        env = dict(os.environ, AWB_CONFIG=str(self.explicit), LOCALAPPDATA=str(self.user))
        command = [sys.executable, "-B", "-X", "utf8", str(source / "awb_gui.py"), "--selftest"]
        for extra, report in (([], self.user / "AWB" / "diagnostics" / "awb_selftest.json"),
                              (["--selftest-output", str(self.root / "custom.json")], self.root / "custom.json")):
            result = subprocess.run(command + extra, cwd=self.root, env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
            self.assertTrue(json.loads(report.read_text(encoding="utf-8"))["ok"])
        self.assertFalse((self.root / "awb_selftest.json").exists())
        result = subprocess.run(command, cwd=self.root, env=dict(env, AWB_CONFIG=" "),
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"AWB_CONFIG is empty", result.stdout)
