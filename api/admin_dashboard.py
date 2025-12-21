# admin_dashboard/views.py
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from zoneinfo import ZoneInfo
from api.db import get_db


# -----------------------------
# helpers
# -----------------------------
def _parse_range(range_str: Optional[str]) -> int:
    """
    range=7d|30d|90d
    """
    s = (range_str or "30d").strip().lower()
    if s.endswith("d"):
        try:
            return max(1, int(s[:-1]))
        except Exception:
            return 30
    return 30


def _time_bounds(days: int) -> Tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    return start, now


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


# -----------------------------
# API
# -----------------------------
class AdminDashboardKPIView(APIView):
    """
    GET /api/admin/dashboard/kpis?range=7d
    """
    permission_classes = [AllowAny]  # TODO: đổi sang permission admin nếu cần

    def get(self, request):
        db = get_db()
        days = _parse_range(request.query_params.get("range", "7d"))
        start, end = _time_bounds(days)

        # Articles
        total_articles = db.articles.count_documents({"is_deleted": False, "status": "published"})
        new_articles = db.articles.count_documents({
            "is_deleted": False,
            "status": "published",
            "created_at": {"$gte": start, "$lt": end},
        })

        # Views (user_activity)
        total_views = db.user_activity.count_documents({
            "action": "view",
            "ts": {"$gte": start, "$lt": end},
        })

        # Comments
        pending_comments = db.comments.count_documents({"is_deleted": False, "is_checked": False})
        new_comments = db.comments.count_documents({
            "is_deleted": False,
            "created_at": {"$gte": start, "$lt": end},
        })

        # Bookmarks (nếu bạn có created_at)
        bookmarks_7d = db.bookmarks.count_documents({"created_at": {"$gte": start, "$lt": end}})

        # Users (nếu bạn có created_at)
        total_users = db.users.count_documents({})
        new_users = db.users.count_documents({"created_at": {"$gte": start, "$lt": end}})

        return Response({
            "range_days": days,
            "articles": {"total": total_articles, "new": new_articles},
            "views": {"total": total_views},
            "comments": {"pending": pending_comments, "new": new_comments},
            "bookmarks": {"new": bookmarks_7d},
            "users": {"total": total_users, "new": new_users},
        })


def _parse_range(range_str: str) -> int:
    s = (range_str or "30d").strip().lower()
    if s.endswith("d"):
        try:
            return max(1, int(s[:-1]))
        except Exception:
            return 30
    return 30


class AdminViewsSeriesView(APIView):
    """
    GET /api/admin/dashboard/views-series?range=7d&site=thanhnien

    ✅ Tính theo UTC day
    ✅ Range dựa theo latest ts trong DB
    ✅ Trả đủ N ngày (fill 0) ngay trên BE
    """
    permission_classes = [AllowAny]

    def get(self, request):
        db = get_db()
        days = _parse_range(request.query_params.get("range", "7d"))
        site = request.query_params.get("site")

        # ✅ chốt UTC
        tz_str = "UTC"
        tz = ZoneInfo("UTC")

        base_match = {"action": "view"}
        if site:
            base_match["site"] = site

        # 1) lấy ts mới nhất trong DB
        latest = list(
            db.user_activity.find(base_match, {"ts": 1}).sort("ts", -1).limit(1)
        )
        if not latest:
            return Response({"range_days": days, "site": site, "tz": tz_str, "data": []})

        latest_ts = latest[0].get("ts")
        if not isinstance(latest_ts, datetime):
            return Response({"range_days": days, "site": site, "tz": tz_str, "data": []})

        # 2) range theo "ngày UTC"
        end_day = latest_ts.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start_day = end_day - timedelta(days=days - 1)

        start_utc = start_day.astimezone(timezone.utc)
        end_utc_excl = (end_day + timedelta(days=1)).astimezone(timezone.utc)

        match = {**base_match, "ts": {"$gte": start_utc, "$lt": end_utc_excl}}

        # 3) aggregate theo ngày UTC
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": {
                        "day": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$ts",
                                "timezone": tz_str,  # ✅ UTC
                            }
                        }
                    },
                    "views": {"$sum": 1},
                }
            },
            {"$sort": {"_id.day": 1}},
            {"$project": {"_id": 0, "day": "$_id.day", "views": 1}},
        ]

        raw = list(db.user_activity.aggregate(pipeline))
        m = {x["day"]: int(x.get("views", 0) or 0) for x in raw}

        # 4) fill đủ N ngày ngay trên BE
        out = []
        d = start_day
        for _ in range(days):
            key = d.strftime("%Y-%m-%d")
            out.append({"day": key, "views": m.get(key, 0)})
            d = d + timedelta(days=1)

        return Response(
            {
                "range_days": days,
                "site": site,
                "tz": tz_str,
                "range_start": start_day.strftime("%Y-%m-%d"),
                "range_end": end_day.strftime("%Y-%m-%d"),
                "data": out,
            }
        )


class AdminCommentsSeriesView(APIView):
    """
    GET /api/admin/dashboard/comments-series?range=30d
    Stacked: pending vs checked theo ngày
    """
    permission_classes = [AllowAny]

    def get(self, request):
        db = get_db()
        days = _parse_range(request.query_params.get("range", "30d"))
        start, end = _time_bounds(days)

        pipeline = [
            {
                "$match": {
                    "is_deleted": False,
                    "created_at": {"$gte": start, "$lt": end},
                }
            },
            {
                "$group": {
                    "_id": {
                        "day": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": "$created_at",
                                "timezone": "Asia/Ho_Chi_Minh",
                            }
                        },
                        "checked": "$is_checked",
                    },
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"_id.day": 1}},
            {
                "$project": {
                    "_id": 0,
                    "day": "$_id.day",
                    "status": {"$cond": ["$_id.checked", "checked", "pending"]},
                    "count": 1,
                }
            },
        ]

        data = list(db.comments.aggregate(pipeline))
        return Response({"range_days": days, "data": data})


class AdminTopArticlesView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        db = get_db()
        days = _parse_range(request.query_params.get("range", "7d"))
        limit = _safe_int(request.query_params.get("limit", 10), 10)
        limit = max(1, min(limit, 50))
        site = request.query_params.get("site")

        tz = ZoneInfo("UTC")
        base_match = {"action": "view"}
        if site:
            base_match["site"] = site

        latest = list(db.user_activity.find(base_match, {"ts": 1}).sort("ts", -1).limit(1))
        if not latest:
            return Response({"range_days": days, "limit": limit, "site": site, "data": []})

        latest_ts = latest[0].get("ts")
        if not isinstance(latest_ts, datetime):
            return Response({"range_days": days, "limit": limit, "site": site, "data": []})

        end_day = latest_ts.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start_day = end_day - timedelta(days=days - 1)

        start_utc = start_day.astimezone(timezone.utc)
        end_utc_excl = (end_day + timedelta(days=1)).astimezone(timezone.utc)

        match = {**base_match, "ts": {"$gte": start_utc, "$lt": end_utc_excl}}

        pipeline = [
            {"$match": match},
            {"$group": {"_id": "$article_id", "views": {"$sum": 1}}},
            {"$sort": {"views": -1}},
            {"$limit": limit},
            {
                "$lookup": {
                    "from": "articles",
                    "localField": "_id",
                    "foreignField": "_id",
                    "as": "a",
                }
            },
            {"$unwind": "$a"},
            {
                "$project": {
                    "_id": 0,
                    "article_id": {"$toString": "$_id"},  # ✅ FIX
                    "title": "$a.title",
                    "site": "$a.site",
                    "views": 1,
                    "published_at": "$a.published_at",
                    "created_at": "$a.created_at",
                }
            },
        ]

        data = list(db.user_activity.aggregate(pipeline))
        return Response({
            "range_days": days,
            "site": site,
            "limit": limit,
            "range_start": start_day.strftime("%Y-%m-%d"),
            "range_end": end_day.strftime("%Y-%m-%d"),
            "data": data,
        })


class AdminViewsByCategoryView(APIView):
    """
    GET /api/admin/dashboard/views-by-category?range=30d&limit=8&site=thanhnien
    """
    permission_classes = [AllowAny]

    def get(self, request):
        db = get_db()
        days = _parse_range(request.query_params.get("range", "30d"))
        limit = _safe_int(request.query_params.get("limit", 8), 8)
        limit = max(1, min(limit, 30))
        site = request.query_params.get("site")
        start, end = _time_bounds(days)

        match = {
            "action": "view",
            "ts": {"$gte": start, "$lt": end},
        }
        if site:
            match["site"] = site

        pipeline = [
            {"$match": match},
            {"$group": {"_id": "$category_name", "views": {"$sum": 1}}},
            {"$sort": {"views": -1}},
            {"$limit": limit},
            {"$project": {"_id": 0, "name": "$_id", "views": 1}},
        ]

        data = list(db.user_activity.aggregate(pipeline))
        return Response({"range_days": days, "site": site, "limit": limit, "data": data})


class AdminViewsBySiteView(APIView):
    """
    GET /api/admin/dashboard/views-by-site?range=30d
    """
    permission_classes = [AllowAny]

    def get(self, request):
        db = get_db()
        days = _parse_range(request.query_params.get("range", "30d"))
        start, end = _time_bounds(days)

        pipeline = [
            {"$match": {"action": "view", "ts": {"$gte": start, "$lt": end}}},
            {"$group": {"_id": "$site", "views": {"$sum": 1}}},
            {"$sort": {"views": -1}},
            {"$project": {"_id": 0, "site": "$_id", "views": 1}},
        ]

        data = list(db.user_activity.aggregate(pipeline))
        return Response({"range_days": days, "data": data})
