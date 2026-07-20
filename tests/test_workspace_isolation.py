import json

from fastapi.testclient import TestClient

from api.main import app
from api.routers import videos
from api import workspace


WORKSPACE_A = "workspace_a_123"
WORKSPACE_B = "workspace_b_456"


def _headers(workspace_id: str) -> dict[str, str]:
    return {"X-Workspace-ID": workspace_id}


def test_video_results_are_isolated_by_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path)
    for workspace_id, video_id in ((WORKSPACE_A, "video-a"), (WORKSPACE_B, "video-b")):
        data_dir = workspace.workspace_data_dir(workspace_id) / "bilibili"
        data_dir.mkdir(parents=True, exist_ok=True)
        record = {"video_id": video_id, "title": video_id, "duration_seconds": 700}
        (data_dir / "search_contents_test.jsonl").write_text(
            json.dumps(record) + "\n",
            "utf-8",
        )

    client = TestClient(app)
    response_a = client.get("/api/videos", params={"platform": "bili"}, headers=_headers(WORKSPACE_A))
    response_b = client.get("/api/videos", params={"platform": "bili"}, headers=_headers(WORKSPACE_B))

    assert [item["id"] for item in response_a.json()["videos"]] == ["video-a"]
    assert [item["id"] for item in response_b.json()["videos"]] == ["video-b"]


def test_dubbing_job_cannot_be_read_from_another_workspace():
    job_id = "isolated-job"
    videos._DUB_JOBS[job_id] = {
        "job_id": job_id,
        "workspace_id": WORKSPACE_A,
        "status": "running",
    }
    try:
        client = TestClient(app)
        own = client.get(f"/api/videos/dub/status/{job_id}", headers=_headers(WORKSPACE_A))
        other = client.get(f"/api/videos/dub/status/{job_id}", headers=_headers(WORKSPACE_B))

        assert own.status_code == 200
        assert other.status_code == 404
    finally:
        videos._DUB_JOBS.pop(job_id, None)


def test_dubbing_job_status_survives_in_memory_state_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path)
    job_id = "persisted-job"
    videos._store_dub_job({
        "job_id": job_id,
        "workspace_id": WORKSPACE_A,
        "status": "running",
        "stage": "Đang dịch",
        "progress": 10,
    })
    videos._DUB_JOBS.pop(job_id, None)

    try:
        response = TestClient(app).get(
            f"/api/videos/dub/status/{job_id}",
            headers=_headers(WORKSPACE_A),
        )

        assert response.status_code == 200
        assert response.json()["stage"] == "Đang dịch"
        assert job_id in videos._DUB_JOBS
    finally:
        videos._DUB_JOBS.pop(job_id, None)


def test_dubbed_file_cannot_be_downloaded_from_another_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path)
    output = workspace.workspace_video_dir(WORKSPACE_A) / "dy" / "video-a" / "output_vi.mp4"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"private-video")

    client = TestClient(app)
    own = client.get("/api/videos/dub/output/video-a", headers=_headers(WORKSPACE_A))
    other = client.get("/api/videos/dub/output/video-a", headers=_headers(WORKSPACE_B))

    assert own.status_code == 200
    assert own.content == b"private-video"
    assert other.status_code == 404


def test_same_video_render_cannot_write_shared_files_concurrently(tmp_path):
    work_dir = tmp_path / "workspace" / "video" / "dy" / "same-video"

    assert videos._claim_dub_render(work_dir, "job-one") is None
    try:
        assert videos._claim_dub_render(work_dir, "job-two") == "job-one"
        assert videos._claim_dub_render(work_dir.parent / "different-video", "job-three") is None
    finally:
        videos._release_dub_render(work_dir, "job-one")
        videos._release_dub_render(work_dir.parent / "different-video", "job-three")

    assert videos._claim_dub_render(work_dir, "job-four") is None
    videos._release_dub_render(work_dir, "job-four")


def test_completed_transcript_can_be_recovered_after_job_state_is_lost(tmp_path):
    work_dir = tmp_path / "video"
    work_dir.mkdir()
    (work_dir / "video.mp4").write_bytes(b"video")
    transcript = "1\n00:00:00,000 --> 00:00:01,000\n你好\n"
    (work_dir / "audio.srt").write_text(transcript, "utf-8")

    assert videos._cached_transcript(work_dir) == transcript


def test_invalid_cached_transcript_is_not_reused(tmp_path):
    work_dir = tmp_path / "video"
    work_dir.mkdir()
    (work_dir / "video.mp4").write_bytes(b"video")
    (work_dir / "audio.srt").write_text("incomplete", "utf-8")

    assert videos._cached_transcript(work_dir) is None
