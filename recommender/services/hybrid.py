from django.conf import settings
from bson import ObjectId
import math
from .topic_affinity import compute_user_topic_affinity

# ---------- RRF ----------
def rrf_fuse(lists, k=60):
    score = {}
    for L in lists:
        for rank, id_ in enumerate(L, start=1):
            score[id_] = score.get(id_, 0.0) + 1.0 / (k + rank)
    return score

# ---------- MMR ----------
def mmr_rerank(ids, sim_matrix, lambda_=0.7, topk=10):
    selected = []
    candidates = set(range(len(ids)))
    while candidates and len(selected) < topk:
        best, best_score = None, -1e9
        for i in list(candidates):
            # relevance ~ sim với chính mình = 1 (hoặc dùng fused score bên ngoài)
            relevance = 1.0
            diversity = max([sim_matrix[i][j] for j in selected], default=0.0)
            s = lambda_ * relevance - (1.0 - lambda_) * diversity
            if s > best_score:
                best_score, best = s, i
        selected.append(best)
        candidates.remove(best)
    return [ids[i] for i in selected]

# ---------- Popular + recent ----------
def top_popular_recent(db, limit=200):
    # yêu cầu có collection article_popularity {_id: article_id, views: int}
    # join articles để lấy published_at
    pipeline = [
        {"$lookup":{"from":"articles","localField":"_id","foreignField":"_id","as":"a"}},
        {"$unwind":"$a"},
        {"$project":{
            "_id":1,"views":{"$ifNull":["$views",0]},
            "published_at":"$a.published_at"
        }}
    ]
    docs = list(db["article_popularity"].aggregate(pipeline))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    scored = []
    for d in docs:
        views = d.get("views", 0)
        pub = d.get("published_at")
        age_h = 24.0
        if pub:
            try: age_h = max((now - pub).total_seconds()/3600.0, 1.0)
            except: pass
        rec = math.exp(-age_h/72.0)
        score = (views/10.0) + 2.0*rec
        scored.append((str(d["_id"]), score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [i for i,_ in scored[:limit]]

def top_popular_recent_by_categories(db, category_names=None, category_ids=None, limit=200):
    pipeline = [
        {"$lookup":{"from":"articles","localField":"_id","foreignField":"_id","as":"a"}},
        {"$unwind":"$a"},
    ]
    if category_names:
        pipeline.append({"$match":{"a.category_name":{"$in":category_names}}})
    if category_ids:
        pipeline.append({"$match":{"a.category_id":{"$in":category_ids}}})
    pipeline.append({"$project":{"_id":1,"views":{"$ifNull":["$views",0]},"published_at":"$a.published_at"}})
    docs = list(db["article_popularity"].aggregate(pipeline))
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    scored = []
    for d in docs:
        views = d.get("views", 0)
        pub = d.get("published_at")
        age_h = 24.0
        if pub:
            try: age_h = max((now - pub).total_seconds()/3600.0, 1.0)
            except: pass
        rec = math.exp(-age_h/72.0)
        score = (views/10.0) + 2.0*rec
        scored.append((str(d["_id"]), score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [i for i,_ in scored[:limit]]

# ---------- Chroma helpers ----------
def chroma_by_user_vector(collection, user_vec, n_results=300):
    if user_vec is None:
        # không có hồ sơ, trả rỗng -> rrf sẽ dựa nhiều vào popular
        return []
    q = collection.query(query_embeddings=[user_vec], n_results=n_results)
    return q.get("ids",[[]])[0]

# ---------- Fetch meta ----------
def fetch_meta_map(db, ids):
    """
    Trả về map {article_id(str): {...meta...}}
    Trong đó có luôn:
      - category_id
      - category_child_id
      - category_name (ưu tiên child, nếu không có dùng category cha)
    """
    # 1) Convert string id -> ObjectId
    oids = []
    for s in ids:
        try:
            oids.append(ObjectId(s))
        except Exception:
            pass

    # 2) Lấy articles
    cur = db["articles"].find(
        {"_id": {"$in": oids}},
        {
            "title": 1,
            "site": 1,
            "external_url": 1,
            "published_at": 1,
            "category_id": 1,
            "category_child_id": 1,
        }
    )

    # 3) Gom tất cả category_id, category_child_id lại để query 1 lần
    cat_ids = set()
    child_ids = set()
    docs = []
    for d in cur:
        docs.append(d)
        if d.get("category_id"):
            cat_ids.add(d["category_id"])
        if d.get("category_child_id"):
            child_ids.add(d["category_child_id"])

    # 4) Map id -> name cho categories & category_child
    cat_map = {}
    if cat_ids:
        for c in db["categories"].find({"_id": {"$in": list(cat_ids)}}, {"name": 1}):
            cat_map[str(c["_id"])] = c.get("name", "")

    child_map = {}
    if child_ids:
        for c in db["category_child"].find({"_id": {"$in": list(child_ids)}}, {"name": 1}):
            child_map[str(c["_id"])] = c.get("name", "")

    # 5) Build output meta map
    out = {}
    for d in docs:
        aid = str(d["_id"])
        cat_id = d.get("category_id")
        child_id = d.get("category_child_id")

        cat_id_str = str(cat_id) if cat_id else ""
        child_id_str = str(child_id) if child_id else ""

        # Ưu tiên tên category con, nếu không có thì dùng tên category cha
        cat_name = ""
        if child_id_str and child_id_str in child_map:
            cat_name = child_map[child_id_str]
        elif cat_id_str and cat_id_str in cat_map:
            cat_name = cat_map[cat_id_str]

        out[aid] = {
            "title": d.get("title"),
            "source": d.get("site"),
            "url": d.get("external_url"),
            "published_at": d.get("published_at"),
            "category_id": cat_id_str,
            "category_child_id": child_id_str,
            "category_name": cat_name or "",
        }

    return out

# ---------- Topic helpers (boost mềm) ----------
def topic_for_meta(meta_item: dict) -> str:
    """Ưu tiên category_child_name; nếu không có thì dùng category_name."""
    child = (meta_item.get("category_child_name") or "").strip()
    if child:
        return child
    return (meta_item.get("category_name") or "").strip()

def topic_soft_boost(meta_item: dict, aff: dict[str, float], max_aff: float) -> float:
    """Trả về boost 0–1 dựa trên mức user mê topic đó."""
    if not aff or max_aff <= 0:
        return 0.0
    topic = topic_for_meta(meta_item)
    raw = aff.get(topic, 0.0)
    if raw <= 0:
        return 0.0
    return raw / max_aff

# ---------- Hybrid recommend ----------
def hybrid_recommend(
    db, chroma_collection, embedder, user_profile, topk=20,
    only_categories=None, only_category_ids=None, soft_boost=False
):
    only_categories = only_categories or []
    only_category_ids = only_category_ids or []

    # 1) lấy candidate từ vector profile + popular
    ids_vec = chroma_by_user_vector(chroma_collection, user_profile.get("vector"), n_results=300)
    if only_categories or only_category_ids:
        ids_pop = top_popular_recent_by_categories(db, only_categories, only_category_ids, limit=300)
    else:
        ids_pop = top_popular_recent(db, limit=300)

    fused = rrf_fuse([ids_vec, ids_pop], k=60)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)
    ids = [i for i, _ in ranked[:max(4 * topk, 80)]]

    meta = fetch_meta_map(db, ids)

    # lọc cứng theo category (nếu FE yêu cầu)
    if only_categories or only_category_ids:
        ids = [i for i in ids if (
            (meta.get(i,{}).get("category_name") in set(only_categories)) or
            (meta.get(i,{}).get("category_id") in set(only_category_ids))
        )] or ids_pop
        meta = fetch_meta_map(db, ids)

    # 2) boost mềm theo topic user hay đọc (nếu bật soft_boost)
    boosted_scores = {}
    if soft_boost:
        uid = user_profile.get("user_id") or user_profile.get("id")
        aff = compute_user_topic_affinity(db, uid) if uid else {}
        max_aff = max(aff.values()) if aff else 0.0

        for i in ids:
            base = fused.get(i, 0.0)
            extra = topic_soft_boost(meta.get(i, {}), aff, max_aff)
            # 0.15 = 15% trọng số cho sở thích chủ đề
            boosted_scores[i] = base + 0.15 * extra

        ids = sorted(ids, key=lambda x: boosted_scores[x], reverse=True)
    else:
        boosted_scores = fused

    # 3) MMR đa dạng hoá theo source/category
    sim = [[0.0 for _ in ids] for __ in ids]
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i==j:
                sim[i][j] = 1.0
                continue
            mi, mj = meta.get(a,{}), meta.get(b,{})
            same_cat = mi.get("category_name") and mi.get("category_name")==mj.get("category_name")
            same_src = mi.get("source") and mi.get("source")==mj.get("source")
            sim[i][j] = 1.0 if (same_cat or same_src) else 0.0

    mmr_ids = mmr_rerank(ids, sim, lambda_=0.7, topk=topk)
    final_meta = fetch_meta_map(db, mmr_ids)
    out = [{
        "id": i,
        "title": final_meta.get(i,{}).get("title"),
        "url": final_meta.get(i,{}).get("url"),
        "source": final_meta.get(i,{}).get("source"),
        "category_id": final_meta.get(i,{}).get("category_id"),
        "category_name": final_meta.get(i,{}).get("category_name"),
        "published_at": final_meta.get(i,{}).get("published_at"),
        "score": boosted_scores.get(i, 0.0),
    } for i in mmr_ids]
    return out
