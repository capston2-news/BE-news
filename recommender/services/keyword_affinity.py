# recommender/services/keyword_affinity.py

from __future__ import annotations
from typing import Dict, Any, List
from collections import defaultdict
from datetime import datetime, timezone
from bson import ObjectId
import math
import re

# Stopword cơ bản, em có thể mở rộng thêm
VI_STOPWORDS = {
    "và", "là", "của", "trong", "một", "những", "các", "được",
    "với", "cho", "khi", "này", "đã", "ở", "tại", "vì", "do",
    "thì", "lên", "xuống", "ngày", "tháng", "năm", "trên", "dưới",
}


def simple_tokenize(text: str) -> List[str]:
    """
    Tokenize rất đơn giản:
      - lowercase
      - tách theo non-word
      - bỏ từ ngắn, số, stopword
    """
    if not text:
        return []

    text = text.lower()
    tokens = re.split(r"[^\w]+", text)
    out = []
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok in VI_STOPWORDS:
            continue
        # bỏ token quá ngắn, toàn số
        if len(tok) <= 2:
            continue
        if tok.isdigit():
            continue
        out.append(tok)
    return out


def compute_user_keyword_affinity(
    db,
    user_id: str,
    log_collection: str = "user_activity",
    max_events: int = 1000,
    half_life_days: float = 7.0,
) -> Dict[str, float]:
    """
    Trả về: { "ronaldo": score, "gucci": score, "vnindex": score, ... }

    - Lấy lịch sử đọc (view) gần nhất
    - Tokenize title + summary + (optional) content
    - Dùng decay theo thời gian: bài càng mới -> điểm càng lớn
    - Chuẩn hoá score về 0–1
    """
    logs = list(
        db[log_collection]
        .find({"user_id": str(user_id)}, {"article_id": 1, "ts": 1})
        .sort("ts", -1)
        .limit(max_events)
    )
    if not logs:
        return {}

    article_ids = []
    for ev in logs:
        aid = ev.get("article_id")
        try:
            article_ids.append(ObjectId(aid))
        except Exception:
            continue

    if not article_ids:
        return {}

    # Lấy title/summary/content
    art_map: Dict[str, Any] = {}
    cur = db["articles"].find(
        {"_id": {"$in": article_ids}},
        {
            "title": 1,
            "summary": 1,
            "content": 1,
        },
    )
    for d in cur:
        art_map[str(d["_id"])] = d

    now = datetime.now(timezone.utc)
    lambda_ = math.log(2) / (half_life_days * 86400.0)  # half-life

    score = defaultdict(float)

    for ev in logs:
        aid = ev.get("article_id")
        art = art_map.get(aid)
        if not art:
            continue

        ts = ev.get("ts")
        if isinstance(ts, datetime):
            dt = ts
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        else:
            dt = now

        age_sec = max((now - dt).total_seconds(), 0.0)
        w_time = math.exp(-lambda_ * age_sec)  # mới ~1, cũ -> 0

        text = " ".join([
            art.get("title", "") or "",
            art.get("summary", "") or "",
            # Nếu content quá dài, có thể cắt: (art.get("content", "") or "")[:1000]
        ])
        tokens = simple_tokenize(text)
        if not tokens:
            continue

        # mỗi bài chỉ tính 1 lần trên mỗi token (set)
        for tok in set(tokens):
            score[tok] += w_time

    if not score:
        return {}

    max_val = max(score.values())
    if max_val <= 0:
        return {}

    # Chuẩn hoá 0–1
    return {k: v / max_val for k, v in score.items()}


def keyword_soft_boost(
    meta_item: Dict[str, Any],
    kw_aff: Dict[str, float],
    max_kw: float,
) -> float:
    """
    Tính boost 0–1 dựa trên trùng keyword giữa bài candidate và profile user.
    Dùng title + summary (nhanh, nhẹ).
    """
    if not kw_aff or max_kw <= 0:
        return 0.0

    text = " ".join([
        meta_item.get("title", "") or "",
        meta_item.get("summary", "") or "",
    ])
    tokens = simple_tokenize(text)
    if not tokens:
        return 0.0

    scores = [kw_aff.get(t, 0.0) for t in set(tokens)]
    if not scores:
        return 0.0

    best_raw = max(scores)
    if best_raw <= 0:
        return 0.0

    return best_raw / max_kw  # 0–1
