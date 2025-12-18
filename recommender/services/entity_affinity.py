# recommender/services/entity_affinity.py
from __future__ import annotations

from typing import Dict, Any, List, Optional
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from bson import ObjectId
import math
import re


def _to_oid(x) -> Optional[ObjectId]:
    if x is None:
        return None
    if isinstance(x, ObjectId):
        return x
    s = str(x)
    if ObjectId.is_valid(s):
        return ObjectId(s)
    return None


def _dt_utc(x):
    if x is None:
        return None
    if isinstance(x, datetime):
        dt = x
    else:
        try:
            dt = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _norm_ent(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    # normalize một vài kiểu phổ biến
    s = s.replace("tphcm", "tp.hcm").replace("tp hcm", "tp.hcm")
    s = s.replace("tp.hcm.", "tp.hcm")
    return s


def compute_user_entity_affinity(
    db,
    user_id: str,
    days: int = 60,
    limit: int = 600,
) -> Dict[str, float]:
    """
    Trả về dict {entity: score} dựa trên lịch sử đọc.
    - Lookup bài từ articles bằng article_id
    - Lấy articles.entities (list[str])
    - Có decay theo thời gian + trọng số theo action
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    logs = list(db["user_activity"].find(
        {"user_id": str(user_id), "ts": {"$gte": since}},
        {"article_id": 1, "action": 1, "ts": 1}
    ).sort("ts", -1).limit(limit))

    if not logs:
        return {}

    # gom article_ids
    a_oids: List[ObjectId] = []
    meta = []
    for ev in logs:
        oid = _to_oid(ev.get("article_id"))
        if oid:
            a_oids.append(oid)
            meta.append((oid, ev.get("action") or "view", ev.get("ts")))

    if not a_oids:
        return {}

    arts = list(db["articles"].find(
        {"_id": {"$in": a_oids}},
        {"entities": 1}
    ))
    ent_map = {str(a["_id"]): (a.get("entities") or []) for a in arts}

    # action weight
    w_action = {
        "view": 1.0,
        "click": 1.0,
        "like": 2.0,
        "bookmark": 3.0,
        "share": 3.5,
    }

    now = datetime.now(timezone.utc)
    freq = defaultdict(float)

    for oid, action, ts in meta:
        ents = ent_map.get(str(oid), [])
        if not ents:
            continue

        dt = _dt_utc(ts)
        if not dt:
            continue

        age_h = max((now - dt).total_seconds() / 3600.0, 1.0)
        decay = math.exp(-age_h / 72.0)

        wa = w_action.get(action, 1.0)

        for e in ents:
            k = _norm_ent(str(e))
            if not k:
                continue
            freq[k] += wa * decay

    return dict(freq)


def entity_soft_boost(meta_item: dict, ent_aff: Dict[str, float], max_ent: float) -> float:
    """
    meta_item cần có "entities": list[str]
    """
    if not ent_aff or max_ent <= 0:
        return 0.0

    ents = meta_item.get("entities") or []
    if not isinstance(ents, list) or not ents:
        return 0.0

    s = 0.0
    for e in ents:
        k = _norm_ent(str(e))
        if k and k in ent_aff:
            s += ent_aff[k]

    return min(1.0, s / max_ent)  # normalize 0..1
