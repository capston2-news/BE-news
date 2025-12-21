from rest_framework.views import APIView
from rest_framework.permissions import AllowAny

from django.http import HttpResponse
from bson import json_util

from api.db import get_db


class GetAllBookmarksOfUsers(APIView):
    permission_classes = [AllowAny]

    def get(self, request, user_id):
        db = get_db()
        user_id_str = str(user_id).strip()

        pipeline = [
            # ✅ match được cả user_id kiểu ObjectId lẫn string
            {"$match": {"$expr": {"$eq": [{"$toString": "$user_id"}, user_id_str]}}},

            {"$sort": {"created_at": -1}},

            # ✅ convert article_id an toàn
            {"$addFields": {
                "article_oid": {
                    "$convert": {
                        "input": "$article_id",
                        "to": "objectId",
                        "onError": None,
                        "onNull": None
                    }
                }
            }},

            {"$lookup": {
                "from": "articles",
                "localField": "article_oid",
                "foreignField": "_id",
                "as": "article"
            }},

            # ✅ không làm rớt bookmark nếu article lookup không ra
            {"$unwind": {"path": "$article", "preserveNullAndEmptyArrays": True}},

            {"$project": {
                "_id": 1,
                "created_at": 1,
                "user_id": 1,
                "article_id": 1,

                "title": "$article.title",
                "content": "$article.content",
                "published_at": "$article.published_at",
                "category_id": "$article.category_id",
                "category_child_id": "$article.category_child_id",

                "is_bookmarked": {"$literal": True},
            }},
        ]

        docs = list(db["bookmarks"].aggregate(pipeline))
        return HttpResponse(json_util.dumps(docs), content_type="application/json")