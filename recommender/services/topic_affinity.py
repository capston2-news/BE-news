# recommender/services/topic_affinity.py

from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict
from bson import ObjectId


def _ensure_aware_utc(dt):
    """
    Đưa datetime về dạng aware UTC.
    - None -> None
    - String ISO -> parse -> UTC
    - Naive -> gắn tzinfo=UTC
    - Aware (múi khác) -> convert về UTC
    """
    if dt is None:
        return None

    if isinstance(dt, str):
        try:
            # hỗ trợ cả ...Z
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None

    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    return None


def _decay_weight(ts, now=None, half_life_h: float = 72.0) -> float:
    """
    Trọng số suy giảm theo thời gian (half-life).
    Sau half_life_h giờ, weight còn 0.5.
    """
    now = _ensure_aware_utc(now or datetime.now(timezone.utc))
    ts = _ensure_aware_utc(ts) or now
    dt_h = (now - ts).total_seconds() / 3600.0
    if dt_h < 0:
        dt_h = 0.0
    return 0.5 ** (dt_h / half_life_h)


def compute_user_topic_affinity(
    db,
    user_id: str,
    log_collection: str = "user_activity",
    categories_collection: str = "categories",
    half_life_h: float = 72.0,
):
    """
    Tính độ "mê" của user đối với từng category (topic) dựa trên user_activity.

    Ưu tiên các field sau trong user_activity:
        - category_name
        - category_id (ObjectId hoặc string)

    Nếu chỉ có category_id:
        -> join sang collection `categories` để lấy `name`
        -> sử dụng `categories.name` làm topic (VD: "Bóng đá Việt Nam", "Tennis", "Thời sự")

    Kết quả trả về dạng:
        {
            "Bóng đá Việt Nam": 0.6,
            "Tennis": 0.3,
            "Thời sự": 0.1,
        }
    """

    now = datetime.now(timezone.utc)

    # 1) Lấy toàn bộ log có liên quan đến category
    logs = list(
        db[log_collection].find(
            {
                "user_id": str(user_id),
                "$or": [
                    {"category_name": {"$ne": None}},
                    {"category_id": {"$ne": None}},
                ],
            },
            {
                "category_name": 1,
                "category_id": 1,
                "ts": 1,
                "timestamp": 1,
                "created_at": 1,
            },
        )
    )

    if not logs:
        return {}

    # 2) Gom tất cả category_id xuất hiện trong log để load 1 lần
    category_ids = set()
    for ev in logs:
        cid = ev.get("category_id")
        if cid:
            # có thể đang là string hoặc ObjectId
            if isinstance(cid, str):
                try:
                    cid = ObjectId(cid)
                except Exception:
                    # để nguyên string, vẫn cho join theo string nếu bạn lưu như vậy
                    pass
            category_ids.add(cid)

    # 3) Map category_id -> category_name (name trong bảng categories)
    id_to_name = {}
    if category_ids:
        cat_docs = db[categories_collection].find(
            {"_id": {"$in": list(category_ids)}},
            {"name": 1, "slug": 1},
        )
        for c in cat_docs:
            cid = c.get("_id")
            name = c.get("name") or c.get("slug")
            if name:
                id_to_name[cid] = name

    # 4) Tính score với trọng số decay theo thời gian
    scores = defaultdict(float)

    for ev in logs:
        # Ưu tiên category_name trong log
        topic = ev.get("category_name")

        # Nếu không có category_name -> dùng category_id -> tra trong categories
        if not topic:
            cid = ev.get("category_id")
            if isinstance(cid, str):
                try:
                    cid_obj = ObjectId(cid)
                except Exception:
                    cid_obj = cid  # nếu không convert được thì để nguyên
            else:
                cid_obj = cid
            topic = id_to_name.get(cid_obj)

        if not topic:
            # Không suy được topic -> bỏ qua log này
            continue

        # Chọn timestamp: ưu tiên ts -> timestamp -> created_at
        ts = ev.get("ts") or ev.get("timestamp") or ev.get("created_at")
        w = _decay_weight(ts, now=now, half_life_h=half_life_h)
        scores[topic] += w

    if not scores:
        return {}

    total = sum(scores.values())
    if total <= 0:
        return {}

    aff = {topic: val / total for topic, val in scores.items()}
    return aff
