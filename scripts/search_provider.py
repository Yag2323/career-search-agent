from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote

import requests
from tavily import TavilyClient
from tavily.errors import UsageLimitExceededError

TAVILY_USAGE_URL = "https://api.tavily.com/usage"
SERPAPI_ACCOUNT_URL = "https://serpapi.com/account.json"
SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
JINA_SEARCH_URL = "https://s.jina.ai/"
JINA_READER_URL = "https://r.jina.ai"
FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
APIFY_RUN_URL = "https://api.apify.com/v2/actors/{actor_id}/run-sync-get-dataset-items"
APIFY_SEARCH_ACTOR = "apify~google-search-scraper"
APIFY_READER_ACTOR = "apify~website-content-crawler"

SEARCH_PROVIDER_ORDER = ("TAVILY", "SERPAPI", "JINA", "FIRECRAWL", "APIFY")
EXTRACTION_PROVIDER_ORDER = ("TAVILY", "JINA", "FIRECRAWL", "APIFY")
EXTRACTION_WORKERS = 8
APIFY_EXTRACTION_LIMIT = 5


class SearchProviderError(RuntimeError):
    pass


class SearchProviderTerminalError(SearchProviderError):
    pass


class SearchConfigurationError(SearchProviderTerminalError):
    pass


class SearchProviderRateLimited(SearchProviderTerminalError):
    pass


class ProviderAuthenticationError(SearchProviderTerminalError):
    pass


class FirecrawlAuthenticationError(ProviderAuthenticationError):
    pass


class TavilyCreditsExhausted(SearchProviderTerminalError):
    pass


class SerpApiCreditsExhausted(SearchProviderTerminalError):
    pass


class JinaCreditsExhausted(SearchProviderTerminalError):
    pass


class FirecrawlCreditsExhausted(SearchProviderTerminalError):
    pass


class ApifyCreditsExhausted(SearchProviderTerminalError):
    pass


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _response_json(response: Any) -> Any:
    try:
        return response.json()
    except (ValueError, TypeError, AttributeError):
        return None


def _payload_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    values = [
        payload.get("error"),
        payload.get("message"),
        payload.get("detail"),
        (payload.get("error") or {}).get("message") if isinstance(payload.get("error"), dict) else "",
        (payload.get("error") or {}).get("type") if isinstance(payload.get("error"), dict) else "",
    ]
    return " ".join(str(value) for value in values if value).lower()


def tavily_usage_is_exhausted(payload: dict[str, Any]) -> bool:
    """Return True only when a documented Tavily plan/key limit is reached."""
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    key = payload.get("key") if isinstance(payload.get("key"), dict) else {}
    candidates = [
        (account.get("plan_usage"), account.get("plan_limit")),
        (key.get("usage"), key.get("limit")),
        (payload.get("usage"), payload.get("limit")),
    ]
    for usage_value, limit_value in candidates:
        usage = _number(usage_value)
        limit = _number(limit_value)
        if usage is not None and limit is not None and limit > 0 and usage >= limit:
            return True

    remaining_fields = [
        account.get("plan_credits_remaining"),
        account.get("credits_remaining"),
        key.get("credits_remaining"),
        payload.get("credits_remaining"),
    ]
    return any(_number(value) is not None and _number(value) <= 0 for value in remaining_fields)


def tavily_error_indicates_transient_rate_limit(error: BaseException) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in [
            "rate limit",
            "requests per minute",
            "requests/minute",
            "too many requests",
            " rpm",
        ]
    )


def tavily_error_indicates_credit_exhaustion(error: BaseException) -> bool:
    if tavily_error_indicates_transient_rate_limit(error):
        return False
    message = str(error).lower()
    return any(
        marker in message
        for marker in [
            "credit",
            "quota",
            "plan limit",
            "monthly limit",
            "usage limit",
            "usage exceeded",
            "limit exceeded",
            "exhausted",
        ]
    )


def _time_filter(time_range: str | None) -> str:
    filters = {
        "day": "qdr:d",
        "d": "qdr:d",
        "week": "qdr:w",
        "w": "qdr:w",
        "month": "qdr:m",
        "m": "qdr:m",
        "year": "qdr:y",
        "y": "qdr:y",
    }
    return filters.get(str(time_range or "").lower(), "")


def _jina_query(query: str, time_range: str | None) -> str:
    days = {
        "day": 1,
        "d": 1,
        "week": 7,
        "w": 7,
        "month": 31,
        "m": 31,
        "year": 366,
        "y": 366,
    }.get(str(time_range or "").lower())
    if not days:
        return query
    after = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    return f"{query} after:{after}"


class SearchProviderRouter:
    """Quota-aware broad search and extraction across five configured providers."""

    def __init__(
        self,
        tavily_key: str = "",
        firecrawl_key: str = "",
        *,
        serpapi_key: str = "",
        jina_key: str = "",
        apify_token: str = "",
        tavily_client: Any | None = None,
        http_session: Any | None = None,
        check_tavily_usage: bool = True,
        check_serpapi_usage: bool = True,
    ) -> None:
        self.keys = {
            "TAVILY": str(tavily_key or "").strip(),
            "SERPAPI": str(serpapi_key or "").strip(),
            "JINA": str(jina_key or "").strip(),
            "FIRECRAWL": str(firecrawl_key or "").strip(),
            "APIFY": str(apify_token or "").strip(),
        }
        self.provider_order = [provider for provider in SEARCH_PROVIDER_ORDER if self.keys[provider]]
        if not self.provider_order:
            raise SearchConfigurationError(
                "No search provider is configured. Add TAVILY_API_KEY, SERPAPI_KEY, JINA_API_KEY, "
                "FIRECRAWL_API_KEY, or APIFY_TOKEN to secure storage."
            )

        self.tavily_key = self.keys["TAVILY"]
        self.firecrawl_key = self.keys["FIRECRAWL"]
        self.serpapi_key = self.keys["SERPAPI"]
        self.jina_key = self.keys["JINA"]
        self.apify_token = self.keys["APIFY"]
        self.tavily_client = tavily_client
        if self.tavily_key and self.tavily_client is None:
            self.tavily_client = TavilyClient(api_key=self.tavily_key)
        self.http = http_session or requests.Session()

        self.initial_provider = self.provider_order[0]
        self.active_provider = self.initial_provider
        self.providers_used: list[str] = []
        self.disabled_providers: set[str] = set()
        self.provider_failures: list[dict[str, str]] = []
        self.fallback_reason = ""

        self._check_tavily_usage = check_tavily_usage
        self._tavily_usage_checked = False
        self._tavily_usage_exhausted: bool | None = None
        self._check_serpapi_usage = check_serpapi_usage
        self._serpapi_usage_checked = False
        self._serpapi_searches_left: int | None = None
        self._firecrawl_content: dict[str, str] = {}

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "SearchProviderRouter":
        return cls(
            tavily_key=env.get("TAVILY_API_KEY", ""),
            firecrawl_key=env.get("FIRECRAWL_API_KEY", ""),
            serpapi_key=env.get("SERPAPI_KEY", ""),
            jina_key=env.get("JINA_API_KEY", ""),
            apify_token=env.get("APIFY_TOKEN", ""),
        )

    def status(self) -> dict[str, Any]:
        return {
            "initial_provider": self.initial_provider,
            "active_provider": self.active_provider,
            "configured_providers": list(self.provider_order),
            "providers_used": list(self.providers_used),
            "disabled_providers": [item for item in self.provider_order if item in self.disabled_providers],
            "provider_failures": list(self.provider_failures),
            "fallback_reason": self.fallback_reason,
            "serpapi_searches_left": self._serpapi_searches_left,
        }

    def _record_provider(self, provider: str) -> None:
        if provider not in self.providers_used:
            self.providers_used.append(provider)

    def _reason_for_error(self, provider: str, error: BaseException) -> str:
        if isinstance(error, TavilyCreditsExhausted):
            return "Tavily plan credits exhausted."
        if isinstance(error, SerpApiCreditsExhausted):
            return "SerpApi monthly searches exhausted."
        if isinstance(error, JinaCreditsExhausted):
            return "Jina token balance exhausted."
        if isinstance(error, FirecrawlCreditsExhausted):
            return "Firecrawl credits exhausted."
        if isinstance(error, ApifyCreditsExhausted):
            return "Apify usage balance or run allowance exhausted."
        if isinstance(error, ProviderAuthenticationError):
            return f"{provider.title()} authentication rejected."
        if isinstance(error, SearchProviderRateLimited):
            return f"{provider.title()} temporarily rate limited."
        return f"{provider.title()} request failed."

    def _disable_provider(self, provider: str, error: BaseException) -> None:
        reason = self._reason_for_error(provider, error)
        self.disabled_providers.add(provider)
        self.provider_failures.append({"provider": provider, "reason": reason})
        self.fallback_reason = reason
        available = [item for item in self.provider_order if item not in self.disabled_providers]
        if available:
            self.active_provider = available[0]

    def _tavily_plan_exhausted(self, *, force: bool = False) -> bool | None:
        if not self.tavily_key or not self._check_tavily_usage:
            return None
        if self._tavily_usage_checked and not force:
            return self._tavily_usage_exhausted

        self._tavily_usage_checked = True
        try:
            response = self.http.get(
                TAVILY_USAGE_URL,
                headers={"Authorization": f"Bearer {self.tavily_key}"},
                timeout=15,
            )
            if response.status_code != 200:
                self._tavily_usage_exhausted = None
                return None
            payload = _response_json(response)
            if not isinstance(payload, dict):
                self._tavily_usage_exhausted = None
                return None
            self._tavily_usage_exhausted = tavily_usage_is_exhausted(payload)
            return self._tavily_usage_exhausted
        except requests.RequestException:
            self._tavily_usage_exhausted = None
            return None

    def _serpapi_check_quota(self) -> None:
        if not self.serpapi_key or not self._check_serpapi_usage or self._serpapi_usage_checked:
            return
        self._serpapi_usage_checked = True
        try:
            response = self.http.get(
                SERPAPI_ACCOUNT_URL,
                params={"api_key": self.serpapi_key},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise SearchProviderError("SerpApi account check could not be reached.") from exc
        payload = _response_json(response)
        if response.status_code in {401, 403}:
            raise ProviderAuthenticationError("SerpApi authentication was rejected.")
        if response.status_code == 429:
            raise SearchProviderRateLimited("SerpApi is temporarily rate limited.")
        if response.status_code >= 400 or not isinstance(payload, dict):
            raise SearchProviderError("SerpApi account check failed.")
        remaining = _number(payload.get("total_searches_left"))
        if remaining is None:
            remaining = _number(payload.get("plan_searches_left"))
        self._serpapi_searches_left = int(remaining) if remaining is not None else None
        if remaining is not None and remaining <= 0:
            raise SerpApiCreditsExhausted("SerpApi monthly searches are exhausted.")

    def search(self, query: str, max_results: int, time_range: str | None = None) -> dict[str, Any]:
        last_error: SearchProviderError | None = None
        for provider in self.provider_order:
            if provider in self.disabled_providers:
                continue
            self.active_provider = provider
            try:
                if provider == "TAVILY":
                    return self._search_tavily(query, max_results, time_range)
                if provider == "SERPAPI":
                    return self._search_serpapi(query, max_results, time_range)
                if provider == "JINA":
                    return self._search_jina(query, max_results, time_range)
                if provider == "FIRECRAWL":
                    return self._search_firecrawl(query, max_results, time_range)
                return self._search_apify(query, max_results, time_range)
            except UsageLimitExceededError as exc:
                exhausted = self._tavily_plan_exhausted(force=True)
                if exhausted is True or tavily_error_indicates_credit_exhaustion(exc):
                    error: SearchProviderError = TavilyCreditsExhausted("Tavily plan credits are exhausted.")
                else:
                    error = SearchProviderRateLimited("Tavily is temporarily rate limited.")
                last_error = error
                self._disable_provider(provider, error)
            except SearchProviderError as exc:
                last_error = exc
                self._disable_provider(provider, exc)

        if last_error is not None:
            raise last_error
        raise SearchProviderTerminalError("All configured search providers are unavailable for this run.")

    def _search_tavily(
        self,
        query: str,
        max_results: int,
        time_range: str | None,
    ) -> dict[str, Any]:
        if self._tavily_plan_exhausted() is True:
            raise TavilyCreditsExhausted("Tavily plan credits are exhausted.")
        kwargs: dict[str, Any] = {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if time_range:
            kwargs["time_range"] = time_range

        try:
            response = self.tavily_client.search(**kwargs, timeout=25)
        except UsageLimitExceededError:
            raise
        except Exception as exc:
            raise SearchProviderError("Tavily search request failed.") from exc
        if not isinstance(response, dict):
            raise SearchProviderError("Tavily returned an invalid search response.")
        results: list[dict[str, Any]] = []
        for item in response.get("results") or []:
            if not isinstance(item, dict):
                continue
            normalized = dict(item)
            normalized["search_provider"] = "TAVILY"
            results.append(normalized)
        output = dict(response)
        output["results"] = results
        output["_provider"] = "TAVILY"
        self._record_provider("TAVILY")
        return output

    def _search_serpapi(
        self,
        query: str,
        max_results: int,
        time_range: str | None,
    ) -> dict[str, Any]:
        self._serpapi_check_quota()
        params: dict[str, Any] = {
            "api_key": self.serpapi_key,
            "engine": "google",
            "q": query,
            "location": "Singapore",
            "google_domain": "google.com.sg",
            "gl": "sg",
            "hl": "en",
            "num": max(1, min(int(max_results), 20)),
            "output": "json",
        }
        if time_filter := _time_filter(time_range):
            params["tbs"] = time_filter
        try:
            response = self.http.get(SERPAPI_SEARCH_URL, params=params, timeout=45)
        except requests.RequestException as exc:
            raise SearchProviderError("SerpApi could not be reached.") from exc
        payload = _response_json(response)
        message = _payload_message(payload)
        if response.status_code in {401, 403} or "invalid api key" in message:
            raise ProviderAuthenticationError("SerpApi authentication was rejected.")
        if response.status_code == 429 or "rate limit" in message:
            raise SearchProviderRateLimited("SerpApi is temporarily rate limited.")
        if response.status_code == 402 or any(marker in message for marker in ["out of searches", "no searches left", "quota"]):
            raise SerpApiCreditsExhausted("SerpApi monthly searches are exhausted.")
        if response.status_code >= 400 or not isinstance(payload, dict):
            raise SearchProviderError("SerpApi search request failed.")
        if payload.get("error"):
            raise SearchProviderError("SerpApi returned an unsuccessful response.")

        results: list[dict[str, Any]] = []
        for index, item in enumerate(payload.get("organic_results") or [], start=1):
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "title": item.get("title") or "",
                    "url": item.get("link") or "",
                    "content": item.get("snippet") or "",
                    "raw_content": "",
                    "score": "",
                    "position": item.get("position") or index,
                    "date": item.get("date") or "",
                    "search_provider": "SERPAPI",
                }
            )
        self._record_provider("SERPAPI")
        return {"results": results, "_provider": "SERPAPI"}

    def _jina_headers(self, *, no_cache: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.jina_key}",
            "Accept": "application/json",
        }
        if no_cache:
            headers["X-No-Cache"] = "true"
            headers["X-Cache-Tolerance"] = "0"
        return headers

    def _jina_get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        no_cache: bool = False,
        timeout: int = 30,
    ) -> Any:
        try:
            response = self.http.get(
                url,
                params=params,
                headers=self._jina_headers(no_cache=no_cache),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise SearchProviderError("Jina could not be reached.") from exc
        payload = _response_json(response)
        message = _payload_message(payload)
        if response.status_code == 401:
            raise ProviderAuthenticationError("Jina authentication was rejected.")
        if response.status_code in {402, 403} and any(
            marker in message for marker in ["token", "balance", "credit", "insufficient", "quota"]
        ):
            raise JinaCreditsExhausted("Jina token balance is exhausted.")
        if response.status_code == 403:
            raise ProviderAuthenticationError("Jina authentication was rejected.")
        if response.status_code == 429:
            raise SearchProviderRateLimited("Jina is temporarily rate limited.")
        if response.status_code >= 400:
            raise SearchProviderError("Jina request failed.")
        if payload is not None:
            return payload
        text = str(getattr(response, "text", "") or "").strip()
        if text:
            return text
        raise SearchProviderError("Jina returned an empty response.")

    def _search_jina(
        self,
        query: str,
        max_results: int,
        time_range: str | None,
    ) -> dict[str, Any]:
        payload = self._jina_get(
            JINA_SEARCH_URL,
            params={"q": _jina_query(query, time_range)},
            no_cache=True,
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        if isinstance(data, dict):
            data = data.get("results") or []
        if not isinstance(data, list):
            data = []
        results: list[dict[str, Any]] = []
        for index, item in enumerate(data[: max(1, int(max_results))], start=1):
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "title": item.get("title") or "",
                    "url": item.get("url") or item.get("link") or "",
                    "content": item.get("description") or item.get("content") or "",
                    "raw_content": item.get("content") or "",
                    "score": item.get("score") or "",
                    "position": index,
                    "date": item.get("publishedTime") or item.get("published_time") or "",
                    "search_provider": "JINA",
                }
            )
        self._record_provider("JINA")
        return {"results": results, "_provider": "JINA"}

    def _firecrawl_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.firecrawl_key}",
            "Content-Type": "application/json",
        }

    def _firecrawl_post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.http.post(
                url,
                headers=self._firecrawl_headers(),
                json=payload,
                timeout=75,
            )
        except requests.RequestException as exc:
            raise SearchProviderError("Firecrawl could not be reached.") from exc

        if response.status_code == 402:
            raise FirecrawlCreditsExhausted("Firecrawl credits are exhausted.")
        if response.status_code in {401, 403}:
            raise FirecrawlAuthenticationError("Firecrawl authentication was rejected.")
        if response.status_code == 429:
            raise SearchProviderRateLimited("Firecrawl is temporarily rate limited.")
        if response.status_code >= 400:
            raise SearchProviderError(f"Firecrawl request failed with HTTP {response.status_code}.")
        data = _response_json(response)
        if not isinstance(data, dict) or data.get("success") is False:
            raise SearchProviderError("Firecrawl returned an unsuccessful response.")
        return data

    def _search_firecrawl(
        self,
        query: str,
        max_results: int,
        time_range: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query,
            "limit": max(1, min(int(max_results), 100)),
            "sources": ["web"],
            "location": "Singapore",
            "country": "sg",
            "timeout": 60000,
            "ignoreInvalidURLs": True,
        }
        if time_filter := _time_filter(time_range):
            payload["tbs"] = time_filter

        response = self._firecrawl_post(FIRECRAWL_SEARCH_URL, payload)
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        web_results = data.get("web") if isinstance(data.get("web"), list) else []
        results: list[dict[str, Any]] = []
        for index, item in enumerate(web_results, start=1):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            markdown = str(item.get("markdown") or "").strip()
            description = str(item.get("description") or "").strip()
            if url and markdown:
                self._firecrawl_content[url] = markdown
            results.append(
                {
                    "title": item.get("title") or "",
                    "url": url,
                    "content": description or markdown[:2000],
                    "raw_content": markdown,
                    "score": item.get("score") or "",
                    "position": item.get("position") or index,
                    "date": (
                        item.get("publishedDate")
                        or item.get("published_date")
                        or item.get("date")
                        or ""
                    ),
                    "search_provider": "FIRECRAWL",
                }
            )

        self._record_provider("FIRECRAWL")
        return {
            "results": results,
            "_provider": "FIRECRAWL",
            "_credits_used": response.get("creditsUsed", ""),
        }

    def _apify_post(
        self,
        actor_id: str,
        payload: dict[str, Any],
        *,
        max_items: int,
        memory: int = 256,
    ) -> list[dict[str, Any]]:
        url = APIFY_RUN_URL.format(actor_id=quote(actor_id, safe="~"))
        params = {
            "clean": "true",
            "format": "json",
            "maxItems": max(1, max_items),
            "maxTotalChargeUsd": 0.50,
            "memory": memory,
            "timeout": 90,
        }
        try:
            response = self.http.post(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self.apify_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=120,
            )
        except requests.RequestException as exc:
            raise SearchProviderError("Apify could not be reached.") from exc
        data = _response_json(response)
        message = _payload_message(data)
        if response.status_code in {401, 403} and not any(
            marker in message for marker in ["usage", "charge", "plan", "billing", "limit"]
        ):
            raise ProviderAuthenticationError("Apify authentication was rejected.")
        if response.status_code == 402 or any(
            marker in message
            for marker in [
                "not-enough-usage",
                "monthly-usage-limit",
                "insufficient amount",
                "usage limit",
                "payment required",
            ]
        ):
            raise ApifyCreditsExhausted("Apify usage balance or run allowance is exhausted.")
        if response.status_code == 429 or "rate-limit" in message or "too many requests" in message:
            raise SearchProviderRateLimited("Apify is temporarily rate limited.")
        if response.status_code >= 400 or not isinstance(data, list):
            raise SearchProviderError("Apify actor request failed.")
        return [item for item in data if isinstance(item, dict)]

    def _search_apify(
        self,
        query: str,
        max_results: int,
        time_range: str | None,
    ) -> dict[str, Any]:
        query_with_time = _jina_query(query, time_range)
        records = self._apify_post(
            APIFY_SEARCH_ACTOR,
            {
                "queries": query_with_time,
                "resultsPerPage": max(1, min(int(max_results), 20)),
                "maxPagesPerQuery": 1,
                "countryCode": "sg",
                "languageCode": "en",
                "mobileResults": False,
            },
            max_items=1,
        )
        candidates: list[dict[str, Any]] = []
        for record in records:
            nested = (
                record.get("organicResults")
                or record.get("nonPromotedSearchResults")
                or record.get("unfilteredSearchResults")
                or record.get("searchResults")
            )
            if isinstance(nested, list):
                candidates.extend(item for item in nested if isinstance(item, dict))
            elif record.get("url") or record.get("link"):
                candidates.append(record)

        results: list[dict[str, Any]] = []
        for index, item in enumerate(candidates[: max(1, int(max_results))], start=1):
            results.append(
                {
                    "title": item.get("title") or "",
                    "url": item.get("url") or item.get("link") or "",
                    "content": item.get("description") or item.get("snippet") or "",
                    "raw_content": "",
                    "score": "",
                    "position": item.get("position") or index,
                    "date": (
                        item.get("date")
                        or item.get("publishedAt")
                        or item.get("published_at")
                        or ""
                    ),
                    "search_provider": "APIFY",
                }
            )
        self._record_provider("APIFY")
        return {"results": results, "_provider": "APIFY"}

    def extract_urls(self, urls: list[str]) -> dict[str, str]:
        targets = list(dict.fromkeys(str(url).strip() for url in urls if str(url).strip()))
        if not targets:
            return {}

        extracted: dict[str, str] = {}
        for provider in EXTRACTION_PROVIDER_ORDER:
            if not self.keys.get(provider) or provider in self.disabled_providers:
                continue
            missing = [url for url in targets if url not in extracted]
            if not missing:
                break
            if provider == "APIFY":
                missing = missing[:APIFY_EXTRACTION_LIMIT]
            try:
                if provider == "TAVILY":
                    values = self._extract_tavily(missing)
                elif provider == "JINA":
                    values = self._extract_jina(missing)
                elif provider == "FIRECRAWL":
                    values = self._extract_firecrawl(missing)
                else:
                    values = self._extract_apify(missing)
                extracted.update({url: value for url, value in values.items() if value})
            except UsageLimitExceededError as exc:
                exhausted = self._tavily_plan_exhausted(force=True)
                error: SearchProviderError
                if exhausted is True or tavily_error_indicates_credit_exhaustion(exc):
                    error = TavilyCreditsExhausted("Tavily plan credits are exhausted.")
                else:
                    error = SearchProviderRateLimited("Tavily is temporarily rate limited.")
                self._disable_provider(provider, error)
            except SearchProviderError as exc:
                self._disable_provider(provider, exc)
        return extracted

    def _extract_tavily(self, urls: list[str]) -> dict[str, str]:
        if self._tavily_plan_exhausted() is True:
            raise TavilyCreditsExhausted("Tavily plan credits are exhausted.")
        try:
            try:
                response = self.tavily_client.extract(
                    urls=urls,
                    extract_depth="basic",
                    format="markdown",
                    include_images=False,
                    timeout=30,
                )
            except TypeError:
                response = self.tavily_client.extract(urls)
        except UsageLimitExceededError:
            raise
        except Exception as exc:
            raise SearchProviderError("Tavily extraction request failed.") from exc

        extracted: dict[str, str] = {}
        if not isinstance(response, dict):
            return extracted
        for item in response.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            raw = str(item.get("raw_content") or item.get("content") or "").strip()
            if url and raw:
                extracted[url] = raw
        self._record_provider("TAVILY")
        return extracted

    @staticmethod
    def _parallel_extract(
        urls: list[str],
        reader: Callable[[str], str],
        *,
        max_workers: int = EXTRACTION_WORKERS,
    ) -> dict[str, str]:
        extracted: dict[str, str] = {}
        if not urls:
            return extracted

        with ThreadPoolExecutor(max_workers=min(max_workers, len(urls))) as executor:
            futures = {executor.submit(reader, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    content = future.result()
                except SearchProviderTerminalError:
                    for pending in futures:
                        pending.cancel()
                    raise
                except SearchProviderError:
                    continue
                if content:
                    extracted[url] = content
        return extracted

    def _extract_jina(self, urls: list[str]) -> dict[str, str]:
        def read(url: str) -> str:
            payload = self._jina_get(
                f"{JINA_READER_URL}/{url}",
                no_cache=True,
                timeout=20,
            )
            if isinstance(payload, dict):
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                return str(data.get("content") or data.get("markdown") or "").strip()
            return str(payload or "").strip()

        extracted = self._parallel_extract(urls, read)
        self._record_provider("JINA")
        return extracted

    def _extract_firecrawl(self, urls: list[str]) -> dict[str, str]:
        extracted: dict[str, str] = {}
        missing: list[str] = []
        for url in urls:
            cached = self._firecrawl_content.get(url, "")
            if cached:
                extracted[url] = cached
            else:
                missing.append(url)

        def read(url: str) -> str:
            response = self._firecrawl_post(
                FIRECRAWL_SCRAPE_URL,
                {
                    "url": url,
                    "formats": ["markdown"],
                    "onlyMainContent": True,
                    "timeout": 60000,
                },
            )
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            return str(data.get("markdown") or "").strip()

        fetched = self._parallel_extract(missing, read)
        self._firecrawl_content.update(fetched)
        extracted.update(fetched)
        self._record_provider("FIRECRAWL")
        return extracted

    def _extract_apify(self, urls: list[str]) -> dict[str, str]:
        def read(url: str) -> str:
            records = self._apify_post(
                APIFY_READER_ACTOR,
                {
                    "startUrls": [{"url": url}],
                    "crawlerType": "playwright:adaptive",
                    "maxCrawlDepth": 0,
                    "maxCrawlPages": 1,
                    "maxResults": 1,
                    "saveHtml": False,
                    "saveMarkdown": True,
                    "useSitemaps": False,
                },
                max_items=1,
                memory=1024,
            )
            for item in records:
                content = str(item.get("markdown") or item.get("text") or item.get("content") or "").strip()
                if content:
                    return content
            return ""

        extracted = self._parallel_extract(urls, read, max_workers=2)
        self._record_provider("APIFY")
        return extracted
