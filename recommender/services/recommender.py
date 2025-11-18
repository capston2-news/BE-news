# recommender/services/recommender.py
from typing import List
from django.conf import settings
from .embeddings import load_embedder, encode_texts
from .chroma_store import get_client, get_articles_collection, query_topk
from .utils import user_recent_article_ids, fetch_articles, article_to_text, get_popular_recent_ids
from .rrf import rrf_fuse

def _rank_from_chroma_result(res):
    # res["ids"] là List[List[str]] cho từng query_embedding
    ranks = []
    for ids in res["ids"]:
        ranks.append(ids)  # đã là theo thứ tự similarity giảm dần
    return ranks

def recommend_by_rrf(db, user_id: str, topk: int = 20):
    # 1) Load embedder + Chroma
    embedder = load_embedder(settings.SENTENCE_MODEL)
    client = get_client(settings.CHROMA_DIR)
    coll = get_articles_collection(client)

    # 2) Lấy các hạt giống (seeds) từ hành vi người dùng
    seed_ids = user_recent_article_ids(db, user_id, days=30, limit=5)
    seeds = fetch_articles(db, seed_ids)

    queries = []

    # a) Hồ sơ người dùng = mean text các bài đã xem (nếu có)
    if seeds:
        user_profile_text = " \n\n".join(article_to_text(d) for d in seeds if d)
        queries.append(user_profile_text)

    # b) Query theo bài gần nhất (tăng đa dạng)
    if seeds:
        latest = seeds[0]
        if latest:
            queries.append(article_to_text(latest))

    # c) Query theo “popular recent centroid” (nếu user mới)
    if not queries:
        pop_ids = get_popular_recent_ids(db, days=7, limit=10)
        pop_docs = fetch_articles(db, pop_ids)
        if pop_docs:
            queries.append(" \n\n".join(article_to_text(d) for d in pop_docs if d))

    # d) Nếu vẫn rỗng (hệ khởi tạo), dùng 1 query dummy
    if not queries:
        queries.append("latest trending news technology sports politics finance entertainment")

    # 3) Embed các query và truy vấn Chroma (mỗi query là một “nguồn xếp hạng”)
    Q = encode_texts(embedder, queries)
    res = coll.query(query_embeddings=Q, n_results=max(100, topk*5))
    # res: {"ids": [[...], [...]], "embeddings": None, "distances": [[...]] ...}

    # 4) Lấy thứ hạng từng nguồn và RRF fuse
    rank_lists = _rank_from_chroma_result(res)
    fused_ids = rrf_fuse(rank_lists, k=60)

    # 5) Trả về topk + metadata từ Mongo
    docs = fetch_articles(db, fused_ids[:topk])
    # Giữ đúng thứ tự theo fused_ids
    by = {str(d["_id"]): d for d in docs if d}
    ordered = [by[i] for i in fused_ids[:topk] if i in by]
    return ordered
