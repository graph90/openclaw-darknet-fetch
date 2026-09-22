"""Content extraction: HTML -> text / markdown / links / metadata,
plus content-kind dispatch for JSON / PDF / plain text / images / feeds.
"""

import json
import re
import shutil
from html.parser import HTMLParser
from urllib.parse import urljoin

SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
NAV_TAGS = {"nav", "footer", "aside"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {
    "p", "li", "pre", "blockquote", "dt", "dd", "td", "th",
    "figcaption", "caption", "address",
} | HEADING_TAGS

# SPA markers that hint the page needs a renderer (or embedded-JSON reader).
SPA_MARKERS = (
    "__NEXT_DATA__", "__NUXT__", "__INITIAL_STATE__", "window.store",
    "ng-app", "data-server-rendered", "application/json",
)


def _collapse(text):
    return re.sub(r"\s+", " ", text).strip()


def _normalize_space(text):
    """Collapse internal whitespace runs, preserving segment boundaries."""
    return re.sub(r"\s+", " ", text)


class _Block:
    __slots__ = ("kind", "tokens")

    def __init__(self, kind):
        self.kind = kind
        # tokens: ("text", s) or ("link", label, url)
        self.tokens = []

    def _plain(self):
        for token in self.tokens:
            if token[0] == "text":
                yield token[1]
            else:
                yield token[1]  # link label flows into plain text

    def text(self):
        return _collapse("".join(self._plain()))

    def markdown(self):
        md = []
        for token in self.tokens:
            if token[0] == "text":
                md.append(token[1])
            else:
                md.append("[%s](%s)" % (token[1], token[2]))
        return _collapse("".join(md))

    def has_text(self):
        return bool(self.text())


def _is_plain_link_artifact(href):
    """Filter icon/anchors, hashes, mailto noise for quality links."""
    if not href:
        return True
    if href.startswith("#"):
        return True
    if href.startswith("mailto:"):
        return True
    if href.startswith("javascript:"):
        return True
    return False


class HTMLDocumentParser(HTMLParser):
    """Extract title, links, plain text blocks, markdown, and metadata."""

    def __init__(self, base_url="", source_len=0):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.source_len = source_len
        self.title = ""
        self.in_title = False
        self.skip_depth = 0
        self.nav_depth = 0
        self.blocks = []
        self.current = None
        self.links = []          # list of dict {url, text, title}
        self.link_stack = []     # dict {href, title, label_parts}
        self.lang = ""

    # -- helpers ----------------------------------------------------------
    def _in_skip(self):
        return self.skip_depth > 0

    def _in_nav(self):
        return self.nav_depth > 0

    def _start_block(self, kind):
        self.current = _Block(kind)

    def _flush_block(self):
        if self.current is not None:
            if self.current.has_text():
                self.blocks.append(self.current)
        self.current = None

    # -- parser overrides -------------------------------------------------
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            self.lang = (attrs.get("lang") or "").strip()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if tag in NAV_TAGS:
            self.nav_depth += 1
        if self._in_skip():
            return
        if tag == "title":
            self.in_title = True
        elif tag == "a":
            self.link_stack.append(
                {
                    "href": attrs.get("href"),
                    "title": attrs.get("title"),
                    "aria": attrs.get("aria-label"),
                    "label_parts": [],
                }
            )
        elif tag in BLOCK_TAGS:
            self._flush_block()
            self._start_block(tag)
        elif tag == "img":
            alt = attrs.get("alt") or attrs.get("title")
            if alt:
                alt = _collapse(alt)
                if self.link_stack:
                    self.link_stack[-1]["label_parts"].append(alt)
                elif not self._in_nav():
                    self._emit_text(" " + alt + " ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag == "a":
            self._push_link()

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self._in_skip():
            return
        if tag == "title":
            self.in_title = False
        elif tag in NAV_TAGS:
            if self.nav_depth:
                self.nav_depth -= 1
        elif tag == "a":
            self._push_link()
        elif tag in BLOCK_TAGS:
            self._flush_block()

    def handle_data(self, data):
        if self._in_skip() or not data:
            return
        if self.in_title:
            self.title += data
            return
        self._emit_text(data)

    # -- internals --------------------------------------------------------
    def _emit_text(self, text):
        text = _normalize_space(text)
        if not text.strip():
            return
        if self.link_stack:
            self.link_stack[-1]["label_parts"].append(text)
            if not self._in_nav() and self.current is None:
                self._start_block("p")
            return
        if self._in_nav():
            return
        if self.current is None:
            self._start_block("p")
        self.current.tokens.append(("text", text))

    def _push_link(self):
        stack = self.link_stack.pop() if self.link_stack else None
        if stack is None:
            return
        href = stack["href"]
        title = stack["title"]
        if _is_plain_link_artifact(href):
            return
        label = _collapse("".join(stack["label_parts"]) or stack["aria"] or title or "")
        if not label:
            return
        absolute = urljoin(self.base_url, href)
        self.links.append({"url": absolute, "text": label, "title": title})
        if not self._in_nav():
            if self.current is None:
                self._start_block("p")
            self.current.tokens.append(("link", label, absolute))

    # -- public API -------------------------------------------------------
    def get_text(self):
        lines = [block.text() for block in self.blocks]
        return re.sub(r"\n{3,}", "\n\n", "\n\n".join(lines)).strip()

    def get_markdown(self):
        out = []
        for block in self.blocks:
            body = block.markdown()
            if not body:
                continue
            kind = block.kind
            if kind in HEADING_TAGS:
                out.append("%s %s" % ("#" * int(kind[1]), body))
            elif kind == "li":
                out.append("- " + body)
            elif kind == "pre":
                out.append("```\n%s\n```" % body)
            elif kind == "blockquote":
                out.append("> " + body)
            else:
                out.append(body)
        return "\n\n".join(out).strip() + "\n"

    def get_title(self):
        return _collapse(self.title)

    def get_headings(self):
        seen = []
        for block in self.blocks:
            if block.kind in HEADING_TAGS:
                seen.append({"tag": block.kind, "text": block.text()})
        return seen

    def get_links(self):
        """Deduplicate by absolute URL (keep first occurrence)."""
        seen = {}
        for link in self.links:
            seen.setdefault(link["url"], link)
        return list(seen.values())

    def finish(self):
        """Flush any trailing block left open by the final unclosed tag."""
        self._flush_block()


def extract_metadata(html, base_url=""):
    """Pull og/meta/canonical/ld+json metadata with a light regex pass."""
    meta = {}
    for pattern, key in (
        (r'<meta[^>]+property=["\']og:title["\'][^>]*?content=["\']([^"\']+)', "og:title"),
        (r'<meta[^>]+property=["\']og:description["\'][^>]*?content=["\']([^"\']+)', "og:description"),
        (r'<meta[^>]+property=["\']og:image["\'][^>]*?content=["\']([^"\']+)', "og:image"),
        (r'<meta[^>]+name=["\']description["\'][^>]*?content=["\']([^"\']+)', "description"),
        (r'<meta[^>]+name=["\']twitter:card["\'][^>]*?content=["\']([^"\']+)', "twitter:card"),
        (r'<link[^>]+rel=["\']canonical["\'][^>]*?href=["\']([^"\']+)', "canonical"),
    ):
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            value = _collapse(match.group(1))
            if value:
                meta[key] = value
    if base_url and meta.get("canonical"):
        meta["canonical"] = urljoin(base_url, meta["canonical"])
    ld = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
            ld.append(parsed)
        except ValueError:
            ld.append({"raw": raw})
    if ld:
        meta["ld_json"] = ld
    return meta


def needs_renderer(html, visible_text_len):
    """Heuristic: page is likely JS-rendered, static parse is unproductive."""
    if not html:
        return False
    raw_len = len(html)
    low_text = visible_text_len < 200
    if any(marker in html for marker in SPA_MARKERS):
        return True
    if low_text and raw_len > 60000:
        return True
    if low_text and raw_len > 20000:
        return True
    return False


def classify_content_type(content_type):
    """Map a raw Content-Type header to a stable ``content_kind``."""
    ct = (content_type or "").lower()
    if "text/html" in ct:
        return "html"
    if "application/json" in ct or ct.endswith("+json"):
        return "json"
    if "application/pdf" in ct:
        return "pdf"
    if "application/rss+xml" in ct or "application/atom+xml" in ct:
        return "rss"
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("text/") or ct in ("application/xml", "text/xml"):
        return "text"
    return "unknown"


def parse_document(text, *, content_type="", requested_url="", final_url="",
                   source_len=0, max_links=50, kind=None, raw_body=None):
    """Parse body text into a normalized document dict.

    ``text`` is the already-decoded body; ``raw_body`` (bytes) is optional and
    lets binary kinds (PDF) extract from the original bytes. Never raises:
    random bytes or malformed HTML are survivable (fuzz contract from plan
    section 12).
    """
    if kind is None:
        kind = classify_content_type(content_type)
    result = {
        "kind": kind,
        "title": "",
        "text": "",
        "markdown": "",
        "links": [],
        "metadata": {},
        "needs_renderer": False,
        "headings": [],
        "items": [],
        "payload_extracted": False,
    }
    if kind == "json":
        pretty = _try_json_pretty(text)
        result["text"] = pretty or text
        result["markdown"] = ""
        return result
    if kind == "pdf":
        result["text"], pdf_method = extract_pdf_text(raw_body)
        if pdf_method:
            result["markdown"] = result["text"]
            result["metadata"]["pdf_extraction"] = pdf_method
        else:
            result["text"] = "[pdf content: %s bytes (extraction needs pypdf or the pdftotext binary)]" % source_len
        return result
    if kind == "image":
        result["text"] = "[image content: %s bytes (use --save-raw to download)]" % source_len
        return result
    if kind == "rss":
        feed = parse_feed(text)
        result["title"] = feed.get("title", "")
        result["items"] = feed.get("items", [])
        result["text"] = feed.get("text", "")
        result["markdown"] = feed.get("text", "")
        result["links"] = [
            {"url": item["link"], "text": item.get("title", ""), "title": item.get("title", "")}
            for item in result["items"][:max_links]
            if item.get("link")
        ]
        result["link_count"] = len(result["links"])
        result["metadata"] = feed.get("metadata", {})
        return result
    if "html" not in kind:
        result["text"] = text
        return result
    parser = HTMLDocumentParser(base_url=final_url or requested_url, source_len=source_len)
    try:
        parser.feed(text)
        parser.close()
        parser.finish()
    except Exception:
        result["text"] = text
        return result
    result["title"] = parser.get_title()
    result["text"] = parser.get_text()
    result["markdown"] = parser.get_markdown()
    result["headings"] = parser.get_headings()
    result["links"] = parser.get_links()[:max_links]
    result["metadata"] = extract_metadata(text, base_url=final_url or requested_url)
    if not result["title"]:
        result["title"] = result["metadata"].get("og:title", "")
    result["needs_renderer"] = needs_renderer(text, len(result["text"]))
    payload = extract_embedded_json(text)
    if payload and result["needs_renderer"]:
        result["text"] = payload
        result["markdown"] = payload
        result["payload_extracted"] = True
    return result


def extract_pdf_text(raw_body):
    """Extract text from PDF bytes. Returns ``(text, method)`` where ``method``
    is ``"pypdf"``, ``"pdftotext"``, or ``None`` when neither is usable."""
    if raw_body is None:
        return "", None
    method = _pdf_via_pypdf(raw_body)
    if method:
        return method
    method = _pdf_via_pdftotext(raw_body)
    if method:
        return method
    return "", None


def _pdf_via_pypdf(raw_body):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        import io

        reader = PdfReader(io.BytesIO(raw_body))
        pages = []
        for page in reader.pages[:50]:
            pages.append(page.extract_text() or "")
        text = "\n\n".join(pages).strip()
        if text:
            return text, "pypdf"
    except Exception:
        pass
    return None


def _pdf_via_pdftotext(raw_body):
    import subprocess
    import tempfile

    if shutil.which("pdftotext") is None:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(raw_body)
            tmp.flush()
            proc = subprocess.run(
                ["pdftotext", "-layout", tmp.name, "-"],
                capture_output=True,
                timeout=30,
            )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.decode("utf-8", errors="replace").strip(), "pdftotext"
    except Exception:
        pass
    return None


def parse_feed(text):
    """Parse RSS 2.0 / Atom / RDF XML into a feed dict.

    Returns ``{title, items: [{title, link, guid, date, summary, tags}],
    text, metadata}``. Never raises on malformed XML.
    """
    import xml.etree.ElementTree as ET

    feed = {"title": "", "items": [], "text": "", "metadata": {}}
    try:
        root = ET.fromstring(text)
    except Exception:
        feed["text"] = _collapse(text)
        return feed

    def local(tag):
        return tag.rsplit("}", 1)[-1].lower()

    def child_text(el, name):
        for node in el.iter():
            if local(node.tag) == name and node.text:
                return node.text.strip()
        return ""

    def child_all(el, name):
        out = []
        for node in el.iter():
            if local(node.tag) == name and node.text and node.text.strip():
                out.append(node.text.strip())
        return out

    feed["title"] = child_text(root, "title")
    channel = root if local(root.tag) in ("rss", "feed", "rdf") else root
    item_nodes = [
        node for node in channel.iter() if local(node.tag) in ("item", "entry")
    ]
    seen = set()
    for node in item_nodes:
        link = child_text(node, "link")
        full_link = ""
        for link_node in node.iter():
            if local(link_node.tag) == "link":
                href = link_node.attrib.get("href")
                if href:
                    full_link = href
                elif link_node.text:
                    full_link = link_node.text.strip()
                if full_link:
                    break
        link = full_link or link
        title = child_text(node, "title")
        guid = child_text(node, "guid") or link
        if not (link or guid):
            continue
        if guid in seen:
            continue
        seen.add(guid)
        item = {
            "title": title,
            "link": _collapse(link) if link else "",
            "guid": guid,
            "date": child_text(node, "pubdate") or child_text(node, "published") or child_text(node, "updated"),
            "summary": _collapse(child_text(node, "description") or child_text(node, "summary") or child_text(node, "content")),
            "tags": child_all(node, "category")[:10],
        }
        feed["items"].append(item)
    lines = []
    for item in feed["items"]:
        title = item["title"] or item["link"]
        lines.append("- %s" % title)
        if item["summary"]:
            lines.append("  %s" % item["summary"][:400])
    feed["text"] = "\n".join(lines)
    feed["metadata"]["feed_title"] = feed["title"]
    feed["metadata"]["feed_type"] = local(root.tag)
    feed["metadata"]["item_count"] = len(feed["items"])
    return feed


def extract_embedded_json(html):
    """Pull structured content out of SPA payloads embedded in the page.

    Recognizes ``<script type="application/json">{...}</script>`` (Next.js
    ``__NEXT_DATA__`` and friends) and JavaScript assignments like
    ``window.__INITIAL_STATE__ = {...}`` / ``window.store = {...}``.
    Returns pretty-printed JSON text (or ``""`` when nothing useful is found).
    """
    if not html:
        return ""
    candidates = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        candidates.append(match.group(1).strip())
    for block in re.finditer(
        r"window\.(__NEXT_DATA__|__INITIAL_STATE__|store|__NUXT__)\s*=\s*(\{.*?\});",
        html,
        re.DOTALL,
    ):
        candidates.append(_balanced_json(block.group(2)))
    for cand in candidates:
        if not cand:
            continue
        pretty = _try_json_pretty(cand)
        if pretty:
            return pretty
    return ""


def _balanced_json(raw):
    """Trim a JS object literal to its matching close brace (best effort)."""
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(raw):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[: i + 1]
    return raw


def _try_json_pretty(text):
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        return None