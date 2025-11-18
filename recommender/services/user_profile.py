from datetime import datetime, timezone
from bson import ObjectId
from .utils import article_to_text
import numpy as np
from collections import Counter


def build_or_get_user_profile(db, user_id: str, embedder, max_events=100):
    """
    Lấy các bài user đã xem gần đây -> encode -> trung bình vector.
    """
    evs = list(db["user_activity"].find({"user_id": user_id}).sort("ts", -1).limit(max_events))
    if not evs:
        return {"vector": None, "topic_affinity": {}}

    art_ids = [e.get("article_id") for e in evs if e.get("article_id")]
    # fetch bài
    arts = list(db["articles"].find({"_id": {"$in": art_ids}}))
    texts = [article_to_text(a) for a in arts]
    if not texts:
        return {"vector": None, "topic_affinity": {}}

    vecs = embedder.encode(texts, normalize_embeddings=True, batch_size=64, convert_to_numpy=True)
    prof = vecs.mean(axis=0)
    try: prof = prof.tolist()
    except: pass
    return {"vector": prof, "topic_affinity": {}}
def get_user_top_categories(db, user_id, min_focus=0.5, max_cats=3):
    cursor = db["user_activity"].find(
        {"user_id": user_id, "category_id": {"$ne": None}},
        {"category_id": 1, "category_name": 1}
    )

    counts = Counter()
    name_map = {}
    total = 0
    for act in cursor:
        cid = str(act.get("category_id"))
        cname = act.get("category_name")
        counts[cid] += 1
        total += 1
        if cname:
            name_map[cid] = cname

    if total == 0:
        return []  # user mới, chưa có dữ liệu

    top = []
    for cid, cnt in counts.most_common(max_cats):
        focus = cnt / total
        if focus >= min_focus:
            top.append({
                "category_id": cid,
                "category_name": name_map.get(cid),
                "focus": focus,
            })

    return top
