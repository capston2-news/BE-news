# recommender/apps.py
from django.apps import AppConfig
from django.conf import settings
import threading

class RecommenderConfig(AppConfig):
    name = "recommender"
    model = None
    lock = threading.Lock()

    def ready(self):
        from .services.embeddings import load_embedder
        with self.lock:
            if self.model is None:
                self.model = load_embedder(settings.SENTENCE_MODEL)
