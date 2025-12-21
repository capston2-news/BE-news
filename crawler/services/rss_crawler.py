# crawler/services/rss_crawler.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Dict, Any, Optional, List, Set, Callable
from datetime import datetime, timezone
import time
import hashlib
import re
import os
import unicodedata

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import feedparser
from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument

from bs4 import BeautifulSoup
from urllib.parse import urlparse as _urlparse
from zoneinfo import ZoneInfo

from .rss_parsers import (
    extract_article_vnexpress,
    extract_article_thanhnien,
    extract_article_generic,
)
from api.db import get_db
from crawler.utils.time_utils import now_vn

# ✅ Realtime index: upsert Chroma ngay sau khi upsert article vào Mongo
from recommender.services.article_indexer import upsert_article_to_chroma


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NewsCrawler/1.0; +https://example.com/bot)"
}


def _build_session() -> requests.Session:
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    s = requests.Session()
    s.headers.update(HEADERS)
    ad = HTTPAdapter(max_retries=retry)
    s.mount("http://", ad)
    s.mount("https://", ad)
    return s


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_text(txt: Optional[str]) -> Optional[str]:
    if not txt:
        return None
    return "sha256:" + hashlib.sha256(txt.encode("utf-8", errors="ignore")).hexdigest()


PARSERS = {
    "vnexpress": extract_article_vnexpress,
    "thanhnien": extract_article_thanhnien,
}

# ---------- Author sanitize ----------
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
EMAIL_RE = re.compile(r"\b\S+@\S+\.\S+\b")


def _sanitize_author(author: Optional[str], base_url: str) -> Optional[str]:
    if not author:
        return None
    a = author.strip()
    if not a:
        return None

    low = a.lower()
    host = _urlparse(base_url or "").netloc.lower()

    if EMAIL_RE.search(a):
        return None
    if low.startswith(("http://", "https://")):
        return None
    if DOMAIN_RE.search(low):
        if (host and host in low) or low == host:
            return None
        if DOMAIN_RE.fullmatch(low):
            return None

    if low in {"báo thanh niên", "thanhnien", "thanhnien.vn", "vnexpress", "vnexpress.net"}:
        return None
    return a


def _extract_eic_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(?im)^\s*Tổng\s*biên\s*tập\s*:\s*([^\n\r|]+)", text)
    if not m:
        return None
    return m.group(1).strip(' .-|–—') or None


# ---------- Category helpers ----------
SITE_SLUG_TO_NAME = {
    "thanhnien": {
        "thoi-su": "Thời sự",
        "chinh-tri": "Chính trị",
        "the-gioi": "Thế giới",
        "kinh-te": "Kinh tế",
        "doi-song": "Đời sống",
        "suc-khoe": "Sức khỏe",
        "gioi-tre": "Giới trẻ",
        "giao-duc": "Giáo dục",
        "van-hoa": "Văn hóa",
        "du-lich": "Du lịch",
        "the-thao": "Thể thao",
        "giai-tri": "Giải trí",
        "cong-nghe": "Công nghệ",
    },
    "vnexpress": {
        "thoi-su": "Thời sự",
        "kinh-doanh": "Kinh doanh",
        "the-gioi": "Thế giới",
        "giai-tri": "Giải trí",
        "the-thao": "Thể thao",
        "phap-luat": "Pháp luật",
        "giao-duc": "Giáo dục",
        "suc-khoe": "Sức khỏe",
        "doi-song": "Đời sống",
        "du-lich": "Du lịch",
        "khoa-hoc-cong-nghe": "Khoa học công nghệ",
    },
}

_slug_token_re = re.compile(r"([a-z0-9\-]+)\.rss$", re.I)


def _slug_from_rss_url(rss_url: str) -> str:
    path = (_urlparse(rss_url).path or "").rstrip("/")
    m = _slug_token_re.search(path)
    if m:
        return m.group(1).lower()
    seg = path.split("/")[-1].lower()
    return seg.replace(".rss", "") or "uncategorized"


def _title_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-")) or "Uncategorized"


def _map_category_name_from_rss(rss_url: str, site: str | None = None) -> str:
    slug = _slug_from_rss_url(rss_url or "")
    if not slug:
        return "Uncategorized"
    if site:
        name = SITE_SLUG_TO_NAME.get(site, {}).get(slug)
        if name:
            return name
    return _title_from_slug(slug) or "Uncategorized"


def _slugify(s: str) -> str:
    if not s:
        return "uncategorized"
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9 -]+", "", s).strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "uncategorized"


def _strip_accents(x: str) -> str:
    return unicodedata.normalize("NFKD", x).encode("ascii", "ignore").decode("ascii").lower().strip()


def _is_same_section_as_parent(section: str, parent_name: str) -> bool:
    if not section:
        return False
    s = _strip_accents(section)
    p = _strip_accents(parent_name)
    return s == p or s == p.replace(" ", "-")


# ====== LOOKUP-ONLY category_child (KHÔNG tự tạo) ======
def _find_category_child_id_only(db, parent_id, section_name: str, normalize_fn) -> Optional[Any]:
    if not section_name:
        return None
    name = section_name.strip()
    slug = normalize_fn(name)

    doc = db["category_child"].find_one({"category_id": parent_id, "name": name}, {"_id": 1})
    if doc:
        return doc["_id"]

    doc = db["category_child"].find_one({"category_id": parent_id, "slug": slug}, {"_id": 1})
    if doc:
        return doc["_id"]

    return None


# --- TN fallback: đoán section từ URL ---
def _infer_tn_section_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    u = url.lower()
    MAP = {
        "/phap-luat/": "Pháp luật",
        "/dan-sinh/": "Dân sinh",
        "/viec-lam/": "Việc làm",
        "/giao-thong/": "Giao thông",
        "/quyen-duoc-biet/": "Quyền được biết",
        "/phong-su-dieu-tra/": "Phóng sự / Điều tra",
        "/quoc-phong/": "Quốc phòng",
        "/chong-tin-gia/": "Chống tin giả",
        "/thanh-tuu-y-khoa/": "Thành tựu y khoa",
        "/chinh-tri/": "Chính trị",
    }
    for key, name in MAP.items():
        if key in u:
            return name
    return None


# ✅ Upsert Mongo articles + realtime upsert Chroma
def _upsert_article_from_extracted(
    db,
    src: dict,
    ex_doc: dict,
    allow_update: bool = True,
    verbose_log: Optional[Callable[[str], None]] = None,
) -> Dict[str, int]:
    site = (src.get("site") or "").strip().lower()
    rss_url = (src.get("rss_url") or "").strip()

    external_url = ex_doc.get("source_url")
    title = ex_doc.get("canonical_title")
    content = ex_doc.get("body_text") or ""
    published_at = ex_doc.get("published_at")
    images = ex_doc.get("images") or []
    author_name = ex_doc.get("author")
    section = (ex_doc.get("section") or "").strip()

    if not (site and external_url and title and content):
        if verbose_log:
            verbose_log(f"[SKIP] inline-import thiếu field url={external_url}")
        return {"skipped": 1}

    # parent category
    parent_name = _map_category_name_from_rss(rss_url, site)
    parent = db["categories"].find_one_and_update(
        {"name": parent_name},
        {"$setOnInsert": {"name": parent_name, "slug": _slugify(parent_name), "created_at": now_vn()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    parent_id = parent["_id"]

    # TN: nếu section null/trùng cha -> đoán từ URL (chỉ để hiển thị)
    if site == "thanhnien" and (not section or _is_same_section_as_parent(section, parent_name)):
        guessed = _infer_tn_section_from_url(external_url)
        if guessed:
            section = guessed

    # lookup-only child
    category_child_id = None
    if section and not _is_same_section_as_parent(section, parent_name):
        category_child_id = _find_category_child_id_only(db, parent_id, section, _slugify)

    # author upsert (nhẹ)
    author_id = None
    if author_name:
        a = db["authors"].find_one_and_update(
            {"name": author_name.strip()},
            {"$setOnInsert": {"name": author_name.strip(), "created_at": now_vn()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        author_id = a["_id"]

    filter_doc = {"site": site, "external_url": external_url}
    set_on_insert = {"created_at": now_vn(), "is_deleted": False, "status": "published"}
    set_doc = {
        "title": title,
        "content": content,
        "author_id": author_id,
        "category_id": parent_id,
        "category_child_id": category_child_id,
        "published_at": published_at,
        "images": images,
        "updated_at": now_vn(),
    }

    update_doc = {"$setOnInsert": set_on_insert, "$set": set_doc} if allow_update else {"$setOnInsert": {**set_on_insert, **set_doc}}
    res = db["articles"].update_one(filter_doc, update_doc, upsert=True)

    inserted = (res.matched_count == 0 and res.upserted_id is not None)
    updated = (allow_update and res.modified_count > 0)

    # ✅ realtime upsert Chroma
    if inserted or updated:
        try:
            proj = {
                "_id": 1,
                "title": 1,
                "content": 1,
                "entities": 1,
                "keywords": 1,
                "published_at": 1,
                "site": 1,
                "category_id": 1,
                "category_child_id": 1,
                "status": 1,
                "is_deleted": 1,
                "external_url": 1,
            }

            if inserted and res.upserted_id:
                art = db["articles"].find_one({"_id": res.upserted_id}, proj)
            else:
                art = db["articles"].find_one(filter_doc, proj)

            if art:
                ok = upsert_article_to_chroma(art)
                if verbose_log:
                    verbose_log(f"[INDEX] {'OK' if ok else 'SKIP'} | {title}")
        except Exception as ie:
            if verbose_log:
                verbose_log(f"[INDEX] ERROR | {title} | {ie}")

    if inserted:
        if verbose_log:
            verbose_log(f"[OK]   INLINE INSERT | {title} | child={section or '-'}")
        return {"inserted": 1}
    if updated:
        if verbose_log:
            verbose_log(f"[OK]   INLINE UPDATE | {title} | child={section or '-'}")
        return {"updated": 1}

    if verbose_log:
        verbose_log(f"[SKIP] INLINE EXIST  | {title}")
    return {"skipped": 1}


def crawl_rss_source(
    source: Dict[str, Any],
    limit: int = 50,
    skip_existing: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Crawl RSS:
      - insert raw_pages
      - insert extracted_articles (dedupe theo source_url nếu bạn có unique index)
      - inline upsert articles + realtime upsert Chroma (nếu inserted/updated)
    """
    db = get_db()
    crawl_runs = db["crawl_runs"]
    raw_pages = db["raw_pages"]
    extracted = db["extracted_articles"]

    source_id = source.get("_id")
    name_source = source.get("name_source")
    site = (source.get("site") or "").strip().lower()
    rss_url = (source.get("rss_url") or "").strip()
    base_url = (source.get("base_url") or "").strip()

    if not (source_id and name_source and site and rss_url):
        return {"error": "Source thiếu _id/name_source/site/rss_url"}

    parse_func = PARSERS.get(site)
    title_selectors: List[str] = source.get("title_selectors") or []
    body_selectors: List[str] = source.get("body_selectors") or []

    run_id = crawl_runs.insert_one(
        {
            "source_id": source_id,
            "started_at": now_vn(),
            "finished_at": None,
            "status": "running",
            "stats": {"pages": 0, "saved": 0, "skipped": 0, "errors": 0},
            "stats_debug": [],
        }
    ).inserted_id

    session = _build_session()
    fetched = saved = skipped = 0

    try:
        feed = feedparser.parse(rss_url, request_headers=HEADERS)
        entries_full = getattr(feed, "entries", []) if feed else []
        entries = entries_full if (limit is None or limit <= 0) else entries_full[:limit]

        feed_urls: List[str] = []
        for e in entries:
            u = getattr(e, "link", None)
            if u and isinstance(u, str) and u.startswith("http"):
                feed_urls.append(u)

        existing: Set[str] = set()
        if skip_existing and feed_urls:
            for doc in extracted.find({"source_id": source_id, "source_url": {"$in": feed_urls}}, {"_id": 0, "source_url": 1}):
                existing.add(doc["source_url"])

        for idx, e in enumerate(entries, start=1):
            if os.path.exists("STOP_RSS.txt"):
                if log:
                    log("[STOP] Phát hiện STOP_RSS.txt — dừng crawl.")
                break

            url = getattr(e, "link", None)
            if not url or not url.startswith("http"):
                if log:
                    log(f"[SKIP] #{idx:02d} URL không hợp lệ")
                continue

            if skip_existing and url in existing:
                skipped += 1
                if log:
                    log(f"[SKIP] #{idx:02d} EXTRACTED trùng (từ trước): {url}")
                continue

            time.sleep(0.05)

            try:
                resp = session.get(url, timeout=20)
                status = resp.status_code
                ctype = resp.headers.get("Content-Type")
                html = None
                if status == 200:
                    if not resp.encoding:
                        resp.encoding = resp.apparent_encoding
                    html = resp.text

                content_hash = _hash_text(html) if html else None

                raw_doc = {
                    "source_id": source_id,
                    "crawl_run_id": run_id,
                    "url": url,
                    "crawler_time": now_vn(),
                    "http_status": int(status) if status is not None else None,
                    "content_type": ctype,
                    "raw_html": html,
                    "content_hash": content_hash,
                }

                raw_id = None
                try:
                    raw_id = raw_pages.insert_one(raw_doc).inserted_id
                    if log:
                        log(f"[OK]   #{idx:02d} RAW inserted | {url}")
                except DuplicateKeyError:
                    # nếu bạn có unique index url/source_id thì sẽ vào đây
                    if log:
                        log(f"[SKIP] #{idx:02d} RAW trùng   | {url}")
                    rp = raw_pages.find_one({"source_id": source_id, "url": url}, {"_id": 1})
                    raw_id = rp["_id"] if rp else None

                if status == 200 and html:
                    fetched += 1

                    published_at = None
                    if getattr(e, "published_parsed", None):
                        tm = e.published_parsed
                        published_at = datetime(*tm[:6], tzinfo=ZoneInfo("Asia/Ho_Chi_Minh"))

                    title = None
                    body_text = None
                    images: List[str] = []
                    author: Optional[str] = None
                    section = None

                    # A) site parser
                    if parse_func:
                        art = parse_func(html, base_url)
                        title = art.get("title")
                        body_text = art.get("body")
                        images = art.get("images") or []
                        author = art.get("author") or author
                        section = art.get("section") or section

                    # B) generic parser
                    if not (title and body_text):
                        art_g = extract_article_generic(html, title_selectors, body_selectors, base_url)
                        title = title or art_g.get("title")
                        body_text = body_text or art_g.get("body")
                        images = images or (art_g.get("images") or [])
                        author = author or art_g.get("author")
                        section = section or art_g.get("section")

                    # C) fallback RSS summary
                    if not (title and body_text):
                        rss_summary = getattr(e, "summary", None)
                        if rss_summary:
                            summary_text = BeautifulSoup(rss_summary, "html.parser").get_text(" ", strip=True)
                            if summary_text and len(summary_text) >= 30:
                                if not title:
                                    title = getattr(e, "title", None)
                                body_text = body_text or summary_text

                    author = _sanitize_author(author, base_url)
                    if not author and body_text:
                        author = _extract_eic_from_text(body_text)

                    # TN: nếu section null/trùng cha -> đoán từ url (ID vẫn lookup-only)
                    parent_name = _map_category_name_from_rss(rss_url, site)
                    if site == "thanhnien" and (section is None or _is_same_section_as_parent(section, parent_name)):
                        guessed = _infer_tn_section_from_url(url)
                        if guessed:
                            section = guessed

                    if not (title and body_text):
                        crawl_runs.update_one(
                            {"_id": run_id},
                            {"$push": {"stats_debug": {"url": url, "reason": "no_title_or_body"}}},
                        )
                        if log:
                            log(f"[ERROR]#{idx:02d} Thiếu title/body: {url}")
                    else:
                        ex_doc = {
                            "source_id": source_id,
                            "raw_page_id": raw_id,
                            "source_url": url,
                            "canonical_title": title,
                            "author": author or None,
                            "published_at": published_at,
                            "summary": None,
                            "body_text": body_text,
                            "body_html": None,
                            "images": images,
                            "section": section or None,
                            "topics_raw": [],
                            "copyright_note": None,
                            "content_hash": _hash_text(f"{title}\n{body_text}"),
                            "created_at": now_vn(),
                        }

                        try:
                            extracted.insert_one(ex_doc)
                            saved += 1
                            if log:
                                log(
                                    f"[OK]   #{idx:02d} EXTRACTED | title='{title}' | "
                                    f"child='{section or '-'}' | imgs={len(images)} | author='{author or 'NULL'}'"
                                )

                            # ✅ inline import + realtime index
                            try:
                                _upsert_article_from_extracted(db, source, ex_doc, allow_update=True, verbose_log=log)
                            except Exception as iex:
                                if log:
                                    log(f"[ERROR]#{idx:02d} INLINE-IMPORT exception: {iex}")

                        except DuplicateKeyError:
                            skipped += 1
                            if log:
                                log(f"[SKIP] #{idx:02d} EXTRACTED trùng: {url}")

                else:
                    if log:
                        log(f"[ERROR]#{idx:02d} HTTP={status} url={url}")

                crawl_runs.update_one(
                    {"_id": run_id},
                    {"$set": {"stats.pages": fetched, "stats.saved": saved, "stats.skipped": skipped}},
                )

            except Exception as ex:
                crawl_runs.update_one(
                    {"_id": run_id},
                    {"$push": {"stats_debug": {"url": url, "reason": "exception", "msg": str(ex)}}},
                )
                if log:
                    log(f"[ERROR]#{idx:02d} Exception: {ex}")

        crawl_runs.update_one(
            {"_id": run_id},
            {
                "$set": {
                    "status": "success",
                    "finished_at": now_vn(),
                    "stats.pages": fetched,
                    "stats.saved": saved,
                    "stats.skipped": skipped,
                }
            },
        )
        return {"run_id": str(run_id), "fetched": fetched, "saved": saved, "skipped": skipped}

    except Exception as ex:
        crawl_runs.update_one(
            {"_id": run_id},
            {"$set": {"status": "failed", "finished_at": now_vn()}, "$push": {"stats_debug": {"reason": "exception", "msg": str(ex)}}},
        )
        if log:
            log(f"[ERROR] Exception: {ex}")
        return {"run_id": str(run_id), "error": str(ex)}
