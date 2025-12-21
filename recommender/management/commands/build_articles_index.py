# # python manage.py build_articles_index --reset
# # build_articles_index.py
# from django.core.management.base import BaseCommand
# from django.conf import settings
#
# from api.db import get_db
# from recommender.services.embeddings import load_embedder
# from recommender.services.chroma_store import get_client, get_articles_collection
#
# import shutil
# import os
# import re
#
# def _clean(s: str) -> str:
#     if not s:
#         return ""
#     return re.sub(r"\s+", " ", str(s)).strip()
#
# def _build_doc_text(article: dict, max_chars: int = 6000) -> str:
#     title = _clean(article.get("title") or "")
#     content = _clean(article.get("content") or article.get("body") or article.get("full_text") or "")
#     entities = article.get("entities") or []
#     keywords = article.get("keywords") or []
#     ent_text = " ".join([str(x) for x in entities if x]) if isinstance(entities, list) else ""
#     kw_text = " ".join([str(x) for x in keywords if x]) if isinstance(keywords, list) else ""
#     doc = f"{title}\n\nEntities: {ent_text}\nKeywords: {kw_text}\n\n{content}".strip()
#     return doc[:max_chars] if len(doc) > max_chars else doc
#
# class Command(BaseCommand):
#     help = "Build/refresh Chroma vector index for MongoDB articles."
#
#     def add_arguments(self, parser):
#         parser.add_argument("--reset", action="store_true", help="Reset (delete) existing chroma index before building")
#         parser.add_argument("--limit", type=int, default=0, help="Limit number of articles (0 = no limit)")
#         parser.add_argument("--batch", type=int, default=32, help="Batch size for embedding")
#
#     def handle(self, *args, **opts):
#         reset = bool(opts.get("reset"))
#         limit = int(opts.get("limit") or 0)
#         batch = max(1, int(opts.get("batch") or 32))
#
#         chroma_dir = getattr(settings, "CHROMA_DIR")
#         model_name = getattr(settings, "SENTENCE_MODEL", None)
#
#         db = get_db()
#         emb = load_embedder(model_name)
#
#         client = get_client(chroma_dir)
#
#         if reset:
#             # cách 1: delete collection
#             try:
#                 client.delete_collection("articles")
#             except Exception:
#                 pass
#             # cách 2: xoá folder chroma (fallback)
#             try:
#                 if os.path.isdir(chroma_dir):
#                     shutil.rmtree(chroma_dir, ignore_errors=True)
#                 os.makedirs(chroma_dir, exist_ok=True)
#                 client = get_client(chroma_dir)
#             except Exception:
#                 pass
#
#         coll = get_articles_collection(client)
#
#         q = {"is_deleted": {"$ne": True}, "status": "published"}
#         cursor = db["articles"].find(q, {
#             "_id": 1,
#             "title": 1,
#             "content": 1,
#             "entities": 1,
#             "keywords": 1,
#             "published_at": 1,
#             "site": 1,
#             "category_id": 1,
#             "category_child_id": 1,
#             "external_url": 1,
#         }).sort("published_at", -1)
#
#         if limit > 0:
#             cursor = cursor.limit(limit)
#
#         ids, docs, metas = [], [], []
#         total = 0
#
#         def flush():
#             nonlocal ids, docs, metas, total
#             if not ids:
#                 return
#             vecs = emb.encode(docs)
#             vecs = vecs.tolist() if hasattr(vecs, "tolist") else vecs
#
#             # upsert bằng delete+add cho chắc
#             try:
#                 coll.delete(ids=ids)
#             except Exception:
#                 pass
#
#             coll.add(ids=ids, documents=docs, metadatas=metas, embeddings=vecs)
#             total += len(ids)
#             self.stdout.write(self.style.SUCCESS(f"Indexed {total} articles..."))
#             ids, docs, metas = [], [], []
#
#         for a in cursor:
#             aid = str(a["_id"])
#             doc = _build_doc_text(a)
#             if not doc:
#                 continue
#
#             meta = {
#                 "article_id": aid,
#                 "title": a.get("title"),
#                 "site": a.get("site"),
#                 "published_at": a.get("published_at").isoformat() if hasattr(a.get("published_at"), "isoformat") else str(a.get("published_at")),
#                 "category_id": str(a.get("category_id")) if a.get("category_id") else None,
#                 "category_child_id": str(a.get("category_child_id")) if a.get("category_child_id") else None,
#                 "external_url": a.get("external_url"),
#             }
#
#             ids.append(aid)
#             docs.append(doc)
#             metas.append(meta)
#
#             if len(ids) >= batch:
#                 flush()
#
#         flush()
#         self.stdout.write(self.style.SUCCESS(f"Done. Total indexed: {total}"))
