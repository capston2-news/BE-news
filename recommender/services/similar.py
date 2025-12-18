# recommender/services/similar.py
from __future__ import annotations

from typing import Dict, Any, List, Set, Tuple, Optional
from bson import ObjectId
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import re

from .utils import article_to_text
from .hybrid import (
    top_popular_recent,
    top_popular_recent_by_categories,
    fetch_meta_map,
    mmr_rerank,
    chroma_by_user_vector,
    topic_soft_boost,
)
from .topic_affinity import compute_user_topic_affinity
from .user_profile import build_or_get_user_profile
from .keyword_affinity import compute_user_keyword_affinity, keyword_soft_boost
from .entity_affinity import compute_user_entity_affinity, entity_soft_boost  # ✅ add


# -------------------------------------------------------------------
# CONFIG (CHỈNH Ở ĐÂY)
# -------------------------------------------------------------------
HARD_SCORE_THRESHOLD = 0.0
POPULARITY_LIMIT = 60
AFFINITY_DIVERSITY_THRESHOLD = 0.6

# weight cho RRF (quan trọng)
W_USER_VEC = 0.9
W_SEED = 2.0
W_POP_WHEN_PHRASE = 0.05
W_POP_DEFAULT = 0.25

# phrase boost (quan trọng)
PHRASE_BONUS = 0.25
PHRASE_BONUS_EXTRA = 0.05
PHRASE_BONUS_CAP = 0.40

# ✅ weights boost thêm
W_TOPIC = 0.25
W_KEYWORD = 0.45
W_ENTITY = 0.55


# -------------------------------------------------------------------
# TIME HELPERS
# -------------------------------------------------------------------
def _get_timestamp(m_data: Dict[str, Any]) -> float:
    if not m_data:
        return 0.0
    pub = m_data.get("published_at")
    if not pub:
        return 0.0

    dt = None
    if isinstance(pub, datetime):
        dt = pub
    elif isinstance(pub, str):
        try:
            iso = pub.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
        except Exception:
            return 0.0
    else:
        return 0.0

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.timestamp()


def _filter_by_recent_days(meta: Dict[str, Any], ids: List[str], days: int = 7) -> List[str]:
    if not ids:
        return []
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts = now_ts - days * 86400
    out: List[str] = []
    for aid in ids:
        ts = _get_timestamp(meta.get(aid, {}))
        if ts >= cutoff_ts and ts > 0:
            out.append(aid)
    return out


def _to_oid(x) -> Optional[ObjectId]:
    if x is None:
        return None
    if isinstance(x, ObjectId):
        return x
    s = str(x)
    if ObjectId.is_valid(s):
        return ObjectId(s)
    return None


# -------------------------------------------------------------------
# ARTICLE HELPER
# -------------------------------------------------------------------
def _get_article(db, article_id: str):
    oid = _to_oid(article_id)
    if not oid:
        return None
    return db["articles"].find_one({"_id": oid})


# -------------------------------------------------------------------
# RRF WEIGHTED
# -------------------------------------------------------------------
def rrf_fuse_weighted(lists: List[List[str]], weights: List[float], k: int = 60) -> Dict[str, float]:
    score: Dict[str, float] = {}
    for L, w in zip(lists, weights):
        if not L or w <= 0:
            continue
        for rank, id_ in enumerate(L, start=1):
            score[id_] = score.get(id_, 0.0) + w * (1.0 / (k + rank))
    return score


# -------------------------------------------------------------------
# TOKEN/PHRASE HELPERS
# -------------------------------------------------------------------
_VI_STOP = {
    "và","là","của","cho","với","trong","ở","đã","đang","sẽ","một","những","các",
    "khi","vì","do","từ","đến","theo","về","này","đó","nên","còn","lại","ra","bị",
    "tại","trên","dưới","hơn","rất","cũng","như","không","có","được","thì",
    "hôm","nay","mới","nhất","cực","đẹp","đủ","sức","thắng","thua","gặp","đội","trận",
    "lịch","thi","đấu","bán","kết","chung","kết","vđv","sea","games",
    "việt","nam","u23","u.23","u19","u.19","u17","u.17","vietnam","malaysia",
}

def _tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    text = re.sub(
        r"[^0-9a-zàáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ\.\- ]+",
        " ",
        text
    )
    parts = re.split(r"\s+", text)
    toks: List[str] = []
    for p in parts:
        p = p.strip(".-").strip()
        if len(p) < 3:
            continue
        if p in _VI_STOP:
            continue
        toks.append(p)
    return toks


def _get_recent_read_article_oids(
    db,
    user_id: str,
    category_names: Optional[List[str]] = None,
    days: int = 30,
    limit: int = 80,
) -> List[ObjectId]:
    category_names = category_names or []
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    q: Dict[str, Any] = {"user_id": str(user_id), "ts": {"$gte": since}}
    if category_names:
        q["category_name"] = {"$in": category_names}

    logs = list(
        db["user_activity"].find(q, {"article_id": 1, "ts": 1})
        .sort("ts", -1)
        .limit(limit)
    )

    uniq: List[ObjectId] = []
    seen = set()
    for l in logs:
        oid = _to_oid(l.get("article_id"))
        if not oid:
            continue
        s = str(oid)
        if s in seen:
            continue
        seen.add(s)
        uniq.append(oid)

    return uniq


def _build_interest_phrases_from_activity(
    db,
    user_id: str,
    category_names: Optional[List[str]] = None,
    days: int = 30,
    limit: int = 250,
) -> List[str]:
    category_names = category_names or []
    since = datetime.now(timezone.utc) - timedelta(days=days)

    q: Dict[str, Any] = {"user_id": str(user_id), "ts": {"$gte": since}}
    if category_names:
        q["category_name"] = {"$in": category_names}

    rows = list(db["user_activity"].find(q, {"category_child_name": 1}).limit(limit))

    phrases: List[str] = []
    for r in rows:
        child = (r.get("category_child_name") or "").strip().lower()
        if not child:
            continue

        if "bóng đá" in child:
            phrases.append("bóng đá")
        elif "tennis" in child:
            phrases.append("tennis")
        elif "ngoại hạng" in child:
            phrases.append("ngoại hạng")
        elif "champions league" in child or "c1" in child:
            phrases.append("champions league")
        else:
            toks = _tokenize(child)
            if len(toks) >= 2:
                phrases.append(f"{toks[0]} {toks[1]}")
            elif len(toks) == 1:
                phrases.append(toks[0])

    out: List[str] = []
    seen = set()
    for p in phrases:
        p = (p or "").strip().lower()
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)

    return out[:12]


def _build_interest_phrases_from_reads(
    db,
    user_id: str,
    category_names: Optional[List[str]] = None,
    days: int = 30,
    limit_reads: int = 80,
) -> List[str]:
    oids = _get_recent_read_article_oids(db, user_id, category_names=category_names, days=days, limit=limit_reads)
    if not oids:
        return []

    arts = list(db["articles"].find({"_id": {"$in": oids}}, {"title": 1, "summary": 1}))
    if not arts:
        return []

    a_map = {str(a["_id"]): a for a in arts}
    ordered = [a_map.get(str(oid)) for oid in oids]
    ordered = [a for a in ordered if a]

    freq = defaultdict(int)
    for a in ordered:
        text = f'{a.get("title","")} {a.get("summary","")}'
        toks = _tokenize(text)
        for i in range(len(toks) - 1):
            bg = f"{toks[i]} {toks[i+1]}"
            freq[bg] += 1

    if not freq:
        return []
    return [p for p, _ in sorted(freq.items(), key=lambda x: x[1], reverse=True)[:20]]


def _count_phrase_hits(text: str, phrases: List[str]) -> int:
    if not text or not phrases:
        return 0
    t = text.lower()
    c = 0
    for ph in phrases:
        if ph and ph in t:
            c += 1
    return c


def _exclude_read(db, ids: List[str], user_id: str) -> List[str]:
    seen = set(
        str(x.get("article_id"))
        for x in db["user_activity"].find({"user_id": str(user_id)}, {"article_id": 1})
    )
    return [i for i in ids if i not in seen]


def get_user_category_filters(
    db,
    user_id: str,
    log_collection: str = "user_activity",
    limit: int = 500,
):
    logs = list(
        db[log_collection].find(
            {"user_id": str(user_id)},
            {"category_name": 1, "category_child_name": 1, "subcategory_name": 1, "ts": 1},
        ).sort("_id", -1).limit(limit)
    )

    allowed_categories: Set[str] = set()
    allowed_children: Set[str] = set()
    allowed_pairs: Set[Tuple[str, str]] = set()

    for ev in logs:
        cat = (ev.get("category_name") or "").strip() or None
        child = (ev.get("category_child_name") or ev.get("subcategory_name") or "").strip() or None
        if cat:
            allowed_categories.add(cat)
        if child:
            allowed_children.add(child)
        if cat or child:
            allowed_pairs.add((cat, child))

    return allowed_categories, allowed_children, allowed_pairs


def get_focus_category_id(db, user_id: str, days: int = 60) -> Optional[str]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = list(db["user_activity"].find(
        {"user_id": str(user_id), "ts": {"$gte": since}},
        {"category_id": 1}
    ).limit(500))

    freq = defaultdict(int)
    for r in rows:
        cid = r.get("category_id")
        if cid:
            freq[str(cid)] += 1
    if not freq:
        return None
    return max(freq.items(), key=lambda x: x[1])[0]


def user_focus_ratio_by_category(db, user_id: str, days: int = 30, limit: int = 400) -> float:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = list(db["user_activity"].find(
        {"user_id": str(user_id), "ts": {"$gte": since}},
        {"category_name": 1}
    ).limit(limit))

    freq = defaultdict(int)
    total = 0
    for r in rows:
        cat = (r.get("category_name") or "").strip()
        if not cat:
            continue
        freq[cat] += 1
        total += 1
    if total == 0:
        return 0.0
    top = max(freq.values()) if freq else 0
    return top / float(total)


def _build_seed_vectors_from_recent_reads(
    db,
    embedder,
    user_id: str,
    category_names: Optional[List[str]] = None,
    days: int = 30,
    read_limit: int = 60,
    seed_count: int = 6,
) -> List[List[float]]:
    oids = _get_recent_read_article_oids(db, user_id, category_names=category_names, days=days, limit=read_limit)
    if not oids:
        return []

    arts = list(db["articles"].find(
        {"_id": {"$in": oids}},
        {"title": 1, "content": 1, "summary": 1}
    ))
    if not arts:
        return []

    a_map = {str(a["_id"]): a for a in arts}
    ordered = [a_map.get(str(oid)) for oid in oids]
    ordered = [a for a in ordered if a]

    seeds = ordered[:max(1, seed_count)]
    texts = [article_to_text(a) for a in seeds if a]
    if not texts:
        return []

    embs = embedder.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    if embs is None or len(embs) == 0:
        return []

    return [embs[i].tolist() for i in range(len(embs))]


def _chroma_query_many_seeds(chroma_collection, seed_vecs: List[List[float]], n_results_each: int = 180) -> List[List[str]]:
    lists: List[List[str]] = []
    for v in seed_vecs:
        try:
            q = chroma_collection.query(query_embeddings=[v], n_results=n_results_each)
            ids = q.get("ids", [[]])[0]
            if ids:
                lists.append(ids)
        except Exception:
            continue
    return lists


# -------------------------------------------------------------------
# SIMILAR BY ARTICLE
# -------------------------------------------------------------------
def similar_by_article(db, chroma_collection, embedder, article_id, user_id=None, topk: int = 10):
    art = _get_article(db, article_id)
    if not art:
        return []

    base_cat = (art.get("category_name") or "").strip() or None

    qvec = embedder.encode(
        [article_to_text(art)],
        normalize_embeddings=True,
        convert_to_numpy=True
    )[0].tolist()

    q = chroma_collection.query(query_embeddings=[qvec], n_results=max(250, topk * 10))
    vec_ids = q.get("ids", [[]])[0]
    ids_vec = [i for i in vec_ids if i != str(art["_id"])]

    ids_pop = top_popular_recent(db, limit=250)

    fused = rrf_fuse_weighted([ids_vec, ids_pop], [1.2, 0.20], k=60)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
    ids = [i for i, _ in ranked[:max(6 * topk, 120)]]

    if not ids:
        return []

    meta = fetch_meta_map(db, ids)

    if base_cat:
        same_cat = [aid for aid in ids if (meta.get(aid, {}) or {}).get("category_name") == base_cat]
        if same_cat:
            ids = same_cat

    sim = [[0.0 for _ in ids] for __ in ids]
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i == j:
                sim[i][j] = 1.0
                continue
            ma, mb = meta.get(a, {}), meta.get(b, {})
            sim[i][j] = 1.0 if (ma.get("source") and ma.get("source") == mb.get("source")) else 0.0

    mmr_ids = mmr_rerank(ids, sim, lambda_=0.75, topk=topk, relevance_map=fused)
    final_meta = fetch_meta_map(db, mmr_ids)

    return [{
        "id": aid,
        "title": (final_meta.get(aid, {}) or {}).get("title"),
        "images": (final_meta.get(aid, {}) or {}).get("images"),
        "url": (final_meta.get(aid, {}) or {}).get("url"),
        "source": (final_meta.get(aid, {}) or {}).get("source"),
        "category_name": (final_meta.get(aid, {}) or {}).get("category_name"),
        "category_child_name": (final_meta.get(aid, {}) or {}).get("category_child_name"),
        "published_at": (final_meta.get(aid, {}) or {}).get("published_at"),
        "score": fused.get(aid, 0.0),
    } for aid in mmr_ids]


# -------------------------------------------------------------------
# USER TOPIC FEED (THEO PHRASE + KEYWORD + ENTITY)
# -------------------------------------------------------------------
def user_topic_feed(
    db,
    chroma_collection,
    embedder,
    user_id: str,
    topk: int = 10,
    min_focus: float = 0.3,
):
    n_results_query = max(300, topk * 5)

    allowed_categories, allowed_children, allowed_pairs = get_user_category_filters(
        db, user_id, log_collection="user_activity"
    )

    # ------------------- COLD-START -------------------
    if not allowed_categories and not allowed_children and not allowed_pairs:
        prof = build_or_get_user_profile(db, user_id, embedder)
        user_vec = prof.get("vector")

        ids_vec = chroma_by_user_vector(chroma_collection, user_vec, n_results=n_results_query) if user_vec is not None else []
        ids_pop = top_popular_recent(db, limit=POPULARITY_LIMIT)

        fused = rrf_fuse_weighted([ids_vec, ids_pop], [1.0, 0.35], k=60)
        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
        ids = [i for i, score in ranked if score >= HARD_SCORE_THRESHOLD] or [i for i, _ in ranked]
        ids = ids[:max(4 * topk, 100)]

        meta = fetch_meta_map(db, ids)

        focus_cat_id = get_focus_category_id(db, user_id)
        if focus_cat_id:
            filtered = [aid for aid in ids if meta.get(aid, {}).get("category_id") == focus_cat_id]
            if filtered:
                ids = filtered
                meta = fetch_meta_map(db, ids)

        ids = _exclude_read(db, ids, user_id)
        ids = _filter_by_recent_days(meta, ids, days=7)
        if not ids:
            return []

        ids_sorted = sorted(ids, key=lambda x: _get_timestamp(meta.get(x, {})), reverse=True)[:topk]

        return [{
            "id": aid,
            "title": meta.get(aid, {}).get("title"),
            "images": (meta.get(aid, {}) or {}).get("images"),
            "url": meta.get(aid, {}).get("url"),
            "source": meta.get(aid, {}).get("source"),
            "category_name": meta.get(aid, {}).get("category_name"),
            "category_child_name": meta.get(aid, {}).get("category_child_name"),
            "published_at": meta.get(aid, {}).get("published_at"),
            "score": fused.get(aid, 0.0),
        } for aid in ids_sorted]

    # ------------------- 1) Topic affinity (category cha) -------------------
    aff: Dict[str, float] = compute_user_topic_affinity(db, user_id, log_collection="user_activity")
    sorted_aff = sorted(aff.items(), key=lambda x: x[1], reverse=True)
    focus_topic, focus_score = sorted_aff[0] if sorted_aff else (None, 0.0)
    second_topic, second_score = sorted_aff[1] if len(sorted_aff) > 1 else (None, 0.0)

    is_diverse_focus = False
    if focus_score > 0 and second_score > 0:
        is_diverse_focus = (second_score / focus_score >= AFFINITY_DIVERSITY_THRESHOLD)

    if focus_topic and is_diverse_focus and second_topic:
        cat_filter = [focus_topic, second_topic]
    else:
        cat_filter = [focus_topic] if focus_topic else list(allowed_categories)

    # ------------------- 2) Seed vectors -------------------
    if cat_filter and (focus_score >= min_focus):
        seed_vecs = _build_seed_vectors_from_recent_reads(
            db, embedder, user_id,
            category_names=cat_filter,
            days=30,
            read_limit=80,
            seed_count=6,
        )
    else:
        seed_vecs = _build_seed_vectors_from_recent_reads(
            db, embedder, user_id,
            category_names=list(allowed_categories),
            days=30,
            read_limit=80,
            seed_count=6,
        )

    seed_lists = _chroma_query_many_seeds(chroma_collection, seed_vecs, n_results_each=180)

    # fallback user_profile vector
    prof = build_or_get_user_profile(db, user_id, embedder)
    user_vec = prof.get("vector")
    ids_user_vec = chroma_by_user_vector(chroma_collection, user_vec, n_results=n_results_query) if user_vec is not None else []

    # ------------------- 3) Popular -------------------
    if cat_filter:
        ids_pop = top_popular_recent_by_categories(db, category_names=cat_filter, category_ids=None, limit=POPULARITY_LIMIT)
        if not ids_pop:
            ids_pop = top_popular_recent(db, limit=POPULARITY_LIMIT)
    else:
        ids_pop = top_popular_recent(db, limit=POPULARITY_LIMIT)

    # ------------------- 4) Build PHRASES -------------------
    phrases: List[str] = _build_interest_phrases_from_activity(db, user_id, category_names=cat_filter, days=30)
    if len(phrases) < 2:
        phrases += _build_interest_phrases_from_reads(db, user_id, category_names=cat_filter, days=30)

    seen = set()
    tmp = []
    for p in phrases:
        p = (p or "").strip().lower()
        if not p or p in seen:
            continue
        seen.add(p)
        tmp.append(p)
    phrases = tmp[:12]
    enable_phrase = bool(phrases)

    # ------------------- 5) Weighted fuse -------------------
    lists_for_rrf: List[List[str]] = []
    weights: List[float] = []

    if ids_user_vec:
        lists_for_rrf.append(ids_user_vec)
        weights.append(W_USER_VEC)

    for L in seed_lists:
        lists_for_rrf.append(L)
        weights.append(W_SEED)

    lists_for_rrf.append(ids_pop)
    weights.append(W_POP_WHEN_PHRASE if enable_phrase else W_POP_DEFAULT)

    fused = rrf_fuse_weighted(lists_for_rrf, weights, k=60)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)

    ids = [i for i, score in ranked if score >= HARD_SCORE_THRESHOLD] or [i for i, _ in ranked]
    ids = ids[:max(10 * topk, 220)]
    meta = fetch_meta_map(db, ids)  # ✅ meta có summary/keywords/entities nhờ sửa hybrid.py

    # ép category cha (nếu có)
    if cat_filter:
        filtered_ids = [aid for aid in ids if meta.get(aid, {}).get("category_name") in cat_filter]
        if filtered_ids:
            ids = filtered_ids
            meta = fetch_meta_map(db, ids)

    # lọc 7 ngày + exclude đã đọc
    ids = _filter_by_recent_days(meta, ids, days=7)
    ids = _exclude_read(db, ids, user_id)
    if not ids:
        return []

    # ------------------- 6) Boost (topic + keyword + entity + PHRASE) -------------------
    kw_aff = compute_user_keyword_affinity(db, user_id)
    max_kw = max(kw_aff.values()) if kw_aff else 0.0

    ent_aff = compute_user_entity_affinity(db, user_id, days=60, limit=600)
    max_ent = max(ent_aff.values()) if ent_aff else 0.0

    max_topic_aff = max(aff.values()) if aff else 0.0

    boosted_scores: Dict[str, float] = {}
    for aid in ids:
        m = meta.get(aid, {}) or {}
        base = fused.get(aid, 0.0)

        topic_extra = topic_soft_boost(m, aff, max_topic_aff) if max_topic_aff > 0 else 0.0
        keyword_extra = keyword_soft_boost(m, kw_aff, max_kw)
        entity_extra = entity_soft_boost(m, ent_aff, max_ent) if max_ent > 0 else 0.0

        title = m.get("title") or ""
        summary = m.get("summary") or ""
        text = f"{title} {summary}"

        hits = _count_phrase_hits(text, phrases)
        phrase_extra = 0.0
        if hits > 0:
            phrase_extra = PHRASE_BONUS + PHRASE_BONUS_EXTRA * max(0, hits - 1)
            phrase_extra = min(phrase_extra, PHRASE_BONUS_CAP)

        boosted_scores[aid] = (
            base
            + W_TOPIC * topic_extra
            + W_KEYWORD * keyword_extra
            + W_ENTITY * entity_extra
            + phrase_extra
        )

    # ------------------- 7) Sort + MMR diversify -------------------
    ordered = sorted(
        ids,
        key=lambda x: (-boosted_scores.get(x, 0.0), -_get_timestamp(meta.get(x, {}))),
    )[:max(6 * topk, 120)]

    sim = [[0.0 for _ in ordered] for __ in ordered]
    for i, a in enumerate(ordered):
        for j, b in enumerate(ordered):
            if i == j:
                sim[i][j] = 1.0
                continue
            ma, mb = meta.get(a, {}), meta.get(b, {})
            same_src = ma.get("source") and ma.get("source") == mb.get("source")
            same_cat = ma.get("category_name") and ma.get("category_name") == mb.get("category_name")
            sim[i][j] = 1.0 if (same_src or same_cat) else 0.0

    mmr_ids = mmr_rerank(ordered, sim, lambda_=0.78, topk=topk, relevance_map=boosted_scores)

    # ------------------- 8) Output -------------------
    out = []
    for aid in mmr_ids:
        m = meta.get(aid, {}) or {}
        out.append({
            "id": aid,
            "title": m.get("title"),
            "images": m.get("images"),
            "url": m.get("url"),
            "source": m.get("source"),
            "category_name": m.get("category_name"),
            "category_child_name": m.get("category_child_name"),
            "published_at": m.get("published_at"),
            "score": boosted_scores.get(aid, 0.0),
        })
    return out
