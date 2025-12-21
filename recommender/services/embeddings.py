# # cache model để load 1 lần
# from sentence_transformers import SentenceTransformer
# _model_cache = {}
#
# def load_embedder(model_name: str) -> SentenceTransformer:
#     if model_name not in _model_cache:
#         _model_cache[model_name] = SentenceTransformer(model_name)
#     return _model_cache[model_name]
#
# def encode_texts(embedder, texts):
#     emb = embedder.encode(texts, normalize_embeddings=True, batch_size=64, convert_to_numpy=True)
#     return emb


# recommender/services/embeddings.py
from typing import List, Optional, Any
from sentence_transformers import SentenceTransformer

_model_cache = {}


def load_embedder(model_name: Optional[str] = None) -> SentenceTransformer:
    """
    Cache model để load 1 lần.
    """
    name = (model_name or "sentence-transformers/all-MiniLM-L6-v2").strip()
    if name not in _model_cache:
        _model_cache[name] = SentenceTransformer(name)
    return _model_cache[name]


def encode_texts(embedder: SentenceTransformer, texts: List[str], batch_size: int = 64) -> List[list]:
    """
    Trả về list[list[float]] cho Chroma.
    """
    vecs = embedder.encode(
        texts,
        normalize_embeddings=True,
        batch_size=batch_size,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return vecs.tolist() if hasattr(vecs, "tolist") else vecs
