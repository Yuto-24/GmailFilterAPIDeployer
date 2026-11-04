"""CLI output is preserved on disk even when the container is removed."""
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gmail_filter_deployer as d


class RunLogTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.local = Path(tmp.name)

    def invoke(self, func, *options):
        console, errors = StringIO(), StringIO()
        args = ["run", "--local-dir", str(self.local), "--expected-email", "test@example.invalid", *options]
        with patch.object(d, "cmd_run", side_effect=func):
            with redirect_stdout(console), redirect_stderr(errors):
                code = d.main(args)
        paths = sorted((self.local / "logs").glob("*.log"))
        return code, console.getvalue(), errors.getvalue(), paths

    def test_default_and_verbose_are_saved_and_visible(self):
        def output(args):
            print("工程: 500/1000 (50.0%)")
            if args.verbose:
                print("Gmail messages.get batch (1.24s)")
        code, stdout, stderr, paths = self.invoke(output)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(paths), 1)
        self.assertIn("工程: 500/1000", stdout)
        self.assertNotIn("messages.get batch", paths[0].read_text())
        self.assertEqual(stdout, paths[0].read_text())
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(paths[0].parent.stat().st_mode & 0o777, 0o700)

        code, stdout, stderr, paths = self.invoke(output, "-v")
        self.assertEqual(code, 0)
        self.assertEqual(len(paths), 2)
        self.assertIn("messages.get batch (1.24s)", stdout)
        self.assertEqual(stdout, paths[-1].read_text())
        self.assertNotEqual(paths[0], paths[-1])

    def test_stderr_and_handled_failure_are_retained(self):
        def failure(args):
            print("更新処理中")
            print("WARNING: readback delayed", file=d.sys.stderr)
            raise d.DeploymentError("API照合で停止")
        code, stdout, stderr, paths = self.invoke(failure)
        self.assertEqual(code, 2)
        log = paths[0].read_text()
        self.assertIn("更新処理中", stdout)
        self.assertIn("WARNING: readback delayed", stderr)
        self.assertIn("STOP: API照合で停止", stderr)
        self.assertIn("WARNING: readback delayed", log)
        self.assertIn("STOP: API照合で停止", log)
        self.assertIn("更新処理中", log)
