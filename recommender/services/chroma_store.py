from chromadb import PersistentClient
from chromadb.utils import embedding_functions

def get_client(path: str):
    return PersistentClient(path=path)

def get_articles_collection(client):
    # name cố định: "articles"
    try:
        return client.get_collection("articles")
    except Exception:
        return client.create_collection("articles")

def upsert_articles(collection, ids, embeddings, metadatas, documents):
    # Chroma yêu cầu: len(ids) == len(embeddings) == len(metadatas) == len(documents)
    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        metadatas=metadatas,
        documents=documents,
    )
