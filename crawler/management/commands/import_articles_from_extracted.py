# -*- coding: utf-8 -*-
from django.core.management.base import BaseCommand, CommandError
from api.db import get_db
from datetime import datetime, timezone
from pymongo import ReturnDocument
from urllib.parse import urlparse
import re, unicodedata
from crawler.utils.time_utils import now_vn

def _now(): return datetime.now(timezone.utc)

# ===== Category mapping linh hoạt theo rss_url + site =====
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
        "xe": "Xe"
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
        "xe": "Xe"
    },
}

_slug_token_re = re.compile(r'([a-z0-9\-]+)\.rss$', re.I)

def _slug_from_rss_url(rss_url: str) -> str:
    path = (urlparse(rss_url).path or "").rstrip("/")
    m = _slug_token_re.search(path)
    if m:
        return m.group(1).lower()
    seg = path.split("/")[-1].lower()
    return seg.replace(".rss", "") or "uncategorized"

def _title_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-")) or "Uncategorized"

def map_category_name_from_rss(rss_url: str, site: str = None) -> str:
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

# ======= LOOKUP-ONLY cho category_child (không auto-insert) =======
def _find_category_child_id_only(db, parent_id, section_name, normalize_fn):
    """
    Chỉ tra cứu category_child đã có theo (category_id, name) hoặc (category_id, slug).
    Không tạo mới. Không tìm thấy -> None.
    """
    if not section_name:
        return None
    name = section_name.strip()
    slug = normalize_fn(name)

    # Ưu tiên name
    doc = db.category_child.find_one({"category_id": parent_id, "name": name}, {"_id": 1})
    if doc:
        return doc["_id"]

    # Fallback slug
    doc = db.category_child.find_one({"category_id": parent_id, "slug": slug}, {"_id": 1})
    if doc:
        return doc["_id"]

    return None

class Command(BaseCommand):
    help = "Import từ extracted_articles sang authors/categories/category_child/articles (lookup-only category_child) + copy images."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=str, help="crawl_sources.name_source cần import")
        parser.add_argument("--limit", type=int, default=0, help="0 = không giới hạn")
        parser.add_argument("--verbose", action="store_true", help="Log chi tiết")
        parser.add_argument("--update", action="store_true", help="Cho phép update khi upsert")

    def handle(self, *args, **opts):
        db = get_db()
        name_source = opts.get("source")
        if not name_source:
            raise CommandError("Cần --source")

        verbose = bool(opts.get("verbose"))
        limit = int(opts.get("limit"))
        allow_update = bool(opts.get("update"))

        src = db.crawl_sources.find_one({"name_source": name_source})
        if not src:
            raise CommandError(f"Không tìm thấy crawl_source: {name_source}")

        site = (src.get("site") or "").strip().lower()
        rss_url = (src.get("rss_url") or "").strip()
        if verbose:
            self.stdout.write(f"Importing from extracted for source='{name_source}' site='{site}'")

        # 1) parent category (động theo rss_url + site)
        parent_name = map_category_name_from_rss(rss_url, site)
        parent_doc = db.categories.find_one_and_update(
            {"name": parent_name},
            {"$setOnInsert": {"name": parent_name, "slug": _slugify(parent_name), "created_at": now_vn()}},
            upsert=True, return_document=ReturnDocument.AFTER
        )
        parent_id = parent_doc["_id"]

        # 2) cursor
        q = {"source_id": src["_id"]}
        cur = db.extracted_articles.find(q).sort("created_at", -1)
        if limit and limit > 0: cur = cur.limit(limit)

        total = inserted = updated = skipped = 0

        for ex in cur:
            total += 1
            title = ex.get("canonical_title")
            content = ex.get("body_text") or ""
            author_name = ex.get("author")
            external_url = ex.get("source_url")
            published_at = ex.get("published_at")
            images = ex.get("images") or []
            section = (ex.get("section") or "").strip()

            if not title or not content or not external_url:
                skipped += 1
                if verbose: self.stdout.write(self.style.WARNING(f"[SKIP] thiếu title/content/url: {external_url}"))
                continue

            # author upsert (author có thể None)
            author_id = None
            if author_name:
                a = db.authors.find_one_and_update(
                    {"name": author_name.strip()},
                    {"$setOnInsert": {"name": author_name.strip(), "created_at": now_vn()}},
                    upsert=True, return_document=ReturnDocument.AFTER
                )
                author_id = a["_id"]

            # ===== lookup-only category_child =====
            category_child_id = None
            if section and not _is_same_section_as_parent(section, parent_name):
                category_child_id = _find_category_child_id_only(db, parent_id, section, _slugify)
                # Không tìm thấy -> để None (KHÔNG tạo mới)

            # upsert article
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
            update_doc = {"$setOnInsert": set_on_insert, "$set": set_doc} if allow_update \
                         else {"$setOnInsert": {**set_on_insert, **set_doc}}

            res = db.articles.update_one(filter_doc, update_doc, upsert=True)
            if res.matched_count == 0 and res.upserted_id is not None:
                inserted += 1
                if verbose: self.stdout.write(self.style.SUCCESS(f"[OK]   INSERT | {title} | child={section or '-'}"))
            else:
                if allow_update and res.modified_count > 0:
                    updated += 1
                    if verbose: self.stdout.write(self.style.SUCCESS(f"[OK]   UPDATE | {title} | child={section or '-'}"))
                else:
                    skipped += 1
                    if verbose: self.stdout.write(self.style.WARNING(f"[SKIP] EXIST | {title}"))

        self.stdout.write(self.style.SUCCESS(
            f"Import done: total={total} inserted={inserted} updated={updated} skipped={skipped}"
        ))
