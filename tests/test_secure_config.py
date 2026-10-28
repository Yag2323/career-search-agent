from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from secure_config import SENSITIVE_KEYS  # noqa: E402


class SecureConfigTests(unittest.TestCase):
    def test_search_provider_keys_are_allowed_for_dpapi_storage(self) -> None:
        self.assertTrue(
            {
                "TAVILY_API_KEY",
                "FIRECRAWL_API_KEY",
                "APIFY_TOKEN",
                "JINA_API_KEY",
                "SERPAPI_KEY",
            }.issubset(SENSITIVE_KEYS)
        )


if __name__ == "__main__":
    unittest.main()
