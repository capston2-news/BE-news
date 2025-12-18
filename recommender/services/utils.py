from bson import ObjectId
from api.db import get_db

# ------- Helpers: lấy tên category & category_child -------
def get_category_name(db, category_id):
    if not category_id:
        return ""
    if not isinstance(category_id, ObjectId):
        try:
            category_id = ObjectId(category_id)
        except Exception:
            return ""
    doc = db["categories"].find_one({"_id": category_id}, {"name": 1})
    return (doc or {}).get("name", "") or ""

def get_category_child_name(db, category_child_id):
    if not category_child_id:
        return ""
    if not isinstance(category_child_id, ObjectId):
        try:
            category_child_id = ObjectId(category_child_id)
        except Exception:
            return ""
    doc = db["category_child"].find_one({"_id": category_child_id}, {"name": 1})
    return (doc or {}).get("name", "") or ""


# ------- Ghép text để embedding (ưu tiên title, rồi category, child, rồi content) -------
def article_to_text(doc: dict) -> str:
    title = (doc.get("title") or "").strip()
    content = (doc.get("content") or "").strip()
    cat_name = (doc.get("category_name") or "").strip()
    child_name = (doc.get("category_child_name") or "").strip()

    parts = []
    if title:
        parts.append(title)
        parts.append(title)        # nhân trọng số cho title
    if child_name:
        parts.append(child_name)   # child cụ thể (ví dụ: "Bóng đá")
    if cat_name:
        parts.append(cat_name)     # category cha (ví dụ: "Thể thao")
    if content:
        parts.append(content)

    return "\n".join(parts).strip()


def get_all_article_ids(db):
    return [str(d["_id"]) for d in db["articles"].find({}, {"_id": 1})]


# ------- Lấy bài theo danh sách id + kèm tên category & child nếu có -------
def fetch_articles(db, id_list):
    ids = [ObjectId(x) if not isinstance(x, ObjectId) else x for x in id_list]

    cursor = db["articles"].find(
        {"_id": {"$in": ids}},
        projection={
            "_id": 1,
            "title": 1,
            "content": 1,
            "source": 1,
            "author": 1,
            "topics": 1,
            "published_at": 1,
            "category_id": 1,
            "category_child_id": 1,
        },
    ).batch_size(200)

    for doc in cursor:
        # tra cứu tên category và category_child (nếu có)
        cat_name = get_category_name(db, doc.get("category_id"))
        child_name = get_category_child_name(db, doc.get("category_child_id"))
        doc["category_name"] = cat_name
        doc["category_child_name"] = child_name
        yield doc
