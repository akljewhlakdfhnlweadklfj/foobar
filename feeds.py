"""Fetch and parse RSS 2.0, RSS 1.0 (RDF) and Atom feeds using only the stdlib."""

from __future__ import annotations

import html
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = "Mozilla/5.0 (compatible; NewsAggregator/1.0)"
MAX_BYTES = 10 * 1024 * 1024
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class ParsedEntry:
    guid: str
    title: str
    link: str
    published: float  # unix timestamp


@dataclass
class ParsedFeed:
    title: str
    entries: list[ParsedEntry]


class FeedError(Exception):
    pass


def fetch(url: str, timeout: float = 20.0) -> bytes:
    if not url.lower().startswith(("http://", "https://")):
        raise FeedError("Feed URL must start with http:// or https://")
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_BYTES + 1)
    except Exception as e:  # noqa: BLE001 - surface any network error to the UI
        raise FeedError(f"Fetch failed: {e}") from e
    if len(data) > MAX_BYTES:
        raise FeedError("Feed is larger than 10 MB")
    return data


def _local(tag: str) -> str:
    """Strip XML namespace: '{http://www.w3.org/2005/Atom}entry' -> 'entry'."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    raw = "".join(el.itertext())
    raw = html.unescape(_TAG_RE.sub("", html.unescape(raw)))
    return _WS_RE.sub(" ", raw).strip()


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


def parse(data: bytes) -> ParsedFeed:
    try:
        root = ET.fromstring(data)
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
            t = _text(_child(e, "title"))
            if not t:
                continue
            link = _atom_link(e)
            guid = _text(_child(e, "id")) or link or t
            pub = _parse_date(_text(_child(e, "published", "updated"))) or now
            entries.append(ParsedEntry(guid, t, link, pub))
    elif kind in ("rss", "rdf"):  # RSS 2.0 / RSS 1.0
        channel = _child(root, "channel")
        title = _text(_child(channel, "title")) if channel is not None else ""
        # RSS 2.0: items inside <channel>; RSS 1.0: items are siblings of <channel>.
        containers = [root] + ([channel] if channel is not None else [])
        for container in containers:
            for it in container:
                if _local(it.tag) != "item":
                    continue
                t = _text(_child(it, "title"))
                if not t:
                    continue
                link = _text(_child(it, "link")) or it.get(
                    "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about", ""
                )
                guid = _text(_child(it, "guid")) or link or t
                pub = _parse_date(_text(_child(it, "pubDate", "date", "published", "updated"))) or now
                entries.append(ParsedEntry(guid, t, link, pub))
    else:
        raise FeedError(f"Unrecognised feed format (root element <{_local(root.tag)}>)")

    return ParsedFeed(title=title, entries=entries)


def fetch_and_parse(url: str) -> ParsedFeed:
    return parse(fetch(url))
