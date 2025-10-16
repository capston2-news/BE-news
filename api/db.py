from pymongo import MongoClient
from django.conf import settings
import os

_client = None
_db = None

def get_db():
    global _client, _db
    if _db is None:
        uri = getattr(settings, "MONGO_URI", os.getenv("MONGO_URI"))
        name = getattr(settings, "MONGO_DB",  os.getenv("MONGO_DB", "news_db"))
        if not uri:
            raise RuntimeError("Missing MONGO_URI")
        _client = MongoClient(uri)
        _db = _client[name]
    return _db
