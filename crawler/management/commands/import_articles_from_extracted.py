# -*- coding: utf-8 -*-
from django.core.management.base import BaseCommand, CommandError
from api.db import get_db
from datetime import datetime, timezone
from pymongo import ReturnDocument

def _now(): return datetime.now(timezone.utc)

# map slug từ rss_url sang tên category
CATEGORY_RULES = [
    ("thoi-su", "Thời sự"),
    ("giai-tri", "Giải trí"),
    ("the-thao", "Thể thao"),
    ("suc-khoe", "Sức khỏe"),
    ("kinh-doanh", "Kinh doanh"),
    ("the-gioi", "Thế giới"),
    ("giao-duc", "Giáo dục"),
]

def map_category_name_from_rss(rss_url: str) -> str:
    u = (rss_url or "").lower()
    for key, name in CATEGORY_RULES:
        if key in u:
            return name
    return "Uncategorized"

class Command(BaseCommand):
    help = "Import từ extracted_articles sang authors/categories/articles với upsert chống trùng."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=str, help="crawl_sources.name_source cần import")
        parser.add_argument("--limit", type=int, default=0, help="Giới hạn số bài import (0 = không giới hạn)")
        parser.add_argument("--verbose", action="store_true", help="Log chi tiết từng bản ghi")
        parser.add_argument("--update", action="store_true", help="Cho phép update trường đã có khi upsert")

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

        source_id = src["_id"]
        site = src.get("site")
        rss_url = src.get("rss_url") or ""
        if verbose: self.stdout.write(f"Importing from extracted for source='{name_source}' site='{site}'")

        # 1) Category upsert by name
        cat_name = map_category_name_from_rss(rss_url)
        cat = db.categories.find_one_and_update(
            {"name": cat_name},
            {"$setOnInsert": {"name": cat_name}},
            upsert=True, return_document=ReturnDocument.AFTER
        )
        category_id = cat["_id"]

        # 2) Lấy danh sách extracted theo source_id
        q = {"source_id": source_id}
        cur = db.extracted_articles.find(q).sort("created_at", -1)
        if limit and limit > 0:
            cur = cur.limit(limit)

        total = inserted = updated = skipped = 0

        for ex in cur:
            total += 1
            title = ex.get("canonical_title")
            content = ex.get("body_text") or ""
            author_name = ex.get("author")
            external_url = ex.get("source_url")
            published_at = ex.get("published_at")
            if not title or not content or not external_url:
                skipped += 1
                if verbose: self.stdout.write(self.style.WARNING(f"[SKIP] thiếu title/content/url: {external_url}"))
                continue

            # 3) upsert author nếu có (có thể None)
            author_id = None
            if author_name:
                a = db.authors.find_one_and_update(
                    {"name": author_name.strip()},
                    {"$setOnInsert": {"name": author_name.strip()}},
                    upsert=True, return_document=ReturnDocument.AFTER
                )
                author_id = a["_id"]

            # 4) upsert article theo (site, external_url)
            filter_doc = {"site": site, "external_url": external_url}
            set_on_insert = {
                "created_at": _now(),
                "is_deleted": False,
                "status": "published"
            }
            set_doc = {
                "title": title,
                "content": content,
                "author_id": author_id,     # có thể None (schema đã cho phép)
                "category_id": category_id,
                "published_at": published_at,
                "updated_at": _now(),
            }
            if allow_update:
                update_doc = {"$setOnInsert": set_on_insert, "$set": set_doc}
            else:
                update_doc = {"$setOnInsert": {**set_on_insert, **set_doc}}

            res = db.articles.update_one(filter_doc, update_doc, upsert=True)
            if res.matched_count == 0 and res.upserted_id is not None:
                inserted += 1
                if verbose: self.stdout.write(self.style.SUCCESS(f"[OK]   INSERT | {title}"))
            else:
                if allow_update and res.modified_count > 0:
                    updated += 1
                    if verbose: self.stdout.write(self.style.SUCCESS(f"[OK]   UPDATE | {title}"))
                else:
                    skipped += 1
                    if verbose: self.stdout.write(self.style.WARNING(f"[SKIP] EXIST | {title}"))

        self.stdout.write(self.style.SUCCESS(
            f"Import done: total={total} inserted={inserted} updated={updated} skipped={skipped}"
        ))
