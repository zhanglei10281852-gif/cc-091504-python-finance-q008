"""样例数据与端到端演示的对账测试。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SampleDataTest(unittest.TestCase):
    def test_reference_files_exist(self):
        for rel in (
            "reference/domain.json",
            "reference/calendars/CN.json",
            "reference/products/NOTE-A001.terms.json",
            "reference/prices/NOTE-A001.prices.jsonl",
            "reference/cashflows/NOTE-A001.expected.json",
        ):
            self.assertTrue((ROOT / rel).exists(), f"缺少 {rel}")

    def test_demo_scenario_reconciles(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "demo_scenario.py"),
             "--runtime-dir", tempfile.mkdtemp()],
            capture_output=True, text=True, cwd=ROOT,
        )
        self.assertEqual(
            result.returncode, 0,
            f"演示脚本断言失败：\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
