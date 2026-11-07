"""Local-first SearXNG fallback tests; all network calls are mocked."""

import httpx
from unittest.mock import patch

from danica.config import CONFIG
from danica.tools.websearch import SearchError, _searxng


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


old_primary = CONFIG["web"].get("search_url")
old_fallback = CONFIG["web"].get("search_fallback_url")
CONFIG["web"]["search_url"] = "http://localhost:8888/"
CONFIG["web"]["search_fallback_url"] = "https://search.utilibre.org/"


class FakeResponse:
    status_code = 200

    @staticmethod
    def json():
        return {"results": [{"title": "test"}]}


try:
    with patch("danica.tools.websearch.httpx.get",
               side_effect=[httpx.ConnectError("local SearXNG is down"), FakeResponse()]) as get:
        result = _searxng("safe test query")
    urls = [call.args[0] for call in get.call_args_list]
    check("hosted fallback is used only after local connection failure",
          result == [{"title": "test"}]
          and urls == ["http://localhost:8888/search", "https://search.utilibre.org/search"])
    check("the same query is sent to the fallback",
          all(call.kwargs["params"]["q"] == "safe test query" for call in get.call_args_list))

    with patch("danica.tools.websearch.httpx.get", return_value=FakeResponse()) as get:
        result = _searxng("local query")
    check("working local SearXNG is preferred and skips fallback",
          result == [{"title": "test"}] and get.call_count == 1
          and get.call_args.args[0] == "http://localhost:8888/search")

    response_403 = FakeResponse()
    response_403.status_code = 403
    with patch("danica.tools.websearch.httpx.get", return_value=response_403) as get:
        try:
            _searxng("format configuration query")
            refused = False
        except SearchError as exc:
            refused = "refused JSON output" in str(exc)
    check("HTTP configuration errors are reported without silently switching hosts",
          refused and get.call_count == 1)

    with patch("danica.tools.websearch.httpx.get",
               side_effect=[httpx.ConnectError("local down"),
                            httpx.ConnectError("fallback down")]):
        try:
            _searxng("safe test query")
            both_failed = False
        except SearchError as exc:
            both_failed = "primary search server" in str(exc) and "fallback" in str(exc)
    check("failure of both servers is reported explicitly", both_failed)
finally:
    if old_primary is None:
        CONFIG["web"].pop("search_url", None)
    else:
        CONFIG["web"]["search_url"] = old_primary
    if old_fallback is None:
        CONFIG["web"].pop("search_fallback_url", None)
    else:
        CONFIG["web"]["search_fallback_url"] = old_fallback

print("\nAll good.")
