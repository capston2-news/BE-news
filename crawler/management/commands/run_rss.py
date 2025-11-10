# -*- coding: utf-8 -*-
# Tạo lệnh manage.py: ví dụ
#   python manage.py run_rss --all --limit 50
#   python manage.py run_rss --sources vnexpress_moi_nhat,thanhnien_thoi_su --limit 50
#   python manage.py run_rss --source vnexpress_moi_nhat --limit 30
#   python manage.py run_rss --all --limit 0 --verbose (không giới hạn, hiện log chi tiết)

from django.core.management.base import BaseCommand, CommandError
from api.db import get_db
from crawler.services.rss_crawler import crawl_rss_source

class Command(BaseCommand):
    help = "Crawl RSS từ collection crawl_sources (hỗ trợ vnexpress, thanhnien)."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=str, help="crawl_sources.name_source (vd: vnexpress_thoi_su)")
        parser.add_argument("--all", action="store_true", help="Chạy tất cả nguồn is_active=true")
        parser.add_argument("--limit", type=int, default=0, help="0 hoặc âm = không giới hạn")
        parser.add_argument("--verbose", action="store_true", help="Hiện log từng bài (success/skip/error)")

    def handle(self, *args, **opts):
        db = get_db()
        limit = int(opts["limit"])
        verbose = bool(opts.get("verbose"))

        def log_fn(msg: str):
            if msg.startswith("[ERROR]"):
                self.stdout.write(self.style.ERROR(msg))
            elif msg.startswith("[SKIP]"):
                self.stdout.write(self.style.WARNING(msg))
            elif msg.startswith("[OK]"):
                self.stdout.write(self.style.SUCCESS(msg))
            else:
                self.stdout.write(msg)

        if opts.get("all"):
            sources = list(db.crawl_sources.find({"is_active": True}))
            if not sources:
                raise CommandError("Không có nguồn nào is_active=true trong crawl_sources.")
            for src in sources:
                name = src.get("name_source")
                res = crawl_rss_source(src, limit=limit, skip_existing=True, log=log_fn if verbose else None)
                if "error" in res:
                    self.stdout.write(self.style.ERROR(
                        f"[{name}] FAILED run={res.get('run_id')} error={res['error']}"
                    ))
                else:
                    self.stdout.write(self.style.SUCCESS(
                        f"[{name}] OK run={res['run_id']} fetched={res['fetched']} "
                        f"saved={res['saved']} skipped={res.get('skipped', 0)}"
                    ))
        else:
            name = opts.get("source")
            if not name:
                raise CommandError("Cần --source hoặc dùng --all")
            src = db.crawl_sources.find_one({"name_source": name, "is_active": True})
            if not src:
                raise CommandError(f"Source '{name}' không tồn tại hoặc đang disabled.")
            res = crawl_rss_source(src, limit=limit, skip_existing=True, log=log_fn if verbose else None)
            if "error" in res:
                self.stdout.write(self.style.ERROR(
                    f"FAILED run={res.get('run_id')} error={res['error']}"
                ))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"OK run={res['run_id']} fetched={res['fetched']} "
                    f"saved={res['saved']} skipped={res.get('skipped', 0)}"
                ))
