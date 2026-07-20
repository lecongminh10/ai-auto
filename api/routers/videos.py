import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..services.gemini_service import expand_vietnamese_keywords, is_configured
from ..services.dubbing_service import dub, parse_srt, transcribe
from ..services.media_social_service import handoff
from ..workspace import get_workspace_id, workspace_data_dir, workspace_root, workspace_video_dir

router = APIRouter(prefix="/videos", tags=["videos"])

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DUB_JOBS: dict[str, dict] = {}
_ACTIVE_DUB_RENDERS: dict[str, str] = {}


def _dub_job_path(workspace_id: str, job_id: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        return None
    return workspace_root(workspace_id) / "jobs" / "dubbing" / f"{job_id}.json"


def _persist_dub_job(job: dict) -> None:
    path = _dub_job_path(job.get("workspace_id", "default"), job.get("job_id", ""))
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False), "utf-8")
    temporary.replace(path)


def _store_dub_job(job: dict) -> dict:
    _DUB_JOBS[job["job_id"]] = job
    _persist_dub_job(job)
    return job


def _update_dub_job(job: dict, **values) -> None:
    job.update(values)
    _persist_dub_job(job)


def _load_dub_job(workspace_id: str, job_id: str) -> dict | None:
    path = _dub_job_path(workspace_id, job_id)
    if path is None or not path.is_file():
        return None
    try:
        job = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(job, dict) or job.get("workspace_id", "default") != workspace_id:
        return None
    _DUB_JOBS[job_id] = job
    return job


def _claim_dub_render(work_dir: Path, job_id: str) -> str | None:
    """Đăng ký job hoặc trả job đang xử lý cùng video để client theo dõi lại."""
    key = str(work_dir.resolve())
    existing_job_id = _ACTIVE_DUB_RENDERS.get(key)
    if existing_job_id:
        return existing_job_id
    _ACTIVE_DUB_RENDERS[key] = job_id
    return None


def _release_dub_render(work_dir: Path, job_id: str | None = None) -> None:
    key = str(work_dir.resolve())
    if job_id is None or _ACTIVE_DUB_RENDERS.get(key) == job_id:
        _ACTIVE_DUB_RENDERS.pop(key, None)


def _append_job_log(job: dict, message: str) -> None:
    logs = job.setdefault("logs", [])
    entry = f"{time.strftime('%H:%M:%S')} — {message}"
    if not logs or logs[-1] != entry:
        logs.append(entry)
        del logs[:-100]
        _persist_dub_job(job)


def _number(value) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _cached_transcript(work_dir: Path) -> str | None:
    """Tận dụng transcript hoàn chỉnh nếu UI mất job nhưng Whisper đã chạy xong."""
    video = work_dir / "video.mp4"
    transcript_path = work_dir / "audio.srt"
    if not video.is_file() or not transcript_path.is_file():
        return None
    try:
        transcript = transcript_path.read_text("utf-8")
        parse_srt(transcript)
    except (OSError, UnicodeError, ValueError):
        return None
    return transcript


def _read_records(platform: str, workspace_id: str = "default") -> list[dict]:
    platform_dir = workspace_data_dir(workspace_id) / ({"dy": "douyin", "bili": "bilibili"}.get(platform, platform))
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
        "duration_seconds": _number(item.get("duration_seconds")),
    }


def _find_video(platform: str, video_id: str, workspace_id: str = "default") -> dict | None:
    for raw in _read_records(platform, workspace_id):
        video = _normalize(platform, raw)
        if video["id"] == video_id:
            return video
    return None


@router.get("/stream")
async def stream_video(
    request: Request,
    platform: Literal["dy", "xhs"],
    video_id: str,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    """Proxy luồng video để trình duyệt phát được Range request và redirect CDN."""
    video = _find_video(platform, video_id, workspace_id)
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
    workspace_id: Annotated[str, Depends(get_workspace_id)],
    keyword: str = "",
    min_views: int = Query(0, ge=0),
    min_likes: int = Query(0, ge=0),
    min_comments: int = Query(0, ge=0),
    min_shares: int = Query(0, ge=0),
    min_duration_seconds: int = Query(0, ge=0),
    sort_by: Literal["views", "likes", "comments", "shares", "collections"] = "views",
):
    seen: set[str] = set()
    videos = []
    for raw in _read_records(platform, workspace_id):
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
        if platform in {"dy", "bili"} and min_duration_seconds and video["duration_seconds"] <= min_duration_seconds:
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
    mood: Literal["natural", "energetic", "warm"] = "natural"


class SocialSendRequest(BaseModel):
    platform: Literal["dy", "xhs", "bili"]
    video_id: str


def _handoff_metadata(platform: str, video_id: str, workspace_id: str = "default") -> dict:
    video = _find_video(platform, video_id, workspace_id) or {}
    return {
        key: video.get(key, "")
        for key in ("title", "author", "source_url", "source_keyword")
    }


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
async def download_video(
    request: DownloadRequest,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    parsed = urlparse(request.url)
    allowed_hosts = {
        "dy": ("douyin.com", "douyinstatic.com"),
        "xhs": ("xhscdn.com",),
        "bili": ("bilivideo.com", "bilibili.com"),
    }
    if parsed.scheme not in {"http", "https"} or not any(parsed.hostname and parsed.hostname.endswith(host) for host in allowed_hosts[request.platform]):
        raise HTTPException(400, "URL video không hợp lệ")
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.video_id)
    target_dir = workspace_video_dir(workspace_id) / request.platform / safe_id
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
    return {"success": True, "path": str(target), "size": target.stat().st_size}


@router.post("/dub/prepare")
async def prepare_dubbing(
    request: DubbingPrepareRequest,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    """Tạo job nền để tải video và nhận diện transcript."""
    if request.platform != "dy":
        raise HTTPException(400, "Hiện pipeline lồng tiếng chỉ hỗ trợ video Douyin")
    job_id = uuid.uuid4().hex
    _store_dub_job({"job_id": job_id, "workspace_id": workspace_id, "video_id": request.video_id, "status": "running", "stage": "Đang chuẩn bị...", "progress": 0, "transcript_srt": "", "error": "", "logs": []})
    asyncio.create_task(_prepare_dubbing_job(job_id, request, workspace_id))
    return {"job_id": job_id, "video_id": request.video_id}


async def _prepare_dubbing_job(job_id: str, request: DubbingPrepareRequest, workspace_id: str):
    job = _DUB_JOBS[job_id]
    try:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.video_id)
        work_dir = workspace_video_dir(workspace_id) / "dy" / safe_id
        transcript = _cached_transcript(work_dir)
        if transcript is not None:
            _update_dub_job(
                job,
                status="done",
                stage="Đã khôi phục transcript Whisper có sẵn.",
                progress=100,
                transcript_srt=transcript,
            )
            return
        await _download_dubbing_video(request, work_dir, job)
        _update_dub_job(job, stage="Đã tải xong. Đang tách audio WAV 16 kHz...", progress=30)
        _update_dub_job(job, stage="Đang chạy Whisper model small nhận diện tiếng Trung (lần đầu có thể tải model)...", progress=40)
        async def whisper_progress(done: int, total: int):
            _update_dub_job(job, stage=f"Whisper đang xử lý đoạn {done}/{total} (tối đa 3 đoạn song song)...", progress=40 + int(done / total * 55))
        transcript = await transcribe(work_dir / "video.mp4", work_dir, whisper_progress)
        _update_dub_job(job, status="done", stage="Đã nhận diện xong transcript.", progress=100, transcript_srt=transcript)
    except (RuntimeError, ValueError, OSError) as exc:
        _update_dub_job(job, status="error", stage="Pipeline dừng do lỗi.", error=str(exc))


async def _download_dubbing_video(request: DubbingPrepareRequest, work_dir: Path, job: dict):
    parsed = urlparse(request.url)
    if parsed.scheme not in {"http", "https"} or not (parsed.hostname and parsed.hostname.endswith("douyin.com")):
        raise ValueError("URL video Douyin không hợp lệ hoặc đã hết hạn")
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / "video.mp4"
    downloaded = target.stat().st_size if target.exists() else 0
    total = 0
    last_error: Exception | None = None
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=60)) as client:
        for attempt in range(6):
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                if downloaded:
                    headers["Range"] = f"bytes={downloaded}-"
                async with client.stream("GET", request.url, headers=headers) as response:
                    response.raise_for_status()
                    if downloaded and response.status_code != 206:
                        # CDN không hỗ trợ resume trong lượt này; bắt đầu lại an toàn.
                        downloaded = 0
                        target.unlink(missing_ok=True)
                    content_range = response.headers.get("content-range", "")
                    if "/" in content_range:
                        total = int(content_range.rsplit("/", 1)[1])
                    elif response.headers.get("content-length"):
                        total = downloaded + int(response.headers["content-length"])
                    mode = "ab" if downloaded and response.status_code == 206 else "wb"
                    with target.open(mode) as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            output.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                percent = min(25, 5 + int(downloaded / total * 20))
                                stage = f"Đang tải video gốc... {downloaded / 1048576:.1f}/{total / 1048576:.1f} MB"
                            else:
                                percent = 10
                                stage = f"Đang tải video gốc... {downloaded / 1048576:.1f} MB"
                            _update_dub_job(job, stage=stage, progress=percent)
                if total and downloaded < total:
                    raise RuntimeError(f"CDN ngắt ở {downloaded}/{total} byte")
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 416 and downloaded:
                    # Phần tạm không khớp URL CDN hiện tại (đã đủ hoặc khác
                    # phiên bản). Xóa một lần rồi tải lại từ đầu.
                    downloaded = 0
                    total = 0
                    target.unlink(missing_ok=True)
                    _update_dub_job(job, stage="URL CDN đã thay đổi, bắt đầu tải lại từ đầu...", progress=5)
                    continue
                last_error = exc
                _update_dub_job(job, stage=f"Mạng/CDN lỗi, đang tải tiếp (lần {attempt + 1}/6)...", progress=min(24, 5 + attempt * 3))
                await asyncio.sleep(min(2 ** attempt, 10))
            except (httpx.HTTPError, OSError, RuntimeError) as exc:
                last_error = exc
                _update_dub_job(job, stage=f"Mạng gián đoạn, đang tải tiếp (lần {attempt + 1}/6)...", progress=min(24, 5 + attempt * 3))
                await asyncio.sleep(min(2 ** attempt, 10))
    raise RuntimeError(f"Tải video thất bại sau khi thử tiếp 6 lần: {last_error}")


@router.get("/dub/status/{job_id}")
async def dubbing_job_status(
    job_id: str,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    # Đọc file trước để polling vẫn thấy job nếu request được chuyển sang
    # worker khác hoặc dictionary RAM vừa bị mất do reload.
    job = _load_dub_job(workspace_id, job_id) or _DUB_JOBS.get(job_id)
    if not job or job.get("workspace_id", "default") != workspace_id:
        raise HTTPException(404, "Không tìm thấy job lồng tiếng")
    return job


@router.post("/dub/render")
async def render_dubbing(
    request: DubbingRenderRequest,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    """Tạo job nền để request không timeout với video dài."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.video_id)
    work_dir = workspace_video_dir(workspace_id) / "dy" / safe_id
    video_path = work_dir / "video.mp4"
    if not video_path.exists():
        raise HTTPException(404, "Chưa có video gốc; hãy chạy bước tạo transcript trước")
    job_id = uuid.uuid4().hex
    existing_job_id = _claim_dub_render(work_dir, job_id)
    if existing_job_id:
        # Hai tab bấm cùng video không phải là lỗi. Cho tab mới theo dõi đúng
        # job đang chạy thay vì trả 409 và hiện "Pipeline dừng" giả.
        return {"job_id": existing_job_id, "video_id": request.video_id, "reused": True}
    _store_dub_job({
        "job_id": job_id,
        "workspace_id": workspace_id,
        "video_id": request.video_id,
        "status": "running",
        "stage": "Đang chuẩn bị dịch phụ đề...",
        "progress": 0,
        "transcript_srt": "",
        "error": "",
        "output_url": "",
        "logs": [],
    })
    try:
        asyncio.create_task(_render_dubbing_job(job_id, request, video_path, work_dir, workspace_id))
    except Exception:
        _release_dub_render(work_dir, job_id)
        _DUB_JOBS.pop(job_id, None)
        raise
    return {"job_id": job_id, "video_id": request.video_id}


async def _render_dubbing_job(
    job_id: str, request: DubbingRenderRequest, video_path: Path, work_dir: Path,
    workspace_id: str,
):
    job = _DUB_JOBS[job_id]

    async def progress(stage: str, done: int, total: int):
        total = max(total, 1)
        if stage == "translate":
            message = f"Đang dịch phụ đề: nhóm {done}/{total}..."
            _update_dub_job(
                job,
                stage=message,
                progress=min(25, int(done / total * 25)),
            )
            _append_job_log(job, message)
        elif stage == "tts":
            message = f"Đang tạo giọng đọc: câu {done}/{total}..."
            _update_dub_job(
                job,
                stage=message,
                progress=25 + int(done / total * 65),
            )
            _append_job_log(job, message)
        elif stage == "mux":
            message = "Đang ghép âm thanh vào video..." if not done else "Đã ghép video xong."
            _update_dub_job(
                job,
                stage=message,
                progress=92 if not done else 100,
            )
            _append_job_log(job, message)
        else:
            _update_dub_job(job, stage=stage)
            _append_job_log(job, stage)

    try:
        await dub(
            video_path, work_dir, request.transcript_srt, request.voice, request.mood, progress
        )
        handoff.start(
            work_dir / "output_vi.mp4",
            platform="dy",
            video_id=request.video_id,
            variant="dubbed",
            metadata=_handoff_metadata("dy", request.video_id, workspace_id),
            workspace_id=workspace_id,
        )
        _update_dub_job(
            job,
            status="done",
            stage="Hoàn tất: video lồng tiếng đã sẵn sàng.",
            progress=100,
            output_url=f"/api/videos/dub/output/{request.video_id}",
        )
    except (RuntimeError, ValueError, OSError) as exc:
        _append_job_log(job, f"LỖI: {exc}")
        _update_dub_job(job, status="error", stage="Pipeline lồng tiếng dừng do lỗi.", error=str(exc))
    finally:
        _release_dub_render(work_dir, job_id)


@router.get("/dub/output/{video_id}")
async def get_dubbed_video(
    video_id: str,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", video_id)
    output = workspace_video_dir(workspace_id) / "dy" / safe_id / "output_vi.mp4"
    if not output.exists():
        raise HTTPException(404, "Video lồng tiếng chưa được tạo")
    # Không dọn file trung gian tại đây: trình duyệt có thể mở link output cũ
    # trong khi một job retry đang dùng các file TTS/cache cùng thư mục.
    return FileResponse(output, media_type="video/mp4", filename=f"{safe_id}_vi.mp4")


@router.post("/social/send")
async def send_video_to_media_social(
    request: SocialSendRequest,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", request.video_id).strip("_")
    if not safe_id:
        raise HTTPException(400, "Video ID không hợp lệ")
    video = workspace_video_dir(workspace_id) / request.platform / safe_id / "output_vi.mp4"
    if not video.is_file():
        raise HTTPException(404, "Video chưa lồng tiếng; chỉ output_vi.mp4 được gửi sang MediaSocial")
    return handoff.start(
        video,
        platform=request.platform,
        video_id=safe_id,
        variant="dubbed",
        metadata=_handoff_metadata(request.platform, safe_id, workspace_id),
        workspace_id=workspace_id,
    )


@router.get("/social/status/{job_id}")
async def media_social_handoff_status(
    job_id: str,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    job = handoff.jobs.get(job_id)
    if not job or job.get("workspace_id", "default") != workspace_id:
        raise HTTPException(404, "Không tìm thấy job gửi MediaSocial")
    return job
