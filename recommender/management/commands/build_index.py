from django.core.management.base import BaseCommand
from django.conf import settings
from recommender.services.embeddings import load_embedder, encode_texts
from recommender.services.chroma_store import get_client, get_articles_collection, upsert_articles
from recommender.services.utils import get_all_article_ids, fetch_articles, article_to_text, get_category_name

# ⭐️ Cần import MongoClient nếu chưa có trong các file services đã import
try:
    from pymongo import MongoClient
except ImportError:
    # Nếu không có, bạn cần đảm bảo pymongo được cài đặt
    pass


class Command(BaseCommand):
    help = "Build/refresh ChromaDB index for articles"

    def _meta_safe(self, db_instance, d: dict) -> dict:
        """Helper function to safely extract metadata."""
        cat_name = (d.get("category_name") or "").strip()

        # FIX: Sử dụng db_instance thay vì settings.MONGO_DB (string)
        if not cat_name and d.get("category_id"):
            cat_name = get_category_name(db_instance, d.get("category_id"))

        pub_ts = None
        pub = d.get("published_at")
        if pub:
            try:
                pub_ts = int(pub.timestamp())
            except:
                pub_ts = None

        return {
            "mongo_id": str(d["_id"]),
            "title": (d.get("title") or "").strip(),
            "author": (d.get("author") or "").strip(),
            "source": (d.get("source") or "").strip(),
            "topics": ", ".join([str(x) for x in (d.get("topics") or [])]),
            "category": (d.get("category_name") or "").strip(),
            "category_child": (d.get("category_child_name") or "").strip(),
            "publishd_tse": pub_ts,
        }

    def handle(self, *args, **kwargs):

        # ⭐️ FIX START: Khởi tạo PyMongo Client và lấy Database Instance
        try:
            # Giả định settings.MONGO_URI chứa connection string
            mongo_client = MongoClient(settings.MONGO_URI)

            # Lấy đối tượng database instance bằng cách sử dụng tên database (settings.MONGO_DB là tên)
            db = mongo_client[settings.MONGO_DB]

        except AttributeError:
            self.stdout.write(
                self.style.ERROR("FATAL: MONGO_URI hoặc MONGO_DB không được định nghĩa trong settings.py"))
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"FATAL: Không thể kết nối tới MongoDB: {e}"))
            return

        embedder = load_embedder(settings.SENTENCE_MODEL)

        # FIX: Truyền đối tượng db instance đã kết nối vào hàm
        ids = get_all_article_ids(db)
        batch = 512
        client = get_client(settings.CHROMA_DIR)
        coll = get_articles_collection(client)

        self.stdout.write(self.style.WARNING(f"Indexing {len(ids)} articles to Chroma..."))
        for i in range(0, len(ids), batch):
            chunk_ids = ids[i:i + batch]

            # FIX: Truyền đối tượng db instance
            docs = fetch_articles(db, chunk_ids)
            docs = [d for d in docs if d]
            if not docs: continue

            documents = [article_to_text(d) for d in docs]
            embeddings = encode_texts(embedder, documents)
            try:
                embeddings = embeddings.tolist()
            except:
                pass

            # FIX: Gọi phương thức _meta_safe đã sửa
            metadatas = [self._meta_safe(db, d) for d in docs]

            upsert_articles(
                coll,
                ids=[str(d["_id"]) for d in docs],
                embeddings=embeddings,
                metadatas=metadatas,
                documents=documents,
            )
            self.stdout.write(self.style.NOTICE(f"Upsert {len(docs)} docs [{i}-{i + len(docs) - 1}]"))

        self.stdout.write(self.style.SUCCESS("✅ Done building Chroma index"))