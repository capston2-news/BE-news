# api/admin_comments.py
from __future__ import annotations

from bson import ObjectId
from datetime import  timezone as dt_timezone

from datetime import datetime, timezone
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.utils import timezone as dj_timezone
from api.db import get_db
# ---------- helpers ----------



def to_oid(v):
    try:
        return ObjectId(str(v))
    except Exception:
        return None


def _to_utc_aware(dt: datetime) -> datetime:
    """
    Chuẩn hoá datetime sang UTC aware.
    - Nếu dt naive => assume là UTC (rất thường gặp khi insert datetime.utcnow()).
    - Nếu dt aware => đổi về UTC.
    """
    if not isinstance(dt, datetime):
        return None
    if dj_timezone.is_naive(dt):
        return dt.replace(tzinfo=dt_timezone.utc)
    return dt.astimezone(dt_timezone.utc)


def _iso_z(dt: datetime) -> str:
    dt_utc = _to_utc_aware(dt)
    if not dt_utc:
        return None
    # đổi +00:00 thành Z để JS parse chắc chắn là UTC
    return dt_utc.isoformat().replace("+00:00", "Z")


def ser(doc):
    if not doc:
        return None
    out = dict(doc)

    # ids -> string
    if isinstance(out.get("_id"), ObjectId):
        out["_id"] = str(out["_id"])
    if isinstance(out.get("article_id"), ObjectId):
        out["article_id"] = str(out["article_id"])

    # datetime -> ISO Z + epoch ms
    for k in ("created_at", "approved_at", "deleted_at"):
        dt = out.get(k)
        if isinstance(dt, datetime):
            dt_utc = _to_utc_aware(dt)
            out[k] = _iso_z(dt)                 # ✅ ISO có Z (UTC)
            out[f"{k}_ms"] = int(dt_utc.timestamp() * 1000)  # ✅ epoch ms (FE sort cực chuẩn)

    return out


def ensure_indexes(comments):
    # NOTE: bạn đang gọi ensure_indexes mỗi request, không sao nhưng hơi tốn.
    comments.create_index([("article_id", 1), ("created_at", -1)], name="ix_comments_article_time")
    comments.create_index([("is_deleted", 1), ("is_checked", 1)], name="ix_comments_status")
    comments.create_index([("username", 1)], name="ix_comments_username")


class AdminListComments(APIView):
    """
    GET /api/admin/comments/?status=pending|approved|deleted|all&q=&page=&page_size=
    """
    permission_classes = [AllowAny]

    def get(self, request):
        db = get_db()
        comments = db["comments"]
        ensure_indexes(comments)

        status = (request.GET.get("status") or "pending").lower()
        q = (request.GET.get("q") or "").strip()

        try:
            page = int(request.GET.get("page") or "1")
        except ValueError:
            page = 1

        try:
            page_size = int(request.GET.get("page_size") or "30")
        except ValueError:
            page_size = 30

        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        skip = (page - 1) * page_size

        # ✅ status filter
        flt = {}
        if status == "pending":
            flt["is_deleted"] = False
            flt["is_checked"] = False
        elif status == "approved":
            flt["is_deleted"] = False
            flt["is_checked"] = True
        elif status == "deleted":
            flt["is_deleted"] = True
        elif status == "all":
            pass
        else:
            return Response({"detail": "Invalid status. Use pending|approved|deleted|all"}, status=400)

        # ✅ search
        if q:
            flt["$or"] = [
                {"content": {"$regex": q, "$options": "i"}},
                {"username": {"$regex": q, "$options": "i"}},
            ]

        total = comments.count_documents(flt)

        # ✅ sort newest first (thêm _id -1 để ổn định khi created_at trùng)
        cur = (
            comments.find(
                flt,
                {
                    "_id": 1,
                    "username": 1,
                    "article_id": 1,
                    "content": 1,
                    "created_at": 1,
                    "is_deleted": 1,
                    "is_checked": 1,
                    "approved_at": 1,
                    "deleted_at": 1,
                },
            )
            .sort([("created_at", -1), ("_id", -1)])
            .skip(skip)
            .limit(page_size)
        )

        return Response(
            {
                "results": [ser(x) for x in cur],
                "meta": {
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "status": status,
                },
            },
            status=200,
        )



class AdminDeleteComment(APIView):
    """
    DELETE /api/admin/comments/<comment_id>/delete/
    -> soft delete
    """
    # permission_classes = (IsAuthenticated, RoleRequired.any_of("admin"))
    permission_classes = [AllowAny]

    def delete(self, request, comment_id):
        oid = to_oid(comment_id)
        if not oid:
            return Response({"detail": "Invalid comment_id"}, status=400)

        db = get_db()
        comments = db["comments"]
        ensure_indexes(comments)

        cur = comments.find_one({"_id": oid}, {"_id": 1})
        if not cur:
            return Response({"detail": "Comment not found"}, status=404)

        now = datetime.now(timezone.utc)
        comments.update_one({"_id": oid}, {"$set": {"is_deleted": True, "deleted_at": now}})
        return Response({"detail": "Deleted"}, status=200)
# views.py (comments admin)




def _parse_after_ts(v):
    if not v:
        return None
    try:
        n = int(v)
    except Exception:
        return None

    # nếu lỡ truyền giây thì đổi sang ms
    if n < 10**11:
        n *= 1000

    # ✅ dùng naive UTC cho ObjectId.from_datetime
    return datetime.utcfromtimestamp(n / 1000.0)

class AdminPendingCommentsCount(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        db = get_db()
        col = db["comments"]

        after_ts = request.GET.get("after_ts") or request.GET.get("afterTs")
        after_dt = _parse_after_ts(after_ts)

        flt = {"is_deleted": False, "is_checked": False}  # pending

        if after_dt:
            oid_after = ObjectId.from_datetime(after_dt)
            flt["_id"] = {"$gt": oid_after}   # ✅ chuẩn, không lệch timezone

        n = col.count_documents(flt)
        return Response({"count": n}, status=200)
class AdminRestoreComment(APIView):
    """
    PATCH /api/admin/comments/<comment_id>/restore/
    -> restore soft deleted comment và đưa về trạng thái CHỜ DUYỆT
    """
    permission_classes = [AllowAny]

    def patch(self, request, comment_id):
        oid = to_oid(comment_id)
        if not oid:
            return Response({"detail": "Invalid comment_id"}, status=400)

        db = get_db()
        comments = db["comments"]
        ensure_indexes(comments)

        cur = comments.find_one(
            {"_id": oid},
            {"_id": 1, "is_deleted": 1, "is_checked": 1}
        )
        if not cur:
            return Response({"detail": "Comment not found"}, status=404)

        # idempotent: nếu chưa bị xoá thì vẫn ép về pending theo yêu cầu
        comments.update_one(
            {"_id": oid},
            {
                "$set": {
                    "is_deleted": False,
                    "is_checked": False,   # ✅ về chờ duyệt
                },

            },
        )

        out = comments.find_one({"_id": oid})
        return Response(ser(out), status=200)

