# from chromadb import PersistentClient
# from chromadb.utils import embedding_functions
#
# def get_client(path: str):
#     return PersistentClient(path=path)
#
# def get_articles_collection(client):
#     # name cố định: "articles"
#     try:
#         return client.get_collection("articles")
#     except Exception:
#         return client.create_collection("articles")
#
# def upsert_articles(collection, ids, embeddings, metadatas, documents):
#     # Chroma yêu cầu: len(ids) == len(embeddings) == len(metadatas) == len(documents)
#     collection.upsert(
#         ids=ids,
#         embeddings=embeddings,
#         metadatas=metadatas,
#         documents=documents,
#     )


# recommender/services/chroma_store.py
from typing import Optional, List, Dict, Any
import chromadb


def get_client(persist_dir: str):
    """
    Persistent client lưu tại CHROMA_DIR
    """
    return chromadb.PersistentClient(path=persist_dir)


def get_articles_collection(client, name: str = "articles"):
    """
    Collection lưu embeddings bài viết.
    """
    try:
        return client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )
    except TypeError:
        # fallback cho version cũ
        return client.get_or_create_collection(name=name)


def upsert_articles(collection, ids: List[str], embeddings: List[list], metadatas: List[dict], documents: List[str]):
    """
    Chuẩn Chroma: len(ids) == len(embeddings) == len(metadatas) == len(documents)
    """
    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        metadatas=metadatas,
        documents=documents,
    )