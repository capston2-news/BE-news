# recommender/services/rrf.py
from collections import defaultdict

def rrf_fuse(rank_lists, k: int = 60):
    """
    rank_lists: List[List[str]]  (mỗi list là danh sách id theo thứ hạng tăng dần)
    score = Σ 1 / (k + rank)
    """
    scores = defaultdict(float)
    for ranks in rank_lists:
        for r, doc_id in enumerate(ranks):
            scores[doc_id] += 1.0 / (k + (r+1))
    # trả về danh sách id đã sắp xếp
    return [doc for doc, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]
