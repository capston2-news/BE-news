# chatbot/views.py
import json
import os
import re
import mimetypes
import uuid
from datetime import datetime, timedelta

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

from recommender.services.embeddings import load_embedder
from recommender.services.chroma_store import get_client, get_articles_collection
from recommender.services.similar import user_topic_feed


# --------------------------------------------------------
# Helpers
# --------------------------------------------------------

def _build_gemini_history(conv_history):
    """
    conv_history: list message lưu trong Mongo, ví dụ:
      [
        {"sender": "user", "text": "Xin chào", "created_at": ...},
        {"sender": "bot",  "text": "Chào bạn...", "created_at": ...},
      ]

    Trả về history cho Gemini:
      [
        {"role": "user",  "content": "Xin chào"},
        {"role": "model", "content": "Chào bạn..."},
      ]
    """
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
    """
    Nếu Gemini trả dạng code block:

    ```json
    { ... }
    ```

    thì bỏ 2 dòng ``` ở đầu/cuối để còn lại JSON thuần.
    """
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
    """
    Trả về (start, end) cho query Mongo nếu time_range = "today" hoặc "last_7_days".
    """
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
    """
    Làm sạch content trước khi gửi Gemini tóm tắt:
    - Bỏ phần 'Bình luận (0)' + info tòa soạn + sđt, hotline...
    """
    if not raw:
        return raw

    text = raw

    # 1. Bỏ block bắt đầu từ "Bình luận" đến hết
    text = re.sub(r"Bình luận[\s\S]*$", "", text, flags=re.IGNORECASE)

    # 2. Bỏ số điện thoại dạng 0906 645 777, 0908-780-404, 024xxxxxxx...
    text = re.sub(r"\b\d{2,4}[\s\-]?\d{3}[\s\-]?\d{3,4}\b", "", text)

    # 3. Bỏ các dòng info tòa soạn
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

    # 4. Thu gọn khoảng trắng
    text = re.sub(r"\n{2,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def _generate_tts_audio(text: str, lang: str = "vi"):
    """
    Tạo file MP3 đọc text bằng Google Cloud Text-to-Speech.

    - lang: "vi" hoặc "en"
    - Trả về: audio_url (ví dụ: "/api/tts/audio/tts_xxx.mp3") hoặc None nếu lỗi.
    """
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

    audio_url = f"/api/tts/audio/{filename}"
    return audio_url


def get_user_recommendations(user_id: str, topk: int = 10, min_focus: float = 0.35):
    """
    Trả về list bài recommend cho user_id (dạng string ObjectId).
    Dùng chung cho cả API riêng và chatbot.
    """
    db = get_db()

    embedder = load_embedder(settings.SENTENCE_MODEL)
    coll = get_articles_collection(get_client(settings.CHROMA_DIR))

    results = user_topic_feed(
        db=db,
        chroma_collection=coll,
        embedder=embedder,
        user_id=user_id,
        topk=topk,
        min_focus=min_focus,
    )

    return results


# --------------------------------------------------------
# New Conversation
# --------------------------------------------------------

class NewConversationView(APIView):
    """
    Endpoint: POST /api/chat/new-conversation/

    Tạo một conversation_id mới cho user hiện tại
    và trả về cho FE.

    FE gọi:
      POST /api/chat/new-conversation/
      Body: {}
      => { "conversation_id": "..." }
    """

    permission_classes = [
        IsAuthenticated,
        RoleRequired.any_of("user", "admin", "employee"),
    ]

    def post(self, request):
        user = request.user
        username = user.username
        db = get_db()

        conversation_id = uuid.uuid4().hex

        db["conversations"].insert_one(
            {
                "username": username,
                "conversation_id": conversation_id,
                "history": [],
                "last_filters": {},
            }
        )

        return Response(
            {"conversation_id": conversation_id},
            status=status.HTTP_201_CREATED,
        )


# --------------------------------------------------------
# Chatbot
# --------------------------------------------------------

class ChatbotView(APIView):
    """
    Endpoint: POST /api/chatbot/chat/

    Body JSON:
    {
      "message": "Xin chào",
      "conversation_id": "abc123",    # bắt buộc
      "article_id": "..."             # optional, dùng khi SUMMARIZE_ARTICLE
    }
    """

    permission_classes = [
        IsAuthenticated,
        RoleRequired.any_of("user", "admin", "employee"),
    ]

    def post(self, request):
        user = request.user
        username = user.username

        # --------- 1. Lấy params cơ bản ---------
        conversation_id = (request.data.get("conversation_id") or "").strip()
        if not conversation_id:
            return Response(
                {"detail": "conversation_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = (request.data.get("message") or "").strip()
        if not message:
            return Response(
                {"detail": "message is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        db = get_db()

        conv = db["conversations"].find_one(
            {"username": username, "conversation_id": conversation_id}
        ) or {
            "username": username,
            "conversation_id": conversation_id,
            "history": [],
            "last_filters": {},
        }
        conv_history = conv.get("history", [])
        last_filters = conv.get("last_filters") or {}

        now = datetime.utcnow()

        def save_history(user_msg: str, bot_msg: str, extra_filters=None):
            update = {
                "$setOnInsert": {
                    "username": username,
                    "conversation_id": conversation_id,
                },
                "$push": {
                    "history": {
                        "$each": [
                            {
                                "sender": "user",
                                "text": user_msg,
                                "created_at": now,
                            },
                            {
                                "sender": "bot",
                                "text": bot_msg,
                                "created_at": now,
                            },
                        ]
                    }
                },
            }
            if extra_filters is not None:
                update["$set"] = {"last_filters": extra_filters}

            db["conversations"].update_one(
                {"username": username, "conversation_id": conversation_id},
                update,
                upsert=True,
            )

        # =======================================================
        # 2. XỬ LÝ RIÊNG CÂU HỎI “HÔM NAY LÀ NGÀY MẤY”
        #    (KHÔNG GỌI GEMINI, DÙNG THỜI GIAN THỰC)
        # =======================================================
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
            d = now_local.day
            m = now_local.month
            y = now_local.year
            reply_text = f"Hôm nay là ngày {d} tháng {m} năm {y}."
            save_history(message, reply_text)
            return Response({"reply": reply_text}, status=status.HTTP_200_OK)

        # =======================================================
        # 3. TIN TỨC THEO NGÀY CỤ THỂ (HÔM QUA / NGÀY dd/mm(/yyyy))
        #    VÍ DỤ: "tin tức ngày hôm qua", "tin tức ngày 4/12"
        #    -> TRUY VẤN TRỰC TIẾP MONGO, KHÔNG GỌI GEMINI
        # =======================================================
        if "tin tức" in lower_msg or "tin tuc" in lower_msg:
            target_date = None
            now_local = timezone.now()

            # 3.1. "hôm qua"
            if "hôm qua" in lower_msg or "hom qua" in lower_msg:
                target_date = (now_local - timedelta(days=1)).date()
            else:
                # 3.2. Pattern "ngày 4/12" hoặc "ngày 04-12-2025"
                m = re.search(
                    r"ngày\s+(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{4}))?",
                    lower_msg
                )
                if m:
                    day = int(m.group(1))
                    month = int(m.group(2))
                    year = int(m.group(3)) if m.group(3) else now_local.year
                    try:
                        target_date = datetime(year, month, day).date()
                    except ValueError:
                        target_date = None

            if target_date is not None:
                # Khoảng thời gian [00:00; 24:00) cho ngày đó (theo timezone Django)
                start = timezone.make_aware(
                    datetime(
                        target_date.year,
                        target_date.month,
                        target_date.day,
                        0, 0, 0,
                    ),
                    timezone.get_current_timezone(),
                )
                end = start + timedelta(days=1)

                query = {
                    "published_at": {"$gte": start, "$lt": end},
                }

                cursor = (
                    db["articles"]
                    .find(query)
                    .sort("published_at", -1)
                    .limit(10)
                )
                articles_list = list(cursor)

                d, mth, y = target_date.day, target_date.month, target_date.year

                if not articles_list:
                    reply_text = (
                        f"Hiện tại mình không tìm thấy tin tức nào cho ngày {d}/{mth}/{y}."
                    )
                    save_history(message, reply_text)
                    return Response(
                        {
                            "reply": reply_text,
                            "articles": [],
                        },
                        status=status.HTTP_200_OK,
                    )

                articles = json.loads(json_util.dumps(articles_list))
                reply_text = f"Đây là một số tin tức ngày {d}/{mth}/{y}."

                save_history(message, reply_text)
                return Response(
                    {
                        "reply": reply_text,
                        "articles": articles,
                    },
                    status=status.HTTP_200_OK,
                )

        # =======================================================
        # 4. CÁC TRƯỜNG HỢP KHÁC -> GỌI GEMINI
        # =======================================================
        history_for_gemini = _build_gemini_history(conv_history)

        try:
            raw = chat_generic(message, history=history_for_gemini)
        except Exception as e:
            return Response(
                {"detail": f"Gemini error: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

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

        # =======================================================
        # 5. SUMMARIZE_ARTICLE – tóm tắt bài viết + TTS
        # =======================================================
        if action == "SUMMARIZE_ARTICLE":
            article_id = (request.data.get("article_id") or "").strip()

            if not article_id:
                no_id_reply = (
                    reply_text
                    or "Hiện tại mình chưa biết bạn đang đọc bài nào. "
                       "Vui lòng mở bài viết muốn tóm tắt."
                )
                save_history(message, no_id_reply)
                return Response(
                    {
                        "reply": no_id_reply,
                        "need_article": True,
                    },
                    status=status.HTTP_200_OK,
                )

            try:
                obj_id = ObjectId(article_id)
            except Exception:
                error_reply = f"article_id '{article_id}' không hợp lệ. Vui lòng thử lại."
                save_history(message, error_reply)
                return Response(
                    {
                        "reply": error_reply,
                        "need_article": True,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            article = db["articles"].find_one({"_id": obj_id})
            if not article:
                error_reply = (
                    "Mình không tìm thấy bài viết bạn đang đọc."
                )
                save_history(message, error_reply)
                return Response(
                    {
                        "reply": error_reply,
                        "need_article": True,
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

            raw_content = (
                article.get("content")
                or article.get("body")
                or article.get("full_text")
            )
            if not raw_content:
                error_reply = "Bài viết này không có nội dung để tóm tắt."
                save_history(message, error_reply)
                return Response(
                    {
                        "reply": error_reply,
                        "article_id": article_id,
                    },
                    status=status.HTTP_200_OK,
                )

            clean_content = _clean_article_content(raw_content)

            language = "vi"
            if all(ord(c) < 128 for c in message):
                language = "en"

            try:
                summary = summarize_article(clean_content, language=language)
            except Exception as e:
                error_reply = f"Lỗi khi tóm tắt bài viết: {e}"
                save_history(message, error_reply)
                return Response(
                    {"reply": error_reply},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            audio_url = None
            try:
                audio_url = _generate_tts_audio(summary, lang=language)
            except Exception as e:
                print("TTS ERROR:", e)

            save_history(message, summary)

            resp_data = {
                "reply": summary,
                "article_id": article_id,
            }
            if audio_url:
                resp_data["audio_url"] = audio_url

            return Response(resp_data, status=status.HTTP_200_OK)


        # ===== 4. GET_RECOMMENDATIONS – gợi ý theo lịch sử người dùng =====
        if action == "GET_RECOMMENDATIONS":
            # Lấy filter từ Gemini
            time_range = filters_from_gemini.get("time_range")
            raw_topk = filters_from_gemini.get("topk")
            raw_min_focus = filters_from_gemini.get("min_focus")

            # topk an toàn (default 10)
            try:
                topk = int(raw_topk) if raw_topk not in (None, "") else 10
            except (TypeError, ValueError):
                topk = 10

            # min_focus an toàn (default 0.35)
            try:
                min_focus = float(raw_min_focus) if raw_min_focus not in (None, "") else 0.35
            except (TypeError, ValueError):
                min_focus = 0.35

            db = get_db()

            # user_id cho recommender (dùng ObjectId string như UserTopicFeedView)
            try:
                user_oid = ObjectId(request.user.id)
                requested_user_id = str(user_oid)
            except Exception:
                requested_user_id = str(request.user.id)

            # 4.1. Gọi recommender lấy nhiều hơn một xíu để còn lọc theo ngày
            try:
                embedder = load_embedder(settings.SENTENCE_MODEL)
                coll = get_articles_collection(get_client(settings.CHROMA_DIR))

                raw_results = user_topic_feed(
                    db=db,
                    chroma_collection=coll,
                    embedder=embedder,
                    user_id=requested_user_id,
                    topk=max(topk * 2, topk),
                    min_focus=min_focus,
                )
            except Exception as e:
                error_reply = (
                    reply_text
                    or "Hiện tại mình chưa thể gợi ý tin tức cho bạn, có lỗi xảy ra."
                )
                save_history(message, error_reply)
                return Response(
                    {
                        "reply": error_reply,
                        "error": str(e),
                    },
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # 4.2. Nếu Gemini có time_range ("today", "last_7_days"...)
            #      thì lọc tiếp theo published_at
            start, end = _build_time_range_filter(time_range)
            if start and end:
                filtered = []
                for item in raw_results:
                    pub_str = item.get("published_at")
                    if not pub_str:
                        continue
                    try:
                        pub_dt = datetime.fromisoformat(pub_str)
                        # nếu không có tz thì coi như UTC hoặc local
                        if pub_dt.tzinfo is None:
                            pub_dt = timezone.make_aware(pub_dt, timezone.get_current_timezone())
                    except Exception:
                        continue

                    if start <= pub_dt < end:
                        filtered.append(item)
                    if len(filtered) >= topk:
                        break
                rec_results = filtered
            else:
                # không lọc theo ngày -> cắt topk
                rec_results = raw_results[:topk]

            # 4.3. Không có gì để gợi ý
            if not rec_results:
                no_rec_reply = (
                    reply_text
                    or "Mình chưa tìm được bài viết nổi bật phù hợp với yêu cầu của bạn."
                )
                save_history(message, no_rec_reply)
                return Response(
                    {
                        "reply": no_rec_reply,
                        "articles": [],
                    },
                    status=status.HTTP_200_OK,
                )

            final_reply = (
                reply_text
                or "Mình gợi ý cho bạn một số bài viết nổi bật hôm nay:"
            )
            save_history(message, final_reply)

            return Response(
                {
                    "reply": final_reply,
                    "articles": rec_results,
                },
                status=status.HTTP_200_OK,
            )

        # =======================================================
        # 6. GET_ARTICLES – dùng filters Gemini map sang Mongo
        # =======================================================
        if action == "GET_ARTICLES":
            time_range = filters_from_gemini.get("time_range")
            region = filters_from_gemini.get("region")
            locations = filters_from_gemini.get("locations") or []
            keywords = filters_from_gemini.get("keywords") or []

            slug = category_slug
            query = {}

            # category cha / con
            if slug:
                category = db["categories"].find_one({"slug": slug})
                if category:
                    query["category_id"] = category["_id"]
                else:
                    category_child = db["category_child"].find_one({"slug": slug})
                    if category_child:
                        query["category_child_id"] = category_child["_id"]
                    else:
                        error_reply = (
                            reply_text
                            or f"Xin lỗi, mình không tìm thấy chủ đề với slug '{slug}'."
                        )
                        effective_filters = {
                            "time_range": time_range,
                            "region": region,
                            "locations": locations,
                            "keywords": keywords,
                            "category_slug": slug,
                        }
                        save_history(message, error_reply, extra_filters=effective_filters)
                        return Response(
                            {
                                "reply": error_reply,
                                "error": {"detail": "Category not found"},
                            },
                            status=status.HTTP_200_OK,
                        )

            # thời gian
            start, end = _build_time_range_filter(time_range)
            if start and end:
                query["published_at"] = {"$gte": start, "$lt": end}

            # region / location → regex trên title
            title_regex_parts = []

            if region == "vietnam" and not locations:
                title_regex_parts.append(
                    r"(Việt Nam|Viet Nam|VN|Hà Nội|Ha Noi|Hồ Chí Minh|Ho Chi Minh|Sài Gòn|Sai Gon)"
                )

            for loc in locations:
                if loc == "da-nang":
                    title_regex_parts.append(r"(Đà Nẵng|Da Nang)")
                if loc == "sai-gon":
                    title_regex_parts.append(
                        r"(Sài Gòn|Sai Gon|TP\. HCM|TP HCM|Hồ Chí Minh|Ho Chi Minh)"
                    )
                if loc == "ha-noi":
                    title_regex_parts.append(r"(Hà Nội|Ha Noi)")

            if title_regex_parts:
                pattern = "|".join(title_regex_parts)
                query["title"] = {"$regex": pattern, "$options": "i"}

            # keywords: title + content
            keyword_or_clauses = []
            for kw in keywords:
                kw = (kw or "").strip()
                if not kw:
                    continue

                regex = {"$regex": kw, "$options": "i"}
                keyword_or_clauses.append({"title": regex})
                keyword_or_clauses.append({"content": regex})

            if keyword_or_clauses:
                if "$or" in query:
                    query["$or"].extend(keyword_or_clauses)
                else:
                    query["$or"] = keyword_or_clauses

            cursor = (
                db["articles"]
                .find(query)
                .sort("published_at", -1)
                .limit(10)
            )
            articles_list = list(cursor)

            effective_filters = {
                "time_range": time_range,
                "region": region,
                "locations": locations,
                "keywords": keywords,
                "category_slug": slug,
            }
            save_history(message, reply_text, extra_filters=effective_filters)

            if not articles_list:
                no_articles_reply = (
                    reply_text
                    or "Hiện tại mình không tìm thấy bài viết nào phù hợp với yêu cầu của bạn."
                )
                return Response(
                    {
                        "reply": no_articles_reply,
                        "articles": [],
                    },
                    status=status.HTTP_200_OK,
                )

            articles = json.loads(json_util.dumps(articles_list))

            return Response(
                {
                    "reply": reply_text,
                    "articles": articles,
                },
                status=status.HTTP_200_OK,
            )

        # =======================================================
        # 7. Còn lại: CHAT bình thường
        # =======================================================
        save_history(message, reply_text)
        return Response({"reply": reply_text}, status=status.HTTP_200_OK)


# --------------------------------------------------------
# TTS API riêng (tùy FE dùng thêm)
# --------------------------------------------------------

class TextToSpeech(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        text = (request.data.get("text") or "").strip()
        lang = (request.data.get("lang") or "vi").strip()  # "vi" hoặc "en"

        if not text:
            return Response({"detail": "Text is required"}, status=400)

        try:
            audio_url = _generate_tts_audio(text, lang=lang)
            if not audio_url:
                return Response(
                    {"detail": "Could not generate audio."},
                    status=500,
                )

            return Response({"audio_url": audio_url}, status=201)

        except Exception as e:
            return Response(
                {"detail": f"Error generating audio: {e}"},
                status=500,
            )


def tts_audio_stream(request, filename):
    """
    Stream file mp3 với hỗ trợ HTTP Range để <audio> tua được.
    """
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

        if last_byte:
            last_byte = int(last_byte)
        else:
            last_byte = file_size - 1

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
