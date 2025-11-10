# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Dict, Any, Optional, List, Set, Callable
from datetime import datetime, timezone
import time, hashlib, re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import feedparser
from pymongo.errors import DuplicateKeyError
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from .rss_parsers import extract_article_vnexpress, extract_article_thanhnien, extract_article_generic
from api.db import get_db

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsCrawler/1.0; +https://example.com/bot)"}

# =========================
# Helper functions
# =========================

def _build_session() -> requests.Session:
    retry = Retry(
        total=3, read=3, connect=3, status=3, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False
    )
    s = requests.Session()
    s.headers.update(HEADERS)
    ad = HTTPAdapter(max_retries=retry)
    s.mount("http://", ad)
    s.mount("https://", ad)
    return s

def _now():
    return datetime.now(timezone.utc)

def _hash_text(txt: Optional[str]) -> Optional[str]:
    if not txt:
        return None
    return "sha256:" + hashlib.sha256(txt.encode("utf-8", errors="ignore")).hexdigest()

PARSERS = {
    "vnexpress": extract_article_vnexpress,
    "thanhnien": extract_article_thanhnien,
}

# =========================
# Author cleaning & extraction
# =========================

DOMAIN_RE = re.compile(r'\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b', re.I)
EMAIL_RE  = re.compile(r'\b\S+@\S+\.\S+\b')

def _sanitize_author(author: Optional[str], base_url: str) -> Optional[str]:
    """
    Trả None nếu author có vẻ là domain/url/email hoặc trùng host của site.
    Ngược lại trả lại tên đã strip.
    """
    if not author:
        return None
    a = author.strip()
    if not a:
        return None

    low = a.lower()
    host = urlparse(base_url or "").netloc.lower()

    # loại email
    if EMAIL_RE.search(a):
        return None

    # loại nếu là URL đầy đủ
    if low.startswith(("http://", "https://")):
        return None

    # loại nếu là domain (vd: thanhnien.vn) hoặc chứa host của site
    if DOMAIN_RE.search(low):
        if (host and host in low) or low == host:
            return None
        if DOMAIN_RE.fullmatch(low):
            return None

    # Loại vài giá trị “thương hiệu” phổ biến
    BRANDY = {"báo thanh niên", "thanhnien", "thanhnien.vn", "vnexpress", "vnexpress.net"}
    if low in BRANDY:
        return None

    return a

def _extract_eic_from_text(text: str) -> Optional[str]:
    """
    Bắt 'Tổng biên tập: Nguyễn Ngọc Toàn' từ body_text (không phân biệt hoa/thường).
    """
    if not text:
        return None
    m = re.search(r'(?im)^\s*Tổng\s*biên\s*tập\s*:\s*([^\n\r|]+)', text)
    if not m:
        return None
    name = m.group(1).strip(' .-|–—')
    return name or None

# =========================
# Main crawl logic
# =========================

def crawl_rss_source(
    source: Dict[str, Any],
    limit: int = 50,
    skip_existing: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    db = get_db()
    crawl_runs = db.crawl_runs
    raw_pages = db.raw_pages
    extracted = db.extracted_articles

    source_id = source.get("_id")
    name_source = source.get("name_source")
    site = source.get("site")
    rss_url = source.get("rss_url")
    base_url = source.get("base_url") or ""
    if not (source_id and name_source and site and rss_url):
        return {"error": "Source thiếu _id/name_source/site/rss_url"}

    parse_func = PARSERS.get(site)
    title_selectors: List[str] = source.get("title_selectors") or []
    body_selectors: List[str] = source.get("body_selectors") or []

    run_id = crawl_runs.insert_one({
        "source_id": source_id,
        "started_at": _now(),
        "finished_at": None,
        "status": "running",
        "stats": {"pages": 0, "saved": 0, "skipped": 0, "errors": 0},
        "stats_debug": []
    }).inserted_id

    session = _build_session()
    fetched = saved = skipped = 0

    try:
        feed = feedparser.parse(rss_url, request_headers=HEADERS)
        entries_full = getattr(feed, "entries", []) if feed else []
        entries = entries_full if (limit is None or limit <= 0) else entries_full[:limit]

        # URL hợp lệ
        feed_urls: List[str] = []
        for e in entries:
            url = getattr(e, "link", None)
            if url and isinstance(url, str) and url.startswith("http"):
                feed_urls.append(url)

        # Skip URL đã có
        existing: Set[str] = set()
        if skip_existing and feed_urls:
            for doc in extracted.find(
                {"source_id": source_id, "source_url": {"$in": feed_urls}},
                {"_id": 0, "source_url": 1}
            ):
                existing.add(doc["source_url"])

        for idx, e in enumerate(entries, start=1):
            url = getattr(e, "link", None)
            if not url or not url.startswith("http"):
                if log: log(f"[SKIP] #{idx:02d} URL không hợp lệ")
                continue
            if skip_existing and url in existing:
                skipped += 1
                if log: log(f"[SKIP] #{idx:02d} EXTRACTED trùng (từ trước): {url}")
                continue

            time.sleep(0.05)
            try:
                resp = session.get(url, timeout=20)
                status = resp.status_code
                ctype = resp.headers.get("Content-Type")
                html = None
                if status == 200:
                    if not resp.encoding: resp.encoding = resp.apparent_encoding
                    html = resp.text
                content_hash = _hash_text(html) if html else None

                # Lưu raw
                raw_doc = {
                    "source_id": source_id, "crawl_run_id": run_id, "url": url,
                    "crawler_time": _now(), "http_status": int(status) if status is not None else None,
                    "content_type": ctype, "raw_html": html, "content_hash": content_hash
                }
                try:
                    raw_pages.insert_one(raw_doc)
                    if log: log(f"[OK]   #{idx:02d} RAW inserted | {url}")
                except DuplicateKeyError:
                    if log: log(f"[SKIP] #{idx:02d} RAW trùng   | {url}")

                if status == 200 and html:
                    fetched += 1
                    # publish time
                    published_at = None
                    if getattr(e, "published_parsed", None):
                        tm = e.published_parsed
                        published_at = datetime(*tm[:6], tzinfo=timezone.utc)

                    title = body_text = None
                    images: List[str] = []
                    author: Optional[str] = None

                    if parse_func:
                        art = parse_func(html, base_url)
                        title = art.get("title")
                        body_text = art.get("body")
                        images = (art.get("images") or [])
                        author = art.get("author") or author

                    if not (title and body_text):
                        art_g = extract_article_generic(html, title_selectors, body_selectors, base_url)
                        title = title or art_g.get("title")
                        body_text = body_text or art_g.get("body")
                        images = images or (art_g.get("images") or [])
                        author = author or art_g.get("author")

                    # fallback RSS summary
                    if not (title and body_text):
                        rss_summary = getattr(e, "summary", None)
                        if rss_summary:
                            summary_text = BeautifulSoup(rss_summary, "html.parser").get_text(" ", strip=True)
                            if summary_text and len(summary_text) >= 30:
                                if not title: title = getattr(e, "title", None)
                                body_text = body_text or summary_text

                    # sanitize và fallback “Tổng biên tập”
                    author = _sanitize_author(author, base_url)
                    if not author and body_text:
                        author = _extract_eic_from_text(body_text)

                    if not (title and body_text):
                        crawl_runs.update_one({"_id": run_id}, {"$push": {"stats_debug": {"url": url, "reason": "no_title_or_body"}}})
                        if log: log(f"[ERROR]#{idx:02d} Thiếu title/body: {url}")
                    else:
                        ex_doc = {
                            "source_id": source_id,
                            "raw_page_id": raw_pages.find_one({"source_id": source_id, "url": url}, {"_id":1})["_id"],
                            "source_url": url,
                            "canonical_title": title,
                            "author": author or None,
                            "published_at": published_at,
                            "summary": None,
                            "body_text": body_text,
                            "body_html": None,
                            "images": images,
                            "topics_raw": [],
                            "copyright_note": None,
                            "content_hash": _hash_text(f"{title}\n{body_text}"),
                            "created_at": _now()
                        }
                        try:
                            extracted.insert_one(ex_doc)
                            saved += 1
                            if log: log(f"[OK]   #{idx:02d} EXTRACTED | title='{title}' | imgs={len(images)} | author='{author or 'NULL'}'")
                        except DuplicateKeyError:
                            skipped += 1
                            if log: log(f"[SKIP] #{idx:02d} EXTRACTED trùng: {url}")
                else:
                    if log: log(f"[ERROR]#{idx:02d} HTTP={status} url={url}")

                crawl_runs.update_one({"_id": run_id},
                    {"$set": {"stats.pages": fetched, "stats.saved": saved, "stats.skipped": skipped}})

            except Exception as ex:
                crawl_runs.update_one({"_id": run_id}, {"$push": {"stats_debug": {"url": url, "reason": "exception", "msg": str(ex)}}})
                if log: log(f"[ERROR]#{idx:02d} Exception: {ex}")

        crawl_runs.update_one({"_id": run_id}, {"$set": {
            "status":"success","finished_at":_now(),
            "stats.pages":fetched,"stats.saved":saved,"stats.skipped":skipped
        }})
        return {"run_id": str(run_id), "fetched": fetched, "saved": saved, "skipped": skipped}

    except Exception as ex:
        crawl_runs.update_one({"_id": run_id},
            {"$set":{"status":"failed","finished_at":_now()}, "$push":{"stats_debug":{"reason":"exception","msg":str(ex)}}})
        if log: log(f"[ERROR] Exception: {ex}")
        return {"run_id": str(run_id), "error": str(ex)}
