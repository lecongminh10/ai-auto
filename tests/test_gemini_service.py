import httpx
import pytest

from api.services import gemini_service


@pytest.mark.asyncio
async def test_translate_chinese_subtitles_falls_back_when_gemini_fails(monkeypatch):
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("POST", "https://example.test"),
                response=httpx.Response(500),
            )

    async def fake_fallback(lines):
        return [f"translated:{line}" for line in lines]

    monkeypatch.setattr(gemini_service, "_rotated_keys", lambda: ["fake-key"])
    monkeypatch.setattr(gemini_service.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(gemini_service, "_translate_with_google_fallback", fake_fallback, raising=False)

    result = await gemini_service.translate_chinese_subtitles(["你好", "世界"])

    assert result == ["translated:你好", "translated:世界"]
