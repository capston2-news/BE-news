from __future__ import annotations

from django.core.management.base import BaseCommand
from django.conf import settings
from pymongo import UpdateOne
from datetime import datetime, timezone

from keybert import KeyBERT
from api.permissions import AllowAny, IsAuthenticated, RoleRequired
from recommender.services.embeddings import load_embedder, encode_texts
from recommender.services.chroma_store import get_client, get_articles_collection, upsert_articles
from recommender.services.utils import get_all_article_ids, fetch_articles, article_to_text
from recommender.services.entities import extract_entities_proper, extract_keywords_keybert
from api.db import get_db


def _to_iso_utc(dt) -> str:
    if not dt:
        return ""
    if isinstance(dt, str):
        return dt
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=None).isoformat()
    return ""


def _resolve_category_name(db, cat_id) -> str:
    if not cat_id:
        return ""
    try:
        doc = db["categories"].find_one({"_id": cat_id}, {"name": 1, "title": 1})
        if doc:
            return (doc.get("name") or doc.get("title") or "").strip()
    except Exception:
        pass
    return ""


def _meta_safe(d: dict, db) -> dict:
    cat_name = (d.get("category_name") or "").strip()
    if not cat_name and d.get("category_id"):
        cat_name = _resolve_category_name(db, d.get("category_id"))

    pub_ts = None
    pub = d.get("published_at")
    if pub and isinstance(pub, datetime):
        try:
            pub_ts = int(pub.replace(tzinfo=timezone.utc).timestamp()) if pub.tzinfo is None else int(pub.timestamp())
        except Exception:
            pub_ts = None

    entities = d.get("entities") or []
    keywords = d.get("keywords") or []

    ent_str = ", ".join(entities) if isinstance(entities, list) else str(entities)
    kw_str = ", ".join(keywords) if isinstance(keywords, list) else str(keywords)

    return {
        "mongo_id": str(d.get("_id")),
        "title": (d.get("title") or "").strip(),
        "source": (d.get("site") or d.get("source") or "").strip(),
        "url": (d.get("external_url") or d.get("url") or "").strip(),
        "category": cat_name,
        "category_child": (d.get("category_child_name") or "").strip(),
        "published_at": _to_iso_utc(d.get("published_at")),
        "published_ts": pub_ts,
        "entities": ent_str,
        "keywords": kw_str,
    }


def _clean_list(xs, max_n: int) -> list[str]:
    out: list[str] = []
    seen = set()
    for x in (xs or []):
        s = str(x).strip()
        if len(s) < 3:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= max_n:
            break
    return out


class Command(BaseCommand):
    permission_classes = [AllowAny]
    help = "Build/refresh ChromaDB index for articles + update Mongo articles.entities/keywords"

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, default=256)
        parser.add_argument("--force", action="store_true", help="Recompute entities/keywords even if exists")
        parser.add_argument("--skip-mongo-update", action="store_true", help="Do not update Mongo articles, only build Chroma")

    def handle(self, *args, **kwargs):

        db = get_db()

        batch = int(kwargs.get("batch") or 256)
        force = bool(kwargs.get("force"))
        skip_mongo = bool(kwargs.get("skip_mongo_update"))

        embedder = load_embedder(settings.SENTENCE_MODEL)
        kw_model = KeyBERT(model=embedder)

        ids = get_all_article_ids(db)
        client = get_client(settings.CHROMA_DIR)
        coll = get_articles_collection(client)

        self.stdout.write(self.style.WARNING(f"Indexing {len(ids)} articles... batch={batch}, force={force}"))

        for i in range(0, len(ids), batch):
            chunk_ids = ids[i:i + batch]
            docs = fetch_articles(db, chunk_ids)
            docs = [d for d in docs if d]
            if not docs:
                continue

            # A) build entities/keywords + update Mongo (bulk)
            ops: list[UpdateOne] = []
            for d in docs:
                title = (d.get("title") or "").strip()
                summary = (d.get("summary") or "").strip()
                content = (d.get("content") or "")

                # entities: rule-based (title + ít content)
                need_entities = force or not isinstance(d.get("entities"), list) or len(d.get("entities") or []) < 1
                if need_entities:
                    ent_text = f"{title}. {summary}".strip()
                    if len(ent_text) < 25:
                        ent_text = f"{title}. {content[:600]}".strip()
                    entities = extract_entities_proper(ent_text, max_entities=10, max_len=8)
                    entities = _clean_list(entities, 10)
                    d["entities"] = entities

                # keywords: KeyBERT (title + summary/content)
                need_keywords = force or not isinstance(d.get("keywords"), list) or len(d.get("keywords") or []) < 3
                if need_keywords:
                    kw_text = f"{title}. {summary}".strip()
                    if len(kw_text) < 40:
                        kw_text = f"{title}. {content[:900]}".strip()
                    keywords = extract_keywords_keybert(kw_model, kw_text, top_n=10, min_score=0.28)
                    keywords = _clean_list(keywords, 10)
                    d["keywords"] = keywords

                if not skip_mongo and (need_entities or need_keywords):
                    ops.append(
                        UpdateOne(
                            {"_id": d["_id"]},
                            {"$set": {"entities": d.get("entities", []), "keywords": d.get("keywords", [])}},
                            upsert=False,
                        )
                    )

            if ops and not skip_mongo:
                try:
                    db["articles"].bulk_write(ops, ordered=False)
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"bulk_write failed: {e}"))

            # B) upsert Chroma
            documents = [article_to_text(d) for d in docs]
            embeddings = encode_texts(embedder, documents)
            try:
                embeddings = embeddings.tolist()
            except Exception:
                pass

            metadatas = [_meta_safe(d, db) for d in docs]

            upsert_articles(
                coll,
                ids=[str(d["_id"]) for d in docs],
                embeddings=embeddings,
                metadatas=metadatas,
                documents=documents,
            )

            self.stdout.write(self.style.NOTICE(f"Upsert {len(docs)} docs [{i}-{i+len(docs)-1}]"))

        self.stdout.write(self.style.SUCCESS("✅ Done building Chroma index + updated Mongo entities/keywords"))
