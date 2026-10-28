from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from search_provider import (  # noqa: E402
    APIFY_RUN_URL,
    FIRECRAWL_SEARCH_URL,
    JINA_SEARCH_URL,
    SERPAPI_ACCOUNT_URL,
    SERPAPI_SEARCH_URL,
    FirecrawlAuthenticationError,
    FirecrawlCreditsExhausted,
    SearchConfigurationError,
    SearchProviderRouter,
    tavily_usage_is_exhausted,
)
from tavily.errors import UsageLimitExceededError  # noqa: E402


def response(status_code: int, payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(status_code=status_code, json=lambda: payload or {})


class SearchProviderTests(unittest.TestCase):
    def test_at_least_one_provider_key_is_required(self) -> None:
        with self.assertRaises(SearchConfigurationError):
            SearchProviderRouter()

    def test_tavily_is_primary_while_plan_has_credits(self) -> None:
        tavily = MagicMock()
        tavily.search.return_value = {
            "results": [{"title": "AI Intern", "url": "https://example.com/job", "content": "Singapore"}]
        }
        http = MagicMock()
        http.get.return_value = response(
            200,
            {"account": {"plan_usage": 10, "plan_limit": 1000}, "key": {"usage": 10, "limit": None}},
        )
        router = SearchProviderRouter(
            "tavily-key",
            "firecrawl-key",
            tavily_client=tavily,
            http_session=http,
        )

        result = router.search("AI intern Singapore", 5, "month")

        self.assertEqual(result["_provider"], "TAVILY")
        self.assertEqual(result["results"][0]["search_provider"], "TAVILY")
        self.assertEqual(router.providers_used, ["TAVILY"])
        http.post.assert_not_called()

    def test_confirmed_tavily_plan_exhaustion_switches_and_stays_on_firecrawl(self) -> None:
        tavily = MagicMock()
        http = MagicMock()
        http.get.return_value = response(200, {"account": {"plan_usage": 1000, "plan_limit": 1000}})
        http.post.return_value = response(
            200,
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Robotics Intern",
                            "url": "https://employer.example/robotics-intern",
                            "description": "Singapore robotics internship",
                            "markdown": "Full role text",
                        }
                    ]
                },
                "creditsUsed": 1,
            },
        )
        router = SearchProviderRouter(
            "tavily-key",
            "firecrawl-key",
            tavily_client=tavily,
            http_session=http,
        )

        first = router.search("robotics intern Singapore", 5, "month")
        second = router.search("embedded intern Singapore", 5, None)

        self.assertEqual(first["_provider"], "FIRECRAWL")
        self.assertEqual(second["_provider"], "FIRECRAWL")
        self.assertEqual(router.active_provider, "FIRECRAWL")
        self.assertEqual(router.fallback_reason, "Tavily plan credits exhausted.")
        tavily.search.assert_not_called()
        self.assertEqual(http.get.call_count, 1)
        self.assertEqual(http.post.call_count, 2)

    def test_transient_tavily_rate_limit_uses_next_configured_provider(self) -> None:
        tavily = MagicMock()
        tavily.search.side_effect = UsageLimitExceededError("Rate limit exceeded: too many requests per minute")
        http = MagicMock()
        http.get.return_value = response(200, {"account": {"plan_usage": 20, "plan_limit": 1000}})
        http.post.return_value = response(200, {"success": True, "data": {"web": []}})
        router = SearchProviderRouter(
            "tavily-key",
            "firecrawl-key",
            tavily_client=tavily,
            http_session=http,
        )

        result = router.search("AI intern Singapore", 5)

        self.assertEqual(result["_provider"], "FIRECRAWL")
        self.assertEqual(router.active_provider, "FIRECRAWL")
        self.assertIn("TAVILY", router.disabled_providers)
        self.assertIn("temporarily rate limited", router.fallback_reason.lower())

    def test_usage_limit_error_switches_when_usage_endpoint_is_unavailable(self) -> None:
        tavily = MagicMock()
        tavily.search.side_effect = UsageLimitExceededError("Monthly usage limit exceeded")
        http = MagicMock()
        http.get.return_value = response(503, {})
        http.post.return_value = response(200, {"success": True, "data": {"web": []}})
        router = SearchProviderRouter(
            "tavily-key",
            "firecrawl-key",
            tavily_client=tavily,
            http_session=http,
        )

        result = router.search("AI intern Singapore", 5)

        self.assertEqual(result["_provider"], "FIRECRAWL")
        self.assertEqual(router.active_provider, "FIRECRAWL")

    def test_tavily_request_failure_falls_through_without_exposing_error_details(self) -> None:
        tavily = MagicMock()
        tavily.search.side_effect = RuntimeError("request failed with secret-looking internals")
        http = MagicMock()
        http.get.return_value = response(200, {"account": {"plan_usage": 20, "plan_limit": 1000}})
        http.post.return_value = response(200, {"success": True, "data": {"web": []}})
        router = SearchProviderRouter(
            "tavily-key",
            "firecrawl-key",
            tavily_client=tavily,
            http_session=http,
        )

        result = router.search("AI intern Singapore", 5)

        self.assertEqual(result["_provider"], "FIRECRAWL")
        self.assertEqual(router.provider_failures[0]["reason"], "Tavily request failed.")
        self.assertNotIn("secret-looking", str(router.status()))

    def test_firecrawl_maps_search_results_and_reuses_markdown_for_extract(self) -> None:
        http = MagicMock()
        http.post.return_value = response(
            200,
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Computer Vision Intern",
                            "url": "https://example.com/cv-intern",
                            "description": "A Singapore role",
                            "markdown": "# Computer Vision Intern\nBuild evaluation pipelines.",
                            "position": 1,
                        }
                    ]
                },
                "creditsUsed": 1,
            },
        )
        router = SearchProviderRouter(firecrawl_key="firecrawl-key", http_session=http)

        result = router.search("computer vision intern Singapore", 3, "week")
        extracted = router.extract_urls(["https://example.com/cv-intern"])

        self.assertEqual(result["results"][0]["search_provider"], "FIRECRAWL")
        self.assertIn("evaluation pipelines", extracted["https://example.com/cv-intern"])
        self.assertEqual(http.post.call_count, 1)
        called_url = http.post.call_args.args[0]
        called_payload = http.post.call_args.kwargs["json"]
        self.assertEqual(called_url, FIRECRAWL_SEARCH_URL)
        self.assertEqual(called_payload["tbs"], "qdr:w")
        self.assertEqual(called_payload["country"], "sg")
        self.assertNotIn("scrapeOptions", called_payload)

    def test_serpapi_preflight_and_search_mapping(self) -> None:
        http = MagicMock()
        http.get.side_effect = [
            response(200, {"total_searches_left": 249}),
            response(
                200,
                {
                    "organic_results": [
                        {
                            "position": 1,
                            "title": "Robotics Intern",
                            "link": "https://employer.example/robotics",
                            "snippet": "Singapore robotics role",
                        }
                    ]
                },
            ),
        ]
        router = SearchProviderRouter(serpapi_key="serp-key", http_session=http)

        result = router.search("robotics intern Singapore", 5, "day")

        self.assertEqual(result["_provider"], "SERPAPI")
        self.assertEqual(result["results"][0]["search_provider"], "SERPAPI")
        self.assertEqual(router.status()["serpapi_searches_left"], 249)
        self.assertEqual(http.get.call_args_list[0].args[0], SERPAPI_ACCOUNT_URL)
        self.assertEqual(http.get.call_args_list[1].args[0], SERPAPI_SEARCH_URL)
        self.assertEqual(http.get.call_args_list[1].kwargs["params"]["tbs"], "qdr:d")

    def test_exhausted_serpapi_falls_through_to_jina(self) -> None:
        http = MagicMock()
        http.get.side_effect = [
            response(200, {"total_searches_left": 0}),
            response(
                200,
                {
                    "data": [
                        {
                            "title": "Embedded Intern",
                            "url": "https://employer.example/embedded",
                            "description": "Singapore embedded role",
                        }
                    ]
                },
            ),
        ]
        router = SearchProviderRouter(
            serpapi_key="serp-key",
            jina_key="jina-key",
            http_session=http,
        )

        result = router.search("embedded intern Singapore", 5)

        self.assertEqual(result["_provider"], "JINA")
        self.assertIn("SERPAPI", router.disabled_providers)
        self.assertEqual(http.get.call_args_list[1].args[0], JINA_SEARCH_URL)

    def test_apify_search_is_cost_capped(self) -> None:
        http = MagicMock()
        http.post.return_value = response(
            200,
            [
                {
                    "organicResults": [
                        {
                            "title": "AI Intern",
                            "url": "https://employer.example/ai-intern",
                            "description": "Singapore AI internship",
                        }
                    ]
                }
            ],
        )
        router = SearchProviderRouter(apify_token="apify-token", http_session=http)

        result = router.search("AI intern Singapore", 5)

        self.assertEqual(result["_provider"], "APIFY")
        expected_url = APIFY_RUN_URL.format(actor_id="apify~google-search-scraper")
        self.assertEqual(http.post.call_args.args[0], expected_url)
        self.assertEqual(http.post.call_args.kwargs["params"]["maxTotalChargeUsd"], 0.50)
        self.assertEqual(http.post.call_args.kwargs["params"]["maxItems"], 1)

    def test_apify_extraction_uses_one_page_and_sufficient_memory(self) -> None:
        http = MagicMock()
        http.post.return_value = response(
            200,
            [{"url": "https://example.com", "markdown": "# Example\nExtracted content."}],
        )
        router = SearchProviderRouter(apify_token="apify-token", http_session=http)

        extracted = router.extract_urls(["https://example.com"])

        self.assertIn("Extracted content", extracted["https://example.com"])
        called = http.post.call_args
        self.assertIn("apify~website-content-crawler", called.args[0])
        self.assertEqual(called.kwargs["params"]["memory"], 1024)
        self.assertEqual(called.kwargs["params"]["maxItems"], 1)
        self.assertEqual(called.kwargs["json"]["maxCrawlPages"], 1)
        self.assertEqual(called.kwargs["json"]["maxResults"], 1)

    def test_firecrawl_402_has_a_clear_credit_error(self) -> None:
        http = MagicMock()
        http.post.return_value = response(402, {})
        router = SearchProviderRouter(firecrawl_key="firecrawl-key", http_session=http)

        with self.assertRaises(FirecrawlCreditsExhausted):
            router.search("AI intern Singapore", 2)

    def test_firecrawl_invalid_key_has_a_clear_authentication_error(self) -> None:
        http = MagicMock()
        http.post.return_value = response(401, {"success": False, "error": "Unauthorized"})
        router = SearchProviderRouter(firecrawl_key="invalid", http_session=http)

        with self.assertRaises(FirecrawlAuthenticationError):
            router.search("AI intern Singapore", 2)

    def test_tavily_usage_requires_a_real_positive_limit(self) -> None:
        self.assertTrue(tavily_usage_is_exhausted({"account": {"plan_usage": 1000, "plan_limit": 1000}}))
        self.assertFalse(tavily_usage_is_exhausted({"account": {"plan_usage": 20, "plan_limit": 1000}}))
        self.assertFalse(tavily_usage_is_exhausted({"key": {"usage": 0, "limit": 0}}))


if __name__ == "__main__":
    unittest.main()
