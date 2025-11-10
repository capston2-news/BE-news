# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs
import re

# =========================
# Helpers chung
# =========================

def _first_text(soup: BeautifulSoup, selectors: List[str]) -> Optional[str]:
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            t = node.get_text(" ", strip=True)
            if t:
                return t
    return None

def _gather_paragraphs(root) -> List[str]:
    texts: List[str] = []
    if not root:
        return texts
    for p in root.select("p"):
        t = p.get_text(" ", strip=True)
        if not t:
            continue
        tl = t.lower()
        if len(t) >= 8 and "bản quyền" not in tl:
            texts.append(t)
    return texts

# =========================
# Ảnh: thu & lọc & ưu tiên
# =========================

def _collect_images(soup: BeautifulSoup, base_url: str) -> List[str]:
    imgs = set()

    # 1) Meta image
    for sel in [
        'meta[property="og:image"]', 'meta[name="og:image"]',
        'meta[name="twitter:image"]', 'meta[property="twitter:image"]'
    ]:
        m = soup.select_one(sel)
        if m and m.get("content"):
            imgs.add(urljoin(base_url, m["content"].strip()))

    # 2) <img> & lazy-load
    for img in soup.find_all("img"):
        cand = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy")
            or (img.get("srcset") or "").split(" ")[0]
        )
        if not cand:
            continue
        if cand.startswith("data:"):
            continue
        if not cand.startswith(("http", "/")):
            continue
        imgs.add(urljoin(base_url, cand.strip()))

    # 3) <figure><img>
    for fig in soup.find_all("figure"):
        im = fig.find("img")
        if im:
            cand = im.get("src") or im.get("data-src") or im.get("data-original")
            if cand:
                imgs.add(urljoin(base_url, cand.strip()))

    # ---- Lọc asset tĩnh/icon/logo ----
    ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
    HOST_BLACKLIST_SUBSTR = (
        "static.", "cdn-cgi",
    )
    PATH_BLACKLIST_SUBSTR = (
        "/static/", "/assets/", "/sprites/", "/icons/", "/favicon",
        "/emoji/", "/emoticon/", "/svg/",
        "/ads/", "/adserver/", "/banner/",
        "/tracking/", "/analytics/",
    )
    NAME_BLACKLIST_KEYWORDS = (
        "logo", "icon", "sprite", "placeholder", "blank", "transparent",
        "avatar", "ava_", "profile", "userpic",
        "thumb", "thumbnail", "small", "micro", "1x1", "pixel"
    )

    def is_good(u: str) -> bool:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()  # không gồm query

        if not (path.endswith(ALLOWED_EXT) or "upload" in u.lower()):
            return False
        if any(bad in host for bad in HOST_BLACKLIST_SUBSTR):
            return False
        if any(bad in path for bad in PATH_BLACKLIST_SUBSTR):
            return False
        fname = path.rsplit("/", 1)[-1]
        if any(kw in fname for kw in NAME_BLACKLIST_KEYWORDS):
            return False
        return True

    out = [u for u in imgs if is_good(u)]

    # ---- Chấm điểm ưu tiên ảnh nội dung ----
    def _img_score(u: str) -> int:
        s = 0
        ul = u.lower()
        p = urlparse(u)
        host = (p.netloc or "").lower()
        path = (p.path or "").lower()
        q = parse_qs(p.query or "")

        if "upload" in ul:
            s += 2
        if any(dom in host for dom in ["vnecdn.net", "vnexpress", "thanhnien"]):
            s += 2
        try:
            if "w" in q and int(q["w"][0]) >= 600:
                s += 1
            elif "width" in q and int(q["width"][0]) >= 600:
                s += 1
        except Exception:
            pass
        filename = path.rsplit("/", 1)[-1]
        if any(k in filename for k in ["thumb", "small", "avatar", "ava_", "icon", "logo"]):
            s -= 2
        return s

    seen = set()
    uniq = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    uniq.sort(key=lambda x: _img_score(x), reverse=True)

    return uniq[:10]  # tối đa 10 ảnh

# =========================
# Author extraction
# =========================

_AUTHOR_META_SELECTORS = [
    'meta[name="author"]',
    'meta[property="author"]',
    'meta[property="article:author"]',
]
_AUTHOR_NODE_SELECTORS_COMMON = [
    ".author", ".author-name", ".author__name", ".byline", ".detail-author",
    ".details__author", ".article__author", ".bold.author"
]
_AUTHOR_PATTERNS = [
    re.compile(r"(?:Tác giả|Biên tập|Theo|Bài)\s*[:\-]\s*([A-ZÀ-Ỹ][^|,\n]+)", re.IGNORECASE),
]

def _extract_author(soup: BeautifulSoup) -> Optional[str]:
    for sel in _AUTHOR_META_SELECTORS:
        m = soup.select_one(sel)
        if m and m.get("content"):
            name = m["content"].strip()
            if name:
                return name

    for sel in _AUTHOR_NODE_SELECTORS_COMMON:
        node = soup.select_one(sel)
        if node:
            t = node.get_text(" ", strip=True)
            if t:
                for pat in _AUTHOR_PATTERNS:
                    m = pat.search(t)
                    if m:
                        cand = m.group(1).strip(" .")
                        if cand:
                            return cand
                return t.strip(" .")

    full = soup.get_text(" ", strip=True)
    if full:
        for pat in _AUTHOR_PATTERNS:
            m = pat.search(full)
            if m:
                cand = m.group(1).strip(" .")
                if cand:
                    return cand
    return None

def _force_eic_if_present(soup: BeautifulSoup, current_author: Optional[str]) -> Optional[str]:
    full = soup.get_text(" ", strip=True) or ""
    low = full.lower()
    if "tổng biên tập" in low and "nguyễn ngọc toàn" in low:
        return "Nguyễn Ngọc Toàn"
    return current_author

# =========================
# Parsers theo site
# =========================

def extract_article_vnexpress(html: str, base_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title = _first_text(soup, ["h1.title-detail", "h1"])
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    body = None
    for sel in [
        ".fck_detail",
        ".sidebar-1 .detail-cmain .detail-content",
        ".sidebar-1 .detail-content",
        ".article-detail .article-content",
        "article .content-detail",
        "article"
    ]:
        node = soup.select_one(sel)
        texts = _gather_paragraphs(node)
        if texts:
            body = "\n\n".join(texts)
            break
    if not body:
        texts = _gather_paragraphs(soup)
        body = "\n\n".join(texts) if texts else None

    images = _collect_images(soup, base_url)
    author = _force_eic_if_present(soup, _extract_author(soup))

    return {"title": title, "body": body, "images": images, "author": author}

def extract_article_thanhnien(html: str, base_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title = _first_text(soup, [
        "h1.title-detail",
        "h1.article-title",
        "h1.details__headline",
        "h1.details__title",
        "h1"
    ])
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    body = None
    for sel in [
        ".article__main-content",
        ".detail-content__body",
        ".details__content .cms-body",
        ".details__content",
        ".article__body",
        ".article-content",
        "article"
    ]:
        node = soup.select_one(sel)
        texts = _gather_paragraphs(node)
        if texts:
            body = "\n\n".join(texts)
            break
    if not body:
        texts = _gather_paragraphs(soup)
        body = "\n\n".join(texts) if texts else None

    images = _collect_images(soup, base_url)
    author = _force_eic_if_present(soup, _extract_author(soup))

    return {"title": title, "body": body, "images": images, "author": author}

# =========================
# Generic fallback
# =========================

def extract_article_generic(html: str, title_selectors: List[str], body_selectors: List[str], base_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title = None
    if title_selectors:
        title = _first_text(soup, title_selectors)
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    body = None
    for sel in (body_selectors or []):
        node = soup.select_one(sel)
        texts = _gather_paragraphs(node)
        if texts:
            body = "\n\n".join(texts)
            break
    if not body:
        texts = _gather_paragraphs(soup)
        body = "\n\n".join(texts) if texts else None

    images = _collect_images(soup, base_url)
    author = _force_eic_if_present(soup, _extract_author(soup))

    return {"title": title, "body": body, "images": images, "author": author}
