from django.shortcuts import render
from rest_framework import serializers, status
from rest_framework.views import APIView
from rest_framework.response import Response
from .db import get_db
from datetime import datetime, timezone, time
from zoneinfo import ZoneInfo
from .permissions import AllowAny, IsAuthenticated, RoleRequired
from bson import json_util, ObjectId
from django.http import HttpResponse, JsonResponse, Http404
from zoneinfo import ZoneInfo
import re
import os, uuid, json
from django.conf import settings
import mimetypes
from google.cloud import texttospeech

VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
UTC_TZ = ZoneInfo("UTC")

def _parse_date_yyyy_mm_dd(s: str):
    """Chuyển 'YYYY-MM-DD' thành datetime.date (nếu lỗi thì trả None)."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

class PublicGetAllArticles(APIView):
    permission_classes = [AllowAny]
    # ✅ ĐỪNG set authentication_classes = [] nếu muốn request.user hoạt động khi có token

    def get(self, request):
        db = get_db()

        title = (request.GET.get("title") or "").strip()
        published_from = (request.GET.get("published_from") or "").strip()
        published_to = (request.GET.get("published_to") or "").strip()
        category_slug = (request.GET.get("category_slug") or "").strip()

        # limit mặc định 400
        try:
            limit = int(request.GET.get("limit", "400"))
        except ValueError:
            limit = 400
        # ép trong [1..400]
        if limit <= 0:
            limit = 400
        if limit > 400:
            limit = 400

        mongo_filter = {}

        # filter title
        if title:
            mongo_filter["title"] = {"$regex": re.compile(re.escape(title), re.IGNORECASE)}

        # filter published_at theo ngày VN -> UTC
        published_range = {}
        if published_from:
            d = _parse_date_yyyy_mm_dd(published_from)
            if d:
                start_vn = datetime.combine(d, time.min, tzinfo=VIETNAM_TZ)
                published_range["$gte"] = start_vn.astimezone(UTC_TZ)
        if published_to:
            d = _parse_date_yyyy_mm_dd(published_to)
            if d:
                end_vn = datetime.combine(d, time.max, tzinfo=VIETNAM_TZ)
                published_range["$lte"] = end_vn.astimezone(UTC_TZ)
        if published_range:
            mongo_filter["published_at"] = published_range

        # filter theo category_slug (category cha)
        if category_slug:
            cat = db["categories"].find_one({"slug": category_slug}, {"_id": 1})
            if not cat:
                return HttpResponse("[]", content_type="application/json")
            mongo_filter["category_id"] = cat["_id"]

        # user để check bookmark
        user_oid = None
        if getattr(request, "user", None) and getattr(request.user, "is_authenticated", False):
            try:
                user_oid = ObjectId(str(request.user.id))
            except Exception:
                user_oid = None

        pipeline = [
            {"$match": mongo_filter},
            {"$sort": {"published_at": -1}},
            {"$limit": limit},

            # map category_name
            {"$lookup": {
                "from": "categories",
                "localField": "category_id",
                "foreignField": "_id",
                "as": "cat",
            }},
            {"$unwind": {"path": "$cat", "preserveNullAndEmptyArrays": True}},

            # map category_child_name (nếu category_child_id != null)
            {"$lookup": {
                "from": "category_child",
                "localField": "category_child_id",
                "foreignField": "_id",
                "as": "child",
            }},
            {"$unwind": {"path": "$child", "preserveNullAndEmptyArrays": True}},
        ]

        # check bookmark nếu có user
        if user_oid:
            pipeline += [
                {"$lookup": {
                    "from": "bookmarks",
                    "let": {"aid": "$_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$article_id", "$$aid"]},
                            {"$eq": ["$user_id", user_oid]},
                        ]}}},
                        {"$limit": 1},
                    ],
                    "as": "bm",
                }},
                {"$addFields": {"is_bookmarked": {"$gt": [{"$size": "$bm"}, 0]}}},
            ]
        else:
            pipeline += [{"$addFields": {"is_bookmarked": False}}]

        pipeline += [
            {"$project": {
                "_id": 1,
                "site": 1,
                "title": 1,
                "images": 1,
                "content": 1,
                "published_at": 1,

                "category_id": 1,
                "category_name": "$cat.name",
                "category_slug": "$cat.slug",

                "category_child_id": 1,
                "category_child_name": "$child.name",
                "category_child_slug": "$child.slug",

                "is_bookmarked": 1,
            }}
        ]

        docs = list(db["articles"].aggregate(pipeline))
        return HttpResponse(json_util.dumps(docs), content_type="application/json")

class PublicGetArticlesByCategory(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug):
        db = get_db()

        category = db["categories"].find_one({"slug": slug}, {"_id": 1, "name": 1})
        if not category:
            return Response({"detail": "Category not found"}, status=404)

        try:
            limit = int(request.GET.get("limit", "400"))
        except ValueError:
            limit = 400
        if limit <= 0:
            limit = 400
        if limit > 400:
            limit = 400

        user_oid = None
        if getattr(request, "user", None) and getattr(request.user, "is_authenticated", False):
            try:
                user_oid = ObjectId(str(request.user.id))
            except Exception:
                user_oid = None

        pipeline = [
            {"$match": {"category_id": category["_id"]}},
            {"$sort": {"published_at": -1}},
            {"$limit": limit},

            # map category_name
            {"$lookup": {
                "from": "categories",
                "localField": "category_id",
                "foreignField": "_id",
                "as": "cat",
            }},
            {"$unwind": {"path": "$cat", "preserveNullAndEmptyArrays": True}},

            # map category_child_name nếu có category_child_id
            {"$lookup": {
                "from": "category_child",
                "localField": "category_child_id",
                "foreignField": "_id",
                "as": "child",
            }},
            {"$unwind": {"path": "$child", "preserveNullAndEmptyArrays": True}},
        ]

        if user_oid:
            pipeline += [
                {"$lookup": {
                    "from": "bookmarks",
                    "let": {"aid": "$_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$article_id", "$$aid"]},
                            {"$eq": ["$user_id", user_oid]},
                        ]}}},
                        {"$limit": 1},
                    ],
                    "as": "bm",
                }},
                {"$addFields": {"is_bookmarked": {"$gt": [{"$size": "$bm"}, 0]}}},
            ]
        else:
            pipeline += [{"$addFields": {"is_bookmarked": False}}]

        # chỉ trả field cần
        pipeline += [
            {"$project": {
                "_id": 1,
                "site": 1,
                "title": 1,
                "images": 1,
                "content": 1,
                "published_at": 1,

                "category_id": 1,
                "category_name": "$cat.name",
                "category_slug": "$cat.slug",

                "category_child_id": 1,
                "category_child_name": "$child.name",
                "category_child_slug": "$child.slug",

                "is_bookmarked": 1,
            }}
        ]

        docs = list(db["articles"].aggregate(pipeline))
        return HttpResponse(json_util.dumps(docs), content_type="application/json")

class GetAllCategoryChildOfCategory(APIView):
    permission_classes = [AllowAny]
    def get(self, request, category_slug):
        db = get_db()
        category = db["categories"].find_one({"slug": category_slug})
        if not category:
            return HttpResponse({"detail": "Category not found"}, status=404)

        child_category = list(db["category_child"].find({"category_id": category["_id"]}))
        return HttpResponse(json_util.dumps(child_category), content_type="application/json")

class PublicGetArticlesByCategoryChild(APIView):
    permission_classes = [AllowAny]
    # ✅ ĐỪNG set authentication_classes = [] nếu muốn request.user hoạt động khi user đăng nhập

    def get(self, request, category_slug, child_slug):
        db = get_db()

        category = db["categories"].find_one({"slug": category_slug}, {"_id": 1, "name": 1, "slug": 1})
        if not category:
            return Response({"detail": "Category not found"}, status=404)

        child_category = db["category_child"].find_one(
            {"category_id": category["_id"], "slug": child_slug},
            {"_id": 1, "name": 1, "slug": 1}
        )
        if not child_category:
            return Response({"detail": "Child category not found"}, status=404)

        # limit (mặc định 400, tối đa 400)
        try:
            limit = int(request.GET.get("limit", "400"))
        except ValueError:
            limit = 400
        if limit <= 0:
            limit = 400
        if limit > 400:
            limit = 400

        # user để check bookmark
        user_oid = None
        if getattr(request, "user", None) and getattr(request.user, "is_authenticated", False):
            try:
                user_oid = ObjectId(str(request.user.id))
            except Exception:
                user_oid = None

        pipeline = [
            {"$match": {
                "category_id": category["_id"],
                "category_child_id": child_category["_id"],
            }},
            {"$sort": {"published_at": -1}},
            {"$limit": limit},

            # map category_name + category_slug
            {"$lookup": {
                "from": "categories",
                "localField": "category_id",
                "foreignField": "_id",
                "as": "cat",
            }},
            {"$unwind": {"path": "$cat", "preserveNullAndEmptyArrays": True}},

            # map child_name + child_slug
            {"$lookup": {
                "from": "category_child",
                "localField": "category_child_id",
                "foreignField": "_id",
                "as": "child",
            }},
            {"$unwind": {"path": "$child", "preserveNullAndEmptyArrays": True}},
        ]

        # check bookmark
        if user_oid:
            pipeline += [
                {"$lookup": {
                    "from": "bookmarks",
                    "let": {"aid": "$_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$and": [
                            {"$eq": ["$article_id", "$$aid"]},
                            {"$eq": ["$user_id", user_oid]},
                        ]}}},
                        {"$limit": 1},
                    ],
                    "as": "bm",
                }},
                {"$addFields": {"is_bookmarked": {"$gt": [{"$size": "$bm"}, 0]}}},
            ]
        else:
            pipeline += [{"$addFields": {"is_bookmarked": False}}]

        pipeline += [
            {"$project": {
                "_id": 1,
                "site": 1,
                "title": 1,
                "images": 1,
                "content": 1,
                "published_at": 1,

                "category_id": 1,
                "category_name": "$cat.name",
                "category_slug": "$cat.slug",

                "category_child_id": 1,
                "category_child_name": "$child.name",
                "category_child_slug": "$child.slug",

                "is_bookmarked": 1,
            }}
        ]

        docs = list(db["articles"].aggregate(pipeline))
        return HttpResponse(json_util.dumps(docs), content_type="application/json")

class BookmarkArticle(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]
    def post(self, request, article_id):
        db = get_db()
        user_id = request.user.id
        try:
            article_oid = ObjectId(article_id)
        except Exception:
            return Response({"detail": "Invalid article_id"}, status=400)

        article = db["articles"].find_one({"_id": article_oid}, {"_id": 1, "title": 1})
        if not article:
            return Response({"detail": "Article not found"}, status=404)

        res = db["bookmarks"].update_one(
            {"user_id": ObjectId(user_id), "article_id": article_oid},
            {
                "$setOnInsert": {
                    "user_id": ObjectId(user_id),
                    "article_id": article_oid,
                    "created_at": datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
                }
            },
            upsert= True,
        )

        if res.upserted_id:
            data = {
                "detail": "Bookmarked",
                "article_id": article_id
            }
            json_data = json_util.dumps(data)
            return HttpResponse(
                json_data,
                content_type="application/json",
                status=201
            )
        else:
            data = {
                "detail": "Already bookmarked",
                "article_id": article_id
            }
            json_data = json_util.dumps(data)
            return HttpResponse(
                json_data,
                content_type="application/json",
                status=200
            )

    def delete(self, request, article_id):
        db = get_db()
        try:
            user_oid = ObjectId(request.user.id)
        except Exception:
            return Response({"detail": "Invalid user_id"}, status=400)

        try:
            article_oid = ObjectId(article_id)
        except Exception:
            return Response({"detail": "Invalid article_id"}, status=400)

        res = db["bookmarks"].delete_one({
            "user_id": user_oid,
            "article_id": article_oid
        })

        if res.deleted_count == 0:
            data = {
                "detail": "Bookmark not found",
                "article_id": article_id,
            }
            status_code = 404
        else:
            data = {
                "detail": "Bookmark deleted",
                "article_id": article_id,
            }
            status_code = 200

        json_data = json_util.dumps(data)
        return HttpResponse(json_data, content_type="application/json", status=status_code)

    def get(self, request, article_id):
        db = get_db()

        try:
            user_oid = ObjectId(request.user.id)
        except Exception:
            return Response({"detail": "Invalid user_id"}, status=400)

        try:
            article_oid = ObjectId(article_id)
        except Exception:
            return Response({"detail": "Invalid article_id"}, status=400)

        article = db["articles"].find_one({"_id": article_oid}, {"_id": 1, "title": 1})
        if not article:
            return Response({"detail": "Article not found"}, status=404)

        res = db["bookmarks"].find_one({"user_id": user_oid, "article_id": article_oid})

        if res:
            data = {
                "detail": "OK",
            }
            status_code = 200
        else:
            data = {
                "detail": "Bookmark not found",
            }
            status_code = 404

        json_data = json_util.dumps(data)
        return HttpResponse(json_data, content_type="application/json", status=status_code)

class GetAllBookmarksOfUser(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]
    def get(self, request):
        db = get_db()
        try:
            user_oid = ObjectId(request.user.id)
        except Exception:
            return Response({"detail": "Invalid user_id"}, status=400)

        # 2) Aggregation: match bookmark của user + join sang articles
        pipeline = [
            {"$match": {"user_id": user_oid}}, #Lọc chỉ bookmark của user đang login.
            {"$sort": {"created_at": -1}},
            {
                "$lookup": {
                    "from": "articles",            # join sang collection articles
                    "localField": "article_id",    # trong bookmarks
                    "foreignField": "_id",         # trong articles
                    "as": "article"
                }
            },
            {"$unwind": "$article"},  # mỗi bookmark gắn với đúng 1 article
            {
                "$project": {
                    "_id": "$_id",                              # id của bookmark
                    "created_at": 1,
                    "user_id": "$user_id",
                    "article_id": "$article._id",
                    "title": "$article.title",
                    "content": "$article.content",
                }
            }
        ]

        docs = list(db["bookmarks"].aggregate(pipeline))

        json_data = json_util.dumps(docs)
        return HttpResponse(json_data, content_type="application/json")

class CommentOfUser(APIView):

    def get_permissions(self):
        # GET ai cũng xem được, POST thì phải login
        if self.request.method == "GET":
            return [AllowAny()]
        return [IsAuthenticated(), RoleRequired.any_of("user", "admin", "employee")()]

    def get(self, request, article_id):
        db = get_db()

        try:
            article_oid = ObjectId(article_id)
        except Exception:
            return Response({"detail": "Invalid user_id"}, status=400)

        comment = list(db["comments"].find({"article_id": article_oid}, {"username": 1, "content": 1, "created_at": 1, "_id": 0}).sort("created_at", -1))

        json_data = json_util.dumps(comment)
        return HttpResponse(json_data, content_type="application/json")

    def post(self, request):
        db = get_db()

        try:
            username = request.user.username
        except Exception:
            return Response({"detail": "Invalid username"}, status=400)

        payload = request.data

        # 2) Chuẩn hoá: nếu body là object thì convert thành list 1 phần tử
        if isinstance(payload, dict):
            items = [payload]
        elif isinstance(payload, list):
            items = payload
        else:
            return Response(
                {"detail": "Body must be an object or an array of objects"},
                status=400
            )

        now_vn = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh"))
        results = []

        for idx, item in enumerate(items):
            # ưu tiên article_id trong item; nếu không có thì fallback dùng article_id trên URL
            article_id_str = (item.get("article_id") or "").strip()
            content = (item.get("content") or "").strip()

            if not article_id_str:
                results.append({
                    "index": idx,
                    "article_id": None,
                    "status": "error",
                    "detail": "article_id is required"
                })
                continue

            if not content:
                results.append({
                    "index": idx,
                    "article_id": article_id_str,
                    "status": "error",
                    "detail": "content is required"
                })
                continue

            # convert article_id -> ObjectId
            try:
                article_oid = ObjectId(article_id_str)
            except Exception:
                results.append({
                    "index": idx,
                    "article_id": article_id_str,
                    "status": "error",
                    "detail": "Invalid article_id"
                })
                continue

            # 3) Tạo 1 comment mới (mỗi lần là 1 document, KHÔNG upsert)
            comment_doc = {
                "username": username,
                "article_id": article_oid,
                "content": content,
                "created_at": now_vn,
                "is_deleted": False,
            }

            ins = db["comments"].insert_one(comment_doc)

            results.append({
                "index": idx,
                "article_id": article_id_str,
                "comment_id": str(ins.inserted_id),
                "status": "created",
                "detail": "Comment created"
            })

        json_data = json_util.dumps(results)
        return HttpResponse(json_data, content_type="application/json", status=201)

class GetArticleById(APIView):
    permission_classes = [AllowAny]

    def get(self, request, article_id):
        db = get_db()

        try:
            article_oid = ObjectId(article_id)
        except Exception:
            return Response({"detail": "Invalid article_id"}, status=400)

        article = db["articles"].find_one(
            {"_id": article_oid},
            {"keywords": 0, "entities": 0, "status": 0, "is_deleted": 0}
        )
        if not article:
            return Response({"detail": "Article not found"}, status=404)

        # --- Map category cha: name + slug ---
        category_name = None
        category_slug = None
        cat_id = article.get("category_id")
        if cat_id:
            cat = db["categories"].find_one({"_id": cat_id}, {"name": 1, "slug": 1})
            if cat:
                category_name = cat.get("name")
                category_slug = cat.get("slug")

        # --- Map category con: name + slug (nếu có) ---
        category_child_name = None
        category_child_slug = None
        child_id = article.get("category_child_id")
        if child_id:
            child = db["category_child"].find_one({"_id": child_id}, {"name": 1, "slug": 1})
            if child:
                category_child_name = child.get("name")
                category_child_slug = child.get("slug")

        article["category_name"] = category_name
        article["category_slug"] = category_slug
        article["category_child_name"] = category_child_name
        article["category_child_slug"] = category_child_slug

        # --- Check bookmark theo user (nếu đăng nhập) ---
        is_bookmarked = False
        if getattr(request, "user", None) and getattr(request.user, "is_authenticated", False):
            try:
                user_oid = ObjectId(str(request.user.id))
                bm = db["bookmarks"].find_one(
                    {"user_id": user_oid, "article_id": article_oid},
                    {"_id": 1}
                )
                is_bookmarked = bm is not None
            except Exception:
                is_bookmarked = False

        article["is_bookmarked"] = is_bookmarked

        # --- Lấy comments theo bài viết ---
        # Lọc comment chưa bị xoá: is_deleted = false hoặc field không tồn tại
        comment_filter = {
            "article_id": article_oid,
            "$or": [{"is_deleted": False}, {"is_deleted": {"$exists": False}}],
        }

        comments = list(
            db["comments"]
              .find(comment_filter, {"username": 1, "content": 1, "created_at": 1})
              .sort("created_at", -1)
        )

        article["comments"] = comments
        article["comment_count"] = len(comments)

        return HttpResponse(json_util.dumps(article), content_type="application/json")

class GetArticleExpectForArticleById(APIView):
    permission_classes = [AllowAny]
    def get(self, request, article_id):
        db = get_db()
        try:
            article_oid = ObjectId(article_id)
        except Exception:
            return Response({"detail": "Invalid article_id"}, status=400)

        article = db["articles"].find_one({"_id": article_oid})
        if not article:
            return Response({"detail": "Article not found"}, status=404)

        query = {
            "category_id": article.get("category_id"),
            "_id": {"$ne": article_oid},   # loại trừ bài hiện tại
        }

        # Nếu có category_child_name thì lọc theo luôn
        category_child = article.get("category_child_id")
        if category_child:
            query["category_child_id"] = category_child

        # 4) Lấy 2 bài mới nhất
        related_cursor = (
            db["articles"]
            .find(query, {"title": 1, "content": 1, "images": 1})
            .sort("published_at", -1)
            .limit(3)
        )

        related_articles = list(related_cursor)
        json_data = json_util.dumps(related_articles)
        return HttpResponse(json_data, content_type="application/json")

class TextToSpeech(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        # Lấy text và lang từ body JSON
        text = (request.data.get("text") or "").strip()
        lang = (request.data.get("lang") or "vi").strip()  # "vi" hoặc "en"

        if not text:
            return Response({"detail": "Text is required"}, status=400)

        # chọn language_code
        if lang == "vi":
            language_code = "vi-VN"
        else:
            language_code = "en-US"

        try:
            # client sẽ tự đọc GOOGLE_APPLICATION_CREDENTIALS từ env
            client = texttospeech.TextToSpeechClient()

            synthesis_input = texttospeech.SynthesisInput(text=text)

            voice_params = texttospeech.VoiceSelectionParams(
                language_code=language_code,
                ssml_gender=texttospeech.SsmlVoiceGender.FEMALE,  # giọng nữ
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

            # Lưu file mp3
            out_dir = os.path.join(settings.MEDIA_ROOT, "tts")
            os.makedirs(out_dir, exist_ok=True)

            filename = f"tts_{uuid.uuid4().hex}.mp3"
            full_path = os.path.join(out_dir, filename)
            with open(full_path, "wb") as f:
                f.write(response.audio_content)

            # Đường dẫn mới: đi qua view stream có hỗ trợ Range
            audio_url = f"/api/tts/audio/{filename}"

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

    # Lấy header Range: bytes=start-end
    range_header = request.META.get("HTTP_RANGE", "").strip()
    range_match = re.match(r"bytes=(\d+)-(\d*)", range_header) if range_header else None

    if range_match:
        # Có Range → trả 206 Partial Content
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
        # Không có Range → trả full file (200)
        with open(file_path, "rb") as f:
            data = f.read()

        resp = HttpResponse(data, content_type=content_type)
        resp["Content-Length"] = str(file_size)

    # Cho browser biết có thể send Range
    resp["Accept-Ranges"] = "bytes"
    return resp

class FindArticleByCategoryChild(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, slug):
        db = get_db()
        category_child = db["category_child"].find_one({"slug": slug})
        if not category_child:
            return Response({"detail": "Category not found"}, status=404)

        articles = list(db["articles"].find({"category_child_id": category_child["_id"]}).sort("created_at", -1))
        json_data = json_util.dumps(articles)
        return HttpResponse(json_data, content_type="application/json")

class GetAllCategory(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        db = get_db()
        categories = list(db["categories"].find({}))
        if not categories:
            return Response({"detail": "Category not found"}, status=404)

        json_data = json_util.dumps(categories)
        return HttpResponse(json_data, content_type="application/json")

def top10_articles_this_month(db, user_id: str | None = None):
    # Tháng hiện tại theo giờ VN
    tz = ZoneInfo("Asia/Ho_Chi_Minh")
    now = datetime.now(tz)

    month_start_local = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start_local.month == 12:
        next_month_local = month_start_local.replace(year=month_start_local.year + 1, month=1)
    else:
        next_month_local = month_start_local.replace(month=month_start_local.month + 1)

    month_start_utc = month_start_local.astimezone(timezone.utc)
    next_month_utc = next_month_local.astimezone(timezone.utc)

    # parse user ObjectId (nếu có)
    user_oid = None
    if user_id:
        try:
            user_oid = ObjectId(user_id)
        except Exception:
            user_oid = None

    pipeline = [
        {"$sort": {"views": -1}},

        # join sang articles
        {"$lookup": {
            "from": "articles",
            "localField": "_id",         # nếu popularity._id == articles._id
            "foreignField": "_id",
            "as": "article",
        }},
        {"$unwind": "$article"},

        # lọc bài trong tháng theo published_at
        {"$match": {
            "article.published_at": {"$gte": month_start_utc, "$lt": next_month_utc}
        }},

        {"$limit": 10},

        # join categories để lấy category_name
        {"$lookup": {
            "from": "categories",
            "localField": "article.category_id",
            "foreignField": "_id",
            "as": "cat",
        }},
        {"$unwind": {"path": "$cat", "preserveNullAndEmptyArrays": True}},
    ]

    # nếu có user -> lookup bookmarks để check is_bookmarked
    if user_oid:
        pipeline += [
            {"$lookup": {
                "from": "bookmarks",   # nếu tên collection bạn là "bookmark" thì đổi lại
                "let": {"aid": "$article._id"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$article_id", "$$aid"]},
                        {"$eq": ["$user_id", user_oid]},
                    ]}}},
                    {"$limit": 1},
                ],
                "as": "bm",
            }},
            {"$addFields": {"is_bookmarked": {"$gt": [{"$size": "$bm"}, 0]}}},
        ]
    else:
        pipeline += [
            {"$addFields": {"is_bookmarked": False}}
        ]

    pipeline += [
        {"$project": {
            "_id": 0,
            "views": 1,
            "is_bookmarked": 1,
            "article": {
                "_id": "$article._id",
                "images": "$article.images",     # nếu bạn dùng field "image" thì đổi "$article.image"
                "title": "$article.title",
                "published_at": "$article.published_at",
                "category_name": "$cat.name",
            }
        }},
    ]

    return list(db["article_popularity"].aggregate(pipeline))


class TopArticlesThisMonth(APIView):
    permission_classes = [AllowAny]
    # authentication_classes = []

    def get(self, request):
        db = get_db()
        user_id = None
        if getattr(request, "user", None) and request.user.is_authenticated:
            user_id = str(request.user.id)

        data = top10_articles_this_month(db, user_id=user_id)

        return HttpResponse(
            json_util.dumps(data),
            content_type="application/json"
        )



