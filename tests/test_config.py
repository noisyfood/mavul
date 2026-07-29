import tempfile
import unittest
from pathlib import Path

from system.config import Config


class ConfigTest(unittest.TestCase):
    def test_loads_toml_into_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[agents.reverser]\n"
                'workspace = "memory/reverser"\n'
                "max_concurrency = 2\n",
                encoding="utf-8",
            )

            config = Config.from_toml(path)

        self.assertEqual(config.agent("reverser")["max_concurrency"], 2)
        self.assertEqual(config.source, path)

    def test_requires_agents_table(self):
        with self.assertRaises(ValueError):
            Config.from_mapping({"devices": {}})


if __name__ == "__main__":
    unittest.main()
