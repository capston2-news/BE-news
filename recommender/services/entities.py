# recommender/services/entities.py
from __future__ import annotations

from typing import List, Tuple
import re

# -----------------------------
# Stopwords / filters (VI)
# -----------------------------
_VI_STOP = {
    "và", "là", "của", "cho", "với", "trong", "ở", "đã", "đang", "sẽ", "một", "những", "các",
    "khi", "vì", "do", "từ", "đến", "theo", "về", "này", "đó", "nên", "còn", "lại", "ra", "bị",
    "tại", "trên", "dưới", "hơn", "rất", "cũng", "như", "không", "có", "được", "thì",
}

# Các từ hay đứng đầu title -> bị viết hoa, dễ dính nhầm entity
_BAD_SINGLE = {
    "người", "hành", "những", "các", "một", "lịch", "sân", "bất", "vì", "tại", "hôm", "nay",
    "thủy", "nước", "đội", "trận", "cuộc", "vụ", "ngày", "theo", "trao", "đúng",
    "bộ", "sở", "ủy", "ubnd",
    "tp", "tỉnh", "thành", "phố", "quận", "huyện", "xã", "phường",
}

# Các từ nối hợp lệ giữa tên tổ chức/địa danh (lowercase vẫn cho nối)
_CONNECTORS = {
    "ty", "công", "côngty", "công_ty",
    "bộ", "sở", "ủy", "ubnd", "tỉnh", "thành", "phố", "tp", "quận", "huyện", "xã", "phường",
    "phòng",
    "thủy", "điện",
}

_BAD_TAIL = {"công", "đầu", "chế", "tạo", "khí", "mức", "lên", "nhất", "nhiều", "nam", "bắc"}
_BAD_HEAD = {"khí", "công", "tạo"}
_TOO_GENERIC = {"công nghệ", "vũ khí", "quốc phòng"}


# -----------------------------
# Helpers
# -----------------------------
def _is_bad_title_word(tok: str) -> bool:
    return (tok or "").strip().lower() in _BAD_SINGLE


def _normalize_abbrev(text: str) -> str:
    """
    Chuẩn hoá một số viết tắt/biến thể trước khi tokenize để rule-based bắt entity ổn định hơn.
    """
    t = text or ""

    # U.23 / U 23 -> U23
    t = re.sub(r"\bU\.\s?(\d{1,2})\b", r"U\1", t)

    # TP.HCM variants -> TPHCM
    t = re.sub(r"\bTP\.?\s*H\.?\s*C\.?\s*M\b", "TPHCM", t, flags=re.IGNORECASE)
    t = re.sub(r"\bTP\s*HCM\b", "TPHCM", t, flags=re.IGNORECASE)

    # "TP Hồ Chí Minh" -> TPHCM
    t = re.sub(r"\bTP\s*Hồ\s*Chí\s*Minh\b", "TPHCM", t, flags=re.IGNORECASE)
    t = re.sub(r"\bThành\s*phố\s*Hồ\s*Chí\s*Minh\b", "TPHCM", t, flags=re.IGNORECASE)

    return t


def _restore_abbrev(s: str) -> str:
    s = (s or "").replace("TPHCM", "TP.HCM")
    s = re.sub(r"\bU(\d{1,2})\b", r"U.\1", s)
    return s


def _restore_abbrev_lower(s: str) -> str:
    """
    Restore nhưng giữ lowercase cho keywords (để output keywords đồng nhất).
    """
    s = (s or "").replace("tphcm", "tp.hcm").replace("TPHCM", "tp.hcm")
    s = re.sub(r"\bu(\d{1,2})\b", r"u.\1", s)
    s = re.sub(r"\bU(\d{1,2})\b", r"u.\1", s)
    return s


def _is_cap_token(tok: str) -> bool:
    return bool(tok) and bool(re.match(r"^[A-ZĐ]", tok))


def _tokenize_words(text: str) -> List[str]:
    # giữ chữ VN + số + dấu chấm (U23/TPHCM/TP.HCM)
    return re.findall(r"[A-Za-zÀ-ỹĐđ0-9\.]+", text or "", flags=re.UNICODE)


# -----------------------------
# Entities: rule-based proper nouns
# -----------------------------
def extract_entities_proper(text: str, max_entities: int = 10, max_len: int = 8) -> List[str]:
    """
    Rule-based proper noun entities:
    - start: token viết hoa hoặc viết tắt (TPHCM/U23/U.23)
    - continue: token viết hoa hoặc connector lowercase (bộ/phòng/xã/tp/công ty/thủy điện...)
    - KHÔNG nối thêm nếu token tiếp theo là từ mở đầu title (Bất/Người/Hành/Ngày...)
    - entity 1 từ: lọc rất chặt
    """
    t = _normalize_abbrev(text or "")
    toks = _tokenize_words(t)

    out: List[str] = []
    seen = set()

    i = 0
    while i < len(toks):
        tok = toks[i]
        low = tok.lower()

        start_ok = (
            _is_cap_token(tok)
            or low == "tphcm"
            or bool(re.match(r"^U\.?\d{1,2}$", tok, re.I))
        )
        if not start_ok:
            i += 1
            continue

        j = i
        buf = [tok]

        while j + 1 < len(toks) and len(buf) < max_len:
            nxt = toks[j + 1]
            nxt_low = nxt.lower()

            # chặn nối nhầm kiểu "TPHCM Bất", "TPHCM Người", "Bộ ... Ngày"
            if _is_cap_token(nxt) and _is_bad_title_word(nxt):
                break

            if (
                _is_cap_token(nxt)
                or nxt_low in _CONNECTORS
                or nxt_low == "tphcm"
                or bool(re.match(r"^U\.?\d{1,2}$", nxt, re.I))
            ):
                buf.append(nxt)
                j += 1
            else:
                break

        phrase = _restore_abbrev(" ".join(buf).strip())
        phrase = phrase.strip(" ,.;:!?")  # dọn dấu câu cuối entity
        words = phrase.split()

        # 1) entity 1 từ: lọc rất chặt
        if len(words) == 1:
            w = words[0]
            wl = w.lower()

            is_special = (
                "." in w
                or wl in {"tp.hcm", "tphcm"}
                or bool(re.match(r"^U\.?\d{1,2}$", w, re.I))
                or bool(re.match(r"^[A-Z]{2,}$", w))  # FIFA/NASA...
            )

            if not is_special:
                # tên riêng kiểu Messi/Ronaldo (>=5), nhưng không thuộc nhóm từ mở đầu title
                if len(w) < 5:
                    i = j + 1
                    continue
                if wl in _VI_STOP or wl in _BAD_SINGLE:
                    i = j + 1
                    continue

        # 2) entity >=2 từ: nếu token thứ 2 là từ mở đầu title -> loại luôn
        if len(words) >= 2 and _is_bad_title_word(words[1]):
            i = j + 1
            continue

        key = phrase.lower()
        if key not in seen:
            seen.add(key)
            out.append(phrase)
            if len(out) >= max_entities:
                break

        i = j + 1

    return out


# -----------------------------
# Keywords: KeyBERT (không gọi là entities)
# -----------------------------
def _clean_kw(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[\"'“”‘’\(\)\[\]\{\},;:!?]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _restore_abbrev_lower(s)


def extract_keywords_keybert(
    kw_model,
    text: str,
    top_n: int = 10,
    min_score: float = 0.28,
) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []

    t = _normalize_abbrev(text)

    kws: List[Tuple[str, float]] = kw_model.extract_keywords(
        t,
        keyphrase_ngram_range=(2, 3),
        use_mmr=True,
        diversity=0.7,
        top_n=max(top_n * 4, 40),
        stop_words=None,
    )

    cand: List[Tuple[str, float]] = []
    seen = set()

    for phrase, score in kws:
        if score is None or float(score) < float(min_score):
            continue

        p = _clean_kw(phrase)
        toks = [x for x in p.split() if x and x not in _VI_STOP]
        if len(toks) < 2:
            continue

        if toks[0] in _BAD_HEAD or toks[-1] in _BAD_TAIL:
            continue

        p2 = " ".join(toks).strip()
        if p2 in _TOO_GENERIC:
            continue

        if any(len(x) <= 2 for x in toks):
            continue

        if p2 in seen:
            continue
        seen.add(p2)
        cand.append((p2, float(score)))

    if not cand:
        return []

    # ưu tiên cụm dài hơn, rồi score
    cand.sort(key=lambda x: (-len(x[0].split()), -x[1], -len(x[0])))

    out: List[str] = []
    for kw, _sc in cand:
        if any(kw in kept for kept in out):
            continue
        out.append(kw)
        if len(out) >= top_n:
            break

    return out


# Backward-compatible alias (tên cũ). Thực tế trả keywords.
def extract_entities_keybert(
    kw_model,
    text: str,
    top_n: int = 10,
    min_score: float = 0.28
) -> List[str]:
    return extract_keywords_keybert(kw_model, text, top_n=top_n, min_score=min_score)
