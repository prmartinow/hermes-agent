"""Unit and integration tests for the Gemini Grounding web search plugin."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
import pytest

from hermes_cli.plugins import _ensure_plugins_discovered
from agent.web_search_registry import (
    get_active_search_provider,
    get_provider,
    list_providers,
)
from plugins.web.gemini_grounding.provider import (
    GeminiGroundingWebSearchProvider,
    GoogleGroundingWebSearchProvider,
)


@pytest.fixture(autouse=True)
def _ensure_loaded() -> None:
    _ensure_plugins_discovered()


class TestGeminiGroundingRegistration:
    """Gemini Grounding plugin registration and metadata checks."""

    def test_registered_names(self) -> None:
        p1 = get_provider("gemini-grounding")
        p2 = get_provider("google-grounding")
        assert p1 is not None, "gemini-grounding must be registered"
        assert p2 is not None, "google-grounding alias must be registered"
        assert isinstance(p1, GeminiGroundingWebSearchProvider)
        assert isinstance(p2, GoogleGroundingWebSearchProvider)
        assert p1.name == "gemini-grounding"
        assert p2.name == "google-grounding"
        assert p1.display_name == "Google Search Grounding (Gemini OAuth)"

    def test_capabilities(self) -> None:
        p = get_provider("gemini-grounding")
        assert p.supports_search() is True
        assert p.supports_extract() is False

    def test_availability(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = get_provider("gemini-grounding")
        with patch("hermes_cli.auth.has_gemini_oauth_credentials", return_value=True):
            assert p.is_available() is True
            assert p.is_keyless_available() is True

        with patch("hermes_cli.auth.has_gemini_oauth_credentials", return_value=False):
            assert p.is_available() is False
            assert p.is_keyless_available() is False


class TestGeminiGroundingExecution:
    """Execution and response shape parsing for Gemini Grounding."""

    def test_empty_query_returns_empty_results(self) -> None:
        p = get_provider("gemini-grounding")
        res = p.search("")
        assert res == {"success": True, "data": {"web": []}}

    def test_search_parsing_with_mocked_response(self) -> None:
        p = get_provider("gemini-grounding")

        mock_creds = {
            "api_key": "mock_token_123",
            "base_url": "https://example-daily.googleapis.com/v1internal",
            "project_id": "test-project",
        }

        mock_payload = {
            "response": {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "Python 3.14 will be released in October 2025."}]
                        },
                        "groundingMetadata": {
                            "webSearchQueries": ["Python 3.14 release date"],
                            "groundingChunks": [
                                {
                                    "web": {
                                        "title": "PEP 745 Schedule",
                                        "uri": "https://peps.python.org/pep-0745/",
                                    }
                                },
                                {
                                    "web": {
                                        "title": "Python.org News",
                                        "uri": "https://www.python.org/downloads/",
                                    }
                                },
                            ],
                            "groundingSupports": [
                                {
                                    "groundingChunkIndices": [0],
                                    "segment": {
                                        "startIndex": 0,
                                        "endIndex": 44,
                                        "text": "Python 3.14 will be released in October 2025.",
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_payload

        with patch("hermes_cli.auth.resolve_gemini_oauth_runtime_credentials", return_value=mock_creds), \
             patch("httpx.Client.post", return_value=mock_resp):

            res = p.search("Python 3.14 release date", limit=5)

            assert res["success"] is True
            web_results = res["data"]["web"]
            assert len(web_results) == 2

            assert web_results[0]["title"] == "PEP 745 Schedule"
            assert web_results[0]["url"] == "https://peps.python.org/pep-0745/"
            assert web_results[0]["description"] == "Python 3.14 will be released in October 2025."
            assert web_results[0]["position"] == 1

            assert web_results[1]["title"] == "Python.org News"
            assert web_results[1]["url"] == "https://www.python.org/downloads/"
            assert web_results[1]["position"] == 2

    def test_search_handles_http_error(self) -> None:
        p = get_provider("gemini-grounding")

        mock_creds = {
            "api_key": "mock_token_123",
            "base_url": "https://example-daily.googleapis.com/v1internal",
            "project_id": "test-project",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"

        with patch("hermes_cli.auth.resolve_gemini_oauth_runtime_credentials", return_value=mock_creds), \
             patch("httpx.Client.post", return_value=mock_resp):

            res = p.search("test query")
            assert res["success"] is False
            assert "Cloud Code PA error" in res["error"]
