import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx


class MediaSocialHandoff:
    def __init__(self):
        self.base_url = os.getenv("MEDIA_SOCIAL_URL", "http://127.0.0.1:8090").rstrip("/")
        self.intake_key = os.getenv("MEDIASOCIAL_INTAKE_KEY", "").strip()
        self.jobs: dict[str, dict] = {}

    def start(
        self,
        video: Path,
        *,
        platform: str,
        video_id: str,
        variant: str,
        metadata: dict | None = None,
        workspace_id: str = "default",
    ) -> dict:
        job_id = uuid.uuid4().hex
        job = {
            "job_id": job_id,
            "workspace_id": workspace_id,
            "platform": platform,
            "video_id": video_id,
            "variant": variant,
            "status": "running",
            "stage": "Đang gửi video sang MediaSocial...",
            "progress": 1,
            "error": "",
            "result": {},
            "created_at": int(time.time()),
        }
        self.jobs[job_id] = job
        asyncio.create_task(self._run(job, video, metadata or {}))
        return job

    async def _run(self, job: dict, video: Path, metadata: dict) -> None:
        if not video.is_file():
            job.update(status="error", stage="Không tìm thấy video để gửi.", error=str(video))
            return
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                job.update(
                    stage=f"Đang gửi sang MediaSocial (lần {attempt + 1}/3)...",
                    progress=10 + attempt * 10,
                )
                headers = {"X-MediaSocial-Key": self.intake_key} if self.intake_key else {}
                with video.open("rb") as source:
                    files = {"video": (video.name, source, "video/mp4")}
                    data = {
                        "platform": job["platform"],
                        "video_id": job["video_id"],
                        "variant": job["variant"],
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }
                    async with httpx.AsyncClient(timeout=httpx.Timeout(10, write=600, read=60)) as client:
                        response = await client.post(
                            f"{self.base_url}/api/intake/videos",
                            data=data,
                            files=files,
                            headers=headers,
                        )
                if response.status_code >= 400:
                    raise RuntimeError(f"MediaSocial lỗi {response.status_code}: {response.text[:500]}")
                result = response.json()
                job.update(
                    status="done",
                    stage="Đã chuyển video sang MediaSocial.",
                    progress=100,
                    result=result,
                )
                return
            except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
        job.update(
            status="error",
            stage="Không thể gửi video sang MediaSocial.",
            error=str(last_error or "Lỗi không xác định"),
        )


handoff = MediaSocialHandoff()
