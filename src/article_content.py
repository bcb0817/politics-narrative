"""Bounded article-body extraction for already selected RSS/official URLs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
        if tag in {"script", "style", "nav", "footer", "header", "aside"}:
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer", "header", "aside"}:
            self.ignored = max(0, self.ignored - 1)
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.ignored or not self.stack:
            return
        if self.stack[-1] in {"p", "li", "h1", "h2", "h3", "blockquote"}:
            value = re.sub(r"\s+", " ", data).strip()
            if len(value) >= 8:
                self.parts.append(value)


def safe_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            address = ipaddress.ip_address(host)
            if not address.is_global:
                return False
        except ValueError:
            pass
        return not bool(parsed.username or parsed.password)
    except ValueError:
        return False


def extract_article_text(html: str, max_chars: int = 8000) -> str:
    parser = ArticleParser()
    parser.feed(html or "")
    unique = []
    seen = set()
    for value in parser.parts:
        normalized = re.sub(r"\s+", " ", value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return "\n".join(unique)[:max_chars].strip()


def _cache_path(root: Path, url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return root / "data" / "article_content_cache" / f"{digest}.json"


def fetch_article_text(
    url: str, *, root: Path, request_get=None, ttl_hours: int = 24
) -> str:
    if not safe_public_url(url):
        return ""
    path = _cache_path(root, url)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(value["fetched_at"])
        if datetime.now(timezone.utc) - fetched < timedelta(hours=ttl_hours):
            return str(value.get("text") or "")
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    if request_get is None:
        import requests
        request_get = requests.get
    try:
        response = request_get(
            url, timeout=float(os.environ.get(
                "POLITICS_ARTICLE_FETCH_TIMEOUT_SECONDS", "8")),
            headers={"User-Agent": "politics-narrative/1.0"},
            allow_redirects=True,
        )
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type:
            return ""
        max_bytes = int(os.environ.get(
            "POLITICS_ARTICLE_MAX_BYTES", "1000000"))
        if len(response.content) > max_bytes:
            return ""
        text = extract_article_text(
            response.text,
            max_chars=int(os.environ.get(
                "POLITICS_ARTICLE_MAX_CHARS", "8000")))
        if text:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "url": url, "text": text,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False), encoding="utf-8")
        return text
    except Exception:
        return ""


def enrich_article(item: dict, *, root: Path, request_get=None) -> dict:
    enriched = dict(item)
    existing = str(
        item.get("article_text") or item.get("content")
        or item.get("body") or "")
    if existing:
        enriched["article_text"] = existing[:8000]
    elif os.environ.get(
        "POLITICS_FETCH_ARTICLE_BODY", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}:
        enriched["article_text"] = fetch_article_text(
            str(item.get("url") or ""), root=root, request_get=request_get,
            ttl_hours=int(os.environ.get(
                "POLITICS_CACHE_TTL_HOURS", "24")))
    social = item.get("social_posts") or item.get("sns_posts") or []
    enriched["social_context"] = [
        {
            "platform": str(row.get("platform") or ""),
            "text": str(row.get("text") or "")[:1000],
            "url": str(row.get("url") or ""),
            "verified": bool(row.get("verified")),
        }
        for row in social if isinstance(row, dict)
    ][:10]
    return enriched
