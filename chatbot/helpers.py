# chatbot/helpers.py
import json
from django.test import RequestFactory
from api.views import PublicGetArticlesByCategory


def get_articles_by_category_via_view(slug, user=None):
    """
    Gọi trực tiếp class-based view PublicGetArticlesByCategory
    như request GET /api/public/articles/<slug>/

    Trả về: (status_code, data)
      - Nếu 200: data = list article (list of dict)
      - Nếu !=200: data = dict lỗi hoặc string
    """
    factory = RequestFactory()
    fake_request = factory.get(f"/api/articles/category/{slug}/")
    fake_request.user = user

    view_func = PublicGetArticlesByCategory.as_view()
    response = view_func(fake_request, slug=slug)

    # 👇 QUAN TRỌNG: render response trước khi đọc .content
    if hasattr(response, "render") and callable(response.render):
        response = response.render()

    raw = response.content.decode("utf-8")

    try:
        data = json.loads(raw)
    except Exception:
        data = raw

    return response.status_code, data
