"""Safe Amazon Associates workflow for free-note drafts.

No scraping, browser automation, automatic purchase, price lookup, inventory
lookup, review lookup, or note publishing exists in this module.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from metrics_db import apply_additive_migrations, connect, db_path, write


JST = ZoneInfo("Asia/Tokyo")
DISCLOSURE = (
    "本記事にはAmazonアソシエイトのリンクが含まれています。"
    "リンク経由の購入により、運営者に紹介料が発生する場合があります。"
)
PENDING_PREFIX = "AMAZON_LINK_PENDING:"
LINK_STATUSES = {
    "not_applicable", "manual_required", "pending_review",
    "ready", "invalid", "published",
}
PROHIBITED_PROMOTIONAL_PHRASES = (
    "絶対に買うべき", "必ず儲かる", "人生が変わる",
    "唯一の正解", "今すぐ買わないと損",
)


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def note_root() -> Path:
    raw = os.environ.get("FREE_NOTE_OUTPUT_DIR", "outputs/note")
    path = Path(raw)
    return path if path.is_absolute() else _root() / path


def associate_settings() -> dict:
    mode = os.environ.get("AMAZON_ASSOCIATE_MODE", "manual").strip().lower()
    if mode not in {"manual", "paapi"}:
        mode = "manual"
    marketplace = (
        os.environ.get("AMAZON_PAAPI_MARKETPLACE", "www.amazon.co.jp")
        .strip().lower()
    )
    return {
        "enabled": _bool("AMAZON_ASSOCIATE_ENABLED", "true"),
        "mode": mode,
        "tracking_id": os.environ.get(
            "AMAZON_ASSOCIATE_TRACKING_ID", "").strip(),
        "disclosure_enabled": _bool(
            "AMAZON_ASSOCIATE_DISCLOSURE_ENABLED", "true"),
        "minimum": max(0, min(3, _int("AMAZON_RELATED_ITEMS_MIN", 1))),
        "maximum": max(1, min(3, _int("AMAZON_RELATED_ITEMS_MAX", 3))),
        "require_relevance": _bool(
            "AMAZON_RELATED_ITEMS_REQUIRE_RELEVANCE", "true"),
        "minimum_relevance": max(
            0.0, min(10.0, _float("AMAZON_MIN_RELEVANCE_SCORE", 7.0))),
        "manual_placeholder": _bool(
            "AMAZON_MANUAL_LINK_PLACEHOLDER", "true"),
        "paapi_enabled": _bool("AMAZON_PAAPI_ENABLED", "false"),
        "marketplace": marketplace,
        "region": os.environ.get(
            "AMAZON_PAAPI_REGION", "us-west-2").strip(),
        "scraping_enabled": False,
        "auto_purchase_enabled": False,
        "require_links_before_approval": _bool(
            "AMAZON_REQUIRE_LINKS_BEFORE_APPROVAL", "true"),
        "require_disclosure_before_approval": _bool(
            "AMAZON_REQUIRE_DISCLOSURE_BEFORE_APPROVAL", "true"),
        "recommendation_monthly_budget_usd": max(
            0.0, _float("AMAZON_RECOMMENDATION_MONTHLY_BUDGET_USD", 0.20)),
        "recommendation_max_extra_calls_per_article": max(
            0, _int("AMAZON_RECOMMENDATION_MAX_EXTRA_CALLS_PER_ARTICLE", 1)),
    }


def _catalog() -> list[dict]:
    path = _root() / "config" / "free_note_related_books.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    result = []
    for row in rows if isinstance(rows, list) else []:
        isbn = re.sub(r"\D", "", str(row.get("isbn13") or ""))
        if len(isbn) != 13 or not isbn.startswith(("978", "979")):
            continue
        cleaned = dict(row)
        cleaned["isbn13"] = isbn
        result.append(cleaned)
    return result


def _score(book: dict, selection: dict, index: int) -> tuple[float, dict]:
    article_type = str(selection.get("article_type") or "")
    topic = str(selection.get("topic") or "").lower()
    types = {str(value) for value in book.get("article_types") or []}
    keywords = [
        str(value).lower() for value in book.get("keywords") or []
        if str(value).strip()
    ]
    matches = [keyword for keyword in keywords if keyword in topic]
    topic_match = (
        10.0 if matches else
        8.0 if article_type in types else
        4.0 if article_type.startswith("weekly") and "政治" in keywords else
        0.0
    )
    reader_utility = float(book.get("reader_utility", 8.0))
    authority = float(book.get("authority", 8.5))
    accessibility = float(book.get("accessibility", 7.5))
    diversity_bonus = max(4.0, 7.0 - index * .5)
    score = (
        topic_match * .35
        + reader_utility * .25
        + authority * .20
        + accessibility * .10
        + diversity_bonus * .10
    )
    components = {
        "topic_match": topic_match,
        "reader_utility": reader_utility,
        "authority": authority,
        "accessibility": accessibility,
        "diversity_bonus": diversity_bonus,
    }
    return round(score, 2), components


def _introduction(book: dict, selection: dict) -> str:
    title = str(book.get("title") or "関連書籍")
    author = str(book.get("author") or "著者")
    topic = str(selection.get("topic") or "今回のテーマ")
    return (
        f"『{title}』は、{author}による制度・政策の背景を理解するための関連資料です。"
        f"記事「{topic}」で扱った論点を、別の角度から確認したい読者に向いています。"
    )


def manual_candidates(selection: dict) -> list[dict]:
    settings = associate_settings()
    if not settings["enabled"]:
        return []
    scored = []
    seen = set()
    for index, book in enumerate(_catalog()):
        isbn = book["isbn13"]
        if isbn in seen:
            continue
        seen.add(isbn)
        relevance, components = _score(book, selection, index)
        if settings["require_relevance"] and (
            relevance < settings["minimum_relevance"]
        ):
            continue
        scored.append((
            relevance,
            -index,
            {
                "title": str(book.get("title") or ""),
                "author_or_brand": str(book.get("author") or ""),
                "isbn": isbn,
                "asin": "",
                "product_type": "book",
                "relevance_score": relevance,
                "relevance_components": components,
                "selection_reason": (
                    "記事種別と記事テーマの一致度、読者有用性、"
                    "書誌の信頼性、読みやすさをローカル評価"
                ),
                "introduction_text": _introduction(book, selection),
                "tracking_id": "",
                "affiliate_url": "",
                "link_status": "manual_required",
                "data_source": "generated",
                "information_source": "verified_local_isbn_catalog",
                "fetched_at": None,
            },
        ))
    selected = [
        row[2] for row in sorted(scored, key=lambda row: (row[0], row[1]),
                                 reverse=True)
    ][:settings["maximum"]]
    if len(selected) < settings["minimum"]:
        return []
    for index, item in enumerate(selected, 1):
        item["item_id"] = f"amazon-{index:03d}"
    return selected


class CreatorsApiClient:
    """Current official Amazon catalog API client.

    PA-API 5.0 was retired on 2026-05-15. The configuration mode remains
    named ``paapi`` for operator compatibility, but live calls use Amazon's
    official OAuth 2.0 Creators API only.
    """

    def __init__(self, session=None, now_func=None):
        self.session = session or requests.Session()
        self.now_func = now_func or (lambda: datetime.now(JST))
        self._token = ""
        self._token_expires_at: datetime | None = None

    @staticmethod
    def configured() -> bool:
        return all(os.environ.get(name, "").strip() for name in (
            "AMAZON_CREATORS_API_CREDENTIAL_ID",
            "AMAZON_CREATORS_API_CREDENTIAL_SECRET",
            "AMAZON_CREATORS_API_CREDENTIAL_VERSION",
            "AMAZON_PAAPI_PARTNER_TAG",
        ))

    @staticmethod
    def _token_endpoint(version: str) -> str:
        if version.startswith("2."):
            return (
                "https://creatorsapi.auth.us-west-2.amazoncognito.com/"
                "oauth2/token"
            )
        if version.startswith("3."):
            return "https://api.amazon.co.jp/auth/o2/token"
        raise ValueError("unsupported_creators_api_credential_version")

    def _access_token(self) -> str:
        now = self.now_func()
        if (
            self._token
            and self._token_expires_at
            and now < self._token_expires_at - timedelta(seconds=60)
        ):
            return self._token
        credential_id = os.environ.get(
            "AMAZON_CREATORS_API_CREDENTIAL_ID", "").strip()
        credential_secret = os.environ.get(
            "AMAZON_CREATORS_API_CREDENTIAL_SECRET", "").strip()
        version = os.environ.get(
            "AMAZON_CREATORS_API_CREDENTIAL_VERSION", "").strip()
        endpoint = self._token_endpoint(version)
        if version.startswith("2."):
            response = self.session.post(
                endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": credential_id,
                    "client_secret": credential_secret,
                    "scope": "creatorsapi/default",
                },
                timeout=20,
            )
        else:
            response = self.session.post(
                endpoint,
                json={
                    "grant_type": "client_credentials",
                    "client_id": credential_id,
                    "client_secret": credential_secret,
                    "scope": "creatorsapi::default",
                },
                timeout=20,
            )
        response.raise_for_status()
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise RuntimeError("creators_api_token_missing")
        self._token = token
        self._token_expires_at = now + timedelta(
            seconds=max(60, int(payload.get("expires_in", 3600) or 3600)))
        return token

    def search_items(self, keywords: str, maximum: int) -> list[dict]:
        version = os.environ.get(
            "AMAZON_CREATORS_API_CREDENTIAL_VERSION", "").strip()
        marketplace = associate_settings()["marketplace"]
        token = self._access_token()
        authorization = f"Bearer {token}"
        if version.startswith("2."):
            authorization += f", Version {version}"
        response = self.session.post(
            "https://creatorsapi.amazon/catalog/v1/searchItems",
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "x-marketplace": marketplace,
            },
            json={
                "keywords": keywords,
                "partnerTag": os.environ.get(
                    "AMAZON_PAAPI_PARTNER_TAG", "").strip(),
                "marketplace": marketplace,
                "itemCount": max(1, min(10, maximum)),
                "searchIndex": "Books",
                "resources": [
                    "itemInfo.title",
                    "itemInfo.byLineInfo",
                    "itemInfo.externalIds",
                    "itemInfo.classifications",
                ],
            },
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("searchResult") or payload.get("SearchResult") or {}
        return list(result.get("items") or result.get("Items") or [])


def _display_value(container: Any, *names: str) -> str:
    value = container if isinstance(container, dict) else {}
    for name in names:
        value = value.get(name)
        if value is None:
            return ""
    if isinstance(value, dict):
        return str(value.get("displayValue") or value.get("DisplayValue") or "")
    return str(value or "")


def _from_api(raw_items: list[dict], selection: dict) -> list[dict]:
    settings = associate_settings()
    candidates = []
    seen = set()
    for raw in raw_items:
        asin = str(raw.get("asin") or raw.get("ASIN") or "").strip()
        info = raw.get("itemInfo") or raw.get("ItemInfo") or {}
        title = _display_value(info, "title") or _display_value(info, "Title")
        byline = info.get("byLineInfo") or info.get("ByLineInfo") or {}
        author = ""
        contributors = byline.get("contributors") or byline.get("Contributors") or []
        if contributors and isinstance(contributors[0], dict):
            author = str(
                contributors[0].get("name")
                or contributors[0].get("Name")
                or ""
            )
        external = info.get("externalIds") or info.get("ExternalIds") or {}
        isbns = external.get("isbns") or external.get("ISBNs") or {}
        isbn_values = (
            isbns.get("displayValues")
            or isbns.get("DisplayValues")
            or []
        )
        isbn = re.sub(r"\D", "", str(isbn_values[0])) if isbn_values else ""
        detail_url = str(
            raw.get("detailPageURL") or raw.get("DetailPageURL") or "")
        key = asin or isbn
        if not key or key in seen or not title or not detail_url:
            continue
        if not validate_amazon_url(detail_url):
            continue
        seen.add(key)
        synthetic_book = {
            "title": title,
            "author": author,
            "isbn13": isbn,
            "article_types": [selection.get("article_type")],
            "keywords": [
                word for word in re.split(
                    r"[\s・「」『』（）()]+",
                    str(selection.get("topic") or ""),
                )
                if len(word) >= 2 and word in title
            ],
        }
        relevance, components = _score(
            synthetic_book, selection, len(candidates))
        if settings["require_relevance"] and (
            relevance < settings["minimum_relevance"]
        ):
            continue
        candidates.append({
            "item_id": f"amazon-{len(candidates) + 1:03d}",
            "title": title,
            "author_or_brand": author,
            "isbn": isbn,
            "asin": asin,
            "product_type": "book",
            "relevance_score": relevance,
            "relevance_components": components,
            "selection_reason": "Amazon公式Creators API結果と記事テーマの関連性",
            "introduction_text": _introduction(
                {"title": title, "author": author}, selection),
            "tracking_id": "",
            "affiliate_url": detail_url,
            "link_status": "ready",
            "data_source": "paapi",
            "information_source": "amazon_creators_api",
            "fetched_at": datetime.now(JST).isoformat(),
        })
        if len(candidates) >= settings["maximum"]:
            break
    return candidates


def build_items(selection: dict, client: CreatorsApiClient | None = None
                ) -> tuple[list[dict], str, str]:
    settings = associate_settings()
    if not settings["enabled"]:
        return [], "disabled", ""
    manual = manual_candidates(selection)
    if settings["mode"] != "paapi":
        return manual, "manual", ""
    if not settings["paapi_enabled"]:
        return manual, "manual", "paapi_disabled"
    if (
        settings["recommendation_monthly_budget_usd"] <= 0
        or settings["recommendation_max_extra_calls_per_article"] < 1
    ):
        return [], "skipped", "amazon_recommendation_budget_unavailable"
    api_client = client or CreatorsApiClient()
    if not api_client.configured():
        return manual, "manual", "creators_api_credentials_missing"
    try:
        official = _from_api(
            api_client.search_items(
                str(selection.get("topic") or "政治 制度"),
                settings["maximum"],
            ),
            selection,
        )
        if len(official) >= settings["minimum"]:
            return official, "paapi", ""
        return manual, "manual", "paapi_no_relevant_items"
    except (requests.RequestException, ValueError, RuntimeError, KeyError):
        return manual, "manual", "paapi_failed_manual_fallback"


def render_section(items: list[dict], include_disclosure: bool = True) -> str:
    if not items:
        return ""
    lines = ["## 関連書籍", ""]
    if include_disclosure:
        lines.extend([DISCLOSURE, ""])
    for item in items:
        lines.extend([
            f"### 『{item.get('title', '関連書籍')}』",
            "",
            str(item.get("introduction_text") or ""),
            "",
        ])
        if item.get("affiliate_url") and item.get("link_status") in {
            "ready", "published",
        }:
            lines.append(f"[Amazonで見る]({item['affiliate_url']})")
        else:
            lines.extend([
                "Amazonリンク：要作成  ",
                f"[Amazonで見る]({PENDING_PREFIX}{item['item_id']})",
            ])
        if item.get("isbn"):
            lines.append(f"ISBN：{item['isbn']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def append_or_replace_section(article: str, items: list[dict],
                              include_disclosure: bool = True) -> str:
    base = re.split(r"\n## 関連書籍\s*\n", article, maxsplit=1)[0].rstrip()
    section = render_section(items, include_disclosure)
    return base + (f"\n\n{section}" if section else "\n")


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict) -> None:
    _atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _find_note(content_id: str) -> tuple[Path, dict]:
    for status_dir in ("drafts", "approved", "published", "failed"):
        root = note_root() / status_dir
        if not root.exists():
            continue
        for path in root.rglob("metadata.json"):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if metadata.get("content_id") == content_id:
                return path.parent, metadata
    raise FileNotFoundError(f"note draft not found: {content_id}")


def validate_amazon_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url).strip())
    except ValueError:
        return False
    marketplace = associate_settings()["marketplace"]
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and host in {marketplace, marketplace.removeprefix("www.")}
        and not parsed.username
        and not parsed.password
        and len(str(url)) <= 2048
    )


def _url_hash(url: str) -> str:
    return hashlib.sha256(str(url).encode("utf-8")).hexdigest()


def _save_item(content_id: str, item: dict,
               path: Path | None = None) -> None:
    now = datetime.now(JST).isoformat()
    write("""INSERT INTO amazon_associate_items
      (content_id,item_id,title,author_or_brand,isbn,asin,product_type,
       relevance_score,selection_reason,introduction_text,tracking_id,
       affiliate_url,link_status,data_source,fetched_at,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(content_id,item_id) DO UPDATE SET
       title=excluded.title,author_or_brand=excluded.author_or_brand,
       isbn=excluded.isbn,asin=excluded.asin,product_type=excluded.product_type,
       relevance_score=excluded.relevance_score,
       selection_reason=excluded.selection_reason,
       introduction_text=excluded.introduction_text,
       tracking_id=excluded.tracking_id,affiliate_url=excluded.affiliate_url,
       link_status=excluded.link_status,data_source=excluded.data_source,
       fetched_at=excluded.fetched_at,updated_at=excluded.updated_at""", (
        content_id, item.get("item_id"), item.get("title"),
        item.get("author_or_brand"), item.get("isbn"), item.get("asin"),
        item.get("product_type", "book"), item.get("relevance_score", 0),
        item.get("selection_reason"), item.get("introduction_text"),
        item.get("tracking_id"), item.get("affiliate_url"),
        item.get("link_status"), item.get("data_source"),
        item.get("fetched_at"), item.get("created_at", now), now,
    ), path)


def save_items(content_id: str, items: list[dict],
               path: Path | None = None) -> None:
    apply_additive_migrations(path)
    for item in items:
        _save_item(content_id, item, path)
        event_type = (
            "paapi_link_fetched"
            if item.get("data_source") == "paapi"
            else "candidate_generated"
        )
        record_event(
            content_id, str(item.get("item_id")), event_type,
            None, str(item.get("link_status") or ""),
            str(item.get("affiliate_url") or ""), path=path,
        )


def record_event(content_id: str, item_id: str, event_type: str,
                 previous_status: str | None, new_status: str | None,
                 url: str = "", metadata: dict | None = None,
                 path: Path | None = None) -> None:
    write("""INSERT INTO amazon_link_events
      (content_id,item_id,event_type,event_at,previous_status,new_status,
       url_hash,metadata_json) VALUES (?,?,?,?,?,?,?,?)""", (
        content_id, item_id, event_type, datetime.now(JST).isoformat(),
        previous_status, new_status, _url_hash(url) if url else "",
        json.dumps(metadata or {}, ensure_ascii=False),
    ), path)


def _backup_article(folder: Path) -> Path:
    source = folder / "article.md"
    stamp = datetime.now(JST).strftime("%Y%m%d-%H%M%S-%f")
    backup = folder / f"article.md.amazon-backup-{stamp}"
    shutil.copy2(source, backup)
    return backup


def set_manual_link(content_id: str, url: str, *,
                    item_id: str | None = None,
                    isbn: str | None = None,
                    path: Path | None = None) -> dict:
    if not validate_amazon_url(url):
        raise ValueError("invalid Amazon marketplace URL")
    folder, metadata = _find_note(content_id)
    items = list(metadata.get("amazon_items") or [])
    matches = [
        item for item in items
        if (item_id and item.get("item_id") == item_id)
        or (isbn and re.sub(r"\D", "", str(item.get("isbn") or ""))
            == re.sub(r"\D", "", str(isbn)))
    ]
    if len(matches) != 1:
        raise ValueError("unknown or ambiguous Amazon item")
    item = matches[0]
    digest = _url_hash(url)
    history = metadata.setdefault("amazon_link_history", [])
    if (
        item.get("link_status") in {"ready", "published"}
        and item.get("affiliate_url")
        and _url_hash(str(item["affiliate_url"])) == digest
    ):
        return {
            "content_id": content_id,
            "item_id": item["item_id"],
            "status": "duplicate",
            "updated": False,
        }
    article_path = folder / "article.md"
    article = article_path.read_text(encoding="utf-8")
    token = f"{PENDING_PREFIX}{item['item_id']}"
    if token not in article:
        raise ValueError("Amazon link placeholder not found")
    backup = _backup_article(folder)
    article = article.replace(token, url, 1)
    previous = str(item.get("link_status") or "manual_required")
    item["affiliate_url"] = url
    item["link_status"] = "ready"
    item["data_source"] = "manual"
    item["tracking_id"] = os.environ.get(
        "AMAZON_ASSOCIATE_TRACKING_ID", "").strip()
    item["updated_at"] = datetime.now(JST).isoformat()
    metadata["amazon_disclosure_included"] = DISCLOSURE in article
    metadata["updated_at"] = item["updated_at"]
    history.append({
        "item_id": item["item_id"],
        "event": "manual_link_set",
        "at": item["updated_at"],
        "url_hash": digest,
    })
    _atomic_text(article_path, article)
    _atomic_json(folder / "metadata.json", metadata)
    apply_additive_migrations(path)
    _save_item(content_id, item, path)
    record_event(
        content_id, item["item_id"], "manual_link_set",
        previous, "ready", url, path=path,
    )
    record_event(
        content_id, item["item_id"], "link_validated",
        "ready", "ready", url, path=path,
    )
    return {
        "content_id": content_id,
        "item_id": item["item_id"],
        "status": "ready",
        "updated": True,
        "backup_path": str(backup),
    }


def import_links(source: Path, path: Path | None = None) -> dict:
    apply_additive_migrations(path)
    success = duplicates = failed = quarantined = 0
    with open(source, "r", encoding="utf-8-sig", newline="") as handle:
        for row_number, row in enumerate(csv.DictReader(handle), 2):
            try:
                result = set_manual_link(
                    str(row.get("content_id") or "").strip(),
                    str(row.get("affiliate_url") or "").strip(),
                    item_id=str(row.get("item_id") or "").strip() or None,
                    isbn=str(row.get("isbn") or "").strip() or None,
                    path=path,
                )
                if result["status"] == "duplicate":
                    duplicates += 1
                else:
                    success += 1
            except (ValueError, FileNotFoundError, OSError) as exc:
                failed += 1
                canonical = "|".join([
                    str(source), str(row_number),
                    str(row.get("content_id") or ""),
                    str(row.get("item_id") or ""),
                    str(row.get("isbn") or ""),
                ])
                event_key = hashlib.sha256(
                    canonical.encode("utf-8")).hexdigest()
                inserted = write("""INSERT OR IGNORE INTO
                  amazon_link_import_quarantine
                  (imported_at,source_file,row_number,row_json,rejection_reason,
                   event_key) VALUES (?,?,?,?,?,?)""", (
                    datetime.now(JST).isoformat(), str(source), row_number,
                    json.dumps({
                        key: value for key, value in row.items()
                        if key != "affiliate_url"
                    }, ensure_ascii=False),
                    type(exc).__name__, event_key,
                ), path)
                quarantined += int(inserted is not None)
    return {
        "success": success,
        "duplicates": duplicates,
        "failed": failed,
        "quarantined": quarantined,
        "source_unchanged": True,
    }


def links_status(content_id: str | None = None) -> list[dict]:
    rows = []
    for status_dir in ("drafts", "approved", "published", "failed"):
        root = note_root() / status_dir
        if not root.exists():
            continue
        for path in root.rglob("metadata.json"):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if content_id and metadata.get("content_id") != content_id:
                continue
            items = list(metadata.get("amazon_items") or [])
            counts = {
                status: sum(item.get("link_status") == status for item in items)
                for status in (
                    "manual_required", "ready", "invalid", "published")
            }
            rows.append({
                "content_id": metadata.get("content_id"),
                "title": metadata.get("title"),
                "item_count": len(items),
                **counts,
                "disclosure_included": bool(
                    metadata.get("amazon_disclosure_included")),
                "target_publish_date": metadata.get("target_publish_date"),
                "associate_enabled": bool(
                    metadata.get("amazon_associate_enabled")),
            })
    return sorted(
        rows, key=lambda row: str(row.get("content_id") or ""), reverse=True)


def approval_blockers(folder: Path, metadata: dict) -> list[str]:
    settings = associate_settings()
    if not metadata.get("amazon_associate_enabled"):
        return []
    items = list(metadata.get("amazon_items") or [])
    if not items:
        return []
    blockers = []
    article = (folder / "article.md").read_text(encoding="utf-8")
    review = (folder / "review.md").read_text(encoding="utf-8")
    if settings["require_links_before_approval"] and any(
        item.get("link_status") not in {"ready", "published"}
        for item in items
    ):
        blockers.append("amazon_links_not_ready")
    if settings["require_disclosure_before_approval"] and (
        DISCLOSURE not in article
        or not metadata.get("amazon_disclosure_included")
    ):
        blockers.append("amazon_disclosure_missing")
    if "## Amazonアソシエイト確認" not in review:
        blockers.append("amazon_review_checklist_missing")
    return blockers


def mark_items_published(content_id: str, metadata: dict,
                         path: Path | None = None) -> None:
    for item in metadata.get("amazon_items") or []:
        previous = str(item.get("link_status") or "")
        if previous == "ready":
            item["link_status"] = "published"
            item["updated_at"] = datetime.now(JST).isoformat()
            _save_item(content_id, item, path)
            record_event(
                content_id, str(item.get("item_id")),
                "article_published", previous, "published",
                str(item.get("affiliate_url") or ""), path=path,
            )


def disable_for_note(content_id: str, path: Path | None = None) -> dict:
    folder, metadata = _find_note(content_id)
    article_path = folder / "article.md"
    article = article_path.read_text(encoding="utf-8")
    backup = _backup_article(folder)
    article = append_or_replace_section(article, [], False)
    previous_items = list(metadata.get("amazon_items") or [])
    metadata["amazon_associate_enabled"] = False
    metadata["amazon_items"] = []
    metadata["related_books"] = []
    metadata["amazon_disclosure_included"] = False
    metadata["updated_at"] = datetime.now(JST).isoformat()
    _atomic_text(article_path, article)
    _atomic_json(folder / "metadata.json", metadata)
    apply_additive_migrations(path)
    write(
        """UPDATE note_drafts
           SET related_books_json='[]', updated_at=?
           WHERE content_id=?""",
        (metadata["updated_at"], content_id),
        path,
    )
    for item in previous_items:
        previous = str(item.get("link_status") or "")
        item["link_status"] = "not_applicable"
        _save_item(content_id, item, path)
        record_event(
            content_id, str(item.get("item_id")), "associate_disabled",
            previous, "not_applicable", path=path,
        )
    return {
        "content_id": content_id,
        "associate_enabled": False,
        "item_count": 0,
        "backup_path": str(backup),
    }
