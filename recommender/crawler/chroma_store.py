# from typing import Optional
# import chromadb
#
# def get_client(persist_dir: str):
#     """
#     Persistent client lưu tại CHROMA_DIR
#     """
#     return chromadb.PersistentClient(path=persist_dir)
#
# def get_articles_collection(client, name: str = "articles"):
#     """
#     Collection lưu embeddings bài viết.
#     """
#     try:
#         return client.get_or_create_collection(
#             name=name,
#             metadata={"hnsw:space": "cosine"},
#         )
#     except TypeError:
#         # fallback cho version cũ
#         return client.get_or_create_collection(name=name)
