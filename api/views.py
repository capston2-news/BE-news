from django.shortcuts import render
from rest_framework import serializers, status
from rest_framework.views import APIView
from rest_framework.response import Response
from .db import get_db
from datetime import datetime, timezone, time, date
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

from api.utils.serialize import ser_doc

from notifications.services import create_notification_comment_approved
from notifications.realtime import publish
from notifications.views import _ser

VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
UTC_TZ = ZoneInfo("UTC")

def _parse_date_yyyy_mm_dd(s: str):
    """Chuyển 'YYYY-MM-DD' thành datetime.date (nếu lỗi thì trả None)."""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

class UserViewedHistory(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def get(self, request):
        db = get_db()

        # ----- limit -----
        try:
            limit = int(request.GET.get("limit", "50"))
        except Exception:
            limit = 50
        limit = max(1, min(limit, 200))

        # all=1 -> trả về tất cả lượt xem; mặc định unique theo bài (lấy lượt xem mới nhất)
        all_events = str(request.GET.get("all", "")).lower() in ("1", "true", "yes")

        # ----- user id (có thể lưu dạng string hoặc ObjectId trong user_activity) -----
        raw_uid = getattr(request.user, "id", None) or getattr(request.user, "_id", None)
        if not raw_uid:
            return JsonResponse({"detail": "Unauthorized"}, status=401)

        user_id_str = str(raw_uid)
        user_id_oid = ObjectId(user_id_str) if ObjectId.is_valid(user_id_str) else None

        # match user + action=view
        if user_id_oid:
            match_user = {"$or": [{"user_id": user_id_oid}, {"user_id": user_id_str}]}
        else:
            match_user = {"user_id": user_id_str}

        pipeline = [
            {"$match": {**match_user, "action": "view"}},

            # chuẩn hoá article_id -> ObjectId để join articles ổn định
            {
                "$addFields": {
                    "_article_oid": {
                        "$cond": [
                            {"$eq": [{"$type": "$article_id"}, "objectId"]},
                            "$article_id",
                            {"$convert": {"input": "$article_id", "to": "objectId", "onError": None, "onNull": None}},
                        ]
                    }
                }
            },
            {"$match": {"_article_oid": {"$ne": None}}},
            {"$sort": {"ts": -1}},
        ]

        # unique theo bài: lấy record mới nhất cho mỗi article
        if not all_events:
            pipeline += [
                {
                    "$group": {
                        "_id": "$_article_oid",
                        "article_id": {"$first": "$_article_oid"},
                        "viewed_at": {"$first": "$ts"},
                        "site": {"$first": "$site"},
                        "action": {"$first": "$action"},

                        # giữ lại thông tin category snapshot trong activity (nếu có)
                        "ua_category_id": {"$first": "$category_id"},
                        "ua_category_child_id": {"$first": "$category_child_id"},
                        "ua_category_name": {"$first": "$category_name"},
                        "ua_category_child_name": {"$first": "$category_child_name"},
                    }
                },
                {"$sort": {"viewed_at": -1}},
            ]
        else:
            # all events: giữ từng event
            pipeline += [
                {
                    "$addFields": {
                        "article_id": "$_article_oid",
                        "viewed_at": "$ts",
                        "ua_category_id": "$category_id",
                        "ua_category_child_id": "$category_child_id",
                        "ua_category_name": "$category_name",
                        "ua_category_child_name": "$category_child_name",
                    }
                }
            ]

        pipeline += [
            {"$limit": limit},

            # join articles
            {
                "$lookup": {
                    "from": "articles",
                    "localField": "article_id",
                    "foreignField": "_id",
                    "as": "art",
                }
            },
            {"$unwind": {"path": "$art", "preserveNullAndEmptyArrays": True}},

            # chọn category_id/category_child_id ưu tiên từ article, fallback sang snapshot trong activity
            {
                "$addFields": {
                    "_cat_oid": {"$ifNull": ["$art.category_id", "$ua_category_id"]},
                    "_child_oid": {"$ifNull": ["$art.category_child_id", "$ua_category_child_id"]},
                }
            },

            # map category
            {
                "$lookup": {
                    "from": "categories",
                    "localField": "_cat_oid",
                    "foreignField": "_id",
                    "as": "cat",
                }
            },
            {"$unwind": {"path": "$cat", "preserveNullAndEmptyArrays": True}},

            # map category_child
            {
                "$lookup": {
                    "from": "category_child",
                    "localField": "_child_oid",
                    "foreignField": "_id",
                    "as": "child",
                }
            },
            {"$unwind": {"path": "$child", "preserveNullAndEmptyArrays": True}},
        ]

        # check bookmark (bookmarks thường lưu user_id là ObjectId)
        if user_id_oid:
            pipeline += [
                {
                    "$lookup": {
                        "from": "bookmarks",
                        "let": {"aid": "$article_id"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$article_id", "$$aid"]},
                                            {"$eq": ["$user_id", user_id_oid]},
                                        ]
                                    }
                                }
                            },
                            {"$limit": 1},
                        ],
                        "as": "bm",
                    }
                },
                {"$addFields": {"is_bookmarked": {"$gt": [{"$size": "$bm"}, 0]}}},
            ]
        else:
            pipeline += [{"$addFields": {"is_bookmarked": False}}]

        # output
        pipeline += [
            {
                "$project": {
                    "_id": 0,
                    "article_id": 1,
                    "action": 1,
                    "site": 1,
                    "viewed_at": 1,

                    # article fields
                    "title": "$art.title",
                    "images": "$art.images",
                    "content": "$art.content",
                    "published_at": "$art.published_at",

                    # category fields (ưu tiên lookup, fallback snapshot activity)
                    "category_id": "$_cat_oid",
                    "category_name": {"$ifNull": ["$cat.name", "$ua_category_name"]},
                    "category_slug": "$cat.slug",

                    "category_child_id": "$_child_oid",
                    "category_child_name": {"$ifNull": ["$child.name", "$ua_category_child_name"]},
                    "category_child_slug": "$child.slug",

                    "is_bookmarked": 1,
                }
            }
        ]

        docs = list(db["user_activity"].aggregate(pipeline))
        return HttpResponse(json_util.dumps(docs), content_type="application/json")


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
                "external_url": 1,
                "status": 1,

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
                "external_url": 1,
                "status": 1,

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

        comment = list(db["comments"].find({"article_id": article_oid}, {"username": 1, "content": 1, "created_at": 1, "_id": 0, "is_checked": 1}).sort("created_at", -1))

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
                "is_checked": False
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

class GatBookmarkOfUser(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def get(self, request):
        db = get_db()

        try:
            user_oid = ObjectId(str(request.user.id))
        except Exception:
            return Response({"detail": "Invalid user_id"}, status=400)

        pipeline = [
            {"$match": {"user_id": user_oid}},
            {"$sort": {"created_at": -1}},

            {"$lookup": {
                "from": "articles",
                "localField": "article_id",
                "foreignField": "_id",
                "as": "article",
            }},
            {"$unwind": "$article"},

            {"$lookup": {
                "from": "categories",
                "localField": "article.category_id",
                "foreignField": "_id",
                "as": "cat",
            }},
            {"$unwind": {"path": "$cat", "preserveNullAndEmptyArrays": True}},

            {"$lookup": {
                "from": "category_child",
                "localField": "article.category_child_id",
                "foreignField": "_id",
                "as": "child",
            }},
            {"$unwind": {"path": "$child", "preserveNullAndEmptyArrays": True}},

            # ✅ chỉ trả article + is_bookmarked
            {"$project": {
                "_id": "$article._id",
                "site": "$article.site",
                "title": "$article.title",
                "images": "$article.images",
                "content": "$article.content",
                "published_at": "$article.published_at",

                "category_id": "$article.category_id",
                "category_name": "$cat.name",
                "category_slug": "$cat.slug",

                "category_child_id": "$article.category_child_id",
                "category_child_name": "$child.name",
                "category_child_slug": "$child.slug",

                "is_bookmarked": {"$literal": True},
            }},
        ]

        data = list(db["bookmarks"].aggregate(pipeline))
        return HttpResponse(json_util.dumps(data), content_type="application/json")

class SearchArticleByTitle(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        db = get_db()

        key = (request.query_params.get("key") or "").strip()
        if not key:
            return Response({"detail": "Search not found"}, status=404)

        # limit (optional)
        try:
            limit = int(request.query_params.get("limit", "100"))
        except ValueError:
            limit = 100
        if limit <= 0:
            limit = 100
        if limit > 400:
            limit = 400

        mongo_filter = {
            "title": {"$regex": re.compile(re.escape(key), re.IGNORECASE)}
        }

        # user để check bookmark (bookmarks.user_id là ObjectId)
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

            # map category (cha)
            {"$lookup": {
                "from": "categories",
                "localField": "category_id",
                "foreignField": "_id",
                "as": "cat",
            }},
            {"$unwind": {"path": "$cat", "preserveNullAndEmptyArrays": True}},

            # map category_child (con)
            {"$lookup": {
                "from": "category_child",
                "localField": "category_child_id",
                "foreignField": "_id",
                "as": "child",
            }},
            {"$unwind": {"path": "$child", "preserveNullAndEmptyArrays": True}},
        ]

        # check bookmark nếu có user đăng nhập
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
        if not docs:
            return Response({"detail": "Article not found"}, status=404)

        return HttpResponse(json_util.dumps(docs), content_type="application/json")


#comment
def _to_oid(v):
    if not v:
        return None
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, dict) and "$oid" in v:
        try:
            return ObjectId(v["$oid"])
        except Exception:
            return None
    s = str(v).strip()
    return ObjectId(s) if ObjectId.is_valid(s) else None


def _get_username(request):
    u = getattr(request, "user", None)
    if not u or not getattr(u, "is_authenticated", False):
        return None
    if isinstance(u, dict):
        return u.get("username") or u.get("name") or u.get("email")
    return getattr(u, "username", None) or getattr(u, "name", None) or getattr(u, "email", None)


class CommentLookupArticleView(APIView):
    """
    GET /api/<comment_id>/lookup-article/
    Trả: { comment_id, article_id }
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, comment_id):
        oid = _to_oid(comment_id)
        if not oid:
            return Response({"detail": "Invalid comment_id"}, status=400)

        db = get_db()
        username = _get_username(request)

        # ✅ chỉ owner mới lookup (comment lưu username string)
        q = {"_id": oid}
        if username:
            q["username"] = username

        doc = db.comments.find_one(q, {"_id": 1, "article_id": 1})
        if not doc:
            return Response({"detail": "Comment not found"}, status=404)

        if not doc.get("article_id"):
            return Response({"detail": "Missing article_id"}, status=500)

        return Response(
            {"comment_id": str(doc["_id"]), "article_id": str(doc["article_id"])},
            status=200,
        )

def to_oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        return None


def ser(doc):
    if not doc:
        return None
    out = dict(doc)

    if isinstance(out.get("_id"), ObjectId):
        out["_id"] = str(out["_id"])
    if isinstance(out.get("article_id"), ObjectId):
        out["article_id"] = str(out["article_id"])

    for k in ("created_at", "approved_at", "deleted_at"):
        dt = out.get(k)
        if isinstance(dt, datetime):
            out[k] = dt.isoformat()

    return out


def ensure_indexes(comments):
    comments.create_index([("article_id", 1), ("created_at", -1)], name="ix_comments_article_time")
    comments.create_index([("is_deleted", 1), ("is_checked", 1)], name="ix_comments_status")
    comments.create_index([("username", 1)], name="ix_comments_username")



class AdminApproveComment(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("admin", "employee")]

    def patch(self, request, comment_id: str):
        oid = to_oid(comment_id)
        if not oid:
            return Response({"detail": "Invalid comment_id"}, status=400)

        db = get_db()
        comments = db["comments"]
        ensure_indexes(comments)

        cur = comments.find_one(
            {"_id": oid},
            {"_id": 1, "is_deleted": 1, "is_checked": 1, "username": 1}
        )
        if not cur:
            return Response({"detail": "Comment not found"}, status=404)

        if cur.get("is_deleted") is True:
            return Response({"detail": "Comment is deleted"}, status=400)

        if cur.get("is_checked") is True:
            out = comments.find_one({"_id": oid})
            return Response(ser_doc(out), status=200)

        now = datetime.now(timezone.utc)
        comments.update_one(
            {"_id": oid},
            {"$set": {"is_checked": True, "approved_at": now, "is_deleted": False}},
        )

        # tạo notification theo username (nếu có)
        username = cur.get("username")
        if username:
            try:
                udoc = db["users"].find_one({"username": username}, {"_id": 1})
                if udoc and udoc.get("_id"):
                    create_notification_comment_approved(
                        user_id=udoc["_id"],   # lưu DB: ObjectId ok
                        comment_id=oid,        # lưu DB: ObjectId ok
                        message="Bình luận của bạn đã được duyệt ✅",
                    )
            except Exception as e:
                print("[AdminApproveComment] create notification error:", repr(e))

        out = comments.find_one({"_id": oid})
        return Response(ser_doc(out), status=200)






def to_json_user(u: dict) -> dict:
    # convert ObjectId + datetime -> string
    u["_id"] = str(u.get("_id")) if u.get("_id") else None

    for k in ["created_at", "updated_at"]:
        v = u.get(k)
        if isinstance(v, datetime):
            u[k] = v.isoformat()
    return u


class GetAllUsers(APIView):
    permission_classes = [AllowAny]  # ✅ khuyến nghị (tránh lộ data)

    def get(self, request):
        db = get_db()

        # query params
        q = (request.GET.get("q") or "").strip()
        role = (request.GET.get("role") or "").strip()
        is_active = request.GET.get("is_active")  # "true"/"false"
        is_deleted = request.GET.get("is_deleted")  # "true"/"false"

        try:
            page = int(request.GET.get("page", "1"))
        except ValueError:
            page = 1
        if page < 1:
            page = 1

        try:
            page_size = int(request.GET.get("page_size", "20"))
        except ValueError:
            page_size = 20
        page_size = max(1, min(page_size, 200))

        skip = (page - 1) * page_size

        # build filter
        filt = {}
        if q:
            filt["$or"] = [
                {"username": {"$regex": q, "$options": "i"}},
                {"email": {"$regex": q, "$options": "i"}},
                {"fullname": {"$regex": q, "$options": "i"}},
            ]
        if role:
            filt["role"] = role

        if is_active in ("true", "false"):
            filt["is_active"] = (is_active == "true")

        if is_deleted in ("true", "false"):
            filt["is_deleted"] = (is_deleted == "true")

        # query
        total = db["users"].count_documents(filt)

        cursor = (
            db["users"]
            .find(filt, {"password": 0})  # ✅ không trả password hash
            .sort("created_at", -1)
            .skip(skip)
            .limit(page_size)
        )

        users = [to_json_user(u) for u in cursor]

        return Response({
            "total": total,
            "page": page,
            "page_size": page_size,
            "results": users,
        })


def to_oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        return None

def ser(doc):
    """Serialize mongo document -> json-safe"""
    if not doc:
        return None
    out = dict(doc)
    if "_id" in out:
        out["_id"] = str(out["_id"])
    if "category_id" in out and isinstance(out["category_id"], ObjectId):
        out["category_id"] = str(out["category_id"])
    return out

_slug_re = re.compile(r"[^a-z0-9-]+")

def clean_slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("đ", "d")
    s = s.replace(" ", "-")
    s = _slug_re.sub("", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s

class AdminCreateCategorySerializer(serializers.Serializer):
    name = serializers.CharField(required=True, allow_blank=False, max_length=200)
    slug = serializers.CharField(required=False, allow_blank=True, max_length=200)

class AdminUpdateCategorySerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=False, max_length=200)
    slug = serializers.CharField(required=False, allow_blank=True, max_length=200)
    is_deleted = serializers.BooleanField(required=False)

class AdminCreateCategoryChildSerializer(serializers.Serializer):
    name = serializers.CharField(required=True, allow_blank=False, max_length=200)
    slug = serializers.CharField(required=False, allow_blank=True, max_length=200)

class AdminUpdateCategoryChildSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=False, max_length=200)
    slug = serializers.CharField(required=False, allow_blank=True, max_length=200)
    is_deleted = serializers.BooleanField(required=False)
# =========================
# ADMIN: CATEGORY
# =========================
class AdminCreateCategory(APIView):
    permission_classes = [AllowAny]

    def _ensure_indexes(self, categories):
        categories.create_index(
            "slug",
            unique=True,
            partialFilterExpression={"is_deleted": False},
            name="ux_categories_slug_active",
        )

    def post(self, request):
        s = AdminCreateCategorySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        db = get_db()
        categories = db["categories"]
        self._ensure_indexes(categories)

        name = (d.get("name") or "").strip()
        slug = clean_slug(d.get("slug") or name)

        if not name:
            return Response({"detail": "name is required"}, status=400)
        if not slug:
            return Response({"detail": "slug is required"}, status=400)

        if categories.find_one({"slug": slug, "is_deleted": False}, {"_id": 1}):
            return Response({"detail": "Slug exists"}, status=400)

        doc = {
            "name": name,
            "slug": slug,
            "is_deleted": False,
        }
        r = categories.insert_one(doc)
        out = categories.find_one({"_id": r.inserted_id})
        return Response(ser(out), status=201)


class AdminUpdateCategory(APIView):
    permission_classes = [AllowAny]

    def _ensure_indexes(self, categories):
        categories.create_index(
            "slug",
            unique=True,
            partialFilterExpression={"is_deleted": False},
            name="ux_categories_slug_active",
        )

    def patch(self, request, category_id):
        return self._update(request, category_id)

    def _update(self, request, category_id):
        oid = to_oid(category_id)
        if not oid:
            return Response({"detail": "Invalid category_id"}, status=400)

        s = AdminUpdateCategorySerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        db = get_db()
        categories = db["categories"]
        self._ensure_indexes(categories)

        # ✅ cho phép update cả record deleted để restore
        cur = categories.find_one({"_id": oid})
        if not cur:
            return Response({"detail": "Category not found"}, status=404)

        # ✅ nếu đang deleted mà không gửi is_deleted để restore -> chặn update
        if cur.get("is_deleted") is True and "is_deleted" not in d:
            return Response(
                {"detail": "Category is deleted. Set is_deleted=false to restore first."},
                status=400,
            )

        set_doc = {}

        if "name" in d:
            set_doc["name"] = (d["name"] or "").strip()
            if not set_doc["name"]:
                return Response({"detail": "name cannot be blank"}, status=400)

        if "slug" in d:
            new_slug = clean_slug(d.get("slug") or "")
            if not new_slug:
                return Response({"detail": "slug cannot be blank"}, status=400)
            # unique check (exclude current)
            if categories.find_one(
                {"_id": {"$ne": oid}, "slug": new_slug, "is_deleted": False},
                {"_id": 1},
            ):
                return Response({"detail": "Slug exists"}, status=400)
            set_doc["slug"] = new_slug

        # soft delete / restore (KHÔNG date fields)
        if "is_deleted" in d:
            set_doc["is_deleted"] = bool(d["is_deleted"])

        if not set_doc:
            return Response({"detail": "No fields to update"}, status=400)

        categories.update_one({"_id": oid}, {"$set": set_doc})
        out = categories.find_one({"_id": oid})
        return Response(ser(out), status=200)


class AdminDeleteCategory(APIView):
    permission_classes = [AllowAny]

    def delete(self, request, category_id):
        oid = to_oid(category_id)
        if not oid:
            return Response({"detail": "Invalid category_id"}, status=400)

        db = get_db()
        categories = db["categories"]
        childs = db["category_child"]
        cur = categories.find_one({"_id": oid, "is_deleted": False}, {"_id": 1})
        if not cur:
            return Response({"detail": "Category not found"}, status=404)

        # soft delete category (không date)
        categories.update_one({"_id": oid}, {"$set": {"is_deleted": True}})

        # tuỳ bạn: xoá mềm luôn children
        childs.update_many({"category_id": oid, "is_deleted": False}, {"$set": {"is_deleted": True}})

        return Response({"detail": "deleted"}, status=200)


# =========================
# ADMIN: CATEGORY CHILD
# theo category_slug
# =========================
class AdminCreateCategoryChild(APIView):
    permission_classes = [AllowAny]  # đổi sang IsAuthenticated/RoleRequired nếu cần

    def _ensure_indexes(self, childs):
        childs.create_index(
            [("category_id", 1), ("slug", 1)],
            unique=True,
            partialFilterExpression={"is_deleted": False},
            name="ux_child_cat_slug_active",
        )

    def post(self, request, category_slug):
        s = AdminCreateCategoryChildSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        db = get_db()
        categories = db["categories"]
        childs = db["category_child"]
        self._ensure_indexes(childs)

        cat = categories.find_one({"slug": category_slug, "is_deleted": False}, {"_id": 1, "slug": 1})
        if not cat:
            return Response({"detail": "Category not found"}, status=404)

        name = (d.get("name") or "").strip()
        if not name:
            return Response({"detail": "name cannot be blank"}, status=400)

        slug = clean_slug(d.get("slug") or "")
        if not slug:
            return Response({"detail": "slug cannot be blank"}, status=400)

        # check unique slug within category (active)
        # ✅ support both ObjectId/string in stored data
        if childs.find_one(
            {
                "$or": [{"category_id": cat["_id"]}, {"category_id": str(cat["_id"])}],
                "slug": slug,
                "is_deleted": False,
            },
            {"_id": 1},
        ):
            return Response({"detail": "Child slug exists in this category"}, status=400)

        doc = {
            "category_id": cat["_id"],   # ✅ always store ObjectId
            "name": name,
            "slug": slug,
            "is_deleted": False,
        }

        childs.insert_one(doc)
        out = childs.find_one({"slug": slug, "$or": [{"category_id": cat["_id"]}, {"category_id": str(cat["_id"])}]})
        return Response(ser(out), status=201)



class AdminUpdateCategoryChild(APIView):
    permission_classes = [AllowAny]  # đổi sang IsAuthenticated/RoleRequired nếu cần
    # permission_classes = [IsAuthenticated, RoleRequired.any_of("admin")]

    def _ensure_indexes(self, childs):
        childs.create_index(
            [("category_id", 1), ("slug", 1)],
            unique=True,
partialFilterExpression={"is_deleted": False},
            name="ux_child_cat_slug_active",
        )

    def patch(self, request, category_slug, child_id):
        return self._update(request, category_slug, child_id)

    def _update(self, request, category_slug, child_id):
        oid = to_oid(child_id)
        if not oid:
            return Response({"detail": "Invalid child_id"}, status=400)

        s = AdminUpdateCategoryChildSerializer(data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        db = get_db()
        categories = db["categories"]
        childs = db["category_child"]
        self._ensure_indexes(childs)

        cat = categories.find_one({"slug": category_slug, "is_deleted": False}, {"_id": 1})
        if not cat:
            return Response({"detail": "Category not found"}, status=404)

        # ✅ support both ObjectId/string stored category_id
        only = childs.find_one({"_id": oid}, {"_id": 1, "category_id": 1, "is_deleted": 1, "slug": 1, "name": 1})
        if not only:
            return Response({"detail": "child_id not found in category_child"}, status=404)

        # 2) check category match
        child_cat = only.get("category_id")
        ok = (child_cat == cat["_id"]) or (str(child_cat) == str(cat["_id"]))
        if not ok:
            return Response(
                {
                    "detail": "Child exists but does NOT belong to this category_slug",
                    "child_id": str(only["_id"]),
                    "child_category_id": str(child_cat),
                    "expected_category_id": str(cat["_id"]),
                    "category_slug": category_slug,
                },
                status=404,
            )

        # 3) nếu ok thì cur = only (hoặc query đầy đủ như bạn muốn)
        cur = only

        # ✅ if deleted and not restoring => block update
        if cur.get("is_deleted") is True and "is_deleted" not in d:
            return Response(
                {"detail": "Category child is deleted. Set is_deleted=false to restore first."},
                status=400,
            )

        set_doc = {}

        if "name" in d:
            name = (d.get("name") or "").strip()
            if not name:
                return Response({"detail": "name cannot be blank"}, status=400)
            set_doc["name"] = name

        if "slug" in d:
            new_slug = clean_slug(d.get("slug") or "")
            if not new_slug:
                return Response({"detail": "slug cannot be blank"}, status=400)

            # unique slug check inside same category (active)
            if childs.find_one(
                {
                    "_id": {"$ne": oid},
                    "$or": [
                        {"category_id": cat["_id"]},
                        {"category_id": str(cat["_id"])},
                    ],
                    "slug": new_slug,
                    "is_deleted": False,
                },
{"_id": 1},
            ):
                return Response({"detail": "Child slug exists in this category"}, status=400)

            set_doc["slug"] = new_slug

        # soft delete / restore (no created/updated date as you want)
        if "is_deleted" in d:
            set_doc["is_deleted"] = bool(d.get("is_deleted"))

        # ✅ OPTIONAL: normalize category_id to ObjectId if it was string before
        # (để về sau không mismatch nữa)
        if isinstance(cur.get("category_id"), str):
            set_doc["category_id"] = cat["_id"]

        if not set_doc:
            return Response({"detail": "No fields to update"}, status=400)

        childs.update_one({"_id": oid}, {"$set": set_doc})

        out = childs.find_one({"_id": oid})
        return Response(ser(out), status=200)



class AdminDeleteCategoryChild(APIView):
    permission_classes = [AllowAny]  # đổi sang IsAuthenticated/RoleRequired nếu cần

    def delete(self, request, category_slug, child_id):
        oid = to_oid(child_id)
        if not oid:
            return Response({"detail": "Invalid child_id"}, status=400)

        db = get_db()
        categories = db["categories"]
        childs = db["category_child"]

        cat = categories.find_one({"slug": category_slug, "is_deleted": False}, {"_id": 1})
        if not cat:
            return Response({"detail": "Category not found"}, status=404)

        # ✅ support both ObjectId/string stored category_id
        cur = childs.find_one(
            {
                "_id": oid,
                "$or": [
                    {"category_id": cat["_id"]},
                    {"category_id": str(cat["_id"])},
                ],
            },
            {"_id": 1, "is_deleted": 1},
        )
        if not cur:
            return Response({"detail": "Category child not found"}, status=404)

        childs.update_one({"_id": oid}, {"$set": {"is_deleted": True}})
        return Response({"detail": "Deleted"}, status=200)








