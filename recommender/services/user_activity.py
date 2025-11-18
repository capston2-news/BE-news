from datetime import datetime, timezone
from bson import ObjectId

def log_activity(db, user_id: str, article_id: str, action: str = "view"):
    try:
        aid = ObjectId(article_id)
    except Exception:
        return False

    art = db["articles"].find_one({"_id": aid}, {"topics": 1, "source": 1, "country": 1})
    topics = art.get("topics", []) if art else []
    if isinstance(topics, list):
        topics = [str(x) for x in topics]

    doc = {
        "user_id": user_id,
        "article_id": aid,
        "action": action,
        "ts": datetime.now(timezone.utc),
        "topics": topics,
        "source": (art or {}).get("source"),
        "country": (art or {}).get("country"),
    }
    db["user_activity"].insert_one(doc)

    # cập nhật chỉ số phổ biến
    db["article_popularity"].update_one(
        {"_id": aid},
        {"$inc": {"views": 1}, "$setOnInsert": {"first_seen": datetime.now(timezone.utc)}},
        upsert=True
    )
    return True
def compute_user_topic_affinity(db, user_id, log_collection="user_activity"):
    cur = db[log_collection].find(
        {"user_id": user_id},
        {"category_name": 1, "category_child_name": 1}
    )

    counts = Counter()
    total = 0

    for row in cur:
        topic = row.get("category_child_name") or row.get("category_name")
        if not topic:
            continue
        counts[topic] += 1
        total += 1

    if total == 0:
        return {}

    aff = {topic: cnt / total for topic, cnt in counts.items()}
    return aff