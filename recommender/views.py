from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from api.permissions import AllowAny, IsAuthenticated, RoleRequired
from .services.user_profile  import get_user_top_categories
from .services.embeddings import load_embedder
from .services.chroma_store import get_client, get_articles_collection
from .services.user_profile import build_or_get_user_profile
from .services.hybrid import hybrid_recommend
from .services.similar import similar_by_article, user_topic_feed
from api.db import get_db

from chatbot.views import get_user_recommendations

from datetime import datetime, timezone
from bson import ObjectId
CATEGORY_COLLECTION = "categories"
CATEGORY_CHILD_COLLECTION = "category_child"
@method_decorator(csrf_exempt, name="dispatch")
class ReindexView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        from .management.commands.build_index import Command
        Command().handle()
        return Response({"ok": True})

@method_decorator(csrf_exempt, name="dispatch")
class HybridRecommendView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        data = request.data or {}
        user_id = str(data.get("user_id") or "guest")
        topk = int(data.get("topk", 10))
        only_categories = data.get("only_categories") or []
        only_category_ids = data.get("only_category_ids") or []
        soft_boost = bool(data.get("soft_boost", False))

        embedder = load_embedder(settings.SENTENCE_MODEL)
        client = get_client(settings.CHROMA_DIR)
        coll = get_articles_collection(client)
        db = get_db()

        profile = build_or_get_user_profile(db, user_id, embedder)
        results = hybrid_recommend(
            db=db, chroma_collection=coll, embedder=embedder, user_profile=profile, topk=topk,
            only_categories=only_categories, only_category_ids=only_category_ids, soft_boost=soft_boost
        )
        return Response({"user_id": user_id, "count": len(results), "results": results})

@method_decorator(csrf_exempt, name="dispatch")
class SimilarByArticleView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        data = request.data or {}
        article_id = data.get("article_id")
        user_id = str(data.get("user_id") or "guest")
        topk = int(data.get("topk", 10))
        if not article_id:
            return Response({"detail":"article_id required"}, status=400)

        embedder = load_embedder(settings.SENTENCE_MODEL)
        coll = get_articles_collection(get_client(settings.CHROMA_DIR))
        db = get_db()
        results = similar_by_article(db, coll, embedder, article_id, user_id, topk)
        return Response({"article_id": article_id, "count": len(results), "results": results})

@method_decorator(csrf_exempt, name="dispatch")
class UserTopicFeedView(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def post(self, request):
        try:
            user_oid = ObjectId(request.user.id)
        except Exception:
            return Response({"detail": "Invalid user_id"}, status=400)

        data = request.data or {}
        requested_user_id = str(user_oid)
        topk = int(data.get("topk", 10))
        min_focus = float(data.get("min_focus", 0.35))

        results = get_user_recommendations(
            user_id=requested_user_id,
            topk=topk,
            min_focus=min_focus,
        )

        return Response({
            "user_id": requested_user_id,
            "count": len(results),
            "results": results,
        })

def _get_category_name(db, cat_id):
    """
    Lấy tên category từ collection categories.
    Ưu tiên field 'name', fallback sang 'category_name' / 'title' nếu có.
    """
    if not cat_id:
        return None

    try:
        oid = ObjectId(cat_id) if not isinstance(cat_id, ObjectId) else cat_id
    except Exception:
        return None

    doc = db[CATEGORY_COLLECTION].find_one(
        {"_id": oid},
        {"name": 1, "category_name": 1, "title": 1}
    ) or {}

    return (
        doc.get("name")
        or doc.get("category_name")
        or doc.get("title")
    )


def _get_category_child_name(db, child_id):
    """
    Lấy tên category con / subcategory từ collection category_children (hoặc tương đương).
    """
    if not child_id:
        return None

    try:
        oid = ObjectId(child_id) if not isinstance(child_id, ObjectId) else child_id
    except Exception:
        return None

    doc = db[CATEGORY_CHILD_COLLECTION].find_one(
        {"_id": oid},
        {"name": 1, "category_name": 1, "title": 1}
    ) or {}

    return (
        doc.get("name")
        or doc.get("category_name")
        or doc.get("title")
    )


@method_decorator(csrf_exempt, name="dispatch")
class LogActivityView(APIView):
    permission_classes = [IsAuthenticated, RoleRequired.any_of("user", "admin", "employee")]

    def post(self, request):
        """
        Body: { "user_id": "...", "article_id": "...", "action": "view|like|share" }
        """

        db = get_db()
        try:
            user_oid = ObjectId(request.user.id)
        except Exception:
            return Response({"detail": "Invalid user_id"}, status=400)

        data = request.data or {}
        user_id = str(user_oid)
        article_id = data.get("article_id")
        action = (data.get("action") or "view").lower()

        if not article_id:
            return Response({"detail": "article_id required"}, status=400)

        try:
            aid = ObjectId(article_id)
        except Exception:
            return Response({"detail": "invalid article_id"}, status=400)

        # Lấy bài viết: ở đây chỉ có category_id / category_child_id
        art = db["articles"].find_one(
            {"_id": aid},
            {
                "site": 1,
                "source": 1,
                "category_id": 1,
                "category_child_id": 1,
            }
        ) or {}

        site = art.get("site") or art.get("source")

        category_id = art.get("category_id")
        category_child_id = art.get("category_child_id")

        # 👉 JOIN sang bảng category & category_child để lấy tên
        category_name = _get_category_name(db, category_id)
        category_child_name = _get_category_child_name(db, category_child_id)

        doc = {
            "user_id": user_id,
            "article_id": aid,
            "action": action,
            "ts": datetime.now(timezone.utc),
            "site": site,
            "category_id": category_id,
            "category_child_id": category_child_id,
            # tên hiển thị cho recommender
            "category_name": category_name,          # ví dụ: "Bóng đá Việt Nam"
            "category_child_name": category_child_name,  # ví dụ: "Tennis"
        }

        db["user_activity"].insert_one(doc)

        # Cập nhật độ phổ biến
        db["article_popularity"].update_one(
            {"_id": aid},
            {
                "$inc": {"views": 1},
                "$setOnInsert": {"first_seen": datetime.now(timezone.utc)},
            },
            upsert=True
        )

        return Response({"ok": True})

@method_decorator(csrf_exempt, name="dispatch")
class ChromaDebugView(APIView):
    permission_classes = [AllowAny]
    def get(self, request):
        coll = get_articles_collection(get_client(settings.CHROMA_DIR))
        peek = coll.peek(5)
        return Response({
            "count": coll.count(),
            "peek_ids": peek.get("ids", []),
            "peek_titles": [m.get("title") for m in (peek.get("metadatas",[[]])[0] if peek.get("metadatas") else [])]
        })
