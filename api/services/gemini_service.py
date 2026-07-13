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


async def _translate_with_google_fallback(lines: list[str]) -> list[str]:
    """Dịch dự phòng từng câu khi toàn bộ Gemini key/quota không khả dụng."""
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=30) as client:
        async def translate_line(line: str) -> str:
            async with semaphore:
                response = await client.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={"client": "gtx", "sl": "zh-CN", "tl": "vi", "dt": "t", "q": line},
                )
                response.raise_for_status()
                payload = response.json()
                value = "".join(part[0] for part in payload[0] if part and part[0]).strip()
                if not value:
                    raise RuntimeError("Google Translate trả câu rỗng")
                return value

        return await asyncio.gather(*(translate_line(line) for line in lines))


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


async def translate_chinese_subtitles(
    lines: list[str], on_status: Callable[[str], Awaitable[None]] | None = None
) -> list[str]:
    """Dịch phụ đề theo ánh xạ 1-1, ưu tiên câu Việt ngắn và dễ lồng tiếng."""
    keys = _rotated_keys()
    if not keys:
        raise RuntimeError("Gemini chưa được cấu hình; hãy thêm GEMINI_API_KEYS vào .env")
    prompt = f"""Bạn là biên dịch viên lồng tiếng video ngắn Trung Quốc sang tiếng Việt.
Dịch từng phần tử tiếng Trung dưới đây sang tiếng Việt tự nhiên, đúng văn phong mạng xã hội.
Câu dịch phải súc tích, có độ dài nói gần với bản gốc và không thêm ý.
Giữ đúng số phần tử, đúng thứ tự. Chỉ trả JSON dạng {{"translations":["..."]}}.
Đầu vào: {json.dumps(lines, ensure_ascii=False)}
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
                            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
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
            if retry_after and retry_round < 7:
                delay = min(90.0, max(5.0, max(retry_after) + 1.0))
                if on_status:
                    await on_status(
                        f"Gemini tạm giới hạn/quá tải; chờ {delay:.0f} giây rồi thử lại "
                        f"({retry_round + 1}/8)."
                    )
                await asyncio.sleep(delay)
                continue
            break
    if on_status:
        await on_status("Gemini không khả dụng; chuyển sang Google Translate dự phòng.")
    try:
        return await _translate_with_google_fallback(lines)
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError, IndexError) as exc:
        raise RuntimeError(
            f"Không thể dịch phụ đề bằng Gemini ({', '.join(errors)}) "
            f"hoặc Google Translate ({type(exc).__name__})"
        ) from exc
