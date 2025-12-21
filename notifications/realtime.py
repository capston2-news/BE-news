# notifications/realtime.py
import json
import queue
import threading
from typing import Dict, Any, Set

try:
    from bson import ObjectId
except Exception:
    ObjectId = None

_lock = threading.Lock()
_subs: Dict[str, Set[queue.Queue]] = {}


def _norm_key(user_key: Any) -> str:
    """Chuẩn hoá key để subscribe/publish luôn khớp nhau."""
    if not user_key:
        return ""
    # ObjectId -> "hex"
    if ObjectId is not None and isinstance(user_key, ObjectId):
        return str(user_key)
    # {"$oid": "..."}
    if isinstance(user_key, dict) and "$oid" in user_key:
        return str(user_key["$oid"]).strip()
    return str(user_key).strip()


def subscribe(user_key: Any) -> queue.Queue:
    key = _norm_key(user_key)
    q = queue.Queue()
    with _lock:
        _subs.setdefault(key, set()).add(q)
    return q


def unsubscribe(user_key: Any, q: queue.Queue) -> None:
    key = _norm_key(user_key)
    with _lock:
        s = _subs.get(key)
        if not s:
            return
        s.discard(q)
        if not s:
            _subs.pop(key, None)


def publish(user_key: Any, event: str, payload: Any) -> None:
    """
    SSE format:
      event: notification
      data: {...json...}
    """
    key = _norm_key(user_key)
    if not key:
        return

    if not event:
        event = "message"

    data = json.dumps(payload, ensure_ascii=False, default=str)
    msg = f"event: {event}\ndata: {data}\n\n"

    with _lock:
        queues = list(_subs.get(key, set()))

    for q in queues:
        try:
            q.put_nowait(msg)
        except Exception:
            pass
