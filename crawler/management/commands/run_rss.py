# python manage.py run_rss --all --limit 2 --verbose --debug-db
# tất cả nguồn active
# python manage.py run_rss --all --limit 0 --verbose \
#   --auto-import --import-verbose --import-update

# -*- coding: utf-8 -*-
# crawler/management/commands/run_rss.py
# -*- coding: utf-8 -*-

# ✅ load .env sớm để settings/os.getenv có giá trị đúng khi get_db() chạy
# crawler/management/commands/run_rss.py
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import os
from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from api.db import get_db
from crawler.services.rss_crawler import crawl_rss_source


def _db_debug_info():
    uri = getattr(settings, "MONGO_URI", None) or os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or ""
    dbname = getattr(settings, "MONGO_DB", None) or os.getenv("MONGO_DB", "news_db")
    try:
        host = urlparse(uri).hostname or "unknown-host"
    except Exception:
        host = "unknown-host"
    return f"mongo_host={host} db={dbname}"


class Command(BaseCommand):
    help = "Crawl RSS từ collection crawl_sources (realtime import + realtime index nằm trong crawl_rss_source)."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=str, help="crawl_sources.name_source (vd: vnexpress_thoi_su)")
        parser.add_argument("--all", action="store_true", help="Chạy tất cả nguồn is_active=true")
        parser.add_argument("--limit", type=int, default=0, help="0 hoặc âm = không giới hạn")
        parser.add_argument("--verbose", action="store_true", help="Hiện log từng bài (success/skip/error)")
        parser.add_argument("--debug-db", action="store_true", help="In ra host/db đang connect (tránh nhầm DB)")

    def handle(self, *args, **opts):
        if opts.get("debug_db"):
            self.stdout.write(self.style.WARNING(f"[DB] {_db_debug_info()}"))

        db = get_db()
        limit = int(opts.get("limit") or 0)
        verbose = bool(opts.get("verbose"))

        def log_fn(msg: str):
            if msg.startswith("[ERROR]"):
                self.stdout.write(self.style.ERROR(msg))
            elif msg.startswith("[SKIP]"):
                self.stdout.write(self.style.WARNING(msg))
            elif msg.startswith("[OK]") or msg.startswith("[INDEX]"):
                self.stdout.write(self.style.SUCCESS(msg))
            else:
                self.stdout.write(msg)

        active_query = {"is_active": {"$in": [True, "true", "True", 1, "1"]}}

        if opts.get("all"):
            sources = list(db.crawl_sources.find(active_query).sort("name_source", 1))
            if not sources:
                raise CommandError("Không có nguồn nào is_active=true trong crawl_sources (hoặc đang connect nhầm DB).")

            self.stdout.write(self.style.SUCCESS(f"Found {len(sources)} active sources."))

            for src in sources:
                name = src.get("name_source") or str(src.get("_id"))
                try:
                    res = crawl_rss_source(
                        src,
                        limit=limit,
                        skip_existing=True,
                        log=log_fn if verbose else None
                    )
                except Exception as ex:
                    self.stdout.write(self.style.ERROR(f"[{name}] FAILED exception={ex}"))
                    continue

                if "error" in res:
                    self.stdout.write(self.style.ERROR(
                        f"[{name}] FAILED run={res.get('run_id')} error={res.get('error')}"
                    ))
                else:
                    self.stdout.write(self.style.SUCCESS(
                        f"[{name}] OK run={res.get('run_id')} fetched={res.get('fetched',0)} "
                        f"saved={res.get('saved',0)} skipped={res.get('skipped',0)}"
                    ))

        else:
            name = opts.get("source")
            if not name:
                raise CommandError("Cần --source hoặc dùng --all")

            src = db.crawl_sources.find_one({"name_source": name, **active_query})
            if not src:
                raise CommandError(f"Source '{name}' không tồn tại hoặc đang disabled.")

            try:
                res = crawl_rss_source(src, limit=limit, skip_existing=True, log=log_fn if verbose else None)
            except Exception as ex:
                raise CommandError(f"FAILED exception={ex}")

            if "error" in res:
                self.stdout.write(self.style.ERROR(f"FAILED run={res.get('run_id')} error={res.get('error')}"))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"OK run={res.get('run_id')} fetched={res.get('fetched',0)} "
                    f"saved={res.get('saved',0)} skipped={res.get('skipped',0)}"
                ))
