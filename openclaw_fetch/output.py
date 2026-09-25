"""Output formatters: human / text / markdown / json / jsonl / raw."""

import json
import sys

from .util import raw_bytes


def _public(result):
    if isinstance(result, dict):
        return {k: v for k, v in result.items() if not k.startswith("_")}
    return result


def dump_json(result, indent=2):
    return json.dumps(_public(result), indent=indent, ensure_ascii=False)


def dump_jsonl(result):
    return json.dumps(_public(result), ensure_ascii=False)


def human_header(result):
    lines = [
        "=" * 60,
        "NETWORK: %s" % result.get("network", ""),
        "STATUS: %s" % result.get("status", ""),
        "URL: %s" % result.get("requested_url", ""),
        "FINAL_URL: %s" % result.get("final_url", ""),
        "CONTENT_TYPE: %s" % result.get("content_type", ""),
        "CONTENT_LENGTH: %s" % result.get("content_length", ""),
        "CONTENT_KIND: %s" % result.get("content_kind", ""),
    ]
    if result.get("title"):
        lines.append("TITLE: %s" % result["title"])
    lines.append("TEXT_LENGTH: %s" % result.get("text_length", ""))
    lines.append("TOTAL_TEXT_LENGTH: %s" % result.get("total_text_length", ""))
    lines.append("RETURNED_TEXT: %s" % result.get("returned_text_length", ""))
    lines.append("LINK_COUNT: %s" % result.get("link_count", ""))
    if result.get("cached"):
        lines.append("CACHED: true")
    if result.get("took_ms") is not None:
        lines.append("TOOK_MS: %s" % result["took_ms"])
    if result.get("proxy_used"):
        lines.append("PROXY: %s" % result["proxy_used"])
    if result.get("error"):
        lines.append("ERROR: %s" % result["error"])
        lines.append("ERROR_CODE: %s" % result.get("error_code", ""))
    lines.append("=" * 60)
    return "\n".join(lines)


def human_separator():
    return "\n\n" + "~" * 60 + "\n"


def _binary_stream(out):
    """Return a byte-writable view of ``out`` (text streams get ``.buffer``)."""
    buffer = getattr(out, "buffer", None)
    if buffer is not None:
        return buffer
    return _TextFallback(out)


class _TextFallback:
    """Encode bytes for callers that hand us a text-only stream (tests, StringIO)."""

    def __init__(self, out):
        self._out = out

    def write(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return self._out.write(data)

    def flush(self):
        flush = getattr(self._out, "flush", None)
        if flush is not None:
            flush()


def render(results, fmt="human", *, out=None):
    """Render one or many result dicts for the requested format."""
    out = out if out is not None else sys.stdout
    if fmt == "json":
        out.write(dump_json(results if isinstance(results, list) and len(results) > 1 else results[0]))
        out.write("\n")
        return
    if fmt == "jsonl":
        for result in results:
            out.write(dump_jsonl(result))
            out.write("\n")
        return
    if fmt == "raw":
        # --raw means the original bytes, so bypass the text layer entirely.
        binary = _binary_stream(out)
        for result in results:
            binary.write(raw_bytes(result))
        binary.flush()
        return
    if fmt == "text":
        _render_bodies(results, "text", out)
        return
    if fmt == "markdown":
        _render_bodies(results, "markdown", out)
        return
    # human (default)
    for i, result in enumerate(results):
        if i:
            out.write(human_separator())
        out.write(human_header(result))
        out.write("\n\n")
        out.write(result.get("text", ""))
        out.write("\n")


def _render_bodies(results, field, out):
    for i, result in enumerate(results):
        if i:
            out.write(human_separator())
        body = result.get(field) or result.get("text") or ""
        out.write(body)
        out.write("\n")


def print_error_human(error_result, stream=None):
    stream = stream if stream is not None else sys.stderr
    stream.write("ERROR: %s\n" % error_result.get("error", ""))
    if error_result.get("error_code"):
        stream.write("ERROR_CODE: %s\n" % error_result["error_code"])