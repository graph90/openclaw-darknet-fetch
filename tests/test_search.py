"""Search backend tests: parsers, auto fallback order and blocked engines.

Every test here runs offline: HTML fixtures are inline and network access goes
through a stub fetcher.
"""

import pytest

from openclaw_fetch.search import (
    AUTO_BACKENDS,
    _backend_urls,
    _looks_blocked,
    _parse_marginalia,
    _parse_tor66,
    search,
)
from openclaw_fetch.result import Result


TOR66_ONION = "http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion/search?q=x"

TOR66_ONION_HTML = """
<div class="result-block">
  <div class="title">
    <a data-category="sponsored-text" href="/ads/click?s=eyJpdiI6abc">Ad Title</a>
  </div>
  <div class="link"><span class="label-ad">Ad</span> http://adtargetaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion</div>
  <div class="desc">buy things</div>
</div>
<div class="result-block">
  <div class="title">
    <a data-category="text-result" href="/r?s=eyJpdiI6eHl6">Kernel &amp; Security</a>
  </div>
  <div class="link">http://kernelexampleeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee.onion/tag/security</div>
  <div class="desc">IT security, <strong>Linux</strong>, mail server hardening</div>
</div>
"""

TOR66_CLEARNET_HTML = """
<div class="result-block">
  <div class="title">
    <a data-category="text-result" href="http://directaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion/post/1">Direct Hit</a>
  </div>
  <p id="desc">A snippet that goes on Log In Public Pseudonymous user info</p>
</div>
"""

MARGINALIA_HTML = """
<h2 class="text-md sm:text-xl"><a href="https://en.wikipedia.org/wiki/Linux_kernel" rel="noopener">Linux kernel</a></h2>
<p class="mt-2 text-sm">Linux kernel version history. Version history of the Linux kernel.</p>
<h2 class="text-md"><a href="https://example.org/other">Other result</a></h2>
<p class="mt-2 text-sm">Another snippet.</p>
"""

DDG_CHALLENGE = (
    "<html><body>Unfortunately, bots use DuckDuckGo too. Please complete the "
    "following challenge to confirm this search was made by a human.</body></html>"
)

MARGINALIA_THROTTLE = (
    "<html><body><h1>Wait A Moment</h1><p>The search engine is currently "
    "seeing a lot of fairly aggressive bot activity.</p></body></html>"
)


class StubFetcher:
    """Serve canned bodies per URL fragment; records every request."""

    def __init__(self, routes, default=None):
        self.routes = routes
        self.default = default
        self.requested = []

    def fetch(self, url, keep_raw=False, **kwargs):
        self.requested.append(url)
        for needle, body in self.routes.items():
            if needle in url:
                return Result(ok=True, error="", error_code=None, network="TEST",
                              status=200, _raw_body=body, text=body)
        if self.default is not None:
            return Result(ok=True, error="", error_code=None, network="TEST",
                          status=200, _raw_body=self.default, text=self.default)
        return Result(ok=False, error="no route for %s" % url, error_code="CONNECTION",
                      network="TEST", status=None)


class TestTor66Parser:
    def test_prefers_plaintext_link_over_ad_hop(self):
        items = _parse_tor66(TOR66_ONION_HTML, TOR66_ONION)
        assert len(items) == 2
        assert items[1]["url"] == (
            "http://kernelexampleeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee.onion"
            "/tag/security"
        )
        assert items[1]["target_tld"] == "onion"
        assert items[1]["title"] == "Kernel & Security"
        assert "Linux" in items[1]["snippet"]

    def test_flags_sponsored_blocks(self):
        items = _parse_tor66(TOR66_ONION_HTML, TOR66_ONION)
        assert items[0]["sponsored"] is True
        assert items[1]["sponsored"] is False

    def test_clearnet_markup_and_snippet_cleanup(self):
        items = _parse_tor66(TOR66_CLEARNET_HTML, "https://tor66.org/search?q=x")
        assert items[0]["url"].startswith("http://direct")
        assert items[0]["sponsored"] is False
        assert items[0]["snippet"] == "A snippet that goes on"

    def test_dedupes_repeated_targets(self):
        html = TOR66_ONION_HTML + TOR66_ONION_HTML
        items = _parse_tor66(html, TOR66_ONION)
        assert len(items) == 2


class TestMarginaliaParser:
    def test_parses_titles_and_snippets(self):
        items = _parse_marginalia(MARGINALIA_HTML, "https://search.marginalia.nu/search")
        assert [i["url"] for i in items] == [
            "https://en.wikipedia.org/wiki/Linux_kernel",
            "https://example.org/other",
        ]
        assert items[0]["title"] == "Linux kernel"
        assert "version history" in items[0]["snippet"]
        assert items[0]["sponsored"] is False


class TestBackendSelection:
    def test_auto_tor_prefers_onion_then_mirror(self):
        urls = _backend_urls("tor66", "q", "tor")
        assert urls[0].startswith("http://") and ".onion" in urls[0]
        assert urls[1].startswith("https://tor66.org/")

    def test_auto_keeps_onion_for_auto_network(self):
        urls = _backend_urls("tor66", "q", "auto")
        assert ".onion" in urls[0]

    def test_explicit_search_url_overrides(self):
        urls = _backend_urls("tor66", "q", "tor", search_url="https://mirror.example/s")
        assert urls == ["https://mirror.example/s?q=q"]

    def test_auto_order_tables(self):
        assert AUTO_BACKENDS["tor"][0] == "tor66"
        assert AUTO_BACKENDS["normal"][0] == "marginalia"
        assert "tor66" in AUTO_BACKENDS["auto"]

    def test_search_falls_back_to_next_backend(self):
        stub = StubFetcher({"tor66": "<html>no results here</html>",
                            "marginalia": MARGINALIA_HTML})
        res = search("kernel", network="auto", max_results=5, fetcher=stub)
        assert res.ok
        assert res["backend"] == "marginalia"
        assert res["backends_tried"] == ["tor66", "marginalia"]
        assert res.total_results == 2
        assert [i["rank"] for i in res.results] == [1, 2]

    def test_search_reports_blocked_backends(self):
        stub = StubFetcher({"tor66": MARGINALIA_THROTTLE, "marginalia": DDG_CHALLENGE})
        res = search("kernel", network="auto", fetcher=stub)
        assert not res.ok
        assert res["error_code"] == "REQUEST"
        assert res["blocked_backends"] == ["tor66", "marginalia"]
        assert "blocked" in res["error"]

    def test_search_drops_sponsored_by_default(self):
        stub = StubFetcher({"tor66": TOR66_ONION_HTML})
        res = search("x", network="tor", fetcher=stub)
        assert res.ok
        assert all(not i["sponsored"] for i in res.results)
        assert len(res.results) == 1

    def test_search_can_keep_sponsored(self):
        stub = StubFetcher({"tor66": TOR66_ONION_HTML})
        res = search("x", network="tor", fetcher=stub, include_sponsored=True)
        assert len(res.results) == 2

    def test_search_unknown_backend(self):
        res = search("x", backend="nope", network="normal")
        assert res["error_code"] == "USAGE"

    def test_searxng_auto_is_skipped_without_url(self):
        stub = StubFetcher({"marginalia": MARGINALIA_HTML})
        res = search("x", network="normal", fetcher=stub)
        assert res["backend"] == "marginalia"
        assert "searxng" not in res["backends_tried"]

    def test_looks_blocked_markers(self):
        assert _looks_blocked(MARGINALIA_THROTTLE)
        assert _looks_blocked(DDG_CHALLENGE)
        assert not _looks_blocked(MARGINALIA_HTML)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))


class TestEmptyVersusFailed:
    def test_all_engines_failed_is_not_ok(self):
        stub = StubFetcher({})  # every fetch fails
        res = search("x", network="normal", fetcher=stub)
        assert res.ok is False
        assert res.error_code
        assert res["backends_failed"]

    def test_engines_answered_with_zero_hits_is_ok(self):
        stub = StubFetcher({"marginalia": "<html><body>nothing here</body></html>",
                            "ddg": "<html><body>nothing here</body></html>"})
        res = search("x", network="normal", fetcher=stub)
        assert res.ok is True
        assert res.total_results == 0
        assert res["backends_failed"] == []

    def test_one_engine_empty_one_failed_still_ok(self):
        stub = StubFetcher({"marginalia": "<html><body>nothing</body></html>"})
        res = search("x", network="normal", fetcher=stub)
        assert res.ok is True
        assert res.total_results == 0

    def test_results_carry_failed_backend_list(self):
        stub = StubFetcher({"tor66": MARGINALIA_THROTTLE})
        res = search("x", network="tor", fetcher=stub)
        assert res.ok is False
        assert "tor66" in res["blocked_backends"]
