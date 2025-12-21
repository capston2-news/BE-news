# from __future__ import annotations
# import re
# from typing import Any, Dict, List, Optional
#
# from django.conf import settings
# from bson import ObjectId
#
# from recommender.services.embeddings import load_embedder, encode_texts
# from recommender.services.chroma_store import get_client, get_articles_collection, upsert_articles
#
# _EMBEDDER = None
# _COLL = None
#
#
# def _get_embedder():
#     global _EMBEDDER
#     if _EMBEDDER is None:
#         _EMBEDDER = load_embedder(getattr(settings, "SENTENCE_MODEL", None))
#     return _EMBEDDER
#
#
# def _get_coll():
#     global _COLL
#     if _COLL is None:
#         client = get_client(getattr(settings, "CHROMA_DIR"))
#         _COLL = get_articles_collection(client, name="articles")
#     return _COLL
#
#
# def _clean(s: str) -> str:
#     if not s:
#         return ""
#     return re.sub(r"\s+", " ", str(s)).strip()
#
#
# def _to_iso(v) -> Optional[str]:
#     if not v:
#         return None
#     try:
#         if hasattr(v, "isoformat"):
#             return v.isoformat()
#         return str(v)
#     except Exception:
#         return None
#
#
# def _build_doc_text(article: Dict[str, Any], max_chars: int = 6000) -> str:
#     """
#     Text dùng để embedding/search semantic.
#     Bạn có thể thêm field khác tuỳ ý (vd: summary).
#     """
#     title = _clean(article.get("title") or "")
#     content = _clean(article.get("content") or article.get("body") or article.get("full_text") or "")
#     entities = article.get("entities") or []
#     keywords = article.get("keywords") or []
#
#     ent_text = " ".join([str(x) for x in entities if x]) if isinstance(entities, list) else ""
#     kw_text = " ".join([str(x) for x in keywords if x]) if isinstance(keywords, list) else ""
#
#     doc = f"{title}\n\nEntities: {ent_text}\nKeywords: {kw_text}\n\n{content}".strip()
#     return doc[:max_chars] if len(doc) > max_chars else doc
#
#
# def _normalize_oid(oid) -> str:
#     if oid is None:
#         return ""
#     if isinstance(oid, ObjectId):
#         return str(oid)
#     # try convert str -> ObjectId -> str for consistency
#     try:
#         return str(ObjectId(str(oid)))
#     except Exception:
#         return str(oid)
#
#
# def _should_delete_from_index(article: Dict[str, Any]) -> bool:
#     is_deleted = article.get("is_deleted") is True
#     status = (article.get("status") or "").lower().strip()
#     if is_deleted:
#         return True
#     if status and status not in ("published", "publish", "public"):
#         return True
#     return False
#
#
# def delete_article_from_chroma(article_id: str) -> bool:
#     """
#     Xóa 1 bài khỏi Chroma theo id.
#     """
#     if not article_id:
#         return False
#     try:
#         _get_coll().delete(ids=[article_id])
#         return True
#     except Exception:
#         return False
#
#
# def upsert_article_to_chroma(article: Dict[str, Any]) -> bool:
#     """
#     Upsert 1 bài vào Chroma theo _id.
#     - Nếu is_deleted=True hoặc status != published -> xóa khỏi index
#     """
#     if not article:
#         return False
#
#     article_id = _normalize_oid(article.get("_id"))
#     if not article_id:
#         return False
#
#     coll = _get_coll()
#
#     if _should_delete_from_index(article):
#         return delete_article_from_chroma(article_id)
#
#     doc_text = _build_doc_text(article)
#     if not doc_text:
#         return False
#
#     meta = {
#         "article_id": article_id,
#         "title": article.get("title"),
#         "site": article.get("site"),
#         "published_at": _to_iso(article.get("published_at")),
#         "category_id": _normalize_oid(article.get("category_id")) if article.get("category_id") else None,
#         "category_child_id": _normalize_oid(article.get("category_child_id")) if article.get("category_child_id") else None,
#         "external_url": article.get("external_url"),
#     }
#
#     try:
#         emb = _get_embedder()
#
#         vecs = encode_texts(emb, [doc_text], batch_size=16)  # 1 item nhưng vẫn dùng chung util
#         vector = vecs[0]
#
#         # ✅ dùng upsert wrapper (tự chọn .upsert() hoặc delete+add tùy version Chroma)
#         upsert_articles(
#             collection=coll,
#             ids=[article_id],
#             embeddings=[vector],
#             metadatas=[meta],
#             documents=[doc_text],
#         )
#         return True
#
#     except Exception as e:
#         print("UPSERT_CHROMA_ERROR:", e)
#         return False
#
#
# def upsert_articles_to_chroma(articles: List[Dict[str, Any]]) -> int:
#     """
#     Upsert nhiều bài (loop).
#     Nếu muốn nhanh hơn, mình có thể tối ưu theo batch (encode batch, upsert batch).
#     """
#     ok = 0
#     for a in articles or []:
#         if upsert_article_to_chroma(a):
#             ok += 1
#     return ok

# recommender/services/article_indexer.py
import re
from typing import Any, Dict, List, Optional
from datetime import datetime

from django.conf import settings
from bson import ObjectId

from recommender.services.embeddings import load_embedder, encode_texts
from recommender.services.chroma_store import get_client, get_articles_collection, upsert_articles

_EMBEDDER = None
_COLL = None


def _get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = load_embedder(getattr(settings, "SENTENCE_MODEL", None))
    return _EMBEDDER


def _get_coll():
    global _COLL
    if _COLL is None:
        client = get_client(getattr(settings, "CHROMA_DIR"))
        _COLL = get_articles_collection(client, name="articles")
    return _COLL


def _clean(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _to_iso(v) -> Optional[str]:
    if not v:
        return None
    try:
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)
    except Exception:
        return None


def _build_doc_text(article: Dict[str, Any], max_chars: int = 6000) -> str:
    title = _clean(article.get("title") or "")
    content = _clean(article.get("content") or article.get("body") or article.get("full_text") or "")
    entities = article.get("entities") or []
    keywords = article.get("keywords") or []

    ent_text = " ".join([str(x) for x in entities if x]) if isinstance(entities, list) else ""
    kw_text = " ".join([str(x) for x in keywords if x]) if isinstance(keywords, list) else ""

    doc = f"{title}\n\nEntities: {ent_text}\nKeywords: {kw_text}\n\n{content}".strip()
    return doc[:max_chars] if len(doc) > max_chars else doc


def _normalize_oid(oid) -> str:
    if oid is None:
        return ""
    if isinstance(oid, ObjectId):
        return str(oid)
    try:
        return str(ObjectId(str(oid)))
    except Exception:
        return str(oid)


def upsert_article_to_chroma(article: Dict[str, Any]) -> bool:
    """
    Upsert 1 bài vào Chroma theo _id.
    - Nếu is_deleted=True hoặc status != published/public -> xóa khỏi index (nếu có)
    """
    if not article:
        return False

    article_id = _normalize_oid(article.get("_id"))
    if not article_id:
        return False

    is_deleted = article.get("is_deleted") is True
    status = (article.get("status") or "").lower().strip()

    if is_deleted or (status and status not in ("published", "publish", "public")):
        try:
            _get_coll().delete(ids=[article_id])
        except Exception:
            pass
        return False

    doc_text = _build_doc_text(article)
    if not doc_text:
        return False

    meta = {
        "article_id": article_id,
        "title": article.get("title"),
        "site": article.get("site"),
        "published_at": _to_iso(article.get("published_at")),
        "category_id": _normalize_oid(article.get("category_id")) if article.get("category_id") else None,
        "category_child_id": _normalize_oid(article.get("category_child_id")) if article.get("category_child_id") else None,
        "external_url": article.get("external_url") or article.get("url"),
    }

    try:
        emb = _get_embedder()
        coll = _get_coll()

        vectors = encode_texts(emb, [doc_text], batch_size=16)
        upsert_articles(
            coll,
            ids=[article_id],
            embeddings=vectors,
            metadatas=[meta],
            documents=[doc_text],
        )
        return True
    except Exception as e:
        print("UPSERT_CHROMA_ERROR:", e)
        return False


def upsert_articles_to_chroma(articles: List[Dict[str, Any]]) -> int:
    ok = 0
    for a in articles or []:
        if upsert_article_to_chroma(a):
            ok += 1
    return ok

