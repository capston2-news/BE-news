# python manage.py run_rss --all --limit 2 --auto-import --verbose

# tất cả nguồn active
# python manage.py run_rss --all --limit 0 --verbose \
#   --auto-import --import-verbose --import-update

# -*- coding: utf-8 -*-
from django.core.management.base import BaseCommand, CommandError
from django.core.management import call_command
from api.db import get_db
from crawler.services.rss_crawler import crawl_rss_source

class Command(BaseCommand):
    help = "Crawl RSS từ collection crawl_sources. Hỗ trợ --auto-import."

    def add_arguments(self, parser):
        parser.add_argument("--source", type=str, help="crawl_sources.name_source (vd: vnexpress_thoi_su)")
        parser.add_argument("--all", action="store_true", help="Chạy tất cả nguồn is_active=true")
        parser.add_argument("--limit", type=int, default=0, help="0 hoặc âm = không giới hạn")
        parser.add_argument("--verbose", action="store_true", help="Hiện log từng bài (success/skip/error)")
        parser.add_argument("--auto-import", action="store_true", help="Sau khi cào xong tự import articles")
        parser.add_argument("--import-verbose", action="store_true", help="Log chi tiết khi import")
        parser.add_argument("--import-update", action="store_true", help="Cho phép update trường đã có khi upsert")

    def handle(self, *args, **opts):
        db = get_db()
        limit = int(opts["limit"])
        verbose = bool(opts.get("verbose"))
        do_import = bool(opts.get("auto_import"))
        import_verbose = bool(opts.get("import_verbose"))
        import_update = bool(opts.get("import_update"))

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
                    self.stdout.write(self.style.ERROR(f"[{name}] FAILED run={res.get('run_id')} error={res['error']}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"[{name}] OK run={res['run_id']} fetched={res['fetched']} saved={res['saved']} skipped={res.get('skipped',0)}"))
            if do_import:
                self.stdout.write("Auto-import: bắt đầu ánh xạ vào authors/categories/articles ...")
                for src in sources:
                    src_name = src.get("name_source")
                    try:
                        call_command(
                            "import_articles_from_extracted",
                            source=src_name,
                            verbose=import_verbose,
                            update=import_update
                        )
                    except Exception as ex:
                        self.stdout.write(self.style.ERROR(f"Auto-import lỗi với source={src_name}: {ex}"))
                self.stdout.write("Auto-import: hoàn tất.")
        else:
            name = opts.get("source")
            if not name:
                raise CommandError("Cần --source hoặc dùng --all")
            src = db.crawl_sources.find_one({"name_source": name, "is_active": True})
            if not src:
                raise CommandError(f"Source '{name}' không tồn tại hoặc đang disabled.")
            res = crawl_rss_source(src, limit=limit, skip_existing=True, log=log_fn if verbose else None)
            if "error" in res:
                self.stdout.write(self.style.ERROR(f"FAILED run={res.get('run_id')} error={res['error']}"))
            else:
                self.stdout.write(self.style.SUCCESS(f"OK run={res['run_id']} fetched={res['fetched']} saved={res['saved']} skipped={res.get('skipped',0)}"))
            if do_import:
                self.stdout.write("Auto-import: bắt đầu ánh xạ vào authors/categories/articles ...")
                try:
                    call_command(
                        "import_articles_from_extracted",
                        source=name,
                        verbose=import_verbose,
                        update=import_update
                    )
                except Exception as ex:
                    self.stdout.write(self.style.ERROR(f"Auto-import lỗi với source={name}: {ex}"))
                self.stdout.write("Auto-import: hoàn tất.")
