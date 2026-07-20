import asyncio

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


@pytest.mark.asyncio
async def test_google_fallback_retries_sequentially_and_switches_endpoint(monkeypatch):
    requested_urls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, **kwargs):
            requested_urls.append(url)
            request = httpx.Request("GET", url)
            if len(requested_urls) == 1:
                return httpx.Response(429, request=request)
            source = kwargs["params"]["q"]
            return httpx.Response(200, request=request, json=[[[f"vi:{source}", source]]])

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(gemini_service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(gemini_service.asyncio, "sleep", no_sleep)

    result = await gemini_service._translate_with_google_fallback(["你好", "世界"])

    assert result == ["vi:你好", "vi:世界"]
    assert requested_urls[:2] == [
        "https://translate.googleapis.com/translate_a/single",
        "https://translate.google.com/translate_a/single",
    ]


@pytest.mark.asyncio
async def test_google_fallback_batches_share_one_server_queue(monkeypatch):
    active = 0
    maximum_active = 0

    async def fake_unlimited(lines):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return lines

    monkeypatch.setattr(gemini_service, "_GOOGLE_TRANSLATE_SEMAPHORE", asyncio.Semaphore(1))
    monkeypatch.setattr(gemini_service, "_translate_with_google_fallback_unlimited", fake_unlimited)

    first, second = await asyncio.gather(
        gemini_service._translate_with_google_fallback(["一"]),
        gemini_service._translate_with_google_fallback(["二"]),
    )

    assert first == ["一"]
    assert second == ["二"]
    assert maximum_active == 1
