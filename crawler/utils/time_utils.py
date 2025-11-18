from datetime import datetime
from zoneinfo import ZoneInfo

# Định nghĩa múi giờ Việt Nam
VIETNAM_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

def now_vn():
    """Trả về thời gian hiện tại theo múi giờ Việt Nam."""
    return datetime.now(VIETNAM_TZ)

def format_vn(dt):
    """Chuyển datetime về dạng chuỗi giờ VN."""
    if dt is None:
        return None
    return dt.astimezone(VIETNAM_TZ).strftime("%Y-%m-%d %H:%M:%S")
