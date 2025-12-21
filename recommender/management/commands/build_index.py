# # build_index.py
# from __future__ import annotations
#
# from django.core.management.base import BaseCommand
# from django.conf import settings
# from pymongo import UpdateOne
# from datetime import datetime, timezone
#
# from keybert import KeyBERT
# from api.permissions import AllowAny, IsAuthenticated, RoleRequired
# from recommender.services.embeddings import load_embedder, encode_texts
# from recommender.services.chroma_store import get_client, get_articles_collection, upsert_articles
# from recommender.services.utils import get_all_article_ids, fetch_articles, article_to_text
# from recommender.services.entities import extract_entities_proper, extract_keywords_keybert
# from api.db import get_db
#
#
# def _to_iso_utc(dt) -> str:
#     if not dt:
#         return ""
#     if isinstance(dt, str):
#         return dt
#     if isinstance(dt, datetime):
#         if dt.tzinfo is None:
#             dt = dt.replace(tzinfo=timezone.utc)
#         else:
#             dt = dt.astimezone(timezone.utc)
#         return dt.replace(tzinfo=None).isoformat()
#     return ""
#
#
# def _resolve_category_name(db, cat_id) -> str:
#     if not cat_id:
#         return ""
#     try:
#         doc = db["categories"].find_one({"_id": cat_id}, {"name": 1, "title": 1})
#         if doc:
#             return (doc.get("name") or doc.get("title") or "").strip()
#     except Exception:
#         pass
#     return ""
#
#
# def _meta_safe(d: dict, db) -> dict:
#     cat_name = (d.get("category_name") or "").strip()
#     if not cat_name and d.get("category_id"):
#         cat_name = _resolve_category_name(db, d.get("category_id"))
#
#     pub_ts = None
#     pub = d.get("published_at")
#     if pub and isinstance(pub, datetime):
#         try:
#             pub_ts = int(pub.replace(tzinfo=timezone.utc).timestamp()) if pub.tzinfo is None else int(pub.timestamp())
#         except Exception:
#             pub_ts = None
#
#     entities = d.get("entities") or []
#     keywords = d.get("keywords") or []
#
#     ent_str = ", ".join(entities) if isinstance(entities, list) else str(entities)
#     kw_str = ", ".join(keywords) if isinstance(keywords, list) else str(keywords)
#
#     return {
#         "mongo_id": str(d.get("_id")),
#         "title": (d.get("title") or "").strip(),
#         "source": (d.get("site") or d.get("source") or "").strip(),
#         "url": (d.get("external_url") or d.get("url") or "").strip(),
#         "category": cat_name,
#         "category_child": (d.get("category_child_name") or "").strip(),
#         "published_at": _to_iso_utc(d.get("published_at")),
#         "published_ts": pub_ts,
#         "entities": ent_str,
#         "keywords": kw_str,
#     }
#
#
# def _clean_list(xs, max_n: int) -> list[str]:
#     out: list[str] = []
#     seen = set()
#     for x in (xs or []):
#         s = str(x).strip()
#         if len(s) < 3:
#             continue
#         key = s.lower()
#         if key in seen:
#             continue
#         seen.add(key)
#         out.append(s)
#         if len(out) >= max_n:
#             break
#     return out
#
#
# class Command(BaseCommand):
#     # permission_classes = [AllowAny]
#     help = "Build/refresh ChromaDB index for articles + update Mongo articles.entities/keywords"
#
#     def add_arguments(self, parser):
#         parser.add_argument("--batch", type=int, default=256)
#         parser.add_argument("--force", action="store_true", help="Recompute entities/keywords even if exists")
#         parser.add_argument("--skip-mongo-update", action="store_true", help="Do not update Mongo articles, only build Chroma")
#
#     def handle(self, *args, **kwargs):
#
#         db = get_db()
#
#         batch = int(kwargs.get("batch") or 256)
#         force = bool(kwargs.get("force"))
#         skip_mongo = bool(kwargs.get("skip_mongo_update"))
#
#         embedder = load_embedder(settings.SENTENCE_MODEL)
#         kw_model = KeyBERT(model=embedder)
#
#         ids = get_all_article_ids(db)
#         client = get_client(settings.CHROMA_DIR)
#         coll = get_articles_collection(client)
#
#         self.stdout.write(self.style.WARNING(f"Indexing {len(ids)} articles... batch={batch}, force={force}"))
#
#         for i in range(0, len(ids), batch):
#             chunk_ids = ids[i:i + batch]
#             docs = fetch_articles(db, chunk_ids)
#             docs = [d for d in docs if d]
#             if not docs:
#                 continue
#
#             # A) build entities/keywords + update Mongo (bulk)
#             ops: list[UpdateOne] = []
#             for d in docs:
#                 title = (d.get("title") or "").strip()
#                 summary = (d.get("summary") or "").strip()
#                 content = (d.get("content") or "")
#
#                 # entities: rule-based (title + ít content)
#                 need_entities = force or not isinstance(d.get("entities"), list) or len(d.get("entities") or []) < 1
#                 if need_entities:
#                     ent_text = f"{title}. {summary}".strip()
#                     if len(ent_text) < 25:
#                         ent_text = f"{title}. {content[:600]}".strip()
#                     entities = extract_entities_proper(ent_text, max_entities=10, max_len=8)
#                     entities = _clean_list(entities, 10)
#                     d["entities"] = entities
#
#                 # keywords: KeyBERT (title + summary/content)
#                 need_keywords = force or not isinstance(d.get("keywords"), list) or len(d.get("keywords") or []) < 3
#                 if need_keywords:
#                     kw_text = f"{title}. {summary}".strip()
#                     if len(kw_text) < 40:
#                         kw_text = f"{title}. {content[:900]}".strip()
#                     keywords = extract_keywords_keybert(kw_model, kw_text, top_n=10, min_score=0.28)
#                     keywords = _clean_list(keywords, 10)
#                     d["keywords"] = keywords
#
#                 if not skip_mongo and (need_entities or need_keywords):
#                     ops.append(
#                         UpdateOne(
#                             {"_id": d["_id"]},
#                             {"$set": {"entities": d.get("entities", []), "keywords": d.get("keywords", [])}},
#                             upsert=False,
#                         )
#                     )
#
#             if ops and not skip_mongo:
#                 try:
#                     db["articles"].bulk_write(ops, ordered=False)
#                 except Exception as e:
#                     self.stdout.write(self.style.WARNING(f"bulk_write failed: {e}"))
#
#             # B) upsert Chroma
#             documents = [article_to_text(d) for d in docs]
#             embeddings = encode_texts(embedder, documents)
#             # try:
#             #     embeddings = embeddings.tolist()
#             # except Exception:
#             #     pass
#
#             metadatas = [_meta_safe(d, db) for d in docs]
#
#             upsert_articles(
#                 coll,
#                 ids=[str(d["_id"]) for d in docs],
#                 embeddings=embeddings,
#                 metadatas=metadatas,
#                 documents=documents,
#             )
#
#             self.stdout.write(self.style.NOTICE(f"Upsert {len(docs)} docs [{i}-{i+len(docs)-1}]"))
#
#         self.stdout.write(self.style.SUCCESS("✅ Done building Chroma index + updated Mongo entities/keywords"))

# python manage.py build_index --reset --skip-mongo-update

# recommender/management/commands/build_index.py
from __future__ import annotations

import os
import shutil
import re
from typing import List, Dict, Any
from datetime import datetime, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.conf import settings
from pymongo import UpdateOne

from api.db import get_db
from recommender.services.embeddings import load_embedder, encode_texts
from recommender.services.chroma_store import get_client, get_articles_collection, upsert_articles


def _clean(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _to_iso(dt) -> str:
    if not dt:
        return ""
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(dt_timezone.utc).replace(tzinfo=None).isoformat()
    return str(dt)


def _clean_list(xs, max_n: int) -> List[str]:
    out = []
    seen = set()
    for x in (xs or []):
        s = str(x).strip()
        if len(s) < 2:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) >= max_n:
            break
    return out


def _build_doc_text(article: Dict[str, Any], max_chars: int = 6000) -> str:
    title = _clean(article.get("title") or "")
    content = _clean(article.get("content") or article.get("body") or article.get("full_text") or "")
    entities = article.get("entities") or []
    keywords = article.get("keywords") or []
    ent_text = " ".join([str(x) for x in entities if x]) if isinstance(entities, list) else ""
    kw_text = " ".join([str(x) for x in keywords if x]) if isinstance(keywords, list) else ""
    doc = f"{title}\n\nEntities: {ent_text}\nKeywords: {kw_text}\n\n{content}".strip()
    return doc[:max_chars] if len(doc) > max_chars else doc


class Command(BaseCommand):
    help = "Build/refresh Chroma index for MongoDB articles. Optionally compute entities/keywords."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Reset (delete) existing chroma index before building")
        parser.add_argument("--limit", type=int, default=0, help="Limit number of articles (0 = no limit)")
        parser.add_argument("--batch", type=int, default=256, help="Batch size for embedding")
        parser.add_argument("--force", action="store_true", help="Recompute entities/keywords even if exists")
        parser.add_argument(
            "--skip-mongo-update",
            action="store_true",
            help="Do NOT update Mongo entities/keywords; only build Chroma (no KeyBERT needed)",
        )

    def handle(self, *args, **opts):
        reset = bool(opts.get("reset"))
        limit = int(opts.get("limit") or 0)
        batch = max(1, int(opts.get("batch") or 256))
        force = bool(opts.get("force"))
        skip_mongo = bool(opts.get("skip_mongo_update"))

        chroma_dir = getattr(settings, "CHROMA_DIR")
        model_name = getattr(settings, "SENTENCE_MODEL", None)

        db = get_db()
        embedder = load_embedder(model_name)

        # ---------- optional: entities/keywords tools ----------
        extract_entities_proper = None
        extract_keywords_keybert = None
        kw_model = None

        if not skip_mongo:
            try:
                from recommender.services.entities import extract_entities_proper as _ent
                from recommender.services.entities import extract_keywords_keybert as _kw
                extract_entities_proper = _ent
                extract_keywords_keybert = _kw
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"⚠️ Không import được recommender.services.entities ({e}). "
                    "Sẽ bỏ qua update entities/keywords. Bạn có thể dùng --skip-mongo-update."
                ))
                skip_mongo = True

        if not skip_mongo and extract_keywords_keybert is not None:
            try:
                from keybert import KeyBERT
                kw_model = KeyBERT(model=embedder)
            except Exception as e:
                self.stdout.write(self.style.WARNING(
                    f"⚠️ KeyBERT chưa sẵn sàng ({e}). "
                    "Chạy index-only: --skip-mongo-update, hoặc cài: pip install keybert"
                ))
                skip_mongo = True

        # ---------- chroma init ----------
        client = get_client(chroma_dir)

        if reset:
            try:
                client.delete_collection("articles")
                self.stdout.write(self.style.WARNING("🧹 Deleted Chroma collection: articles"))
            except Exception:
                pass

            try:
                if os.path.isdir(chroma_dir):
                    shutil.rmtree(chroma_dir, ignore_errors=True)
                os.makedirs(chroma_dir, exist_ok=True)
                client = get_client(chroma_dir)
                self.stdout.write(self.style.WARNING(f"🧹 Reset Chroma directory: {chroma_dir}"))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Reset folder failed: {e}"))

        coll = get_articles_collection(client, name="articles")

        # ---------- mongo query ----------
        status_ok = ["published", "publish", "public", "Published", "PUBLIC"]
        q = {
            "is_deleted": {"$ne": True},
            "$or": [{"status": {"$in": status_ok}}, {"status": {"$exists": False}}],
        }

        cursor = db["articles"].find(
            q,
            {
                "_id": 1,
                "title": 1,
                "content": 1,
                "summary": 1,
                "entities": 1,
                "keywords": 1,
                "published_at": 1,
                "site": 1,
                "category_id": 1,
                "category_child_id": 1,
                "external_url": 1,
                "url": 1,
            },
        ).sort("published_at", -1)

        if limit > 0:
            cursor = cursor.limit(limit)

        ids: List[str] = []
        docs: List[str] = []
        metas: List[dict] = []
        mongo_ops: List[UpdateOne] = []
        total = 0

        def flush():
            nonlocal ids, docs, metas, mongo_ops, total
            if not ids:
                return

            # A) update Mongo (entities/keywords)
            if mongo_ops and not skip_mongo:
                try:
                    db["articles"].bulk_write(mongo_ops, ordered=False)
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"bulk_write failed: {e}"))

            # B) upsert Chroma
            vectors = encode_texts(embedder, docs, batch_size=64)
            upsert_articles(
                coll,
                ids=ids,
                embeddings=vectors,
                metadatas=metas,
                documents=docs,
            )

            total += len(ids)
            self.stdout.write(self.style.SUCCESS(f"Indexed {total} articles..."))
            ids, docs, metas, mongo_ops = [], [], [], []

        for a in cursor:
            aid = str(a["_id"])

            # --------- compute entities/keywords (optional) ----------
            if (
                not skip_mongo
                and extract_entities_proper is not None
                and extract_keywords_keybert is not None
                and kw_model is not None
            ):
                title = _clean(a.get("title") or "")
                summary = _clean(a.get("summary") or "")
                content = _clean(a.get("content") or "")

                need_entities = force or not isinstance(a.get("entities"), list) or len(a.get("entities") or []) < 1
                need_keywords = force or not isinstance(a.get("keywords"), list) or len(a.get("keywords") or []) < 3

                if need_entities:
                    ent_text = f"{title}. {summary}".strip()
                    if len(ent_text) < 25:
                        ent_text = f"{title}. {content[:600]}".strip()
                    entities = extract_entities_proper(ent_text, max_entities=10, max_len=8)
                    a["entities"] = _clean_list(entities, 10)

                if need_keywords:
                    kw_text = f"{title}. {summary}".strip()
                    if len(kw_text) < 40:
                        kw_text = f"{title}. {content[:900]}".strip()
                    keywords = extract_keywords_keybert(kw_model, kw_text, top_n=10, min_score=0.28)
                    a["keywords"] = _clean_list(keywords, 10)

                if need_entities or need_keywords:
                    mongo_ops.append(
                        UpdateOne(
                            {"_id": a["_id"]},
                            {"$set": {"entities": a.get("entities", []), "keywords": a.get("keywords", [])}},
                            upsert=False,
                        )
                    )

            doc_text = _build_doc_text(a)
            if not doc_text:
                continue

            meta = {
                "article_id": aid,
                "title": a.get("title"),
                "site": a.get("site"),
                "published_at": _to_iso(a.get("published_at")),
                "category_id": str(a.get("category_id")) if a.get("category_id") else None,
                "category_child_id": str(a.get("category_child_id")) if a.get("category_child_id") else None,
                "external_url": a.get("external_url") or a.get("url"),
            }

            ids.append(aid)
            docs.append(doc_text)
            metas.append(meta)

            if len(ids) >= batch:
                flush()

        flush()
        self.stdout.write(self.style.SUCCESS(f"✅ Done. Total indexed: {total}"))
