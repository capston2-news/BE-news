# from typing import List, Union, Optional
#
# _embedder_cache = {}
#
# class _SentenceTransformerWrapper:
#     def __init__(self, model_name: str):
#         from sentence_transformers import SentenceTransformer
#         self.model = SentenceTransformer(model_name)
#
#     def encode(self, texts: List[str]):
#         # return numpy array
#         return self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
#
#
# def load_embedder(model_name: Optional[str] = None):
#     """
#     Trả về object có method encode(list[str]) -> vectors
#     Cache theo model_name.
#     """
#     name = (model_name or "sentence-transformers/all-MiniLM-L6-v2").strip()
#     if name in _embedder_cache:
#         return _embedder_cache[name]
#
#     emb = _SentenceTransformerWrapper(name)
#     _embedder_cache[name] = emb
#     return emb
