import unittest
from pathlib import Path
import tomllib
import gtktube

class TestVersion(unittest.TestCase):
    def test_version_matches_pyproject(self):
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
            expected_version = data["project"]["version"]
        
        self.assertEqual(gtktube.__version__, expected_version)

if __name__ == "__main__":
    unittest.main()
