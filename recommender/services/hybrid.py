# recommender/services/hybrid.py
from __future__ import annotations

from typing import Dict, Any, List, Optional
from bson import ObjectId
import math
from datetime import datetime, timezone, timedelta


# ---------- RRF ----------
def rrf_fuse(lists: List[List[str]], k: int = 60) -> Dict[str, float]:
    score: Dict[str, float] = {}
    for L in lists:
        for rank, id_ in enumerate(L, start=1):
            score[id_] = score.get(id_, 0.0) + 1.0 / (k + rank)
    return score


# ---------- MMR ----------
def mmr_rerank(
    ids: List[str],
    sim_matrix: List[List[float]],
    lambda_: float = 0.7,
    topk: int = 10,
    relevance_map: Optional[Dict[str, float]] = None,
) -> List[str]:
    if not ids:
        return []

    selected: List[int] = []
    candidates = set(range(len(ids)))

    while candidates and len(selected) < topk:
        best, best_score = None, -1e18
        for i in list(candidates):
            relevance = float(relevance_map.get(ids[i], 0.0)) if relevance_map else 1.0
            diversity = max((sim_matrix[i][j] for j in selected), default=0.0)
            s = lambda_ * relevance - (1.0 - lambda_) * diversity
            if s > best_score:
                best_score, best = s, i

        if best is None:
            break
        selected.append(best)
        candidates.remove(best)

    return [ids[i] for i in selected]


# ---------- time normalize ----------
def _dt_utc(x) -> Optional[datetime]:
    if x is None:
        return None
    if isinstance(x, datetime):
        dt = x
    elif isinstance(x, str):
        try:
            iso = x.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
        except Exception:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


# ---------- Popular + recent ----------
def top_popular_recent(db, limit: int = 200, days: int = 7, today_bonus: float = 2.0) -> List[str]:
    pipeline = [
        {"$lookup": {"from": "articles", "localField": "_id", "foreignField": "_id", "as": "a"}},
        {"$unwind": "$a"},
        {"$project": {"_id": 1, "views": {"$ifNull": ["$views", 0]}, "published_at": "$a.published_at"}},
    ]
    docs = list(db["article_popularity"].aggregate(pipeline))

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    scored: List[tuple[str, float]] = []
    for d in docs:
        views = d.get("views", 0)
        pub = _dt_utc(d.get("published_at"))
        if not pub or pub < cutoff:
            continue

        age_h = max((now - pub).total_seconds() / 3600.0, 1.0)
        rec = math.exp(-age_h / 72.0)

        is_today = (pub.date() == now.date())
        bonus = today_bonus if is_today else 0.0

        score = (views / 10.0) + 2.0 * rec + bonus
        scored.append((str(d["_id"]), score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [i for i, _ in scored[:limit]]


def top_popular_recent_by_categories(
    db,
    category_names: Optional[List[str]] = None,
    category_ids: Optional[List[str]] = None,
    limit: int = 200,
    days: int = 7,
    today_bonus: float = 2.0,
) -> List[str]:
    category_names = category_names or []
    category_ids = category_ids or []

    pipeline: List[dict] = [
        {"$lookup": {"from": "articles", "localField": "_id", "foreignField": "_id", "as": "a"}},
        {"$unwind": "$a"},
    ]

    if category_names:
        pipeline.append({"$match": {"a.category_name": {"$in": category_names}}})

    if category_ids:
        oids = []
        for s in category_ids:
            try:
                oids.append(ObjectId(s))
            except Exception:
                pass
        if oids:
            pipeline.append({"$match": {"a.category_id": {"$in": oids}}})

    pipeline.append({"$project": {"_id": 1, "views": {"$ifNull": ["$views", 0]}, "published_at": "$a.published_at"}})

    docs = list(db["article_popularity"].aggregate(pipeline))

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)

    scored: List[tuple[str, float]] = []
    for d in docs:
        views = d.get("views", 0)
        pub = _dt_utc(d.get("published_at"))
        if not pub or pub < cutoff:
            continue

        age_h = max((now - pub).total_seconds() / 3600.0, 1.0)
        rec = math.exp(-age_h / 72.0)

        is_today = (pub.date() == now.date())
        bonus = today_bonus if is_today else 0.0

        score = (views / 10.0) + 2.0 * rec + bonus
        scored.append((str(d["_id"]), score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [i for i, _ in scored[:limit]]


# ---------- Chroma helpers ----------
def chroma_by_user_vector(collection, user_vec, n_results: int = 300) -> List[str]:
    if user_vec is None:
        return []
    q = collection.query(query_embeddings=[user_vec], n_results=n_results)
    return q.get("ids", [[]])[0]


# ---------- Fetch meta ----------
def fetch_meta_map(db, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not ids:
        return {}

    oids: List[ObjectId] = []
    for s in ids:
        try:
            oids.append(ObjectId(s))
        except Exception:
            pass
    if not oids:
        return {}

    docs = list(db["articles"].find(
        {"_id": {"$in": oids}},
        {
            "title": 1,
            "summary": 1,     # ✅ add
            "keywords": 1,    # ✅ add
            "entities": 1,    # ✅ add
            "images":1,
            "site": 1,
            "external_url": 1,
            "published_at": 1,
            "category_id": 1,
            "category_child_id": 1,
            "category_name": 1,
            "category_child_name": 1,
        }
    ))

    # gom ObjectId để map tên
    cat_oids: List[ObjectId] = []
    child_oids: List[ObjectId] = []

    for d in docs:
        c = d.get("category_id")
        cc = d.get("category_child_id")

        if isinstance(c, ObjectId):
            cat_oids.append(c)
        else:
            try:
                if c and ObjectId.is_valid(str(c)):
                    cat_oids.append(ObjectId(str(c)))
            except Exception:
                pass

        if isinstance(cc, ObjectId):
            child_oids.append(cc)
        else:
            try:
                if cc and ObjectId.is_valid(str(cc)):
                    child_oids.append(ObjectId(str(cc)))
            except Exception:
                pass

    cat_map: Dict[str, str] = {}
    if cat_oids:
        for c in db["categories"].find({"_id": {"$in": cat_oids}}, {"name": 1, "title": 1}):
            cat_map[str(c["_id"])] = c.get("name") or c.get("title") or ""

    child_map: Dict[str, str] = {}
    if child_oids:
        for c in db["category_child"].find({"_id": {"$in": child_oids}}, {"name": 1, "title": 1}):
            child_map[str(c["_id"])] = c.get("name") or c.get("title") or ""

    out: Dict[str, Dict[str, Any]] = {}
    for d in docs:
        aid = str(d["_id"])
        cat_id = d.get("category_id")
        child_id = d.get("category_child_id")

        cat_id_str = str(cat_id) if cat_id else ""
        child_id_str = str(child_id) if child_id else ""

        parent_name = (d.get("category_name") or "").strip() or cat_map.get(cat_id_str, "")
        child_name = (d.get("category_child_name") or "").strip() or child_map.get(child_id_str, "")

        out[aid] = {
            "title": d.get("title"),
            "summary": d.get("summary"),      # ✅ add
            "keywords": d.get("keywords"),    # ✅ add
            "entities": d.get("entities"),
            "images" : d.get("images"),

            "source": d.get("site"),
            "url": d.get("external_url"),
            "published_at": d.get("published_at"),
            "category_id": cat_id_str,
            "category_name": parent_name or "",
            "category_child_id": child_id_str,
            "category_child_name": child_name or None,
        }

    return out


# ---------- Topic helpers ----------
def topic_for_meta(meta_item: dict) -> str:
    child = (meta_item.get("category_child_name") or "").strip()
    return child if child else (meta_item.get("category_name") or "").strip()


def topic_soft_boost(meta_item: dict, aff: Dict[str, float], max_aff: float) -> float:
    if not aff or max_aff <= 0:
        return 0.0
    topic = topic_for_meta(meta_item)
    raw = aff.get(topic, 0.0)
    return (raw / max_aff) if raw > 0 else 0.0


def hybrid_recommend(
    db,
    chroma_collection,
    embedder,  # giữ signature cho tương thích
    user_profile: Dict[str, Any],
    topk: int = 20,
    only_categories: Optional[List[str]] = None,
    only_category_ids: Optional[List[str]] = None,
    soft_boost: bool = False,
):
    only_categories = only_categories or []
    only_category_ids = only_category_ids or []

    ids_vec = chroma_by_user_vector(chroma_collection, user_profile.get("vector"), n_results=300)

    if only_categories or only_category_ids:
        ids_pop = top_popular_recent_by_categories(db, only_categories, only_category_ids, limit=300)
    else:
        ids_pop = top_popular_recent(db, limit=300)

    fused = rrf_fuse([ids_vec, ids_pop], k=60)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
    ids = [i for i, _ in ranked[:max(4 * topk, 80)]]
    if not ids:
        return []

    meta = fetch_meta_map(db, ids)

    if only_categories or only_category_ids:
        ids = [
            i for i in ids if (
                (meta.get(i, {}).get("category_name") in set(only_categories)) or
                (meta.get(i, {}).get("category_id") in set(only_category_ids))
            )
        ] or ids_pop
        meta = fetch_meta_map(db, ids)

    boosted_scores: Dict[str, float] = dict(fused)

    if soft_boost:
        from .topic_affinity import compute_user_topic_affinity  # tránh vòng import
        uid = user_profile.get("user_id") or user_profile.get("id")
        aff = compute_user_topic_affinity(db, uid) if uid else {}
        max_aff = max(aff.values()) if aff else 0.0

        for i in ids:
            base = fused.get(i, 0.0)
            extra = topic_soft_boost(meta.get(i, {}), aff, max_aff)
            boosted_scores[i] = base + 0.15 * extra

        ids = sorted(ids, key=lambda x: boosted_scores.get(x, 0.0), reverse=True)

    sim = [[0.0 for _ in ids] for __ in ids]
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i == j:
                sim[i][j] = 1.0
                continue
            mi, mj = meta.get(a, {}), meta.get(b, {})
            same_cat = mi.get("category_name") and mi.get("category_name") == mj.get("category_name")
            same_src = mi.get("source") and mi.get("source") == mj.get("source")
            sim[i][j] = 1.0 if (same_cat or same_src) else 0.0

    mmr_ids = mmr_rerank(ids, sim, lambda_=0.7, topk=topk, relevance_map=boosted_scores)
    final_meta = fetch_meta_map(db, mmr_ids)

    out = []
    for i in mmr_ids:
        out.append({
            "id": i,
            "title": final_meta.get(i, {}).get("title"),
            "images": final_meta.get(i, {}).get("images"),
            "url": final_meta.get(i, {}).get("url"),
            "source": final_meta.get(i, {}).get("source"),
            "category_id": final_meta.get(i, {}).get("category_id"),
            "category_name": final_meta.get(i, {}).get("category_name"),
            "category_child_name": final_meta.get(i, {}).get("category_child_name"),
            "published_at": final_meta.get(i, {}).get("published_at"),
            "score": boosted_scores.get(i, 0.0),
        })
    return out
