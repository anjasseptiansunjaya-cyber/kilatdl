import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(__file__)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ExtensionClassifierTest(unittest.TestCase):
    def test_media_detection_rules(self):
        script = os.path.join(HERE, "classify_test.js")
        background = os.path.abspath(os.path.join(HERE, "..", "extension", "background.js"))
        r = subprocess.run(["node", script, background], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("ok  "), 10, r.stdout)


if __name__ == "__main__":
    unittest.main()
