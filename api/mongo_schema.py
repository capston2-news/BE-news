# api/mongo_schema.py
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import CollectionInvalid
from bson import ObjectId
from .db import get_db

def _create_or_update_collection(name: str, validator: dict | None):
    """
    Tạo collection nếu chưa có. Nếu đã có, cập nhật $jsonSchema (nếu truyền vào).
    """
    db = get_db()
    if name in db.list_collection_names():
        if validator:
            db.command({
                "collMod": name,
                "validator": {"$jsonSchema": validator},
                "validationLevel": "moderate",
                "validationAction": "error",
            })
        return db[name]
    # chưa có -> tạo mới
    opts = {}
    if validator:
        opts["validator"] = {"$jsonSchema": validator}
        opts["validationLevel"] = "moderate"
        opts["validationAction"] = "error"
    try:
        db.create_collection(name, **opts)
    except CollectionInvalid:
        pass
    return db[name]

def ensure_collections_and_indexes():
    """
    Ánh xạ các bảng SQL -> Mongo collections + validator + indexes tương đương.
    Dùng ObjectId cho các _id; các "FK" chỉ được kiểm tra kiểu (objectId), không có ON DELETE CASCADE tự động.
    """
    db = get_db()

    # ───────────────────────── users ─────────────────────────
    users_validator = {
        "bsonType": "object",
        "required": ["username","email","password","fullname","age","is_active","is_deleted","role","created_at"],
        "properties": {
            "_id": {"bsonType": "objectId"},
            "username": {"bsonType": "string", "minLength": 3},
            "email": {"bsonType": "string", "pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
            "password": {"bsonType": "string", "minLength": 8},
            "fullname": {"bsonType": "string"},
            "age": {"bsonType": "int", "minimum": 0},
            "created_at": {"bsonType": ["date","string"]},
            "is_active": {"bsonType": "bool"},
            "is_deleted": {"bsonType": "bool"},
            "role": {"enum": ["user","employee","admin"]},
        }
    }
    users = _create_or_update_collection("users", users_validator)

    # SQL: unique(username) & unique(email) khi is_deleted = FALSE  → partial unique index
    users.create_index(
        [("username", ASCENDING)],
        name="ux_users_username_active",
        unique=True,
        partialFilterExpression={"is_deleted": False}
    )
    users.create_index(
        [("email", ASCENDING)],
        name="ux_users_email_active",
        unique=True,
        partialFilterExpression={"is_deleted": False}
    )

    # ───────────────────────── categories ─────────────────────────
    categories_validator = {
        "bsonType":"object",
        "required":["name"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "name":{"bsonType":"string"}
        }
    }
    categories = _create_or_update_collection("categories", categories_validator)
    categories.create_index([("name", ASCENDING)], name="ux_categories_name", unique=True)

    # ───────────────────────── category_child ─────────────────────────
    category_child_validator = {
        "bsonType":"object",
        "required":["name","category_id"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "name":{"bsonType":"string"},
            "category_id":{"bsonType":"objectId"}  # FK -> categories._id
        }
    }
    category_child = _create_or_update_collection("category_child", category_child_validator)
    category_child.create_index([("name", ASCENDING)], name="ux_category_child_name", unique=True)
    category_child.create_index([("category_id", ASCENDING)], name="ix_category_child_category_id")

    # ───────────────────────── authors ─────────────────────────
    authors_validator = {
        "bsonType":"object",
        "required":["name"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "name":{"bsonType":"string"}
        }
    }
    authors = _create_or_update_collection("authors", authors_validator)
    authors.create_index([("name", ASCENDING)], name="ux_authors_name", unique=True)

    # ───────────────────────── topics ─────────────────────────
    topics_validator = {
        "bsonType":"object",
        "required":["title"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "title":{"bsonType":"string"}
        }
    }
    topics = _create_or_update_collection("topics", topics_validator)
    topics.create_index([("title", ASCENDING)], name="ix_topics_title")

    # ───────────────────────── articles ─────────────────────────
    # SQL có status article_status (enum); dùng enum string trong Mongo.
    articles_validator = {
        "bsonType":"object",
        "required":["title","content","author_id","category_id","is_deleted"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "title":{"bsonType":"string"},
            "content":{"bsonType":"string"},
            "author_id":{"bsonType":"objectId"},      # -> authors._id
            "category_id":{"bsonType":"objectId"},    # -> categories._id
            "category_child_id":{"bsonType":["objectId","null"]},  # -> category_child._id
            "published_at":{"bsonType":["date","null","string"]},
            "status":{"enum":["draft","scheduled","published","archived", None]},  # tuỳ bạn định nghĩa
            "is_deleted":{"bsonType":"bool"}
        }
    }
    articles = _create_or_update_collection("articles", articles_validator)
    articles.create_index([("title", ASCENDING)], name="ix_articles_title")
    articles.create_index([("author_id", ASCENDING)], name="ix_articles_author_id")
    articles.create_index([("category_id", ASCENDING)], name="ix_articles_category_id")

    # ───────────────────────── article_topics (junction) ─────────────────────────
    article_topics_validator = {
        "bsonType":"object",
        "required":["article_id","topic_id"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "article_id":{"bsonType":"objectId"},  # -> articles._id
            "topic_id":{"bsonType":"objectId"}     # -> topics._id
        }
    }
    article_topics = _create_or_update_collection("article_topics", article_topics_validator)
    article_topics.create_index(
        [("article_id", ASCENDING), ("topic_id", ASCENDING)],
        name="article_topics_unique",
        unique=True
    )

    # ───────────────────────── comments ─────────────────────────
    comments_validator = {
        "bsonType":"object",
        "required":["article_id","user_id","content","is_deleted"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "article_id":{"bsonType":"objectId"},  # -> articles._id
            "user_id":{"bsonType":"objectId"},     # -> users._id
            "content":{"bsonType":"string"},
            "created_at":{"bsonType":["date","string"]},
            "is_deleted":{"bsonType":"bool"}
        }
    }
    comments = _create_or_update_collection("comments", comments_validator)
    comments.create_index([("article_id", ASCENDING)], name="ix_comments_article_id")
    comments.create_index([("user_id", ASCENDING)], name="ix_comments_user_id")

    # ───────────────────────── bookmarks ─────────────────────────
    bookmarks_validator = {
        "bsonType":"object",
        "required":["user_id","article_id","created_at"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "user_id":{"bsonType":"objectId"},     # -> users._id
            "article_id":{"bsonType":"objectId"},  # -> articles._id
            "created_at":{"bsonType":["date","string"]}
        }
    }
    bookmarks = _create_or_update_collection("bookmarks", bookmarks_validator)
    bookmarks.create_index(
        [("user_id", ASCENDING), ("article_id", ASCENDING)],
        name="bookmarks_unique",
        unique=True
    )

    # ───────────────────────── notifications ─────────────────────────
    notifications_validator = {
        "bsonType":"object",
        "required":["user_id","message","is_read","created_at"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "user_id":{"bsonType":"objectId"},
            "message":{"bsonType":"string"},
            "is_read":{"bsonType":"bool"},
            "created_at":{"bsonType":["date","string"]}
        }
    }
    notifications = _create_or_update_collection("notifications", notifications_validator)
    notifications.create_index([("user_id", ASCENDING), ("is_read", ASCENDING)], name="ix_notifications_user_read")

    # ───────────────────────── crawl_sources ─────────────────────────
    crawl_sources_validator = {
        "bsonType":"object",
        "required":["name_source","rss_url","is_active","created_at", "site"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "name_source":{"bsonType":"string"},
            "base_url":{"bsonType":"string"},
            "rss_url":{"bsonType":"string"},
            "is_active":{"bsonType":"bool"},
            "created_at":{"bsonType":["date","string"]},
            "site":{"bsonType":"string"}
        }
    }
    crawl_sources = _create_or_update_collection("crawl_sources", crawl_sources_validator)
    crawl_sources.create_index(
        [("name_source", ASCENDING)],
        name="ux_crawl_sources_name_source",
        unique=True)

    # ───────────────────────── crawl_runs ─────────────────────────
    # SQL: status crawl_status (enum)
    crawl_runs_validator = {
        "bsonType":"object",
        "required":["source_id","status"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "source_id":{"bsonType":"objectId"},  # -> crawl_sources._id
            "status":{"enum":["pending","running","success","failed"]},  # tuỳ bạn định nghĩa
            "started_at":{"bsonType":["date","null","string"]},
            "finished_at":{"bsonType":["date","null","string"]},
            "stats_pages":{"bsonType":["int","null"]},
            "stats_errors":{"bsonType":["int","null"]},
        }
    }
    crawl_runs = _create_or_update_collection("crawl_runs", crawl_runs_validator)
    crawl_runs.create_index([("source_id", ASCENDING), ("started_at", DESCENDING)], name="ix_crawl_runs_source_time")

    # ───────────────────────── raw_pages ─────────────────────────
    raw_pages_validator = {
        "bsonType":"object",
        "required":["url","source_id","crawl_run_id"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "url":{"bsonType":"string"},
            "source_id":{"bsonType":"objectId"},     # -> crawl_sources._id
            "crawl_run_id":{"bsonType":"objectId"},  # -> crawl_runs._id
            "crawler_time":{"bsonType":["date","null","string"]},
            "http_status":{"bsonType":["int","null"]},
            "content_type":{"bsonType":["string","null"]},
            "raw_html":{"bsonType":["string","null"]},
            "content_hash":{"bsonType":["string","null"]}
        }
    }
    raw_pages = _create_or_update_collection("raw_pages", raw_pages_validator)
    # raw_pages.create_index(
    #     [("source_id", ASCENDING), ("crawl_run_id", ASCENDING)],
    #     name="ux_raw_pages_source_run",
    #     unique=True
    # )
    raw_pages.create_index(
    [("source_id", ASCENDING), ("url", ASCENDING)],
    name="ux_raw_pages_source_url",
    unique=True
    )

    # ───────────────────────── extracted_articles ─────────────────────────
    extracted_articles_validator = {
        "bsonType":"object",
        "required":["raw_page_id","source_id","source_url","images","created_at"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "raw_page_id":{"bsonType":"objectId"},  # -> raw_pages._id
            "source_id":{"bsonType":"objectId"},    # -> crawl_sources._id
            "source_url":{"bsonType":"string"},
            "canonical_title":{"bsonType":["string","null"]},
            "author":{"bsonType":["string","null"]},
            "published_at":{"bsonType":["date","null","string"]},
            "summary":{"bsonType":["string","null"]},
            "body_text":{"bsonType":["string","null"]},
            "body_html":{"bsonType":["string","null"]},
            "images":{"bsonType":"array", "items":{"bsonType":"string"}},
            "topics_raw":{"bsonType":["array","null"], "items":{"bsonType":"string"}},
            "copyright_note":{"bsonType":["string","null"]},
            "content_hash":{"bsonType":["string","null"]},
            "created_at":{"bsonType":["date","string"]}
        }
    }
    extracted_articles = _create_or_update_collection("extracted_articles", extracted_articles_validator)
    extracted_articles.create_index(
        [("source_id", ASCENDING), ("source_url", ASCENDING)],
        name="ux_extracted_articles_src_url",
        unique=True
    )

    # ───────────────────────── import_mappings ─────────────────────────
    import_mappings_validator = {
        "bsonType":"object",
        "required":["article_id","extracted_id"],
        "properties":{
            "_id":{"bsonType":"objectId"},
            "article_id":{"bsonType":"objectId"},    # -> articles._id
            "extracted_id":{"bsonType":"objectId"},  # -> extracted_articles._id
            "imported_at":{"bsonType":["date","null","string"]}
        }
    }
    import_mappings = _create_or_update_collection("import_mappings", import_mappings_validator)
    import_mappings.create_index([("article_id", ASCENDING)], name="ix_import_mappings_article")
    import_mappings.create_index([("extracted_id", ASCENDING)], name="ix_import_mappings_extracted")
