from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app


def _bilibili_record(video_id: str, duration_seconds: int) -> dict:
    return {
        "video_id": video_id,
        "title": f"Video {video_id}",
        "duration_seconds": duration_seconds,
    }


def test_video_list_requires_duration_strictly_greater_than_minimum():
    records = [
        _bilibili_record("exactly-ten", 600),
        _bilibili_record("over-ten", 601),
    ]

    with patch("api.routers.videos._read_records", return_value=records):
        response = TestClient(app).get(
            "/api/videos",
            params={"platform": "bili", "min_duration_seconds": 600},
        )

    assert response.status_code == 200
    assert [video["id"] for video in response.json()["videos"]] == ["over-ten"]


def test_duration_filter_is_not_applied_to_platform_without_duration_metadata():
    with patch("api.routers.videos._read_records", return_value=[{"note_id": "xhs-1", "title": "Video"}]):
        response = TestClient(app).get(
            "/api/videos",
            params={"platform": "xhs", "min_duration_seconds": 600},
        )

    assert response.status_code == 200
    assert response.json()["total"] == 1
