from datetime import datetime, timezone
from bson import ObjectId
from .utils import get_category_name, get_category_child_name

def log_activity(db, user_id: str, article_id: str, action: str = "view"):
    try:
        aid = ObjectId(article_id)
    except Exception:
        return False

    art = db["articles"].find_one(
        {"_id": aid},
        {
            "topics": 1,
            "source": 1,
            "country": 1,
            "category_id": 1,
            "category_child_id": 1,
        }
    )

    topics = (art or {}).get("topics", [])
    if isinstance(topics, list):
        topics = [str(x) for x in topics]

    cat_id = (art or {}).get("category_id")
    child_id = (art or {}).get("category_child_id")
    cat_name = get_category_name(db, cat_id)
    child_name = get_category_child_name(db, child_id)

    doc = {
        "user_id": user_id,
        "article_id": aid,
        "action": action,
        "ts": datetime.now(timezone.utc),
        "topics": topics,
        "source": (art or {}).get("source"),
        "country": (art or {}).get("country"),
        "category_id": cat_id,
        "category_child_id": child_id,
        "category_name": cat_name,
        "category_child_name": child_name,
    }
    db["user_activity"].insert_one(doc)

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