"""Fetch and parse RSS 2.0, RSS 1.0 (RDF) and Atom feeds using only the stdlib."""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

USER_AGENT = "Mozilla/5.0 (compatible; NewsAggregator/1.0)"
MAX_BYTES = 10 * 1024 * 1024
_TAG_RE = re.compile(r"<(/?[a-zA-Z][a-zA-Z0-9:-]*(\s[^<>]*)?/?|!--.*?--)>", re.S)
DESCRIPTION_CHARS = 600
# WordPress/Blogger footers and "read more" links that carry no information about the story.
_DESC_BOILERPLATE = re.compile(
    r"(The post .{1,300}? appeared first on .{1,120}?\.?\s*$)|(\[?(…|\.\.\.)\]?\s*$)|"
    r"((Continue|Keep) reading.{0,80}$)|(Read (more|the full (article|story)).{0,80}$)|"
    r"(submitted by\s+/?u/\S+.{0,40}$)", re.I | re.S)  # Reddit's "submitted by /u/x [link] [comments]"
# Google News titles end with " - <Publisher>".
_GOOGLE_NEWS_SUFFIX = re.compile(r"\s+[-–—]\s+[^-–—]{2,60}$")
_WS_RE = re.compile(r"\s+")


@dataclass
class ParsedEntry:
    guid: str
    title: str
    link: str
    published: float  # unix timestamp
    description: str = ""  # plain-text summary, trimmed to DESCRIPTION_CHARS
    image: str = ""        # http(s) URL of a picture for the item, if the feed has one


@dataclass
class ParsedFeed:
    title: str
    entries: list[ParsedEntry]


class FeedError(Exception):
    pass


# Feed URLs come from users, so by default they may not point at this machine or its private
# network (SSRF). Checked for the URL and for every redirect. Set ALLOW_PRIVATE = True
# (app.py --allow-private-feeds) to fetch feeds served on localhost/LAN, e.g. for testing.
ALLOW_PRIVATE = False
FETCH_DEADLINE = 45.0  # seconds for the whole download, however slowly the server trickles bytes


def check_url(url: str) -> None:
    """Raise FeedError unless url is http(s) and its host resolves only to public addresses."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise FeedError("Feed URL must start with http:// or https://")
    if ALLOW_PRIVATE:
        return
    host = parts.hostname
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Not resolvable here (e.g. DNS only reachable through an egress proxy): the request will
        # go through that proxy or fail. Literal IPs still get checked below.
        try:
            infos = [(None, None, None, None, (str(ipaddress.ip_address(host)), 0))]
        except ValueError:
            return
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise FeedError(f"Refusing to fetch {host}: it points to a private or local address ({ip})")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirects)


def fetch(url: str, timeout: float = 20.0) -> bytes:
    check_url(url)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    deadline = time.monotonic() + FETCH_DEADLINE
    try:
        with _opener.open(req, timeout=timeout) as resp:
            chunks, size = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise FeedError(f"Feed took longer than {FETCH_DEADLINE:g}s to download")
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FeedError("Feed is larger than 10 MB")
            data = b"".join(chunks)
    except FeedError:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise FeedError("Rate-limited by the site (HTTP 429): temporary, retried at the next refresh") from e
        raise FeedError(f"Fetch failed: HTTP {e.code} {e.reason}") from e
    except Exception as e:  # noqa: BLE001 - surface any network error to the UI
        raise FeedError(f"Fetch failed: {e}") from e
    return data


def _local(tag: str) -> str:
    """Strip XML namespace: '{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def clean_text(raw: str) -> str:
    """Decode entities once (feeds often double-encode), strip HTML tags, collapse whitespace.
    Only real tags are stripped, so text like "3 < 5 and 7 > 2" survives."""
    raw = _TAG_RE.sub("", html.unescape(raw or ""))
    return _WS_RE.sub(" ", raw).strip()


def _raw(el: ET.Element | None) -> str:
    """Element text exactly as the feed gave it (for links and guids, which must not be altered)."""
    return "" if el is None else "".join(el.itertext()).strip()


def _text(el: ET.Element | None) -> str:
    return "" if el is None else clean_text("".join(el.itertext()))


def clean_description(raw: str) -> str:
    """Plain-text description: tags/entities removed, boilerplate footers cut, trimmed on a word."""
    text = clean_text(raw)
    if text.lower() in ("null", "none", "undefined", "nan"):  # some feeds/APIs send these literally
        return ""
    for _ in range(2):
        text = _DESC_BOILERPLATE.sub("", text).strip()
    if len(text) > DESCRIPTION_CHARS:
        text = text[:DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def clean_title(title: str, feed_url: str = "") -> str:
    """Strip the " - Publisher" suffix Google News appends to every headline."""
    if "news.google." in feed_url:
        title = _GOOGLE_NEWS_SUFFIX.sub("", title).strip() or title
    return title


_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|gif|webp|avif)(\?|$)", re.I)


def _image(el: ET.Element, base_url: str = "") -> str:
    """An item's picture: media:content / media:thumbnail, an image enclosure, or the first <img> in
    its description or content. Returns an absolute http(s) URL or ""."""
    candidates = []
    for c in el.iter():
        tag = _local(c.tag)
        url = c.get("url") or c.get("href") or ""
        if not url:
            continue
        kind = (c.get("type") or "").lower()
        medium = (c.get("medium") or "").lower()
        if tag == "thumbnail" or (tag == "content" and (medium == "image" or kind.startswith("image/")
                                                       or (not medium and not kind and _IMAGE_EXT.search(url)))):
            candidates.append(url)
        elif tag in ("enclosure", "link") and (kind.startswith("image/") or (tag == "enclosure" and _IMAGE_EXT.search(url))):
            if tag == "enclosure" or c.get("rel") == "enclosure":
                candidates.append(url)
    if not candidates:
        for names in (("description", "summary"), ("encoded", "content")):
            child = _child(el, *names)
            if child is not None:
                m = _IMG_SRC.search(html.unescape("".join(child.itertext())))
                if m:
                    candidates.append(html.unescape(m.group(1)))
                    break
    for url in candidates:
        url = urljoin(base_url, url.strip()) if base_url else url.strip()
        if url.lower().startswith(("http://", "https://")) and "feedburner.com/~r" not in url:
            return url
    return ""


def _description(el: ET.Element) -> str:
    """Prefer the short summary; fall back to the full content."""
    for names in (("description", "summary"), ("encoded", "content")):
        d = _text(_child(el, *names))
        if d:
            return clean_description(d)
    return ""


def _parse_date(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)  # RFC 822 (RSS)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))  # ISO 8601 (Atom / dc:date)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _atom_link(entry: ET.Element) -> str:
    fallback = ""
    for c in entry:
        if _local(c.tag) == "link":
            href = c.get("href", "")
            rel = c.get("rel", "alternate")
            if rel == "alternate" and href:
                return href
            fallback = fallback or href
    return fallback


def parse(data: bytes, base_url: str = "") -> ParsedFeed:
    """Parse RSS/Atom. base_url (the feed's URL) resolves relative article links."""
    try:
        root = ET.fromstring(data.lstrip())
    except ET.ParseError as e:
        raise FeedError(f"Not valid XML: {e}") from e

    kind = _local(root.tag).lower()
    now = time.time()
    entries: list[ParsedEntry] = []

    if kind == "feed":  # Atom
        title = _text(_child(root, "title"))
        for e in root:
            if _local(e.tag) != "entry":
                continue
            t = clean_title(_text(_child(e, "title")), base_url)
            if not t:
                continue
            link = urljoin(base_url, _atom_link(e)) if base_url else _atom_link(e)
            guid = _raw(_child(e, "id")) or link or t
            pub = min(_parse_date(_text(_child(e, "published", "updated"))) or now, now)  # no future dates
            entries.append(ParsedEntry(guid, t, link, pub, _description(e), _image(e, base_url)))
    elif kind in ("rss", "rdf"):  # RSS 2.0 / RSS 1.0
        channel = _child(root, "channel")
        title = _text(_child(channel, "title")) if channel is not None else ""
        # RSS 2.0: items inside <channel>; RSS 1.0: items are siblings of <channel>.
        containers = [root] + ([channel] if channel is not None else [])
        for container in containers:
            for it in container:
                if _local(it.tag) != "item":
                    continue
                t = clean_title(_text(_child(it, "title")), base_url)
                if not t:
                    continue
                link = _raw(_child(it, "link")) or it.get(
                    "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""
                )
                if base_url and link:
                    link = urljoin(base_url, link)
                guid = _raw(_child(it, "guid")) or link or t
                pub = min(_parse_date(_text(_child(it, "pubDate", "date", "published", "updated"))) or now, now)
                entries.append(ParsedEntry(guid, t, link, pub, _description(it), _image(it, base_url)))
    else:
        raise FeedError(f"Unrecognised feed format (root element <{_local(root.tag)}>)")

    return ParsedFeed(title=title, entries=entries)


def fetch_and_parse(url: str) -> ParsedFeed:
    return parse(fetch(url), base_url=url)
