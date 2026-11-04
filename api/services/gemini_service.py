import asyncio
import json
import os
import re
from typing import Awaitable, Callable

import httpx
from dotenv import load_dotenv

load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
_key_cursor = 0
# Các semaphore phải dùng chung ở cấp process. Nếu tạo bên trong từng job thì
# tổng số request vẫn tăng theo số người dùng và nhanh chóng chạm rate-limit.
_SUBTITLE_TRANSLATION_SEMAPHORE = asyncio.Semaphore(
    max(1, int(os.getenv("TRANSLATION_GLOBAL_PARALLELISM", "2")))
)
_GOOGLE_TRANSLATE_SEMAPHORE = asyncio.Semaphore(
    max(1, int(os.getenv("GOOGLE_TRANSLATE_GLOBAL_PARALLELISM", "3")))
)


def _keys() -> list[str]:
    raw = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
    return [key.strip() for key in raw.split(",") if key.strip()]


def is_configured() -> bool:
    return bool(_keys())


def _rotated_keys() -> list[str]:
    """Mỗi request bắt đầu bằng key kế tiếp, sau đó thử các key còn lại."""
    global _key_cursor
    keys = _keys()
    if not keys:
        return []
    start = _key_cursor % len(keys)
    _key_cursor = (_key_cursor + 1) % len(keys)
    return keys[start:] + keys[:start]


def _parse_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    return json.loads(cleaned)


async def _translate_with_google_fallback_unlimited(lines: list[str]) -> list[str]:
    """Dịch dự phòng tuần tự để tránh Google Translate chặn do quá nhiều request."""
    endpoints = (
        "https://translate.googleapis.com/translate_a/single",
        "https://translate.google.com/translate_a/single",
    )
    max_attempts = max(2, int(os.getenv("GOOGLE_TRANSLATE_MAX_RETRIES", "5")))
    request_delay = max(0.0, float(os.getenv("GOOGLE_TRANSLATE_REQUEST_DELAY", "0.2")))
    translated: list[str] = []
    async with httpx.AsyncClient(
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 MediaCrawler/1.0"},
    ) as client:
        for line_number, line in enumerate(lines, 1):
            errors: list[str] = []
            for attempt in range(max_attempts):
                endpoint = endpoints[attempt % len(endpoints)]
                try:
                    async with _GOOGLE_TRANSLATE_SEMAPHORE:
                        response = await client.get(
                            endpoint,
                            params={
                                "client": "gtx",
                                # Whisper may keep short Japanese/Korean phrases
                                # inside otherwise Chinese videos.
                                "sl": "auto",
                                "tl": "vi",
                                "dt": "t",
                                "q": line,
                            },
                        )
                    response.raise_for_status()
                    payload = response.json()
                    value = "".join(part[0] for part in payload[0] if part and part[0]).strip()
                    if not value:
                        raise ValueError("Google Translate trả câu rỗng")
                    translated.append(value)
                    break
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    errors.append(f"HTTP {status}")
                    retry_after = exc.response.headers.get("Retry-After", "")
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = min(10.0, 1.0 * (2 ** attempt))
                except (httpx.HTTPError, ValueError, TypeError, IndexError, json.JSONDecodeError) as exc:
                    errors.append(type(exc).__name__)
                    delay = min(10.0, 1.0 * (2 ** attempt))

                if attempt + 1 < max_attempts:
                    await asyncio.sleep(delay)
            else:
                raise RuntimeError(
                    f"Google Translate thất bại ở câu {line_number}/{len(lines)} "
                    f"sau {max_attempts} lần thử: {', '.join(errors)}"
                )

            if request_delay and line_number < len(lines):
                await asyncio.sleep(request_delay)
    return translated


async def _translate_with_google_fallback(lines: list[str]) -> list[str]:
    """Giới hạn request Google toàn server nhưng cho các video dịch xen kẽ."""
    return await _translate_with_google_fallback_unlimited(lines)


async def expand_vietnamese_keywords(keywords: list[str]) -> list[str]:
    """Hiểu ý định tiếng Việt và tạo cụm tìm kiếm tự nhiên cho nền tảng Trung Quốc."""
    keys = _rotated_keys()
    if not keys:
        raise RuntimeError("Gemini chưa được cấu hình")

    prompt = f"""Bạn là chuyên gia tìm kiếm video trên Douyin, Xiaohongshu và Bilibili.
Người dùng Việt Nam muốn tìm: {json.dumps(keywords, ensure_ascii=False)}
Hãy chuyển ý định sang tiếng Trung giản thể tự nhiên, dùng từ mà người Trung Quốc thực sự tìm kiếm.
Với mỗi ý định, tạo tối đa 3 cụm ngắn gồm từ khóa chính, cách nói phổ biến hoặc hashtag liên quan.
Không thêm ý nghĩa ngoài yêu cầu. Chỉ trả JSON đúng dạng:
{{"keywords":["关键词1","关键词2"]}}
"""
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for key in keys:
            try:
                response = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
                    },
                )
                response.raise_for_status()
                payload = response.json()
                text = payload["candidates"][0]["content"]["parts"][0]["text"]
                values = _parse_json(text).get("keywords", [])
                result = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
                if result:
                    return result[: max(3, len(keywords) * 3)]
                errors.append("Gemini trả danh sách rỗng")
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                errors.append(type(exc).__name__)
                continue
    raise RuntimeError(f"Tất cả Gemini API key đều thất bại: {', '.join(errors)}")


async def _translate_chinese_subtitles_unlimited(
    lines: list[str],
    on_status: Callable[[str], Awaitable[None]] | None = None,
    durations_ms: list[int] | None = None,
    style: str = "faithful",
) -> list[str]:
    """Dịch phụ đề theo ánh xạ 1-1, ưu tiên câu Việt ngắn và dễ lồng tiếng."""
    keys = _rotated_keys()
    if not keys:
        raise RuntimeError("Gemini chưa được cấu hình; hãy thêm GEMINI_API_KEYS vào .env")
    if durations_ms is None or len(durations_ms) != len(lines):
        durations_ms = [0] * len(lines)
    strict_one_to_one = style == "faithful"
    entries = [
        {
            "id": index,
            "text": line,
            "duration_ms": duration,
            # Giọng Việt tự nhiên trong video ngắn thường dễ nghe ở khoảng
            # 3 từ/giây. Giữ câu thoáng hơn để TTS không phải đọc gấp.
            "max_vietnamese_words": max(
                2,
                round(duration / (330 if strict_one_to_one else 280)),
            ) if duration else None,
        }
        for index, (line, duration) in enumerate(zip(lines, durations_ms), 1)
    ]
    style_instruction = {
        "faithful": (
            "Chế độ SÁT NGHĨA 1-1: mỗi phần tử đầu vào phải tạo đúng một phần tử đầu ra. "
            "Giữ nguyên đầy đủ sự kiện, con số, tên riêng, quan hệ nguyên nhân-kết quả, mức độ "
            "khẳng định và sắc thái của chính câu đó. Không gộp câu, không tách câu, không chuyển "
            "ý sang câu bên cạnh, không thêm giải thích, không suy diễn và không lược bỏ ý. Chỉ "
            "rút gọn cách diễn đạt tiếng Việt bằng từ đồng nghĩa ngắn hơn để vừa thời lượng."
        ),
        "conversational": (
            "Phong cách: lời nói đời thường, tự nhiên, gọn và dễ nghe. "
            "Dùng cách xưng hô trung tính, tránh văn viết."
        ),
        "narration": (
            "Phong cách: thuyết minh mượt, rõ nghĩa, câu văn tròn hơn nhưng vẫn ngắn. "
            "Phù hợp video kiến thức, kể chuyện, giải thích."
        ),
        "review": (
            "Phong cách: review/video mạng xã hội, có năng lượng vừa phải, câu nói bắt tai "
            "nhưng không lố, không thêm cảm xúc ngoài nội dung gốc."
        ),
    }.get(style, "Phong cách: lời nói đời thường, tự nhiên, gọn và dễ nghe.")
    recognition_instruction = (
        "Không tự sửa, bổ sung hoặc đoán phần lời nguồn; dịch đúng nội dung text được cung cấp."
        if strict_one_to_one else
        "Có thể sửa hợp lý lỗi nhận dạng nhỏ nếu ngữ cảnh cho thấy rõ, nhưng không bịa thông tin."
    )
    prompt = f"""Bạn là biên dịch viên và biên tập lời thoại lồng tiếng chuyên nghiệp.
Hãy đọc toàn bộ nhóm câu để hiểu ngữ cảnh rồi chuyển từng câu sang tiếng Việt tự nhiên,
trôi chảy như người Việt đang nói ngoài đời; không dịch máy từng chữ. Ưu tiên câu
ngắn, thân mật, có nhịp nghỉ bằng dấu phẩy/dấu chấm khi cần để giọng đọc không bị
liền tù tì. Giữ nhất quán chủ ngữ, đại từ, tên riêng và thuật ngữ giữa các câu.
{recognition_instruction}
{style_instruction}

Mỗi câu phải súc tích và không vượt quá max_vietnamese_words, ưu tiên diễn đạt lại
ngắn hơn thay vì làm giọng đọc chạy nhanh, nhưng không được cụt ý hoặc nghe như
dịch từng mảnh. Tránh văn viết cứng như "do đó", "tuy nhiên" nếu có thể nói tự
nhiên hơn. Chỉ dùng chữ tiếng Việt/Latin, chữ số và dấu câu
thông thường. Với đầu vào chỉ là tiếng đệm thì dùng tiếng đệm Việt tự nhiên; với đầu
vào vô nghĩa hoặc chỉ có ký hiệu thì trả một dấu chấm để giữ khoảng lặng.

Giữ đúng số phần tử và đúng thứ tự. Chỉ trả JSON dạng
{{"translations":["..."]}}.
Đầu vào: {json.dumps(entries, ensure_ascii=False)}
"""
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=90) as client:
        # Free tier thường trả 429 kèm thời gian phải chờ. Thử các key một
        # lượt, sau đó chỉ retry một key để không nhân số request khi các key
        # cùng thuộc một Google project/quota.
        for retry_round in range(8):
            retry_after: list[float] = []
            # Bỏ qua key bị thu hồi/403 để tìm key còn hoạt động. Khi gặp
            # 429/503 thì dừng vòng key ở dưới và chờ vì các key thường dùng
            # chung project/quota.
            round_keys = keys if retry_round == 0 else _rotated_keys()
            for key in round_keys:
                try:
                    response = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                        json={
                            "contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
                        },
                    )
                    if response.status_code == 429:
                        message = response.text
                        match = re.search(r"retry in\s+([\d.]+)s", message, re.IGNORECASE)
                        retry_after.append(float(match.group(1)) if match else 60.0)
                        errors.append("429 RESOURCE_EXHAUSTED")
                        break
                    if response.status_code in {500, 502, 503, 504}:
                        retry_after.append(min(60.0, 5.0 * (retry_round + 1)))
                        errors.append(f"HTTP {response.status_code} Gemini tạm quá tải")
                        break
                    response.raise_for_status()
                    payload = response.json()
                    text = payload["candidates"][0]["content"]["parts"][0]["text"]
                    values = _parse_json(text).get("translations", [])
                    result = [str(value).strip() for value in values]
                    if len(result) == len(lines) and all(result):
                        return result
                    errors.append("Gemini trả sai số lượng câu")
                except httpx.HTTPStatusError as exc:
                    errors.append(f"HTTP {exc.response.status_code}: {exc.response.text[:160]}")
                except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    errors.append(type(exc).__name__)
            if retry_after:
                # 429 thường là quota theo Google project, chờ nhiều vòng vẫn
                # không giải quyết được. Chuyển ngay sang phương án dự phòng.
                break
            break
    if on_status:
        await on_status("Gemini không khả dụng; chuyển sang Google Translate dự phòng.")
    try:
        return await _translate_with_google_fallback(lines)
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError, IndexError) as exc:
        raise RuntimeError(
            f"Không thể dịch phụ đề bằng Gemini ({', '.join(errors)}) "
            f"hoặc Google Translate ({exc})"
        ) from exc


async def translate_chinese_subtitles(
    lines: list[str],
    on_status: Callable[[str], Awaitable[None]] | None = None,
    durations_ms: list[int] | None = None,
    style: str = "faithful",
) -> list[str]:
    """Dịch phụ đề qua hàng đợi chung để nhiều job không dội request vào API."""
    if _SUBTITLE_TRANSLATION_SEMAPHORE.locked() and on_status:
        await on_status("Đang chờ lượt dịch; server đang xử lý một video khác...")
    async with _SUBTITLE_TRANSLATION_SEMAPHORE:
        return await _translate_chinese_subtitles_unlimited(lines, on_status, durations_ms, style)
