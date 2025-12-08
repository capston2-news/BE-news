# chatbot/gemini_client.py
import os
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
# Dùng gemini-2.5-flash làm default
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not API_KEY:
    raise ValueError("GEMINI_API_KEY is missing in .env")

client = genai.Client(api_key=API_KEY)

BOT_NAME = "Nana – Trợ lý Tin Tức"


def chat_generic(message: str, history=None) -> str:
    """
    Gọi Gemini cho chatbot, trả về RAW TEXT (JSON string) với schema:

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

VERY IMPORTANT – OUTPUT FORMAT:
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
    all lowercase.
    Examples:
      ["messi"]
      ["ronaldo"]
      ["messi", "ronaldo"]

Category slug rules:
- lowercase
- no accents
- spaces -> hyphens
  "Thời sự"   -> "thoi-su"
  "Thể thao"  -> "the-thao"
  "Bóng đá"   -> "bong-da"

VERY IMPORTANT – HOW TO USE HISTORY:

- You see the full conversation history (user + assistant messages).
- Use it to understand context (location, time, topic).
- EVERY TIME you output action = "GET_ARTICLES", you must output a FULL FILTER SET
  for THIS TURN. Do not rely on backend to remember filters for you.

Examples:
1) If previous turn: "tin tức ở Đà Nẵng hôm nay"
   and current turn: "còn bóng đá thì sao?"
   You may output:
   {{
     "action": "GET_ARTICLES",
     "reply": "...",
     "category_slug": "the-thao" or "bong-da",
     "filters": {{
       "time_range": "today",
       "region": "vietnam",
       "locations": ["da-nang"],
       "keywords": ["bong da"]
     }}
   }}

2) If previous turn: "báo thời sự"
   and current turn: "các bài viết về messi"
   You MUST treat it as a new topic 'messi':
   - Usually set "category_slug": null (unless user clearly says "thời sự về messi")
   - Filters example:
     "filters": {{
       "time_range": null,
       "region": null,
       "locations": [],
       "keywords": ["messi"]
     }}

Rules for actions:

1) GET_ARTICLES:
   If the user asks to list/show news/articles, like:
     - "tin tức nổi bật hôm nay"
     - "các tin tức thời sự"
     - "tin thời sự nổi bật ở Đà Nẵng và Sài Gòn"
     - "tin bóng đá ở Đà Nẵng"
     - "các bài viết về messi"
     - "news about Messi"
     - "breaking news in Vietnam today"
   then:
     - action = "GET_ARTICLES"
     - reply  = short confirmation in user's language
     - category_slug = best guess if user clearly indicates a topic/category
     - filters = FULL FILTER SET for this turn only.

2) SUMMARIZE_ARTICLE:
   If the user asks to summarize the article they are currently reading:
     - "tóm tắt bài viết đang đọc"
     - "tóm tắt bài báo này"
     - "summarize this article"
   then:
     - action        = "SUMMARIZE_ARTICLE"
     - reply         = short confirmation
     - category_slug = null
     - filters       = {{
         "time_range": null,
         "region": null,
         "locations": [],
         "keywords": []
       }}

3) CHAT:
   For all other requests (small talk, explanations, etc.):
     - action        = "CHAT"
     - category_slug = null
     - filters       = {{
         "time_range": null,
         "region": null,
         "locations": [],
         "keywords": []
       }}
     - reply         = your normal answer for the user.

Output MUST be valid JSON ONLY.
"""

    contents.append(
        {
            "role": "user",
            "parts": [{"text": system_prompt}],
        }
    )

    # thêm history
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

    # message hiện tại
    contents.append(
        {
            "role": "user",
            "parts": [{"text": message}],
        }
    )

    # gọi Gemini, ép JSON
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
            "in about 10–12 sentences. Focus on the main facts and key points. "
            "Answer with plain text only."
        )
    else:
        instr = (
            "Hãy tóm tắt bài báo tin tức dưới đây bằng tiếng Việt, "
            "khoảng 10-12 câu, dễ hiểu, tập trung vào các ý chính và bối cảnh quan trọng. "
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
            config=types.GenerateContentConfig(
                max_output_tokens=512,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        # debug nếu cần:
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

    # 3) Fallback cuối
    if language == "en":
        return "Sorry, I couldn’t generate a summary for this article right now."
    return (
        "Xin lỗi, hiện tại mình chưa thể tóm tắt bài viết này. "
        "Bạn có thể thử lại sau hoặc hỏi mình nội dung khác nhé."
    )
