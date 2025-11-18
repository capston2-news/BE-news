# recommender/services/similar.py
from typing import Dict, Any, List, Set, Tuple
from bson import ObjectId
import numpy as np
from collections import defaultdict

from .utils import article_to_text
from .hybrid import (
    rrf_fuse,
    top_popular_recent,
    fetch_meta_map,
    mmr_rerank,
    chroma_by_user_vector,
    top_popular_recent_by_categories,
    topic_soft_boost,
)
from .topic_affinity import compute_user_topic_affinity
from .user_profile import build_or_get_user_profile


def _get_article(db, article_id):
    try:
        oid = ObjectId(article_id)
    except Exception:
        return None
    return db["articles"].find_one({"_id": oid})


def similar_by_article(db, chroma_collection, embedder, article_id, user_id=None, topk=10):
    # 1. Lấy bài gốc
    art = _get_article(db, article_id)
    if not art:
        return []

    base_cat = art.get("category_name") or None

    # 2. Encode bài gốc thành vector
    qvec = embedder.encode(
        [article_to_text(art)],
        normalize_embeddings=True,
        convert_to_numpy=True
    )[0].tolist()

    # 3. Query chroma lấy danh sách bài giống theo embedding
    q = chroma_collection.query(query_embeddings=[qvec], n_results=max(200, topk * 5))
    vec_ids = q.get("ids", [[]])[0]

    # Bỏ bài gốc
    ids_vec = [i for i in vec_ids if i != str(art["_id"])]

    # 4. Lấy popular để tăng độ đa dạng
    ids_pop = top_popular_recent(db, limit=200)

    # 5. Fuse embedding + popular
    fused = rrf_fuse([ids_vec, ids_pop], k=60)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)

    # List ban đầu
    ids = [i for i, _ in ranked[:max(4 * topk, 100)]]

    # 6. Fetch metadata
    meta = fetch_meta_map(db, ids)

    # 7. LỌC CỨNG THEO CATEGORY BÀI GỐC
    if base_cat:
        filtered = [i for i in ids if meta.get(i, {}).get("category_name") == base_cat]
        if filtered:
            ids = filtered

    # 8. Nếu còn rỗng → fallback theo embedding
    if not ids:
        ids = ids_vec[:topk]

    # 9. MMR để đa dạng nguồn
    if not ids:
        return []

    sim = [[0.0 for _ in ids] for __ in ids]
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i == j:
                sim[i][j] = 1.0
                continue
            mi, mj = meta.get(a, {}), meta.get(b, {})

            same_cat = mi.get("category_name") == mj.get("category_name")
            same_src = mi.get("source") == mj.get("source")
            sim[i][j] = 1.0 if (same_cat or same_src) else 0.0

    mmr_ids = mmr_rerank(ids, sim, lambda_=0.75, topk=topk)

    # 10. Output
    final_meta = fetch_meta_map(db, mmr_ids)
    out = [{
        "id": i,
        "title": final_meta.get(i, {}).get("title"),
        "url": final_meta.get(i, {}).get("url"),
        "source": final_meta.get(i, {}).get("source"),
        "category_name": final_meta.get(i, {}).get("category_name"),
        "published_at": final_meta.get(i, {}).get("published_at"),
        "score": fused.get(i, 0.0),
    } for i in mmr_ids]

    return out


def _exclude_read(db, ids: List[str], user_id: str) -> List[str]:
    """
    Loại bỏ các article mà user đã đọc khỏi danh sách ids.
    """
    seen = set(
        str(x["article_id"])
        for x in db["user_activity"].find(
            {"user_id": str(user_id)},
            {"article_id": 1}
        )
    )
    return [i for i in ids if i not in seen]


def _build_topic_profile_vector(db, embedder, user_id: str, topic_name: str, max_articles: int = 50):
    """
    Xây embedding profile RIÊNG cho 1 topic (vd: 'Tennis' hoặc 'Bóng đá Việt Nam')
    dựa trên các bài user đã đọc trong topic đó.
    """
    # Lấy log các bài user đã đọc trong topic này (mới nhất trước)
    logs = list(
        db["user_activity"].find(
            {
                "user_id": str(user_id),
                "category_name": topic_name,
            },
            {"article_id": 1, "ts": 1}
        ).sort("ts", -1).limit(max_articles)
    )
    if not logs:
        return None

    article_ids = [ObjectId(l["article_id"]) for l in logs if l.get("article_id")]
    if not article_ids:
        return None

    arts = list(
        db["articles"].find(
            {"_id": {"$in": article_ids}},
            {"title": 1, "content": 1, "summary": 1}
        )
    )
    if not arts:
        return None

    texts = [article_to_text(a) for a in arts]
    if not texts:
        return None

    embs = embedder.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True
    )

    if embs is None or len(embs) == 0:
        return None

    # Trung bình các vector -> 1 vector đại diện cho "gu" của user trong topic này
    topic_vec = np.mean(embs, axis=0).tolist()
    return topic_vec


def get_user_category_filters(
    db,
    user_id: str,
    log_collection: str = "user_activity",
    limit: int = 500,
):
    """
    Lấy ra:
      - allowed_categories: tất cả category_name user từng đọc
      - allowed_pairs: tất cả cặp (category_name, category_child_name) user từng đọc
    """

    logs = list(
        db[log_collection].find(
            {
                "user_id": str(user_id),
                "$or": [
                    {"category_name": {"$ne": None}},
                    {"category_child_name": {"$ne": None}},
                ],
            },
            {
                "category_name": 1,
                "category_child_name": 1,
                "subcategory_name": 1,
            },
        ).sort("_id", -1).limit(limit)
    )

    allowed_categories: Set[str] = set()
    allowed_pairs: Set[Tuple[str, str]] = set()

    for ev in logs:
        cat = ev.get("category_name")
        child = (
            ev.get("category_child_name")
            or ev.get("subcategory_name")
        )

        if cat:
            allowed_categories.add(cat)
        if cat or child:
            allowed_pairs.add((cat, child))

    return allowed_categories, allowed_pairs


# Ngưỡng score
HARD_SCORE_THRESHOLD = 0.0          # hạ xuống 0 và có fallback
SECONDARY_BOOST_THRESHOLD = 0.030   # ngưỡng cho bài secondary
AFFINITY_DIVERSITY_THRESHOLD = 0.6
POPULARITY_LIMIT = 50


def user_topic_feed(
    db,
    chroma_collection,
    embedder,
    user_id: str,
    topk: int = 10,
    min_focus: float = 0.3,
):
    """
    Feed cá nhân hoá theo category/content, có logic đa dạng hóa vector tìm kiếm
    và giới hạn ảnh hưởng của độ phổ biến.
    """
    n_results_query = max(300, topk * 5)

    # 0) Lấy tập category + (category, category_child) user đã đọc
    allowed_categories, allowed_pairs = get_user_category_filters(
        db, user_id, log_collection="user_activity"
    )

    # --- Fallback Global (cho người dùng mới / không có lịch sử) ---
    if not allowed_categories and not allowed_pairs:
        prof = build_or_get_user_profile(db, user_id, embedder)
        user_vec = prof.get("vector")

        if user_vec is not None:
            ids_vec = chroma_by_user_vector(chroma_collection, user_vec, n_results=n_results_query)
        else:
            ids_vec = []

        ids_pop = top_popular_recent(db, limit=POPULARITY_LIMIT)

        fused = rrf_fuse([ids_vec, ids_pop], k=60)
        ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)

        ids_filtered_by_score = [i for i, score in ranked if score >= HARD_SCORE_THRESHOLD]
        if not ids_filtered_by_score:
            ids_filtered_by_score = [i for i, _ in ranked]

        ids = ids_filtered_by_score[:max(4 * topk, 80)]

        meta = fetch_meta_map(db, ids)
        ids = _exclude_read(db, ids, user_id)

        out = []
        for i in ids[:topk]:
            m = meta.get(i, {}) or {}
            out.append({
                "id": i,
                "title": m.get("title"),
                "url": m.get("url"),
                "source": m.get("source"),
                "category_name": m.get("category_name"),
                "published_at": m.get("published_at"),
                "score": fused.get(i, 0.0),
            })
        return out

    # 1) Affinity theo category
    aff: Dict[str, float] = compute_user_topic_affinity(
        db,
        user_id,
        log_collection="user_activity",
    )

    # 2) Chọn vector tìm kiếm (topic_vec Hẹp hay user_vec Rộng)
    sorted_aff = sorted(aff.items(), key=lambda x: x[1], reverse=True)
    focus_topic, focus_score = sorted_aff[0] if sorted_aff else (None, 0.0)
    second_topic, second_score = sorted_aff[1] if len(sorted_aff) > 1 else (None, 0.0)

    is_diverse_focus = False
    if focus_score > 0 and second_score > 0:
        is_diverse_focus = (second_score / focus_score >= AFFINITY_DIVERSITY_THRESHOLD)

    topic_vec_to_use = None

    if focus_topic and (not is_diverse_focus) and (focus_score >= min_focus):
        # Trường hợp 1: Tập trung RẤT MẠNH vào một topic
        topic_vec_to_use = _build_topic_profile_vector(
            db, embedder, user_id, focus_topic, max_articles=50,
        )

    if topic_vec_to_use is not None:
        ids_vec = chroma_by_user_vector(chroma_collection, topic_vec_to_use, n_results=n_results_query)
    else:
        # Trường hợp 2: Đa dạng sở thích -> Dùng vector profile chung
        prof = build_or_get_user_profile(db, user_id, embedder)
        user_vec = prof.get("vector")
        if user_vec is not None:
            ids_vec = chroma_by_user_vector(chroma_collection, user_vec, n_results=n_results_query)
        else:
            ids_vec = []

    # 3) Popular ưu tiên trong allowed_categories
    ids_pop = top_popular_recent_by_categories(
        db,
        category_names=list(allowed_categories),
        category_ids=None,
        limit=POPULARITY_LIMIT,
    )
    if not ids_pop:
        ids_pop = top_popular_recent(db, limit=POPULARITY_LIMIT)

    # 4) Fuse embedding + popular
    fused = rrf_fuse([ids_vec, ids_pop], k=60)
    ranked = sorted(fused.items(), key=lambda x: x[1], reverse=True)

    ids_filtered_by_score = [i for i, score in ranked if score >= HARD_SCORE_THRESHOLD]
    if not ids_filtered_by_score:
        ids_filtered_by_score = [i for i, _ in ranked]

    base_ids = ids_filtered_by_score[:max(4 * topk, 80)]

    if not base_ids:
        # Fallback nữa nếu vẫn rỗng
        base_ids = ids_pop[:max(4 * topk, 80)]

    meta = fetch_meta_map(db, base_ids)

    # 5) TÍNH BOOSTED SCORE TRƯỚC VÀ PHÂN LOẠI PRIMARY/SECONDARY
    max_aff = max(aff.values()) if aff else 0.0
    boosted_scores: Dict[str, float] = {}

    for aid in base_ids:
        m = meta.get(aid, {}) or {}
        base = fused.get(aid, 0.0)
        extra = topic_soft_boost(m, aff, max_aff)
        boosted_scores[aid] = base + 0.30 * extra

    def _get_child_name(m: Dict[str, Any]) -> str:
        return (
            m.get("category_child_name")
            or m.get("subcategory_name")
            or m.get("category_child")
            or m.get("subcategory")
        )

    primary_ids: List[str] = []
    secondary_ids: List[str] = []

    for aid in base_ids:
        m = meta.get(aid, {}) or {}
        cat = m.get("category_name")
        child = _get_child_name(m)
        pair = (cat, child)

        is_relevant_category = (cat and cat in allowed_categories)
        is_relevant_pair = (pair and pair in allowed_pairs)

        if is_relevant_category or is_relevant_pair:
            primary_ids.append(aid)
        else:
            if boosted_scores.get(aid, 0.0) >= SECONDARY_BOOST_THRESHOLD:
                secondary_ids.append(aid)

    # 6) Bỏ bài đã đọc
    primary_ids = _exclude_read(db, primary_ids, user_id)
    secondary_ids = _exclude_read(db, secondary_ids, user_id)

    # 7) Nếu vẫn rỗng -> fallback về popular global
    if not primary_ids and not secondary_ids:
        ids_pop_fallback = top_popular_recent(db, limit=max(4 * topk, 80))
        ids_pop_fallback = _exclude_read(db, ids_pop_fallback, user_id)

        if not ids_pop_fallback:
            return []

        ordered_ids = ids_pop_fallback[:topk]
        final_meta = fetch_meta_map(db, ordered_ids)
        out = []
        for aid in ordered_ids:
            m = final_meta.get(aid, {}) or {}
            out.append({
                "id": aid,
                "title": m.get("title"),
                "url": m.get("url"),
                "source": m.get("source"),
                "category_name": m.get("category_name"),
                "published_at": m.get("published_at"),
                "score": fused.get(aid, 0.0) if 'fused' in locals() else 0.0,
            })
        return out

    # 8) Sắp xếp theo boosted_scores
    primary_sorted = sorted(
        primary_ids, key=lambda x: boosted_scores.get(x, 0.0), reverse=True
    )
    secondary_sorted = sorted(
        secondary_ids, key=lambda x: boosted_scores.get(x, 0.0), reverse=True
    )

    ordered_ids = primary_sorted + secondary_sorted
    ordered_ids = ordered_ids[:max(2 * topk, topk + 5)]

    # 9) MMR
    if len(ordered_ids) > 1:
        sim = [[0.0 for _ in ordered_ids] for __ in ordered_ids]
        for i, a in enumerate(ordered_ids):
            for j, b in enumerate(ordered_ids):
                if i == j:
                    sim[i][j] = 1.0
                    continue
                mi, mj = meta.get(a, {}), meta.get(b, {})
                same_src = mi.get("source") and mi.get("source") == mj.get("source")
                sim[i][j] = 1.0 if same_src else 0.0

        mmr_ids = mmr_rerank(ordered_ids, sim, lambda_=0.75, topk=topk)
    else:
        mmr_ids = ordered_ids[:topk]

    # 10) Output
    final_meta = fetch_meta_map(db, mmr_ids)

    out = []
    for aid in mmr_ids:
        m = final_meta.get(aid, {}) or {}
        out.append({
            "id": aid,
            "title": m.get("title"),
            "url": m.get("url"),
            "source": m.get("source"),
            "category_name": m.get("category_name"),
            "published_at": m.get("published_at"),
            "score": boosted_scores.get(aid, 0.0),
        })
    return out
