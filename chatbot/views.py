# chatbot/views.py
import re
import json
from datetime import datetime, timedelta
import uuid

from django.utils import timezone
from bson import json_util, ObjectId

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from api.db import get_db
from api.permissions import IsAuthenticated, RoleRequired
from .gemini_client import chat_generic, summarize_article


def _build_gemini_history(conv_history):
    """
    Chuyển history trong Mongo -> format mà Gemini hiểu.
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
    Bỏ ```json ... ``` nếu model trả về dạng code block.
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


def _clean_article_content(content: str) -> str:
    """
    Làm sạch nội dung bài viết trước khi tóm tắt:
    - Bỏ dòng 'ẢNH: ...'
    - Cắt bỏ phần 'Bình luận ...' (comment, footer, số điện thoại, ban biên tập)
    - Xoá chuỗi số điện thoại kiểu 0906 645 777 / 0908645777
    """
    if not content:
        return content

    text = content

    # 1) Xoá dòng ảnh: 'ẢNH: ...'
    text = re.sub(r"^ẢNH:.*$", "", text, flags=re.MULTILINE)

    # 2) Cắt mọi thứ từ 'Bình luận' trở xuống
    lower = text.lower()
    idx = lower.find("bình luận")
    if idx != -1:
        text = text[:idx]

    # 3) Xoá các cụm giống số điện thoại
    text = re.sub(r"\b\d{3,4}\s?\d{3}\s?\d{3,4}\b", " ", text)

    # 4) Dọn khoảng trắng dư
    text = re.sub(r"\n{2,}", "\n\n", text)  # gộp nhiều dòng trống
    text = re.sub(r"[ \t]{2,}", " ", text)  # gộp nhiều space

    return text.strip()


class NewConversationView(APIView):
    """
    Endpoint: POST /api/chat/new-conversation/

    Tạo một conversation_id mới cho user hiện tại
    và trả về cho FE.
    """

    permission_classes = [
        IsAuthenticated,
        RoleRequired.any_of("user", "admin", "employee"),
    ]

    def post(self, request):
        user = request.user
        username = user.username
        db = get_db()

        # FE có thể gửi conversation_id custom, nếu không thì server tự tạo
        client_conv_id = (request.data.get("conversation_id") or "").strip()
        if client_conv_id:
            conversation_id = client_conv_id
        else:
            conversation_id = uuid.uuid4().hex  # tạo id random

        db["conversations"].update_one(
            {"username": username, "conversation_id": conversation_id},
            {
                "$setOnInsert": {
                    "username": username,
                    "conversation_id": conversation_id,
                    "history": [],
                    "last_filters": {},
                }
            },
            upsert=True,
        )

        return Response(
            {"conversation_id": conversation_id},
            status=status.HTTP_201_CREATED,
        )


class ChatbotView(APIView):
    """
    Endpoint: POST /api/chatbot/chat/ (tuỳ em map url)

    Body JSON:
    {
      "message": "Xin chào",
      "conversation_id": "abc123",  # BẮT BUỘC
      "article_id": "..."           # OPTIONAL - dùng khi tóm tắt bài viết
    }
    """

    permission_classes = [
        IsAuthenticated,
        RoleRequired.any_of("user", "admin", "employee"),
    ]

    def post(self, request):
        user = request.user
        username = user.username

        # 1. Lấy conversation_id
        conversation_id = (request.data.get("conversation_id") or "").strip()
        if not conversation_id:
            return Response(
                {"detail": "conversation_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 2. Lấy message
        message = (request.data.get("message") or "").strip()
        if not message:
            return Response(
                {"detail": "message is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        db = get_db()

        # 3. Lấy conversation từ Mongo
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

        # 4. Gọi Gemini
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
        gemini_category_slug = None
        filters_from_gemini = {}

        try:
            data = json.loads(clean_raw)
            action = data.get("action", "CHAT")
            reply_text = data.get("reply", "") or clean_raw
            gemini_category_slug = data.get("category_slug")
            filters_from_gemini = data.get("filters") or {}
        except Exception:
            # Nếu JSON lỗi thì coi như CHAT
            action = "CHAT"
            reply_text = raw
            gemini_category_slug = None
            filters_from_gemini = {}

        now = datetime.utcnow()

        def save_history(user_msg: str, bot_msg: str):
            db["conversations"].update_one(
                {"username": username, "conversation_id": conversation_id},
                {
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
                },
                upsert=True,
            )

        # ====================== 1. SUMMARIZE_ARTICLE ====================== #
        if action == "SUMMARIZE_ARTICLE":
            article_id = (request.data.get("article_id") or "").strip()

            # Không có article_id -> yêu cầu user chọn bài
            if not article_id:
                no_id_reply = (
                    reply_text
                    or "Mình chưa biết bạn đang đọc bài nào. "
                       "Vui lòng mở bài viết muốn tóm tắt rồi thử lại nhé."
                )
                save_history(message, no_id_reply)
                return Response(
                    {"reply": no_id_reply, "need_article": True},
                    status=status.HTTP_200_OK,
                )

            # article_id sai format
            try:
                obj_id = ObjectId(article_id)
            except Exception:
                error_reply = (
                    f"article_id '{article_id}' không hợp lệ. "
                    "Vui lòng thử lại bằng bài viết khác."
                )
                save_history(message, error_reply)
                return Response(
                    {"reply": error_reply, "need_article": True},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Tìm bài
            article = db["articles"].find_one({"_id": obj_id})
            if not article:
                error_reply = (
                    "Mình không tìm thấy bài viết bạn đang đọc. "
                    "Có thể bài đã bị xoá hoặc id không đúng."
                )
                save_history(message, error_reply)
                return Response(
                    {"reply": error_reply, "need_article": True},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Lấy content (CHỖ NÀY EM CHỈNH THEO SCHEMA CỦA EM)
            raw_content = (
                article.get("content")
            )
            if not raw_content:
                error_reply = "Bài viết này không có nội dung để tóm tắt."
                save_history(message, error_reply)
                return Response(
                    {"reply": error_reply, "article_id": article_id},
                    status=status.HTTP_200_OK,
                )

            # Làm sạch content: bỏ ảnh, bình luận, footer, số điện thoại
            content = _clean_article_content(raw_content)
            if not content:
                error_reply = (
                    "Nội dung chính của bài viết không hợp lệ hoặc quá ngắn để tóm tắt."
                )
                save_history(message, error_reply)
                return Response(
                    {"reply": error_reply, "article_id": article_id},
                    status=status.HTTP_200_OK,
                )

            # Đoán ngôn ngữ đơn giản
            language = "vi"
            if all(ord(c) < 128 for c in message):
                language = "en"

            try:
                summary = summarize_article(content, language=language)
                if not summary or not summary.strip():
                    summary = (
                        "Xin lỗi, hiện tại mình chưa thể tóm tắt bài viết này. "
                        "Bạn có thể thử lại sau hoặc hỏi mình nội dung khác nhé."
                    )
            except Exception as e:
                error_reply = f"Lỗi khi tóm tắt bài viết: {e}"
                save_history(message, error_reply)
                return Response(
                    {"reply": error_reply},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            save_history(message, summary)
            return Response(
                {"reply": summary, "article_id": article_id},
                status=status.HTTP_200_OK,
            )

        # ======================== 2. GET_ARTICLES ======================== #
        if action == "GET_ARTICLES":
            # old_time_range = last_filters.get("time_range")
            old_region = last_filters.get("region")
            old_locations = last_filters.get("locations") or []
            old_keywords = last_filters.get("keywords") or []
            old_category_slug = last_filters.get("category_slug")

            new_time_range = filters_from_gemini.get("time_range", None)
            new_region = filters_from_gemini.get("region", None)

            raw_locations = filters_from_gemini.get("locations", None)
            if isinstance(raw_locations, list):
                new_locations = raw_locations
            else:
                new_locations = None

            new_keywords = filters_from_gemini.get("keywords") or []
            new_category_slug = gemini_category_slug

            # if new_time_range is not None:
            #     # User nói rõ "hôm nay", "tuần này" => dùng cái mới
            #     time_range = new_time_range
            # elif new_keywords:
            #     # User đổi sang chủ đề mới (vd: "các bài viết về messi")
            #     # mà không nói gì về thời gian => bỏ lọc time_range cũ
            #     time_range = None
            # else:
            #     # Không có filter mới => giữ cái cũ
            #     time_range = old_time_range

            # 🔥 TIME RANGE: CHỈ DỰA VÀO CÂU HIỆN TẠI
            # Nếu Gemini không set time_range cho câu này -> KHÔNG LỌC THEO THỜI GIAN
            time_range = new_time_range  # None => không áp dụng published_at filter

            region = new_region if new_region is not None else old_region

            if new_locations not in (None, []):
                locations = new_locations
            else:
                locations = old_locations or []

            keywords = new_keywords or []
            category_slug = new_category_slug or old_category_slug

            query = {}

            # Category cha / con
            if category_slug:
                category = db["categories"].find_one({"slug": category_slug})
                if category:
                    query["category_id"] = category["_id"]
                else:
                    category_child = db["category_child"].find_one(
                        {"slug": category_slug}
                    )
                    if category_child:
                        query["category_child_id"] = category_child["_id"]
                    else:
                        error_reply = (
                            reply_text
                            or f"Xin lỗi, mình không tìm thấy chủ đề với slug '{category_slug}'."
                        )
                        effective_filters = {
                            "time_range": time_range,
                            "region": region,
                            "locations": locations,
                            "keywords": keywords,
                            "category_slug": category_slug,
                        }
                        db["conversations"].update_one(
                            {"username": username, "conversation_id": conversation_id},
                            {"$set": {"last_filters": effective_filters}},
                            upsert=True,
                        )
                        save_history(message, error_reply)
                        return Response(
                            {
                                "reply": error_reply,
                                "error": {"detail": "Category not found"},
                            },
                            status=status.HTTP_200_OK,
                        )

            # Thời gian
            start, end = _build_time_range_filter(time_range)
            if start and end:
                query["published_at"] = {"$gte": start, "$lt": end}

            # Region + locations -> regex title
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

            # Keywords
            keyword_or_clauses = []
            for kw in keywords:
                if not kw:
                    continue
                regex = {"$regex": kw, "$options": "i"}
                keyword_or_clauses.append({"title": regex})
                # nếu muốn, có thể search thêm description/content ở đây

            if keyword_or_clauses:
                if "$or" in query:
                    query["$or"].extend(keyword_or_clauses)
                else:
                    query["$or"] = keyword_or_clauses

            # Query Mongo: GIỚI HẠN 10 BÀI
            cursor = (
                db["articles"]
                .find(query)
                .sort("published_at", -1)
                .limit(10)
            )
            articles_list = list(cursor)

            # Lưu last_filters
            effective_filters = {
                "time_range": time_range,
                "region": region,
                "locations": locations,
                "keywords": keywords,
                "category_slug": category_slug,
            }
            db["conversations"].update_one(
                {"username": username, "conversation_id": conversation_id},
                {"$set": {"last_filters": effective_filters}},
                upsert=True,
            )

            if not articles_list:
                no_articles_reply = (
                    reply_text
                    or "Hiện tại mình không tìm thấy bài viết nào phù hợp với yêu cầu của bạn."
                )
                save_history(message, no_articles_reply)
                return Response(
                    {"reply": no_articles_reply, "articles": []},
                    status=status.HTTP_200_OK,
                )

            articles = json.loads(json_util.dumps(articles_list))

            save_history(message, reply_text)
            return Response(
                {"reply": reply_text, "articles": articles},
                status=status.HTTP_200_OK,
            )

        # ====================== 3. MẶC ĐỊNH: CHAT ====================== #
        save_history(message, reply_text)
        return Response({"reply": reply_text}, status=status.HTTP_200_OK)
