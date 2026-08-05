from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


class AutoMutationSafetyContractTests(unittest.TestCase):
    def test_uncertain_mutation_guard_is_loaded_and_fail_closed(self) -> None:
        entry = (EXTENSION / "background_full_entry.js").read_text(encoding="utf-8")
        safety = (
            EXTENSION / "background_auto_mutation_safety.js"
        ).read_text(encoding="utf-8")

        self.assertLess(
            entry.index('"background_auto_mutation_safety.js"'),
            entry.index('"background_task_controller.js"'),
        )
        self.assertIn('BDB_AUTO_MUTATION_UNCERTAIN_STATUS = "uncertain"', safety)
        self.assertIn('"multi_file_patch"', safety)
        self.assertIn('"replace_exact_and_test"', safety)
        self.assertIn("claimAutoReplayWithMutationSafety", safety)
        self.assertIn("considerAutoWithMutationSafety", safety)
        self.assertIn('"manual_reconciliation_required"', safety)
        self.assertIn("uncertain_execution: true", safety)

    def test_mutation_safety_script_has_valid_javascript(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required")
        completed = subprocess.run(
            [
                node,
                "--check",
                str(EXTENSION / "background_auto_mutation_safety.js"),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
