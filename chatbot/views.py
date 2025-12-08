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

        history_for_gemini = _build_gemini_history(conv_history)

        # 1. Gọi Gemini
        try:
            raw = chat_generic(message, history=history_for_gemini)
        except Exception as e:
            return Response(
                {"detail": f"Gemini error: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # 2. Parse JSON từ Gemini
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

        # --------------------------------------------------------
        # 3. SUMMARIZE_ARTICLE – tóm tắt bài viết
        # --------------------------------------------------------
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

            # tạo audio cho phần tóm tắt
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

        # --------------------------------------------------------
        # 4. GET_ARTICLES – Gemini quyết filter, backend chỉ map → Mongo
        # --------------------------------------------------------
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

            # region / location
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

        # --------------------------------------------------------
        # 5. Còn lại: CHAT bình thường
        # --------------------------------------------------------
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
