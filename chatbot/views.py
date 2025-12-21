# chatbot/views.py
import json
import os
import re
import mimetypes
import uuid
import unicodedata
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

from bson import json_util, ObjectId
from django.utils import timezone
from django.http import HttpResponse, Http404
from django.conf import settings

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny

from google.cloud import texttospeech

from api.db import get_db
from api.permissions import IsAuthenticated, RoleRequired
from .gemini_client import chat_generic, summarize_article

from recommender.services.embeddings import load_embedder, encode_texts
from recommender.services.chroma_store import get_client, get_articles_collection
from recommender.services.similar import user_topic_feed


# --------------------------------------------------------
# Helpers
# --------------------------------------------------------

_TEXT_INDEX_READY = False


def _build_gemini_history(conv_history):
    history_for_gemini = []
    for msg in conv_history:
        sender = msg.get("sender")
        text = msg.get("text", "")
        if not text:
            continue
        if sender == "user":
            history_for_gemini.append({"role": "user", "content": text})
        elif sender == "bot":
            history_for_gemini.append({"role": "model", "content": text})
    return history_for_gemini


def _strip_markdown_fence(text: str) -> str:
    if not text:
        return text
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _build_time_range_filter(time_range: str):
    if not time_range:
        return None, None

    now = timezone.now()

    if time_range == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return start, end

    if time_range == "last_7_days":
        end = now
        start = end - timedelta(days=7)
        return start, end

    return None, None


def _clean_article_content(raw: str) -> str:
    if not raw:
        return raw

    text = raw
    text = re.sub(r"Bình luận[\s\S]*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d{2,4}[\s\-]?\d{3}[\s\-]?\d{3,4}\b", "", text)

    patterns = [
        r"Tổng biên tập:.*",
        r"Phó tổng biên tập:.*",
        r"Tổng thư ký tòa soạn:.*",
        r"Liên hệ quảng cáo.*",
        r"Hotline.*",
        r"Email:.*",
    ]
    for p in patterns:
        text = re.sub(p, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\n{2,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def _generate_tts_audio(text: str, lang: str = "vi"):
    if not text:
        return None

    language_code = "vi-VN" if lang == "vi" else "en-US"

    client = texttospeech.TextToSpeechClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)

    voice_params = texttospeech.VoiceSelectionParams(
        language_code=language_code,
        ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,
    )

    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
        speaking_rate=1.0,
        pitch=0.0,
    )

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice_params,
        audio_config=audio_config,
    )

    out_dir = os.path.join(settings.MEDIA_ROOT, "tts")
    os.makedirs(out_dir, exist_ok=True)

    filename = f"tts_{uuid.uuid4().hex}.mp3"
    full_path = os.path.join(out_dir, filename)

    with open(full_path, "wb") as f:
        f.write(response.audio_content)

    return f"/api/tts/audio/{filename}"


def get_user_recommendations(user_id: str, topk: int = 10, min_focus: float = 0.35):
    db = get_db()
    embedder = load_embedder(settings.SENTENCE_MODEL)
    coll = get_articles_collection(get_client(settings.CHROMA_DIR), name="articles")

    results = user_topic_feed(
        db=db,
        chroma_collection=coll,
        embedder=embedder,
        user_id=user_id,
        topk=topk,
        min_focus=min_focus,
    )
    return results


def _safe_objectid(v) -> Optional[ObjectId]:
    try:
        return ObjectId(str(v))
    except Exception:
        return None


def _extract_oid_str(v) -> Optional[str]:
    if not v:
        return None
    if isinstance(v, dict) and "$oid" in v:
        return v["$oid"]
    return str(v)


def _extract_article_id_from_request(data: dict) -> str:
    def norm_id(val) -> str:
        if val is None:
            return ""
        if isinstance(val, str):
            s = val.strip()
            if s.lower() in ("null", "none", ""):
                return ""
            return s
        if isinstance(val, dict):
            if "$oid" in val:
                return str(val["$oid"]).strip()
        return ""

    for k in ("article_id", "articleId", "articleID"):
        if k in data:
            got = norm_id(data.get(k))
            if got:
                return got

    art = data.get("article")
    got = norm_id(art)
    if got:
        return got

    if isinstance(art, dict):
        for kk in ("_id", "id", "article_id", "articleId"):
            if kk in art:
                v = art.get(kk)
                if isinstance(v, dict) and "$oid" in v:
                    return str(v["$oid"]).strip()
                if isinstance(v, str):
                    s = v.strip()
                    if s.lower() not in ("null", "none", ""):
                        return s
    return ""


def _articles_enrich_pipeline(
    match_query: Dict[str, Any],
    limit: int = 10,
    sort: Optional[Dict[str, int]] = None,
    user_oid: Optional[ObjectId] = None,
) -> List[Dict[str, Any]]:
    sort = sort or {"published_at": -1}

    pipeline: List[Dict[str, Any]] = [
        {"$match": match_query},
        {"$sort": sort},
        {"$limit": int(limit) if limit else 10},
        {
            "$lookup": {
                "from": "categories",
                "localField": "category_id",
                "foreignField": "_id",
                "as": "category_doc",
            }
        },
        {"$unwind": {"path": "$category_doc", "preserveNullAndEmptyArrays": True}},
        {
            "$lookup": {
                "from": "category_child",
                "localField": "category_child_id",
                "foreignField": "_id",
                "as": "category_child_doc",
            }
        },
        {"$unwind": {"path": "$category_child_doc", "preserveNullAndEmptyArrays": True}},
        {
            "$addFields": {
                "category": {
                    "$cond": [
                        {"$ifNull": ["$category_doc", False]},
                        {"name": "$category_doc.name", "slug": "$category_doc.slug"},
                        None,
                    ]
                },
                "category_child": {
                    "$cond": [
                        {"$ifNull": ["$category_child_doc", False]},
                        {"name": "$category_child_doc.name", "slug": "$category_child_doc.slug"},
                        None,
                    ]
                },
                "category_name": "$category_doc.name",
                "category_slug": "$category_doc.slug",
                "category_child_name": "$category_child_doc.name",
                "category_child_slug": "$category_child_doc.slug",
            }
        },
    ]

    if user_oid is not None:
        pipeline += [
            {
                "$lookup": {
                    "from": "bookmarks",
                    "let": {"aid": "$_id"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$article_id", "$$aid"]},
                                        {"$eq": ["$user_id", user_oid]},
                                    ]
                                }
                            }
                        },
                        {"$limit": 1},
                        {"$project": {"_id": 1}},
                    ],
                    "as": "_bm",
                }
            },
            {"$addFields": {"is_bookmarked": {"$gt": [{"$size": "$_bm"}, 0]}}},
        ]
    else:
        pipeline += [{"$addFields": {"is_bookmarked": False}}]

    pipeline += [{"$project": {"category_doc": 0, "category_child_doc": 0, "_bm": 0}}]
    return pipeline


def _aggregate_articles_enriched(db, match_query, limit=10, sort=None, user_oid=None):
    pipeline = _articles_enrich_pipeline(match_query, limit=limit, sort=sort, user_oid=user_oid)
    return list(db["articles"].aggregate(pipeline))


def _meta_map_for_article_ids(db, article_ids: List[ObjectId], user_oid: Optional[ObjectId] = None):
    if not article_ids:
        return {}

    pipeline = _articles_enrich_pipeline(
        match_query={"_id": {"$in": article_ids}},
        limit=len(article_ids),
        sort={"published_at": -1},
        user_oid=user_oid,
    ) + [
        {
            "$project": {
                "_id": 1,
                "category": 1,
                "category_child": 1,
                "category_name": 1,
                "category_slug": 1,
                "category_child_name": 1,
                "category_child_slug": 1,
                "is_bookmarked": 1,
            }
        }
    ]

    docs = list(db["articles"].aggregate(pipeline))
    return {str(d["_id"]): d for d in docs}


def _parse_iso_datetime_maybe_z(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        ss = s.replace("Z", "+00:00") if isinstance(s, str) else s
        return datetime.fromisoformat(ss)
    except Exception:
        return None


def _override_filters_by_message(message, time_range, region, locations):
    lower = (message or "").lower()

    msg_time = None
    if any(x in lower for x in ["hôm nay", "today"]):
        msg_time = "today"
    elif any(x in lower for x in ["tuần này", "7 ngày", "7 ngay", "last 7 days", "last seven days", "last few days", "mấy ngày qua"]):
        msg_time = "last_7_days"
    time_range = msg_time

    msg_region = None
    if any(x in lower for x in ["trong nước", "trong nuoc", "việt nam", "viet nam", "tin việt nam", "tin viet nam"]):
        msg_region = "vietnam"
    elif any(x in lower for x in ["quốc tế", "quoc te", "thế giới", "the gioi", "world", "international"]):
        msg_region = "world"
    region = msg_region

    msg_locs = []
    if any(x in lower for x in ["đà nẵng", "da nang"]):
        msg_locs.append("da-nang")
    if any(x in lower for x in ["sài gòn", "sai gon", "tp hcm", "hcm", "hồ chí minh", "ho chi minh"]):
        msg_locs.append("sai-gon")
    if any(x in lower for x in ["hà nội", "ha noi"]):
        msg_locs.append("ha-noi")
    locations = msg_locs

    return time_range, region, locations


# ---------------- HYBRID SEARCH ----------------

def _norm_no_accents(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s.lower().strip()


def _extract_topk_from_message(message: str, default: int = 10, max_k: int = 30) -> int:
    m = _norm_no_accents(message)

    x = re.search(r"\btop\s*(\d{1,2})\b", m)
    if x:
        k = int(x.group(1))
        return max(1, min(max_k, k))

    x = re.search(r"\b(\d{1,2})\s*(bai|tin|articles?|news)\b", m)
    if x:
        k = int(x.group(1))
        return max(1, min(max_k, k))

    return default


def _ensure_articles_text_index(db):
    global _TEXT_INDEX_READY
    if _TEXT_INDEX_READY:
        return

    try:
        idxs = list(db["articles"].list_indexes())
        for ix in idxs:
            key = ix.get("key") or {}
            if any(v == "text" for v in key.values()):
                _TEXT_INDEX_READY = True
                return
    except Exception:
        pass

    try:
        db["articles"].create_index(
            [("title", "text"), ("content", "text"), ("entities", "text"), ("keywords", "text")],
            name="tx_articles_all",
            default_language="none",
        )
    except Exception:
        pass

    _TEXT_INDEX_READY = True


def _text_search_article_ids(db, query_text: str, topn: int = 120) -> List[ObjectId]:
    if not query_text:
        return []

    _ensure_articles_text_index(db)

    try:
        cursor = db["articles"].find(
            {"$text": {"$search": query_text}, "is_deleted": {"$ne": True}},
            {"score": {"$meta": "textScore"}},
        ).sort([("score", {"$meta": "textScore"})]).limit(int(topn))

        out = []
        for d in cursor:
            oid = d.get("_id")
            if isinstance(oid, ObjectId):
                out.append(oid)
        return out
    except Exception:
        try:
            safe = re.escape(query_text.strip())
            rgx = {"$regex": safe, "$options": "i"}
            cursor = db["articles"].find(
                {"$or": [{"title": rgx}, {"content": rgx}], "is_deleted": {"$ne": True}},
                {"_id": 1},
            ).limit(int(topn))
            return [d["_id"] for d in cursor if d.get("_id")]
        except Exception:
            return []


def _semantic_search_article_ids(query_text: str, topn: int = 120) -> List[ObjectId]:
    """
    Chroma semantic search.
    (Chroma phải được build bằng: python manage.py build_index ...)
    """
    if not query_text:
        return []

    try:
        embedder = load_embedder(settings.SENTENCE_MODEL)
        coll = get_articles_collection(get_client(settings.CHROMA_DIR), name="articles")
    except Exception:
        return []

    try:
        q_emb = encode_texts(embedder, [query_text], batch_size=16)
    except Exception:
        return []

    try:
        res = coll.query(
            query_embeddings=q_emb,
            n_results=int(topn),
            include=["metadatas", "ids", "distances"],
        )
    except Exception:
        return []

    metas = (res.get("metadatas") or [[]])[0]
    ids0 = (res.get("ids") or [[]])[0]

    out = []
    for i, meta in enumerate(metas):
        aid = None
        if isinstance(meta, dict):
            aid = meta.get("article_id") or meta.get("_id") or meta.get("id")
        if not aid and i < len(ids0):
            aid = ids0[i]
        try:
            out.append(ObjectId(str(aid)))
        except Exception:
            continue

    seen = set()
    uniq = []
    for oid in out:
        if oid in seen:
            continue
        seen.add(oid)
        uniq.append(oid)
    return uniq


def _rrf_merge(a: List[ObjectId], b: List[ObjectId], k: int = 60, topn: int = 300) -> List[ObjectId]:
    scores: Dict[ObjectId, float] = {}

    for rank, oid in enumerate(a, start=1):
        scores[oid] = scores.get(oid, 0.0) + 1.0 / (k + rank)
    for rank, oid in enumerate(b, start=1):
        scores[oid] = scores.get(oid, 0.0) + 1.0 / (k + rank)

    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [oid for oid, _ in merged[:topn]]


def _aggregate_articles_by_rank_enriched(db, ranked_ids, extra_match=None, limit=10, user_oid=None):
    if not ranked_ids:
        return []

    extra_match = extra_match or {}
    match_query = {"_id": {"$in": ranked_ids}}
    match_query.update(extra_match)

    pipeline = [
        {"$match": match_query},
        {"$addFields": {"_rank": {"$indexOfArray": [ranked_ids, "$_id"]}}},
        {"$sort": {"_rank": 1, "published_at": -1}},
        {"$limit": int(limit) if limit else 10},
    ]

    enrich = _articles_enrich_pipeline(match_query={}, limit=10, sort={"published_at": -1}, user_oid=user_oid)
    enrich_tail = enrich[3:] if len(enrich) >= 3 else enrich
    pipeline.extend(enrich_tail)

    pipeline.append({"$project": {"_rank": 0}})
    return list(db["articles"].aggregate(pipeline))


def _augment_query_text(message: str, region: Optional[str], locations: List[str], keywords: List[str]) -> str:
    q = (message or "").strip()

    if keywords:
        q = q + " " + " ".join([str(x) for x in keywords if x])

    if region == "vietnam":
        q = q + " Việt Nam"
    elif region == "world":
        q = q + " quốc tế thế giới"

    for loc in locations:
        if loc == "da-nang":
            q = q + " Đà Nẵng"
        if loc == "ha-noi":
            q = q + " Hà Nội"
        if loc == "sai-gon":
            q = q + " Sài Gòn TP HCM Hồ Chí Minh"

    return q.strip()


# ---------------- Views ----------------

class NewConversationView(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def post(self, request):
        user = request.user
        username = user.username
        db = get_db()

        conversation_id = uuid.uuid4().hex
        db["conversations"].insert_one(
            {"username": username, "conversation_id": conversation_id, "history": [], "last_filters": {}}
        )
        return Response({"conversation_id": conversation_id}, status=status.HTTP_201_CREATED)


class ChatbotView(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def post(self, request):
        user = request.user
        username = user.username

        conversation_id = (request.data.get("conversation_id") or "").strip()
        if not conversation_id:
            return Response({"detail": "conversation_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        message = (request.data.get("message") or "").strip()
        if not message:
            return Response({"detail": "message is required"}, status=status.HTTP_400_BAD_REQUEST)

        db = get_db()

        conv = db["conversations"].find_one({"username": username, "conversation_id": conversation_id}) or {
            "username": username,
            "conversation_id": conversation_id,
            "history": [],
            "last_filters": {},
        }

        conv_history = conv.get("history", [])
        now = datetime.utcnow()

        def save_history(user_msg: str, bot_msg: str, extra_filters=None):
            update = {
                "$setOnInsert": {"username": username, "conversation_id": conversation_id},
                "$push": {
                    "history": {
                        "$each": [
                            {"sender": "user", "text": user_msg, "created_at": now},
                            {"sender": "bot", "text": bot_msg, "created_at": now},
                        ]
                    }
                },
            }
            if extra_filters is not None:
                update["$set"] = {"last_filters": extra_filters}

            db["conversations"].update_one({"username": username, "conversation_id": conversation_id}, update, upsert=True)

        # 2) date question
        lower_msg = message.lower()
        date_phrases = [
            "hôm nay là ngày mấy",
            "hôm nay ngày mấy",
            "hôm nay là ngày bao nhiêu",
            "hôm nay ngày bao nhiêu",
            "today's date",
            "what is today's date",
        ]
        if any(p in lower_msg for p in date_phrases):
            now_local = timezone.now()
            reply_text = f"Hôm nay là ngày {now_local.day} tháng {now_local.month} năm {now_local.year}."
            save_history(message, reply_text)
            return Response({"reply": reply_text}, status=status.HTTP_200_OK)

        # 3) news by specific date
        if "tin tức" in lower_msg or "tin tuc" in lower_msg:
            target_date = None
            now_local = timezone.now()

            if "hôm qua" in lower_msg or "hom qua" in lower_msg:
                target_date = (now_local - timedelta(days=1)).date()
            else:
                m = re.search(r"ngày\s+(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{4}))?", lower_msg)
                if m:
                    day = int(m.group(1))
                    month = int(m.group(2))
                    year = int(m.group(3)) if m.group(3) else now_local.year
                    try:
                        target_date = datetime(year, month, day).date()
                    except ValueError:
                        target_date = None

            if target_date is not None:
                start = timezone.make_aware(
                    datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0),
                    timezone.get_current_timezone(),
                )
                end = start + timedelta(days=1)

                base_match = {"is_deleted": {"$ne": True}, "published_at": {"$gte": start, "$lt": end}}

                user_oid = _safe_objectid(request.user.id)
                articles_list = _aggregate_articles_enriched(
                    db=db, match_query=base_match, limit=10, sort={"published_at": -1}, user_oid=user_oid
                )

                d, mth, y = target_date.day, target_date.month, target_date.year
                if not articles_list:
                    reply_text = f"Không tìm thấy bài viết nào cho ngày {d}/{mth}/{y}."
                    save_history(message, reply_text)
                    return Response({"reply": reply_text, "articles": []}, status=status.HTTP_200_OK)

                articles = json.loads(json_util.dumps(articles_list))
                reply_text = f"Đây là một số tin tức ngày {d}/{mth}/{y}."
                save_history(message, reply_text)
                return Response({"reply": reply_text, "articles": articles}, status=status.HTTP_200_OK)

        # 4) Gemini
        history_for_gemini = _build_gemini_history(conv_history)

        try:
            raw = chat_generic(message, history=history_for_gemini)
        except Exception as e:
            return Response({"detail": f"Gemini error: {e}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        clean_raw = _strip_markdown_fence(raw)

        action = "CHAT"
        reply_text = clean_raw
        category_slug = None
        filters_from_gemini = {}

        try:
            data = json.loads(clean_raw)
            action = data.get("action", "CHAT")
            reply_text = data.get("reply", "") or clean_raw
            category_slug = data.get("category_slug")
            filters_from_gemini = data.get("filters") or {}
        except Exception:
            action = "CHAT"
            reply_text = raw
            category_slug = None
            filters_from_gemini = {}

        # 5) summarize
        if action == "SUMMARIZE_ARTICLE":
            article_id = _extract_article_id_from_request(request.data)

            if not article_id:
                no_id_reply = "Để mình tóm tắt chính xác, bạn hãy mở bài viết muốn tóm tắt rồi gửi lại yêu cầu giúp mình nhé."
                save_history(message, no_id_reply)
                return Response({"reply": no_id_reply, "need_article": True}, status=status.HTTP_200_OK)

            try:
                obj_id = ObjectId(article_id)
            except Exception:
                error_reply = f"article_id '{article_id}' không hợp lệ. Bạn hãy chọn lại bài viết và thử lại nhé."
                save_history(message, error_reply)
                return Response({"reply": error_reply, "need_article": True}, status=status.HTTP_400_BAD_REQUEST)

            article = db["articles"].find_one({"_id": obj_id})
            if not article:
                error_reply = "Mình không tìm thấy bài viết bạn muốn tóm tắt. Bạn hãy chọn lại bài khác nhé."
                save_history(message, error_reply)
                return Response({"reply": error_reply, "need_article": True}, status=status.HTTP_404_NOT_FOUND)

            raw_content = article.get("content") or article.get("body") or article.get("full_text")
            if not raw_content:
                error_reply = "Bài viết này không có nội dung để tóm tắt."
                save_history(message, error_reply)
                return Response({"reply": error_reply, "article_id": article_id}, status=status.HTTP_200_OK)

            clean_content = _clean_article_content(raw_content)

            language = "vi"
            if all(ord(c) < 128 for c in message):
                language = "en"

            try:
                summary = summarize_article(clean_content, language=language)
            except Exception as e:
                error_reply = f"Lỗi khi tóm tắt bài viết: {e}"
                save_history(message, error_reply)
                return Response({"reply": error_reply}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            audio_url = None
            try:
                audio_url = _generate_tts_audio(summary, lang=language)
            except Exception as e:
                print("TTS ERROR:", e)

            save_history(message, summary)
            resp_data = {"reply": summary, "article_id": article_id}
            if audio_url:
                resp_data["audio_url"] = audio_url
            return Response(resp_data, status=status.HTTP_200_OK)

        # 6) recommendations
        if action == "GET_RECOMMENDATIONS":
            time_range = (filters_from_gemini.get("time_range") or "").strip()
            topk = 5
            min_focus = 0.35

            try:
                user_oid = ObjectId(request.user.id)
                requested_user_id = str(user_oid)
            except Exception:
                user_oid = _safe_objectid(request.user.id)
                requested_user_id = str(request.user.id)

            try:
                raw_results = get_user_recommendations(
                    user_id=requested_user_id,
                    topk=max(topk * 2, topk),
                    min_focus=min_focus,
                )
            except Exception as e:
                error_reply = reply_text or "Hiện tại mình chưa thể gợi ý tin tức cho bạn, có lỗi xảy ra."
                save_history(message, error_reply)
                return Response({"reply": error_reply, "error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            start, end = _build_time_range_filter(time_range)
            if start and end:
                filtered = []
                for item in raw_results:
                    pub_str = item.get("published_at")
                    if not pub_str:
                        continue
                    pub_dt = _parse_iso_datetime_maybe_z(pub_str)
                    if not pub_dt:
                        continue
                    if pub_dt.tzinfo is None:
                        pub_dt = timezone.make_aware(pub_dt, timezone.get_current_timezone())
                    if start <= pub_dt < end:
                        filtered.append(item)
                    if len(filtered) >= topk:
                        break
                rec_results = filtered
            else:
                rec_results = raw_results[:topk]

            if not rec_results:
                no_rec_reply = reply_text or "Không tìm thấy bài viết nào."
                save_history(message, no_rec_reply)
                return Response({"reply": no_rec_reply, "articles": []}, status=status.HTTP_200_OK)

            oid_list: List[ObjectId] = []
            for item in rec_results:
                oid_str = _extract_oid_str(item.get("article_id") or item.get("_id") or item.get("id"))
                oid = _safe_objectid(oid_str) if oid_str else None
                if oid:
                    oid_list.append(oid)

            meta_map = _meta_map_for_article_ids(db, oid_list, user_oid=user_oid)

            for item in rec_results:
                oid_str = _extract_oid_str(item.get("article_id") or item.get("_id") or item.get("id"))
                oid = _safe_objectid(oid_str) if oid_str else None
                if not oid:
                    item["is_bookmarked"] = False
                    item["category"] = None
                    item["category_child"] = None
                    continue

                meta = meta_map.get(str(oid))
                if not meta:
                    item["is_bookmarked"] = False
                    item["category"] = None
                    item["category_child"] = None
                    continue

                item["is_bookmarked"] = bool(meta.get("is_bookmarked", False))
                item["category"] = meta.get("category")
                item["category_child"] = meta.get("category_child")
                item["category_name"] = meta.get("category_name")
                item["category_slug"] = meta.get("category_slug")
                item["category_child_name"] = meta.get("category_child_name")
                item["category_child_slug"] = meta.get("category_child_slug")

            final_reply = reply_text or "Mình gợi ý cho bạn một số bài viết:"
            save_history(message, final_reply)
            return Response({"reply": final_reply, "articles": rec_results}, status=status.HTTP_200_OK)

        # 7) GET_ARTICLES – HYBRID SEARCH
        if action == "GET_ARTICLES":
            time_range = filters_from_gemini.get("time_range")
            region = filters_from_gemini.get("region")
            locations = filters_from_gemini.get("locations") or []
            keywords = filters_from_gemini.get("keywords") or []

            time_range, region, locations = _override_filters_by_message(message, time_range, region, locations)
            slug = category_slug

            limit = _extract_topk_from_message(message, default=10, max_k=30)

            status_ok = ["published", "publish", "public", "Published", "PUBLIC"]
            base_match: Dict[str, Any] = {
                "is_deleted": {"$ne": True},
                "$or": [{"status": {"$in": status_ok}}, {"status": {"$exists": False}}],
            }

            start, end = _build_time_range_filter(time_range)
            if start and end:
                base_match["published_at"] = {"$gte": start, "$lt": end}

            if slug:
                category = db["categories"].find_one({"slug": slug})
                if category:
                    base_match["category_id"] = category["_id"]
                else:
                    category_child = db["category_child"].find_one({"slug": slug})
                    if category_child:
                        base_match["category_child_id"] = category_child["_id"]

            query_text = _augment_query_text(message, region=region, locations=locations, keywords=keywords)

            vec_ids = _semantic_search_article_ids(query_text=query_text, topn=160)
            txt_ids = _text_search_article_ids(db, query_text=query_text, topn=160)
            ranked_ids = _rrf_merge(vec_ids, txt_ids, k=60, topn=400)

            user_oid = _safe_objectid(request.user.id)

            if ranked_ids:
                articles_list = _aggregate_articles_by_rank_enriched(
                    db=db,
                    ranked_ids=ranked_ids,
                    extra_match=base_match,
                    limit=limit,
                    user_oid=user_oid,
                )
            else:
                articles_list = _aggregate_articles_enriched(
                    db=db,
                    match_query=base_match,
                    limit=limit,
                    sort={"published_at": -1},
                    user_oid=user_oid,
                )

            save_history(message, reply_text)

            if not articles_list:
                return Response({"reply": "Không tìm thấy bài viết nào.", "articles": []}, status=status.HTTP_200_OK)

            articles = json.loads(json_util.dumps(articles_list))
            final_reply = reply_text or "Dưới đây là các bài viết phù hợp:"
            return Response({"reply": final_reply, "articles": articles}, status=status.HTTP_200_OK)

        # Normal CHAT
        save_history(message, reply_text)
        return Response({"reply": reply_text}, status=status.HTTP_200_OK)


class TextToSpeech(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        text = (request.data.get("text") or "").strip()
        lang = (request.data.get("lang") or "vi").strip()

        if not text:
            return Response({"detail": "Text is required"}, status=400)

        try:
            audio_url = _generate_tts_audio(text, lang=lang)
            if not audio_url:
                return Response({"detail": "Could not generate audio."}, status=500)
            return Response({"audio_url": audio_url}, status=201)
        except Exception as e:
            return Response({"detail": f"Error generating audio: {e}"}, status=500)


def tts_audio_stream(request, filename):
    file_path = os.path.join(settings.MEDIA_ROOT, "tts", filename)
    if not os.path.exists(file_path):
        raise Http404("Audio not found")

    file_size = os.path.getsize(file_path)
    content_type, _ = mimetypes.guess_type(file_path)
    content_type = content_type or "audio/mpeg"

    range_header = request.META.get("HTTP_RANGE", "").strip()
    range_match = re.match(r"bytes=(\d+)-(\d*)", range_header) if range_header else None

    if range_match:
        first_byte = int(range_match.group(1))
        last_byte = range_match.group(2)
        last_byte = int(last_byte) if last_byte else file_size - 1
        if last_byte >= file_size:
            last_byte = file_size - 1

        length = last_byte - first_byte + 1

        with open(file_path, "rb") as f:
            f.seek(first_byte)
            data = f.read(length)

        resp = HttpResponse(data, status=206, content_type=content_type)
        resp["Content-Length"] = str(length)
        resp["Content-Range"] = f"bytes {first_byte}-{last_byte}/{file_size}"
    else:
        with open(file_path, "rb") as f:
            data = f.read()
        resp = HttpResponse(data, content_type=content_type)
        resp["Content-Length"] = str(file_size)

    resp["Accept-Ranges"] = "bytes"
    return resp
