# chatbot/gemini_client.py
import os
import json
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
# 👉 DÙNG LẠI gemini-2.5-flash (model này project em đang có)
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in .env")

client = genai.Client(api_key=API_KEY)

BOT_NAME = "Nana – Trợ lý Tin Tức"


def chat_generic(message: str, history=None) -> str:
    """
    Gọi Gemini cho chatbot, trả về RAW TEXT (JSON string) theo schema:

    {
      "action": "CHAT" | "GET_ARTICLES" | "SUMMARIZE_ARTICLE",
      "reply": "<câu trả lời cho user>",
      "category_slug": "<slug hoặc null>",
      "filters": {
        "time_range": "today" | "last_7_days" | null,
        "region": "vietnam" | "world" | null,
        "locations": ["<slug>", ...],
        "keywords": ["<keyword>", ...]
      }
    }

    Backend sẽ json.loads(...) ở ChatbotView.
    """
    contents = []

    system_prompt = f"""
You are {BOT_NAME}, a friendly multilingual news assistant.

- You can understand both Vietnamese and English.
- If the user writes in Vietnamese, respond in natural Vietnamese.
- If the user writes in English, respond in natural English.
- If the user mixes languages, choose the main language of the question
  or match the language they seem to prefer.

VERY IMPORTANT:
Always respond in pure JSON (no markdown, no extra text, no explanation)
with exactly this schema:

{{
  "action": "CHAT" | "GET_ARTICLES" | "SUMMARIZE_ARTICLE",
  "reply": "<your natural language answer for the user>",
  "category_slug": "<slug or null>",
  "filters": {{
    "time_range": "today" | "last_7_days" | null,
    "region": "vietnam" | "world" | null,
    "locations": ["<slug>", ...],
    "keywords": ["<keyword>", ...]
  }}
}}

Semantics of filters:
- time_range:
    "today"       -> user asks for today's news / latest today
    "last_7_days" -> user asks for recent news this week, last few days
    null          -> no explicit time filter
- region:
    "vietnam" -> news in Vietnam / domestic news / 'tin trong nước'
    "world"   -> international news / 'tin quốc tế'
    null      -> no explicit region filter
- locations:
    list of city/province slugs if user mentions one or more places.
    Examples:
      ["da-nang"]
      ["da-nang", "sai-gon"]
      ["ha-noi"]
- keywords:
    list of main entities or topics the user is asking about
    (persons, teams, objects, etc.), in lowercase.
    Examples:
      ["messi"]
      ["ronaldo"]
      ["messi", "ronaldo"]

Slug rules:
- lowercase
- spaces -> hyphens
- no accents
Examples:
  "Thời sự"   -> "thoi-su"
  "Thể thao"  -> "the-thao"
  "Bóng đá"   -> "bong-da"
  "Đà Nẵng"   -> "da-nang"
  "Sài Gòn"   -> "sai-gon"
  "Hà Nội"    -> "ha-noi"

Rules for GET_ARTICLES:

1) If the user asks to list/show news or articles
   (e.g. in Vietnamese:
       "tin tức nổi bật hôm nay",
       "các tin tức thời sự",
       "tin thời sự nổi bật ở Đà Nẵng và Sài Gòn",
       "tin bóng đá ở Đà Nẵng",
       "các bài viết về messi",
       "tin trong nước hôm nay",
    or in English:
       "today's top news",
       "domestic news",
       "football news in Da Nang",
       "breaking news in Vietnam today",
       "articles about Messi",
   ),
   then:
     - set "action" = "GET_ARTICLES"
     - set "reply" = a short confirmation in the user's language,
     - set "category_slug" when the user clearly specifies a topic/category,
       using slug rules above.
     - set filters best-guess for time_range, region, locations, keywords.

Rules for SUMMARIZE_ARTICLE:

2) If the user asks to summarize the article they are currently reading
   (e.g.:
       "tóm tắt bài viết đang đọc",
       "tóm tắt bài báo này",
       "tóm tắt bài viết này",
       "summarize this article",
       "summarize the article I'm reading",
   ),
   then:
     - set "action"        = "SUMMARIZE_ARTICLE"
     - set "reply"         = a short confirmation in the user's language
     - set "category_slug" = null
     - set "filters"       = {{
         "time_range": null,
         "region": null,
         "locations": [],
         "keywords": []
       }}

3) For all other questions (small talk, explanations, etc.):
     - set "action"        = "CHAT"
     - set "category_slug" = null
     - set "filters"       = {{
         "time_range": null,
         "region": null,
         "locations": [],
         "keywords": []
       }}
     - set "reply"         = your normal answer.

Output MUST be valid JSON ONLY.
"""

    contents.append(
        {
            "role": "user",
            "parts": [{"text": system_prompt}],
        }
    )

    # Thêm history nếu có
    if history:
        for turn in history:
            role = turn.get("role")
            content = turn.get("content", "")
            if not content:
                continue

            if role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role in ("model", "assistant"):
                contents.append({"role": "model", "parts": [{"text": content}]})

    # Thêm message hiện tại
    contents.append(
        {
            "role": "user",
            "parts": [{"text": message}],
        }
    )

    # Gọi Gemini, ép trả về JSON
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            # Với chat bình thường, để thinking mặc định cũng được
        ),
    )

    return (response.text or "").strip()


def summarize_article(content: str, language: str = "vi") -> str:
    """
    Gọi Gemini để tóm tắt nội dung bài viết.

    QUAN TRỌNG: với gemini-2.5-flash, phải tắt 'thinking' nếu không
    model có thể dùng hết token cho thoughts và không trả text.

    - Không ép JSON, chỉ yêu cầu model trả plain text.
    - Lấy text từ response.text, nếu không có thì đọc từ
      candidates[*].content.parts[*].text.
    - Nếu vẫn không có thì trả message fallback (không ném lỗi).
    """
    if not content:
        return "Không có nội dung bài viết để tóm tắt."

    if language == "en":
        instr = (
            "Summarize the following news article in clear, concise English, "
            "in about 8–10 sentences. Focus on the main facts and key points. "
            "Answer with plain text only."
        )
    else:
        instr = (
            "Hãy tóm tắt bài báo tin tức dưới đây bằng tiếng Việt, "
            "khoảng 8–10 câu, dễ hiểu, tập trung vào các ý chính và bối cảnh quan trọng. "
            "Chỉ trả về phần tóm tắt dạng text thuần, không markdown, không JSON."
        )

    contents = [
        {
            "role": "user",
            "parts": [
                {"text": instr},
                {"text": content},
            ],
        }
    ]

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            # ❗ TẮT THINKING để không bị ăn hết token mà không có text
            config=types.GenerateContentConfig(
                max_output_tokens=512,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        # Nếu cần debug thêm:
        # print("DEBUG_SUMMARY_RESPONSE:", response)
    except Exception as e:
        if language == "en":
            return f"Error while summarizing: {e}"
        return f"Lỗi khi tóm tắt bài viết: {e}"

    # 1) Thử lấy trực tiếp response.text
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    # 2) Fallback: đọc từ candidates[*].content.parts[*].text
    candidates = getattr(response, "candidates", None) or []
    collected = []

    for cand in candidates:
        content_obj = getattr(cand, "content", None)
        if not content_obj:
            continue

        parts = getattr(content_obj, "parts", None) or []
        for part in parts:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                collected.append(part_text.strip())

    if collected:
        return "\n".join(collected)

    # 3) Fallback cuối cùng: không có text nào từ model
    if language == "en":
        return "Sorry, I couldn’t generate a summary for this article right now."
    return (
        "Xin lỗi, hiện tại mình chưa thể tóm tắt bài viết này. "
        "Bạn có thể thử lại sau hoặc hỏi mình nội dung khác nhé."
    )
