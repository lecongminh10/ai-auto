import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..services.gemini_service import expand_vietnamese_keywords, is_configured
from ..services.dubbing_service import dub, transcribe

router = APIRouter(prefix="/videos", tags=["videos"])

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
VIDEO_DIR = PROJECT_ROOT / "video"
_DUB_JOBS: dict[str, dict] = {}


def _number(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _read_records(platform: str) -> list[dict]:
    platform_dir = DATA_DIR / ({"dy": "douyin", "bili": "bilibili"}.get(platform, platform))
    records: list[dict] = []
    if not platform_dir.exists():
        return records
    files = sorted(
        [p for p in platform_dir.rglob("search_contents_*") if p.suffix in {".json", ".jsonl"}],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in files[:3]:
        try:
            if path.suffix == ".jsonl":
                records.extend(json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip())
            else:
                value = json.loads(path.read_text("utf-8"))
                records.extend(value if isinstance(value, list) else [value])
        except (OSError, json.JSONDecodeError):
            continue
    return records


def _normalize(platform: str, item: dict) -> dict:
    video_id = str(item.get("aweme_id") or item.get("note_id") or item.get("video_id") or "")
    if platform == "dy":
        preview_url = item.get("video_download_url") or ""
    elif platform == "xhs":
        preview_url = item.get("video_url") or ""
    else:
        # Bilibili metadata currently contains the public page URL, not a
        # direct media stream. Do not treat HTML as an MP4 download.
        preview_url = ""
    if isinstance(preview_url, str) and "," in preview_url:
        preview_url = preview_url.split(",", 1)[0]
    return {
        "id": video_id,
        "platform": platform,
        "title": item.get("title") or item.get("desc") or "Video không có tiêu đề",
        "author": item.get("nickname") or "",
        "source_keyword": item.get("source_keyword") or "",
        "source_url": item.get("aweme_url") or item.get("note_url") or item.get("video_url") or "",
        "preview_url": preview_url if str(preview_url).startswith("http") else "",
        "cover_url": item.get("cover_url") or item.get("video_cover_url") or (
            (item.get("image_list") or "").split(",")[0]
        ),
        "views": _number(item.get("video_play_count") or item.get("view_count") or item.get("play_count")),
        "likes": _number(item.get("liked_count")),
        "comments": _number(item.get("comment_count") or item.get("video_comment")),
        "shares": _number(item.get("share_count") or item.get("video_share_count")),
        "collections": _number(item.get("collected_count") or item.get("video_favorite_count")),
    }


def _find_video(platform: str, video_id: str) -> dict | None:
    for raw in _read_records(platform):
        video = _normalize(platform, raw)
        if video["id"] == video_id:
            return video
    return None


@router.get("/stream")
async def stream_video(request: Request, platform: Literal["dy", "xhs"], video_id: str):
    """Proxy luồng video để trình duyệt phát được Range request và redirect CDN."""
    video = _find_video(platform, video_id)
    if not video or not video["preview_url"]:
        raise HTTPException(404, "Không tìm thấy URL preview của video")

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/149 Safari/537.36",
        "Referer": "https://www.douyin.com/" if platform == "dy" else "https://www.xiaohongshu.com/",
    }
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=120))
    try:
        upstream = await client.send(client.build_request("GET", video["preview_url"], headers=headers), stream=True)
        upstream.raise_for_status()
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(502, f"Không thể mở preview video: {exc}") from exc

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300"}
    for name in ("content-length", "content-range"):
        if upstream.headers.get(name):
            response_headers[name.title()] = upstream.headers[name]
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )


@router.get("")
async def list_videos(
    platform: Literal["dy", "xhs", "bili"],
    keyword: str = "",
    min_views: int = Query(0, ge=0),
    min_likes: int = Query(0, ge=0),
    min_comments: int = Query(0, ge=0),
    min_shares: int = Query(0, ge=0),
    sort_by: Literal["views", "likes", "comments", "shares", "collections"] = "views",
):
    seen: set[str] = set()
    videos = []
    for raw in _read_records(platform):
        video = _normalize(platform, raw)
        if not video["id"] or video["id"] in seen:
            continue
        seen.add(video["id"])
        haystack = f'{video["title"]} {video["source_keyword"]}'.lower()
        if keyword and keyword.lower() not in haystack:
            continue
        # Kết quả tìm kiếm Douyin/Xiaohongshu thường không cung cấp lượt xem.
        # Không loại hết video chỉ vì một chỉ số không tồn tại trên nguồn đó.
        if platform == "bili" and video["views"] < min_views:
            continue
        if video["likes"] < min_likes:
            continue
        if video["comments"] < min_comments or video["shares"] < min_shares:
            continue
        videos.append(video)
    videos.sort(key=lambda value: value[sort_by], reverse=True)
    return {"videos": videos, "total": len(videos)}


class DownloadRequest(BaseModel):
    platform: Literal["dy", "xhs", "bili"]
    video_id: str
    url: str


class TranslateRequest(BaseModel):
    keywords: str


class DubbingPrepareRequest(DownloadRequest):
    pass


class DubbingRenderRequest(BaseModel):
    video_id: str
    transcript_srt: str
    voice: Literal["vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"] = "vi-VN-HoaiMyNeural"


@router.post("/translate-keywords")
async def translate_keywords(request: TranslateRequest):
    """Dịch danh sách từ khóa tiếng Việt sang tiếng Trung giản thể."""
    source_keywords = [value.strip() for value in request.keywords.split(",") if value.strip()]
    if not source_keywords:
        raise HTTPException(400, "Từ khóa không được để trống")

    if is_configured():
        try:
            translated = await expand_vietnamese_keywords(source_keywords)
            return {
                "original": source_keywords,
                "translated": translated,
                "crawler_keywords": ",".join(translated),
                "provider": "gemini",
            }
        except RuntimeError:
            # Dịch máy là phương án dự phòng để crawler vẫn hoạt động khi
            # Gemini hết quota hoặc toàn bộ key tạm thời không khả dụng.
            pass

    translated: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            for keyword in source_keywords:
                response = await client.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={"client": "gtx", "sl": "vi", "tl": "zh-CN", "dt": "t", "q": keyword},
                )
                response.raise_for_status()
                payload = response.json()
                value = "".join(part[0] for part in payload[0] if part and part[0]).strip()
                translated.append(value or keyword)
    except (httpx.HTTPError, ValueError, TypeError, IndexError) as exc:
        raise HTTPException(502, "Không thể dịch từ khóa sang tiếng Trung") from exc

    return {
        "original": source_keywords,
        "translated": translated,
        "crawler_keywords": ",".join(translated),
        "provider": "google-translate-fallback",
    }


@router.post("/download")
async def download_video(request: DownloadRequest):
    parsed = urlparse(request.url)
    allowed_hosts = {
        "dy": ("douyin.com", "douyinstatic.com"),
        "xhs": ("xhscdn.com",),
        "bili": ("bilivideo.com", "bilibili.com"),
    }
    if parsed.scheme not in {"http", "https"} or not any(parsed.hostname and parsed.hostname.endswith(host) for host in allowed_hosts[request.platform]):
        raise HTTPException(400, "URL video không hợp lệ")
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.video_id)
    target_dir = VIDEO_DIR / request.platform / safe_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "video.mp4"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            async with client.stream("GET", request.url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                response.raise_for_status()
                with target.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        output.write(chunk)
    except httpx.HTTPError as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(502, f"Không thể tải video: {exc}") from exc
    return {"success": True, "path": str(target.relative_to(PROJECT_ROOT)), "size": target.stat().st_size}


@router.post("/dub/prepare")
async def prepare_dubbing(request: DubbingPrepareRequest):
    """Tạo job nền để tải video và nhận diện transcript."""
    if request.platform != "dy":
        raise HTTPException(400, "Hiện pipeline lồng tiếng chỉ hỗ trợ video Douyin")
    job_id = uuid.uuid4().hex
    _DUB_JOBS[job_id] = {"job_id": job_id, "video_id": request.video_id, "status": "running", "stage": "Đang chuẩn bị...", "progress": 0, "transcript_srt": "", "error": ""}
    asyncio.create_task(_prepare_dubbing_job(job_id, request))
    return {"job_id": job_id, "video_id": request.video_id}


async def _prepare_dubbing_job(job_id: str, request: DubbingPrepareRequest):
    job = _DUB_JOBS[job_id]
    try:
        job.update(stage="Đang tải video gốc...", progress=15)
        await download_video(request)
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.video_id)
        work_dir = VIDEO_DIR / "dy" / safe_id
        job.update(stage="Đã tải xong. Đang tách audio WAV 16 kHz...", progress=30)
        job.update(stage="Đang chạy Whisper model small nhận diện tiếng Trung (lần đầu có thể tải model)...", progress=40)
        transcript = await transcribe(work_dir / "video.mp4", work_dir)
        job.update(status="done", stage="Đã nhận diện xong transcript.", progress=100, transcript_srt=transcript)
    except (RuntimeError, ValueError, OSError) as exc:
        job.update(status="error", stage="Pipeline dừng do lỗi.", error=str(exc))


@router.get("/dub/status/{job_id}")
async def dubbing_job_status(job_id: str):
    job = _DUB_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Không tìm thấy job lồng tiếng")
    return job


@router.post("/dub/render")
async def render_dubbing(request: DubbingRenderRequest):
    """Dịch transcript đã duyệt, tạo TTS, khớp timeline và ghép video."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.video_id)
    work_dir = VIDEO_DIR / "dy" / safe_id
    video_path = work_dir / "video.mp4"
    if not video_path.exists():
        raise HTTPException(404, "Chưa có video gốc; hãy chạy bước tạo transcript trước")
    try:
        output = await dub(video_path, work_dir, request.transcript_srt, request.voice)
        return {"success": True, "path": str(output.relative_to(PROJECT_ROOT))}
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/dub/output/{video_id}")
async def get_dubbed_video(video_id: str):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", video_id)
    output = VIDEO_DIR / "dy" / safe_id / "output_vi.mp4"
    if not output.exists():
        raise HTTPException(404, "Video lồng tiếng chưa được tạo")
    return FileResponse(output, media_type="video/mp4", filename=f"{safe_id}_vi.mp4")
