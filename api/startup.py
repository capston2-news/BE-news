import secrets
from datetime import datetime, timezone
from django.conf import settings
from django.contrib.auth.hashers import make_password
from .db import get_db

_SEEDED = False

def ensure_default_admin():
    global _SEEDED
    if _SEEDED:
        return
    _SEEDED = True

    db = get_db()
    users = db["users"]

    # index đảm bảo unique theo soft-delete
    users.create_index("username", unique=True, partialFilterExpression={"is_deleted": False}, name="ux_users_username_active")
    users.create_index("email",    unique=True, partialFilterExpression={"is_deleted": False}, name="ux_users_email_active")

    username = getattr(settings, "DEFAULT_ADMIN_USERNAME", "admin")
    email    = getattr(settings, "DEFAULT_ADMIN_EMAIL", "admin@example.com")
    password = getattr(settings, "DEFAULT_ADMIN_PASSWORD", "admin123")

    existing = users.find_one({"$or": [{"username": username}, {"email": email}], "is_deleted": {"$ne": True}})
    if existing:
        print(f"[auth] default admin exists: {existing.get('username')}/{existing.get('email')}")
        return

    if not password:
        password = secrets.token_urlsafe(16)
        print(f"[auth] Generated admin password: {password}")

    doc = {
        "username": username,
        "email": email,
        "password": make_password(password),
        "fullname": "System Administrator",
        "age": 22,
        "created_at": datetime.now(timezone.utc),
        "is_active": True,
        "is_deleted": False,
        "role": "admin",
    }
    res = users.insert_one(doc)
    print(f"[auth] default admin created _id={res.inserted_id} user={username}")
