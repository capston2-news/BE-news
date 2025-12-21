from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId
from api.db import get_db
from .realtime import publish

def _to_oid(v):
    if not v:
        return None
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, dict) and "$oid" in v:
        s = str(v["$oid"]).strip()
        return ObjectId(s) if ObjectId.is_valid(s) else None
    s = str(v).strip()
    return ObjectId(s) if ObjectId.is_valid(s) else None


def create_notification_comment_approved(user_id: Any, comment_id: Any, message: str) -> Dict[str, Any]:
    """
    Lưu notification vào DB (ObjectId OK) nhưng payload trả về / publish
    thì convert sang string hết để FE dễ xử lý.
    """
    db = get_db()
    uoid = _to_oid(user_id)
    coid = _to_oid(comment_id)
    if not uoid or not coid:
        raise ValueError("Invalid user_id/comment_id")

    now = datetime.now(timezone.utc)

    doc = {
        "user_id": uoid,
        "comment_id": coid,
        "message": message,
        "is_read": False,
        "created_at": now,
        "type": "comment_approved",
    }
    r = db["notifications"].insert_one(doc)

    notification_payload = {
        "id": str(r.inserted_id),
        "user_id": str(uoid),
        "comment_id": str(coid),
        "message": message,
        "is_read": False,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "type": "comment_approved",
    }

    # Nếu bạn có realtime pub/sub:
    # publish(str(uoid), "notification", {"notification": notification_payload})

    return notification_payload

