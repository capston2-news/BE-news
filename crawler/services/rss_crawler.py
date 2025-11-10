# -*- coding: utf-8 -*-
# Cào RSS và lưu: crawl_runs, raw_pages, extracted_articles (chỉ ảnh, có author)
from __future__ import annotations
from typing import Dict, Any, Optional, List, Set, Callable
from datetime import datetime, timezone
import time
import hashlib
import re
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import feedparser
from pymongo.errors import DuplicateKeyError
from bs4 import BeautifulSoup

from .rss_parsers import (
    extract_article_vnexpress,
    extract_article_thanhnien,
    extract_article_generic,
)
from api.db import get_db

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsCrawler/1.0; +https://example.com/bot)"}

def _build_session() -> requests.Session:
    retry = Retry(
        total=3, read=3, connect=3, status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    s = requests.Session()
    s.headers.update(HEADERS)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

def _now():
    return datetime.now(timezone.utc)

def _hash_text(txt: Optional[str]) -> Optional[str]:
    if not txt:
        return None
    return "sha256:" + hashlib.sha256(txt.encode("utf-8", errors="ignore")).hexdigest()

def _fetch_html(session: requests.Session, url: str, timeout: int = 20):
    try:
        resp = session.get(url, timeout=timeout)
        status = resp.status_code
        ctype = resp.headers.get("Content-Type")
        if status == 200:
            if not resp.encoding:
                resp.encoding = resp.apparent_encoding
            return status, ctype, resp.text
        return status, ctype, None
    except Exception:
        return None, None, None

# ====== Chuẩn hóa author: loại brand/toà soạn nếu không phải tên người ======
_BRAND_MAP = {
    "vnexpress": ["vnexpress", "vne", "vnepress", "vne express"],
    "thanhnien": ["thanhnien", "thanh nien", "báo thanh niên", "bao thanh nien"],
}
_GENERIC_WORDS = {
    "editorial", "newsroom", "ban biên tập", "tòa soạn", "toà soạn",
    "phóng viên", "biên tập viên"
}

def _normalize_author(author: Optional[str], source: Dict[str, Any]) -> Optional[str]:
    if not author:
        return None
    a = author.strip()
    if not a or len(a) <= 2:
        return None

    low = a.lower()
    if low in _GENERIC_WORDS:
        return None

    site = (source.get("site") or "").lower()
    brand_candidates = set()
    if site and site in _BRAND_MAP:
        brand_candidates.update(_BRAND_MAP[site])

    name_source = (source.get("name_source") or "").lower().replace("_", " ")
    if name_source:
        brand_candidates.add(name_source)

    base_url = source.get("base_url") or ""
    if base_url:
        host = urlparse(base_url).netloc.lower()
        if host:
            brand_candidates.add(host)
            brand_candidates.add(host.replace(".", " "))

    low_norm = re.sub(r"\s+", " ", low).strip()
    for cand in brand_candidates:
        cand_norm = re.sub(r"\s+", " ", cand).strip()
        if not cand_norm:
            continue
        if low_norm == cand_norm or cand_norm in low_norm:
            return None

    if any(k in low_norm for k in ["báo ", "bao ", "newspaper", "press"]):
        for cand in brand_candidates:
            cand_norm = re.sub(r"\s+", " ", cand).strip()
            if cand_norm and cand_norm in low_norm:
                return None

    return a

# site -> parser
PARSERS = {
    "vnexpress": extract_article_vnexpress,
    "thanhnien": extract_article_thanhnien,
}

def crawl_rss_source(
    source: Dict[str, Any],
    limit: int = 50,
    skip_existing: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    source: {_id, name_source, site, base_url, rss_url, is_active, title_selectors?, body_selectors?}
    """
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
    fetched = 0
    saved = 0
    skipped = 0

    try:
        feed = feedparser.parse(rss_url, request_headers=HEADERS)
        entries_full = getattr(feed, "entries", []) if feed else []
        entries = entries_full if (limit is None or limit <= 0) else entries_full[:limit]

        # 1) Thu URL hợp lệ
        feed_urls: List[str] = []
        for e in entries:
            url = getattr(e, "link", None)
            if url and isinstance(url, str) and url.startswith("http"):
                feed_urls.append(url)

        # 2) Skip trước URL đã có
        existing: Set[str] = set()
        if skip_existing and feed_urls:
            for doc in extracted.find(
                {"source_id": source_id, "source_url": {"$in": feed_urls}},
                {"_id": 0, "source_url": 1}
            ):
                existing.add(doc["source_url"])

        # 3) Lặp từng entry
        for idx, e in enumerate(entries, start=1):
            url = getattr(e, "link", None)
            if not url or not url.startswith("http"):
                if log: log(f"[SKIP] #{idx} URL không hợp lệ")
                continue
            if skip_existing and url in existing:
                skipped += 1
                if log: log(f"[SKIP] #{idx} Đã tồn tại extracted: {url}")
                continue

            time.sleep(0.05)
            status, ctype, html = _fetch_html(session, url)
            content_hash = _hash_text(html) if html else None

            # Lưu raw_pages & lấy raw_page_id
            raw_page_id = None
            raw_doc = {
                "source_id": source_id,
                "crawl_run_id": run_id,
                "url": url,
                "crawler_time": _now(),
                "http_status": int(status) if status is not None else None,
                "content_type": ctype,
                "raw_html": html,
                "content_hash": content_hash
            }
            try:
                ins = raw_pages.insert_one(raw_doc)
                raw_page_id = ins.inserted_id
                if log: log(f"[OK]   #{idx} RAW inserted: {url}")
            except DuplicateKeyError:
                existed = raw_pages.find_one({"source_id": source_id, "url": url}, {"_id": 1})
                raw_page_id = existed["_id"] if existed else None
                if log: log(f"[SKIP] #{idx} RAW trùng (đã có): {url}")

            if status == 200 and html:
                fetched += 1

                # Publish time từ RSS (nếu có)
                published_at = None
                if getattr(e, "published_parsed", None):
                    tm = e.published_parsed
                    published_at = datetime(*tm[:6], tzinfo=timezone.utc)

                title = None
                body_text = None
                images: List[str] = []
                author: Optional[str] = None  # KHÔNG fallback từ RSS

                # Parser đặc thù
                if parse_func:
                    art = parse_func(html, base_url)
                    title = art.get("title")
                    body_text = art.get("body")
                    images = (art.get("images") or [])
                    author = art.get("author") or author

                # Generic fallback
                if not (title and body_text):
                    art_g = extract_article_generic(html, title_selectors, body_selectors, base_url)
                    title = title or art_g.get("title")
                    body_text = body_text or art_g.get("body")
                    images = images or (art_g.get("images") or [])
                    author = author or art_g.get("author")

                # Chuẩn hoá author: loại brand/toà soạn -> None nếu không phải tên người
                author = _normalize_author(author, source)

                # Fallback cuối từ RSS summary (nếu trang mỏng)
                if not (title and body_text):
                    rss_summary = getattr(e, "summary", None)
                    if rss_summary:
                        summary_text = BeautifulSoup(rss_summary, "html.parser").get_text(" ", strip=True)
                        if summary_text and len(summary_text) >= 30:
                            if not title:
                                title = getattr(e, "title", None)
                            body_text = body_text or summary_text

                if not (title and body_text):
                    crawl_runs.update_one(
                        {"_id": run_id},
                        {"$push": {"stats_debug": {"url": url, "reason": "no_title_or_body"}}}
                    )
                    if log: log(f"[ERROR]#{idx} Thiếu title/body: {url}")
                elif raw_page_id is not None:
                    ex_doc = {
                        "source_id": source_id,
                        "raw_page_id": raw_page_id,
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
                        if log: log(f"[OK]   #{idx} EXTRACTED saved: {title or url}")
                    except DuplicateKeyError:
                        skipped += 1
                        if log: log(f"[SKIP] #{idx} EXTRACTED trùng: {url}")
            else:
                if log: log(f"[ERROR]#{idx} HTTP={status} url={url}")

            # cập nhật thống kê live
            crawl_runs.update_one(
                {"_id": run_id},
                {"$set": {"stats.pages": fetched, "stats.saved": saved, "stats.skipped": skipped}}
            )

        # Kết thúc run
        crawl_runs.update_one(
            {"_id": run_id},
            {"$set": {
                "status": "success",
                "finished_at": _now(),
                "stats.pages": fetched,
                "stats.saved": saved,
                "stats.skipped": skipped
            }}
        )
        return {"run_id": str(run_id), "fetched": fetched, "saved": saved, "skipped": skipped}

    except Exception as ex:
        crawl_runs.update_one(
            {"_id": run_id},
            {"$set": {"status": "failed", "finished_at": _now()},
             "$inc": {"stats.errors": 1},
             "$push": {"stats_debug": {"reason": "exception", "message": str(ex)}}}
        )
        if log: log(f"[ERROR] Exception: {ex}")
        return {"run_id": str(run_id), "error": str(ex)}
