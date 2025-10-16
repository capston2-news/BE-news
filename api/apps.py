from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        # Tạo admin mặc định nếu chưa có
        try:
            from .mongo_schema import ensure_collections_and_indexes
            from .startup import ensure_default_admin
            ensure_collections_and_indexes()  # <— tạo collections + validator + indexes
            ensure_default_admin()            # <— seed admin mặc định
        except Exception as e:
            print(f"[auth] ensure_default_admin() error: {e}")
