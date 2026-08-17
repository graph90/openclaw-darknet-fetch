#!/usr/bin/env python3

"""
openclaw_darknet_fetch.py

Multi-network web fetcher designed for AI agents.

NETWORKS
--------

Normal clearnet:
    python3 openclaw_darknet_fetch.py -n https://example.com

Tor / onion:
    python3 openclaw_darknet_fetch.py -t http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/

Tor -> normal clearnet:
    python3 openclaw_darknet_fetch.py -t https://example.com

I2P:
    python3 openclaw_darknet_fetch.py -i http://example.i2p/


PROXIES
-------

Tor SOCKS5:
    127.0.0.1:9050

I2P HTTP proxy:
    127.0.0.1:4444


OUTPUT MODES
------------

Readable text:
    Default output

JSON:
    python3 openclaw_darknet_fetch.py -t URL --json

Limit returned text:
    python3 openclaw_darknet_fetch.py -t URL --max-chars 12000

Raw HTML:
    python3 openclaw_darknet_fetch.py -t URL --raw


DEPENDENCIES
------------

    pip install requests[socks]
"""

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin
import requests

TOR_PROXY = "socks5h://127.0.0.1:9050"
I2P_PROXY = "http://127.0.0.1:4444"
TIMEOUT = 30

USER_AGENT = (
    "Mozilla/5.0 "
    "(compatible; OpenClawDarknetFetch/1.0)"
)
class HTMLTextExtractor(HTMLParser):
    def __init__(self, base_url=""):
        super().__init__()
        self.base_url = base_url
        self.title = ""
        self.text_parts = []
        self.links = []
        self.in_title = False
        self.ignore_depth = 0
        self.current_link = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script","style","noscript","svg",):
            self.ignore_depth += 1
            return
        if self.ignore_depth:
            return
        if tag == "title":
            self.in_title = True
        if tag == "a":
            href = attrs.get("href")
            if href:
                self.current_link = href
    def handle_endtag(self, tag):
        if tag in ("script","style","noscript","svg",):
            if self.ignore_depth:
                self.ignore_depth -= 1
            return
        if self.ignore_depth:
            return
        if tag == "title":
            self.in_title = False
        if tag == "a":
            self.current_link = None
    def handle_data(self, data):
        if self.ignore_depth:
            return
        cleaned = re.sub(r"\s+"," ",data,).strip()
        if not cleaned:
            return
        if self.in_title:
            self.title += " " + cleaned
            return
        self.text_parts.append(cleaned)
        if self.current_link:
            absolute_url = urljoin(self.base_url,self.current_link,)
            self.links.append({"url": absolute_url,"text": cleaned,})
    def get_text(self):
        text = "\n".join(self.text_parts)
        text = re.sub(r"\n{3,}","\n\n",text,)
        return text.strip()
    def get_title(self):
        return re.sub(r"\s+"," ",self.title,).strip()
def fetch_normal(url):
    return requests.get(url,timeout=TIMEOUT,headers={"User-Agent": USER_AGENT,},allow_redirects=True,)

def fetch_tor(url):
    proxies = {"http": TOR_PROXY,"https": TOR_PROXY,}
    return requests.get(url,proxies=proxies,timeout=TIMEOUT,headers={"User-Agent": USER_AGENT,},allow_redirects=True,)

def fetch_i2p(url):
    proxies = {"http": I2P_PROXY,"https": I2P_PROXY,}
    return requests.get(url,proxies=proxies,timeout=TIMEOUT,headers={"User-Agent": USER_AGENT,},allow_redirects=True,)

def extract_content(response):
    content_type = response.headers.get("content-type","",).lower()
    result = {"title": "","text": "","links": [],}
    if "text/html" not in content_type:
        result["text"] = response.text.strip()
        return result
    parser = HTMLTextExtractor(base_url=response.url)
    parser.feed(response.text)
    result["title"] = parser.get_title()
    result["text"] = parser.get_text()
    result["links"] = parser.links
    return result
def truncate_text(text, max_chars):
    if max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return (text[:max_chars] + "\n\n" + "[OUTPUT TRUNCATED]" + f"\nOriginal text length: {len(text)}" + f"\nReturned: {max_chars}")

def build_result(response,network,requested_url,max_chars,):
    content = extract_content(response)
    text = truncate_text(content["text"],max_chars,)
    return {
        "ok": True,
        "network": network,
        "requested_url": requested_url,
        "final_url": response.url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type","unknown",),
        "content_length": len(response.content),
        "title": content["title"],
        "text_length": len(content["text"]),
        "returned_text_length": len(text),
        "links": content["links"],
        "text": text,
    }

def print_human(result):
    print("=" * 60)
    print("NETWORK:",result["network"],)
    print("STATUS:",result["status"],)
    print("URL:",result["requested_url"],)
    print("FINAL_URL:",result["final_url"],)
    print("CONTENT_TYPE:",result["content_type"],)
    print("CONTENT_LENGTH:",result["content_length"],)
    if result["title"]:
        print("TITLE:",result["title"],)
    print("TEXT_LENGTH:",result["text_length"],)
    print("RETURNED_TEXT:",result["returned_text_length"],)
    print("LINK_COUNT:",len(result["links"]),)
    print("=" * 60)
    print()
    print(result["text"])
def print_json(result):
    print(json.dumps(result,indent=2,ensure_ascii=False,))
def print_raw(response):
    print(response.text)
def print_error(network,message,json_mode,exit_code):
    error = {
        "ok": False,
        "network": network,
        "error": message,
    }
    if json_mode:
        print(json.dumps(error,indent=2,))
    else:
        print(f"ERROR: {message}",file=sys.stderr,)
    sys.exit(exit_code)
def main():
    parser = argparse.ArgumentParser(
        description=(
            "Multi-network web fetcher for AI agents."
        ),
        epilog=(
            "Networks: "
            "-n normal, "
            "-t Tor, "
            "-i I2P"
        ),
    )
    network_group = parser.add_mutually_exclusive_group(required=True)
    network_group.add_argument(
        "-n",
        "--normal",
        metavar="URL",
        help=(
            "Fetch directly over the normal Internet."
        ),
    )
    network_group.add_argument(
        "-t",
        "--tor",
        metavar="URL",
        help=(
            "Fetch through Tor SOCKS5."
        ),
    )
    network_group.add_argument(
        "-i",
        "--i2p",
        metavar="URL",
        help=(
            "Fetch through the I2P HTTP proxy."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help=(
            "Return the original response body."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Return structured JSON."
        ),
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=12000,
        metavar="N",
        help=(
            "Maximum returned text size. "
            "Default: 12000."
        ),
    )
    parser.add_argument(
        "--max-links",
        type=int,
        default=50,
        metavar="N",
        help=(
            "Maximum number of links returned. "
            "Default: 50."
        ),
    )
    args = parser.parse_args()
    if args.normal:
        network = "NORMAL"
        url = args.normal
    elif args.tor:
        network = "TOR"
        url = args.tor
    else:
        network = "I2P"
        url = args.i2p
    if args.raw and args.json:
        parser.error("--raw and --json cannot be used together.")
    if args.max_chars < 0:
        parser.error("--max-chars must be zero or greater.")
    if args.max_links < 0:
        parser.error("--max-links must be zero or greater.")
    try:
        if network == "NORMAL":
            response = fetch_normal(url)
        elif network == "TOR":
            response = fetch_tor(url)
        else:
            response = fetch_i2p(url)
    except requests.exceptions.ProxyError:
        if network == "TOR":
            proxy = TOR_PROXY
        elif network == "I2P":
            proxy = I2P_PROXY
        else:
            proxy = "none"
        print_error(
            network,
            (
                "Could not connect to "
                f"{network} proxy at {proxy}"
            ),
            args.json,
            2,
        )
    except requests.exceptions.Timeout:
        print_error(network,"Request timed out.",args.json,3,)

    except requests.exceptions.ConnectionError as exc:
        print_error(network,f"Connection failed: {exc}",args.json,4,)
    except requests.exceptions.RequestException as exc:
        print_error(network,str(exc),args.json,5,)
    if args.raw:
        print_raw(response)
        return
    result = build_result(response,network,url,args.max_chars,)
    result["links"] = result["links"][:args.max_links]
    if args.json:
        print_json(result)
        return
    print_human(result)
if __name__ == "__main__":
    main()