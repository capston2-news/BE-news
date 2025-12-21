from __future__ import annotations

from typing import Any, Dict, Optional
from datetime import datetime, timezone, date

from bson import ObjectId
from rest_framework.views import APIView
from rest_framework.response import Response

from api.db import get_db
from api.permissions import IsAuthenticated, RoleRequired


def ser(obj: Any):
    """Convert Mongo types -> JSON serializable primitives."""
    if obj is None:
        return None

    if isinstance(obj, ObjectId):
        return str(obj)

    if isinstance(obj, (datetime, date)):
        s = obj.isoformat()
        return s.replace("+00:00", "Z")

    if isinstance(obj, dict):
        return {k: ser(v) for k, v in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [ser(v) for v in obj]

    return obj


def ser_doc(doc: Any):
    """Serialize Mongo doc + normalize _id -> id."""
    d = ser(doc)
    if isinstance(d, dict) and "_id" in d:
        d["id"] = d.pop("_id")
    return d


# ---------------------------
# ObjectId helpers
# ---------------------------
def _to_oid(v: Any) -> Optional[ObjectId]:
    if v is None:
        return None
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, dict) and "$oid" in v:
        try:
            return ObjectId(v["$oid"])
        except Exception:
            return None
    try:
        s = str(v).strip()
        return ObjectId(s) if ObjectId.is_valid(s) else None
    except Exception:
        return None


def to_oid(v: Any) -> Optional[ObjectId]:
    return _to_oid(v)


# ---------------------------
# Index helper (optional)
# ---------------------------
def ensure_indexes(comments_col):
    # Bạn có thể thêm index nếu cần, để trống cũng OK
    try:
        comments_col.create_index([("created_at", -1)])
        comments_col.create_index([("is_checked", 1), ("is_deleted", 1)])
    except Exception:
        pass