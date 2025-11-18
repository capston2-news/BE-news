# cache model để load 1 lần
from sentence_transformers import SentenceTransformer
_model_cache = {}

def load_embedder(model_name: str) -> SentenceTransformer:
    if model_name not in _model_cache:
        _model_cache[model_name] = SentenceTransformer(model_name)
    return _model_cache[model_name]

def encode_texts(embedder, texts):
    emb = embedder.encode(texts, normalize_embeddings=True, batch_size=64, convert_to_numpy=True)
    return emb
