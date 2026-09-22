"""Parser tests: text/markdown/links/metadata/needs_renderer + robustness."""

from openclaw_fetch.parse import (
    classify_content_type,
    extract_metadata,
    needs_renderer,
    parse_document,
)


SAMPLE = """\
<!doctype html>
<html lang="en">
<head>
  <title>Doc Title</title>
  <meta name="description" content="the description">
  <meta property="og:title" content="OG Doc">
  <meta property="og:description" content="og desc">
</head>
<body>
  <h1>Top Heading</h1>
  <h2>Sub Heading</h2>
  <p>Hello <a href="/link">world</a>.</p>
  <p>Second para.</p>
  <ul><li>Item A</li><li>Item B</li></ul>
</body>
</html>
"""


def test_text():
    doc = parse_document(SAMPLE, content_type="text/html", final_url="http://x.test/")
    assert doc["title"] == "Doc Title"
    assert "Top Heading" in doc["text"]
    assert "Second para." in doc["text"]
    assert "Item A" in doc["text"]


def test_markdown():
    doc = parse_document(SAMPLE, content_type="text/html", final_url="http://x.test/")
    assert "# Top Heading" in doc["markdown"]
    assert "## Sub Heading" in doc["markdown"]
    assert "[world](http://x.test/link)" in doc["markdown"]
    assert "- Item A" in doc["markdown"]


def test_links():
    doc = parse_document(SAMPLE, content_type="text/html", final_url="http://x.test/")
    assert any(l["url"] == "http://x.test/link" for l in doc["links"])


def test_metadata():
    meta = extract_metadata(SAMPLE, base_url="http://x.test/")
    assert meta["description"] == "the description"
    assert meta["og:title"] == "OG Doc"
    assert meta["og:description"] == "og desc"


def test_needs_renderer_marker():
    assert needs_renderer('<script>window.__NEXT_DATA__={}</script><div></div>', 5) is True


def test_needs_renderer_big_js_no_text():
    big = "<div>" + "<script>%s</script>" % ("x" * 70000) + "<p></p></div>"
    assert needs_renderer(big, 0) is True


def test_needs_renderer_normal_page():
    assert needs_renderer("<p>Hello world this is content.</p>", 40) is False


def test_json_kind():
    doc = parse_document('{"a": 1}', content_type="application/json")
    assert doc["kind"] == "json"
    assert '"a": 1' in doc["text"]


def test_fuzz_random_bytes_never_raises():
    import os
    import struct

    rng = os.urandom
    for _ in range(200):
        size = struct.unpack("I", rng(4))[0] % 4000
        data = rng(size)
        try:
            text = data.decode("latin-1")
        except Exception:
            continue
        parse_document(text, content_type="text/html", final_url="http://x.test/")
        parse_document(text, content_type="text/html; charset=utf-8")


def test_classify_content_type():
    assert classify_content_type("text/html; charset=utf-8") == "html"
    assert classify_content_type("application/json") == "json"
    assert classify_content_type("application/ld+json") == "json"
    assert classify_content_type("application/pdf") == "pdf"
    assert classify_content_type("application/rss+xml") == "rss"
    assert classify_content_type("image/png") == "image"
    assert classify_content_type("text/plain") == "text"


def test_nav_text_suppressed_but_links_kept():
    html = (
        "<body><nav><a href='/menu'>Menu</a></nav>"
        "<h1>Real</h1><p>Content here.</p></body>"
    )
    doc = parse_document(html, content_type="text/html", final_url="http://x.test/")
    assert "Menu" not in doc["text"]
    assert any(l["url"] == "http://x.test/menu" for l in doc["links"])
    assert "Content here." in doc["text"]


def test_heading_outline():
    doc = parse_document(SAMPLE, content_type="text/html", final_url="http://x.test/")
    assert [h["text"] for h in doc["headings"]] == ["Top Heading", "Sub Heading"]
    assert [h["tag"] for h in doc["headings"]] == ["h1", "h2"]