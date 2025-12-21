# chatbot/gemini_client.py
import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in .env")

client = genai.Client(api_key=API_KEY)

BOT_NAME = "Nana – Trợ lý Tin Tức"


def chat_generic(message: str, history=None) -> str:
    """
    Gọi Gemini cho chatbot, trả về RAW TEXT (JSON string) với schema (KHÔNG có topk/min_focus):

    {
      "action": "CHAT" | "GET_ARTICLES" | "SUMMARIZE_ARTICLE" | "GET_RECOMMENDATIONS",
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

VERY IMPORTANT – OUTPUT FORMAT:
Always respond in pure JSON (no markdown, no extra text, no explanation)
with exactly this schema (DO NOT add extra keys):

{{
  "action": "CHAT" | "GET_ARTICLES" | "SUMMARIZE_ARTICLE" | "GET_RECOMMENDATIONS",
  "reply": "<your natural language answer for the user>",
  "category_slug": "<slug or null>",
  "filters": {{
    "time_range": "today" | "last_7_days" | null,
    "region": "vietnam" | "world" | null,
    "locations": ["<slug>", ...],
    "keywords": ["<keyword>", ...]
  }}
}}

Filter semantics:
- time_range:
    "today"       -> user asks for today's news / latest today
    "last_7_days" -> user asks for recent news this week, last few days
    null          -> no time filter
- region:
    "vietnam" -> domestic news / 'tin trong nước'
    "world"   -> international news / 'tin quốc tế'
    null      -> no region filter
- locations: city/province slugs:
    - lowercase
    - no accents
    - spaces -> hyphens
    Examples:
      "Đà Nẵng" -> "da-nang"
      "Sài Gòn" -> "sai-gon"
      "Hà Nội"  -> "ha-noi"
- keywords:
    main entities/topics in user's request (persons, teams, objects...)
    all lowercase, keep them short.
    Examples:
      ["messi"]
      ["ronaldo"]
      ["ai", "chip"]

Category slug rules:
- lowercase
- no accents
- spaces -> hyphens
  "Thời sự"   -> "thoi-su"
  "Thể thao"  -> "the-thao"
  "Bóng đá"   -> "bong-da"

IMPORTANT:
- Do NOT output "topk" or "min_focus".
  Backend will decide those values.

VERY IMPORTANT – HOW TO USE HISTORY:
- You see the full conversation history (user + assistant messages).
- Use it to understand context (location, time, topic).
- EVERY TIME you output action = "GET_ARTICLES" or "GET_RECOMMENDATIONS",
  you must output a FULL FILTER SET for THIS TURN.
  Do not rely on backend to remember filters for you.

Examples:
1) Previous turn: "tin tức ở Đà Nẵng hôm nay"
   Current turn: "còn bóng đá thì sao?"
   You may output:
   {{
     "action": "GET_ARTICLES",
     "reply": "Mình tìm giúp bạn tin bóng đá ở Đà Nẵng hôm nay nhé.",
     "category_slug": "bong-da",
     "filters": {{
       "time_range": "today",
       "region": "vietnam",
       "locations": ["da-nang"],
       "keywords": ["bong da"]
     }}
   }}

2) Previous turn: "báo thời sự"
   Current turn: "các bài viết về messi"
   Treat it as new topic 'messi':
   {{
     "action": "GET_ARTICLES",
     "reply": "Mình tìm các bài liên quan đến Messi cho bạn nhé.",
     "category_slug": null,
     "filters": {{
       "time_range": null,
       "region": null,
       "locations": [],
       "keywords": ["messi"]
     }}
   }}

Rules for actions:

1) GET_ARTICLES:
Use when the user asks for news by time/location/topic,
BUT does NOT explicitly ask for recommended / highlighted / favourite news.

Examples for GET_ARTICLES:
- "tin tức hôm nay"
- "tin trong nước hôm nay"
- "tin bóng đá hôm nay"
- "news today"
- "latest news in Vietnam today"

2) SUMMARIZE_ARTICLE:
If the user asks to summarize the article they are currently reading:
- "tóm tắt bài viết đang đọc"
- "tóm tắt bài báo này"
- "summarize this article"

Output:
- action = "SUMMARIZE_ARTICLE"
- reply = short confirmation
- category_slug = null
- filters = {{
    "time_range": null,
    "region": null,
    "locations": [],
    "keywords": []
  }}

3) GET_RECOMMENDATIONS:
Use ONLY when the user explicitly wants recommended / highlighted / favourite news.

Vietnamese triggers:
- "tin tức nổi bật hôm nay"
- "gợi ý cho mình vài tin tức"
- "tin mình có thể thích"
- "gợi ý thêm vài bài tương tự"

English triggers:
- "recommend some news for me"
- "highlighted news today"
- "news I might like"

Output:
- action = "GET_RECOMMENDATIONS"
- reply  = short confirmation in user's language
- category_slug: set only if user clearly names a category; otherwise null
- filters: fill time_range/region/locations/keywords only if user mentions them; otherwise null/[].

4) CHAT:
For all other requests (small talk, explanations, etc.):
- action = "CHAT"
- category_slug = null
- filters = {{
    "time_range": null,
    "region": null,
    "locations": [],
    "keywords": []
  }}
- reply = your normal answer.

Output MUST be valid JSON ONLY.
"""

    # system instruction (kept as a message for compatibility with your current usage)
    contents.append({"role": "user", "parts": [{"text": system_prompt}]})

    # history
    if history:
        for turn in history:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                contents.append({"role": "user", "parts": [{"text": content}]})
            elif role in ("model", "assistant"):
                contents.append({"role": "model", "parts": [{"text": content}]})

    # current message
    contents.append({"role": "user", "parts": [{"text": message}]})

    # call Gemini, force JSON
    response = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    return (response.text or "").strip()


def summarize_article(content: str, language: str = "vi") -> str:
    """
    Tóm tắt nội dung bài viết bằng Gemini.

    - TẮT 'thinking' để tránh tốn token không có text.
    - Không ép JSON, chỉ lấy plain text.
    """
    if not content:
        return "Không có nội dung bài viết để tóm tắt."

    if language == "en":
        instr = (
            "Summarize the following news article in clear, concise English, "
            "in about 6-8 sentences. Focus on the main facts and key points. "
            "Answer with plain text only."
        )
    else:
        instr = (
            "Hãy tóm tắt bài báo tin tức dưới đây bằng tiếng Việt, "
            "khoảng 6-8 câu, dễ hiểu, tập trung vào các ý chính và bối cảnh quan trọng. "
            "Chỉ trả về phần tóm tắt dạng text thuần, không markdown, không JSON."
        )

    contents = [
        {
            "role": "user",
            "parts": [{"text": instr}, {"text": content}],
        }
    ]

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                max_output_tokens=512,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
    except Exception as e:
        if language == "en":
            return f"Error while summarizing: {e}"
        return f"Lỗi khi tóm tắt bài viết: {e}"

    # 1) direct response.text
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    # 2) fallback candidates[*].content.parts[*].text
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

    # 3) final fallback
    if language == "en":
        return "Sorry, I couldn’t generate a summary for this article right now."
    return (
        "Xin lỗi, hiện tại mình chưa thể tóm tắt bài viết này. "
        "Bạn có thể thử lại sau hoặc hỏi mình nội dung khác nhé."
    )
