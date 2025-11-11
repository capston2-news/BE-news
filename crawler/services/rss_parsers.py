# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import List, Dict, Any, Optional, Iterable
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

def _meta_images(soup: BeautifulSoup, base_url: str) -> List[str]:
    out = []
    for sel in [
        'meta[property="og:image"]', 'meta[name="og:image"]',
        'meta[name="twitter:image"]', 'meta[property="twitter:image"]'
    ]:
        m = soup.select_one(sel)
        if m and m.get("content"):
            val = m["content"].strip()
            # 🚫 bỏ data: URI
            if val.startswith("data:"):
                continue
            out.append(urljoin(base_url, val))
    # unique
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u); uniq.append(u)
    return uniq

# =========================
# Body containers (chỉ ở trong bài)
# =========================

# VNExpress
VNE_BODY_CANDIDATES = [
    ".fck_detail",
    ".sidebar-1 .detail-cmain .detail-content",
    ".sidebar-1 .detail-content",
    ".article-detail .article-content",
    "article .content-detail",
]

# Thanh Niên
TN_BODY_CANDIDATES = [
    ".article__main-content",
    ".detail-content__body",
    ".details__content .cms-body",
    ".details__content",
    ".article__body",
    ".article-content",
]

# fallback rất nhẹ (nếu 2 bộ trên không khớp)
GENERIC_BODY_CANDIDATES = [
    "article .content", "article .post-content", "article .entry-content", "article"
]

# Loại các vùng không phải nội dung bài, ngay cả khi chúng nằm trong body
EXCLUDE_WITHIN_BODY = [
    ".related", ".article__related", ".list__related", ".box__related",
    ".recommend", ".suggest", ".mostread", ".most-view", ".trending",
    ".aside", ".sidebar", ".header", ".footer", ".breadcrumb",
    ".ads", ".ad", ".adbox", ".banner", ".qc", ".quangcao",
    ".social", ".tags", ".author", ".comment", ".newsletter",
]

# =========================
# Ảnh: thu & lọc & ưu tiên (CHỈ trong body)
# =========================

_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")

HOST_BLACKLIST_SUBSTR = ("cdn-cgi", )
PATH_BLACKLIST_SUBSTR = (
    "/static/", "/assets/", "/sprites/", "/icons/", "/favicon",
    "/emoji/", "/emoticon/", "/svg/",
    "/ads/", "/adserver/", "/banner/",
    "/tracking/", "/analytics/",
    "/image/ava"
)
NAME_BLACKLIST_KEYWORDS = (
    "logo","icon","sprite","placeholder","blank","transparent",
    "avatar","ava_","profile","userpic","micro","1x1","pixel"
)
EXACT_BLACKLIST = {
    "https://static.thanhnien.com.vn/thanhnien.vn/image/ava_inter.png"
}

def _pick_from_srcset(srcset: str) -> Optional[str]:
    try:
        parts = [p.strip() for p in srcset.split(",") if p.strip()]
        best_url, best_score = None, -1
        for part in parts:
            seg = part.split()
            url = seg[0]
            desc = seg[1] if len(seg) > 1 else ""
            score = 0
            if desc.endswith("w"):
                try:
                    score = int(desc[:-1])
                except:
                    score = 0
            elif desc.endswith("x"):
                try:
                    score = int(float(desc[:-1]) * 1000)
                except:
                    score = 0
            if score > best_score:
                best_url, best_score = url, score
        return best_url
    except:
        return None

def _candidate_from_style(style_val: str) -> Optional[str]:
    if not style_val: return None
    m = re.search(r'background-image\s*:\s*url\(([^)]+)\)', style_val, flags=re.I)
    if not m: return None
    u = m.group(1).strip(' "\'')
    # 🚫 loại data:
    if u.startswith("data:"): return None
    return u

def _get_img_candidate(tag) -> Optional[str]:
    # thứ tự ưu tiên các attr “lazy”
    for attr in ("src", "data-zoom-src", "data-src", "data-original", "data-splide-lazy", "data-lazy"):
        v = tag.get(attr)
        if v:
            if v.startswith("data:"):  # 🚫
                return None
            return v
    for attr in ("srcset", "data-srcset"):
        ss = tag.get(attr)
        if ss:
            picked = _pick_from_srcset(ss)
            if picked and not picked.startswith("data:"):  # 🚫
                return picked
    st = tag.get("style")
    if st:
        v = _candidate_from_style(st)
        if v:
            return v
    return None

def _looks_like_image_url(u: str) -> bool:
    low = u.lower()
    if any(low.split("?")[0].endswith(ext) for ext in _IMG_EXT):
        return True
    # một số CDN không có đuôi nhưng có tham số kiểu ảnh
    if any(k in low for k in ["format=webp","image/","img=","photo=","picture="]):
        return True
    return False

def _filter_good_urls(urls: Iterable[str]) -> List[str]:
    out = []
    for u in urls:
        if not u or u in EXACT_BLACKLIST:
            continue

        # 🚫 BỎ QUA data URI & scheme lạ
        if u.startswith("data:"):
            continue

        p = urlparse(u)
        scheme = (p.scheme or "").lower()
        if scheme and scheme not in ("http", "https"):
            continue

        host = (p.netloc or "").lower()
        path = (p.path or "").lower()

        if not (_looks_like_image_url(u)):
            continue
        if any(bad in host for bad in HOST_BLACKLIST_SUBSTR):
            continue
        if any(bad in path for bad in PATH_BLACKLIST_SUBSTR):
            continue

        fname = path.rsplit("/", 1)[-1]
        if any(kw in fname for kw in NAME_BLACKLIST_KEYWORDS):
            continue

        # Bỏ ảnh rất nhỏ qua query
        try:
            q = parse_qs(p.query or "")
            if "w" in q and int(q["w"][0]) < 320: continue
            if "width" in q and int(q["width"][0]) < 320: continue
            if "h" in q and int(q["h"][0]) < 320: continue
            if "height" in q and int(q["height"][0]) < 320: continue
        except:
            pass

        out.append(u)
    return out

def _score_image(u: str) -> int:
    s = 0
    p = urlparse(u)
    host = (p.netloc or "").lower()
    q = parse_qs(p.query or "")
    if any(dom in host for dom in ["vnecdn.net", "vnexpress", "thanhnien"]):
        s += 2
    try:
        if "w" in q and int(q["w"][0]) >= 800:
            s += 1
        elif "width" in q and int(q["width"][0]) >= 800:
            s += 1
    except:
        pass
    return s

def _dedup_and_rank(urls: Iterable[str]) -> List[str]:
    seen, uniq = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); uniq.append(u)
    uniq.sort(key=_score_image, reverse=True)
    return uniq

def _collect_images_strictly_inside(body_node, base_url: str) -> List[str]:
    """
    Chỉ lấy ảnh NẰM BÊN TRONG body_node.
    Không quét toàn trang, không merge ngoài body.
    """
    if not body_node:
        return []

    # loại img nằm trong các vùng loại trừ (related/ads/sidebar ...) DÙ CHÚNG ở trong body_node
    excluded_imgs = set()
    for sel in EXCLUDE_WITHIN_BODY:
        for im in body_node.select(f"{sel} img"):
            excluded_imgs.add(im)

    urls = set()

    # IMG
    for img in body_node.find_all("img"):
        if img in excluded_imgs: continue
        cand = _get_img_candidate(img)
        if not cand: continue
        if not cand.startswith(("http", "/")): continue
        urls.add(urljoin(base_url, cand.strip()))

    # PICTURE/SOURCE
    for pic in body_node.find_all("picture"):
        for src in pic.find_all("source"):
            cand = _get_img_candidate(src)
            if cand and cand.startswith(("http","/")):
                urls.add(urljoin(base_url, cand.strip()))

    # FIGURE
    for fig in body_node.find_all("figure"):
        im = fig.find("img")
        if im and im not in excluded_imgs:
            cand = _get_img_candidate(im)
            if cand:
                urls.add(urljoin(base_url, cand.strip()))

    # A[href=*.jpg|png|webp|avif]
    for a in body_node.find_all("a", href=True):
        href = a["href"].strip()
        if _looks_like_image_url(href):
            urls.add(urljoin(base_url, href))

    # style=background-image
    for node in body_node.find_all(style=True):
        cand = _candidate_from_style(node.get("style") or "")
        if cand and cand.startswith(("http","/")):
            urls.add(urljoin(base_url, cand.strip()))

    return _dedup_and_rank(_filter_good_urls(urls))[:20]

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

# =========================
# Parsers theo site
# =========================

def _pick_body_node_for_vne(soup: BeautifulSoup):
    for sel in VNE_BODY_CANDIDATES:
        node = soup.select_one(sel)
        if node:
            return node
    return None

def _pick_body_node_for_tn(soup: BeautifulSoup):
    for sel in TN_BODY_CANDIDATES:
        node = soup.select_one(sel)
        if node:
            return node
    return None

def _pick_body_node_generic(soup: BeautifulSoup):
    for sel in GENERIC_BODY_CANDIDATES:
        node = soup.select_one(sel)
        if node:
            return node
    return None

def extract_article_vnexpress(html: str, base_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = _first_text(soup, ["h1.title-detail", "h1"]) \
        or (soup.title.string.strip() if soup.title and soup.title.string else None)

    body_node = _pick_body_node_for_vne(soup) or _pick_body_node_generic(soup)
    body = None
    if body_node:
        texts = _gather_paragraphs(body_node)
        if texts:
            body = "\n\n".join(texts)
    if not body:
        # không có body → vẫn lấy text toàn trang nhưng KHÔNG dùng để lấy ảnh
        texts = _gather_paragraphs(soup)
        body = "\n\n".join(texts) if texts else None

    # Ảnh: chỉ trong body_node; nếu không có, fallback 1 ảnh từ meta
    images = _collect_images_strictly_inside(body_node, base_url) if body_node else []
    if not images:
        images = _meta_images(soup, base_url)[:1]

    author = _extract_author(soup)

    return {"title": title, "body": body, "images": images, "author": author}

def extract_article_thanhnien(html: str, base_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    title = _first_text(soup, [
        "h1.title-detail",
        "h1.article-title",
        "h1.details__headline",
        "h1.details__title",
        "h1"
    ]) or (soup.title.string.strip() if soup.title and soup.title.string else None)

    body_node = _pick_body_node_for_tn(soup) or _pick_body_node_generic(soup)
    body = None
    if body_node:
        texts = _gather_paragraphs(body_node)
        if texts:
            body = "\n\n".join(texts)
    if not body:
        texts = _gather_paragraphs(soup)
        body = "\n\n".join(texts) if texts else None

    images = _collect_images_strictly_inside(body_node, base_url) if body_node else []
    if not images:
        images = _meta_images(soup, base_url)[:1]

    author = _extract_author(soup)

    return {"title": title, "body": body, "images": images, "author": author}

# =========================
# Generic fallback
# =========================

def extract_article_generic(html: str, title_selectors: List[str], body_selectors: List[str], base_url: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")

    title = _first_text(soup, title_selectors) if title_selectors else None
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()

    body_node = None
    for sel in (body_selectors or []):
        node = soup.select_one(sel)
        if node:
            body_node = node
            break
    if not body_node:
        body_node = _pick_body_node_generic(soup)

    body = None
    if body_node:
        texts = _gather_paragraphs(body_node)
        if texts:
            body = "\n\n".join(texts)
    if not body:
        texts = _gather_paragraphs(soup)
        body = "\n\n".join(texts) if texts else None

    images = _collect_images_strictly_inside(body_node, base_url) if body_node else []
    if not images:
        images = _meta_images(soup, base_url)[:1]

    author = _extract_author(soup)
    return {"title": title, "body": body, "images": images, "author": author}
