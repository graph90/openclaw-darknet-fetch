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

# "Please wait" shells served by darknet front-ends before the real page.
INTERSTITIAL_MARKERS = (
    "please wait", "proszę czekać", "bitte warten", "wait a moment",
    "wait for a moment", "please be patient", "one moment please",
)

GATED_MARKERS = (
    "members only", "member only", "you must be logged in",
    "sign in to continue", "please log in", "join to read",
)

CHALLENGE_MARKERS = (
    "captcha", "are you a robot", "checking your browser",
    "enable javascript and cookies", "prove you are human",
)


def page_label(html, visible_text):
    """Classify a page for skip logic: interstitial/gated/challenge/None.

    ``html`` is the raw source (for marker search), ``visible_text`` the
    extracted text. Interstitial requires thin visible text, so a real article
    that merely mentions "please wait" is not mislabelled.
    """
    low_html = (html or "").lower()
    low_text = (visible_text or "").lower()
    if len(visible_text or "") < 200 and any(m in low_html or m in low_text for m in INTERSTITIAL_MARKERS):
        return "interstitial"
    if any(m in low_text or m in low_html for m in GATED_MARKERS):
        return "gated"
    if any(m in low_text or m in low_html for m in CHALLENGE_MARKERS):
        return "challenge"
    return None


def looks_like_index(text_len, link_count):
    """Heuristic: many links, little prose -> an index/directory page."""
    return link_count >= 8 and text_len < 800


def index_rows(links):
    """Structured rows for an index page: [{title,url,kind}]."""
    return [{"title": l.get("text") or "", "url": l.get("url"), "kind": l.get("kind") or "page"}
            for l in links or []]


# -- minimal CSS selector extraction (v0.5) -----------------------------------
_CSS_IDENT = re.compile(r"[A-Za-z0-9_-]+")
_CSS_ATTR = re.compile(r"\[\s*([A-Za-z0-9_-]+)\s*(?:([~|^$*]?=)\s*([^\]]+)\s*)?\]")


class _CssNode:
    __slots__ = ("tag", "attrs", "children", "text_parts", "parent")

    def __init__(self, tag, attrs, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.text_parts = []
        self.parent = parent

    def text(self):
        out = list(self.text_parts)
        for child in self.children:
            out.append(child.text())
        return _collapse(" ".join(out))


class _CssParser(HTMLParser):
    void = {"br", "hr", "img", "meta", "link", "input"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _CssNode("[root]", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _CssNode(tag.lower(), {k.lower(): (v or "") for k, v in dict(attrs).items()},
                        parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.lower() not in self.void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data and data.strip():
            self.stack[-1].text_parts.append(data)


def _css_parse_simple(selector):
    """Parse one simple selector into (tag, id, classes, [(attr, op, value)])."""
    m = re.match(r"\s*([A-Za-z][A-Za-z0-9]*)?", selector)
    tag = m.group(1).lower() if m and m.group(1) else None
    rest = selector[m.end():] if m else selector
    id_ = None
    classes = []
    attrs = []
    pos = 0
    # strip tag (already consumed); now parse #id, .class, [attr...] in order
    s = rest.strip()
    i = 0
    while i < len(s):
        if s[i] == ".":
            m2 = _CSS_IDENT.match(s, i + 1)
            if not m2:
                raise ValueError("bad class in selector %r" % selector)
            classes.append(m2.group(0))
            i = m2.end()
        elif s[i] == "#":
            m2 = _CSS_IDENT.match(s, i + 1)
            if not m2:
                raise ValueError("bad id in selector %r" % selector)
            id_ = m2.group(0)
            i = m2.end()
        elif s[i] == "[":
            m2 = _CSS_ATTR.match(s, i)
            if not m2:
                raise ValueError("bad attribute in selector %r" % selector)
            name = m2.group(1).lower()
            op = m2.group(2) or ""
            value = (m2.group(3) or "").strip().strip('"').strip("'")
            attrs.append((name, op, value))
            i = m2.end()
        else:
            raise ValueError("unsupported selector %r" % selector)
    if tag is None and id_ is None and not classes and not attrs:
        raise ValueError("empty selector")
    return tag, id_, classes, attrs


def _css_match(node, simple):
    tag, id_, classes, attrs = simple
    if tag and node.tag != tag:
        return False
    if id_ and node.attrs.get("id") != id_:
        return False
    node_classes = (node.attrs.get("class") or "").split()
    for cls in classes:
        if cls not in node_classes:
            return False
    for name, op, value in attrs:
        actual = node.attrs.get(name)
        if actual is None:
            return False
        if op == "=" and actual != value:
            return False
        if op == "^=" and not actual.startswith(value):
            return False
        if op == "$=" and not actual.endswith(value):
            return False
        if op == "*=" and value not in actual:
            return False
        if op == "~=" and value not in actual.split():
            return False
        if op == "|=" and not (actual == value or actual.startswith(value + "-")):
            return False
    return True


def _css_descendants(node, simple, out):
    for child in node.children:
        if _css_match(child, simple):
            out.append(child)
        _css_descendants(child, simple, out)


def extract_selector(html, selector, base_url="", max_items=100):
    """Extract matches for a CSS selector subset from ``html``.

    Supports ``tag``, ``.class``, ``#id``, ``[attr]`` / ``[attr^=]`` /
    ``[attr$=]`` / ``[attr*=]`` / ``[attr~=]`` / ``[attr|=]`` and space-separated
    descendant chains. Returns ``[{tag, text, attrs}]``; hrefs are resolved
    against ``base_url``. Raises ``ValueError`` on bad selectors.
    """
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError("empty CSS selector")
    parts = selector.strip().split()
    simples = [_css_parse_simple(p) for p in parts]
    parser = _CssParser()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass
    # Walk the descendant chain step by step.
    current = [parser.root]
    for simple in simples:
        found = []
        for node in current:
            _css_descendants(node, simple, found)
        current = found
        if not current:
            return []
    out = []
    for node in current:
        attrs = dict(node.attrs)
        if "href" in attrs and base_url:
            attrs["href"] = urljoin(base_url, attrs["href"])
        out.append({"tag": node.tag, "text": node.text(), "attrs": attrs})
        if len(out) >= max_items:
            break
    return out


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


# File-extension sets for link classification (Task 4). File kinds win over
# network kinds so ``http://x.onion/paper.pdf`` reads as a document.
_DOCUMENT_EXTS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".epub", ".txt", ".md", ".csv",
})
_ARCHIVE_EXTS = frozenset({
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
})
_DOWNLOAD_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
    ".mp3", ".mp4", ".avi", ".mov", ".mkv", ".exe", ".deb",
    ".apk", ".iso", ".dmg", ".torrent", ".bin",
})


def classify_link(url):
    """Return ``(kind, target_tld)`` for a link URL.

    ``target_tld`` is ``onion``/``i2p``/``clearnet``; ``kind`` is one of
    ``page``, ``onion``, ``i2p``, ``mail``, ``magnet``, ``document``,
    ``archive``, ``download``. File kinds win over network kinds.
    """
    from urllib.parse import urlsplit

    if not isinstance(url, str) or not url:
        return "page", "clearnet"
    low = url.strip().lower()
    if low.startswith("mailto:"):
        return "mail", "clearnet"
    if low.startswith("magnet:"):
        return "magnet", "clearnet"
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path or ""
    except ValueError:
        return "page", "clearnet"
    if host.endswith(".onion"):
        tld = "onion"
    elif host.endswith(".i2p"):
        tld = "i2p"
    else:
        tld = "clearnet"
    ext = ""
    last = path.rsplit("/", 1)[-1].lower()
    if "." in last:
        ext = "." + last.rsplit(".", 1)[-1]
    if ext in _DOCUMENT_EXTS:
        return "document", tld
    if ext in _ARCHIVE_EXTS:
        return "archive", tld
    if ext in _DOWNLOAD_EXTS:
        return "download", tld
    if tld == "onion":
        return "onion", tld
    if tld == "i2p":
        return "i2p", tld
    return "page", tld


def enrich_link(link):
    """Add ``kind``/``target_tld`` to a link dict in place (returns it)."""
    url = link.get("url") or ""
    kind, tld = classify_link(url)
    link["kind"] = kind
    link["target_tld"] = tld
    return link


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
        self.links.append(enrich_link({"url": absolute, "text": label, "title": title}))
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
        feed = parse_feed(text, base_url=final_url or requested_url)
        result["title"] = feed.get("title", "")
        result["items"] = feed.get("items", [])
        result["text"] = feed.get("text", "")
        result["markdown"] = feed.get("text", "")
        result["links"] = [
            enrich_link({"url": item["link"], "text": item.get("title", ""), "title": item.get("title", "")})
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
        flattened = extract_flattened_payload(text)
        if flattened:
            result["text"] = flattened
            result["markdown"] = flattened
            result["metadata"]["payload_format"] = "flattened"
        else:
            result["text"] = payload
            result["markdown"] = payload
            result["metadata"]["payload_format"] = "json"
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


def parse_feed(text, base_url=""):
    """Parse RSS 2.0 / Atom / RDF XML into a feed dict.

    Returns ``{title, items: [{title, link, guid, date, summary, tags}],
    text, metadata}``. Never raises on malformed XML. ``base_url`` resolves
    relative item links (feeds are full of ``/path`` links) and relative
    ``xml:base``/``atom:link`` hrefs.
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
        if link and base_url:
            link = urljoin(base_url, link)
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


# -- embedded-payload walking (v0.6, §22.1) ----------------------------------
_CONTENT_KEYS = (
    "title", "name", "headline", "body", "content", "text",
    "description", "excerpt", "summary", "article", "message",
    "caption", "subtitle",
)


def _walk_payload(obj, candidates=None):
    """Collect (key, string) leaves from a nested payload structure."""
    if candidates is None:
        candidates = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and value.strip():
                candidates.append((str(key), value.strip()))
            elif isinstance(value, bool):
                candidates.append((str(key), str(value).lower()))
            elif isinstance(value, (int, float)):
                candidates.append((str(key), str(value)))
            elif isinstance(value, (dict, list)):
                _walk_payload(value, candidates)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, str) and item.strip():
                candidates.append(("", item.strip()))
            elif isinstance(item, (int, float)):
                candidates.append(("", str(item)))
            elif isinstance(item, (dict, list)):
                _walk_payload(item, candidates)
    elif isinstance(obj, str) and obj.strip():
        candidates.append(("", obj.strip()))
    return candidates


def _content_score(key):
    low = (key or "").lower()
    for rank, needle in enumerate(_CONTENT_KEYS):
        if needle in low:
            return (len(_CONTENT_KEYS) - rank)
    return 0


def flatten_payload(obj, max_chars=12000):
    """Flatten a parsed SPA payload to article-like text (no JS engine).

    Content-looking keys (`title`/`body`/`content`/…) are surfaced first so
    Apollo/Next boilerplate does not drown the article. Falls back to all
    readable string leaves when no content keys exist. Never raises.
    """
    try:
        if not obj:
            return ""
        candidates = _walk_payload(obj)
        if not candidates:
            return ""
        seen = set()
        scored = []
        for key, value in candidates:
            if value in seen:
                continue
            seen.add(value)
            scored.append((_content_score(key), len(value), key, value))
        scored.sort(key=lambda t: (-t[0], -t[1]))
        lines = []
        total = 0
        for _score, _n, key, value in scored:
            line = "%s: %s" % (key, value) if key else value
            total += len(line) + 2
            if total > max_chars:
                break
            lines.append(line)
        return "\n\n".join(lines).strip()
    except Exception:
        return ""


def extract_flattened_payload(html, max_chars=12000):
    """Extract + flatten the first embedded SPA payload found in ``html``.

    Returns flattened article text (or ``""``). ``extract_embedded_json`` keeps
    returning pretty JSON for the raw form; this is the readable form.
    """
    if not html:
        return ""
    candidates = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        candidates.append(match.group(1).strip())
    for block in re.finditer(
        r"window\.(__NEXT_DATA__|__INITIAL_STATE__|store|__NUXT__)\s*=\s*(\{.*?\});",
        html, re.DOTALL,
    ):
        candidates.append(_balanced_json(block.group(2)))
    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
        except ValueError:
            continue
        flat = flatten_payload(parsed, max_chars=max_chars)
        if flat:
            # Next.js nests the article under props.pageProps and similar.
            # flatten_payload() walks everything, so the article keys surface.
            return flat
    return ""