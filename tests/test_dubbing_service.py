import wave

import pytest

from api.services.dubbing_service import (
    Subtitle,
    _audio_duration_ms,
    _bounded_tts_ratio,
    _clean_vietnamese_tts_text,
    _dub_mux_args,
    _edge_tts_options,
    _has_speakable_content,
    _natural_tts_target_ms,
    _strict_tts_target_ms,
    _needs_vietnamese_retranslation,
    _write_silence_wav,
)


def test_edge_tts_options_fall_back_after_three_attempts():
    prosody = ("-5%", "+0Hz")

    assert _edge_tts_options(prosody, 0) == {
        "rate": "-5%",
        "pitch": "+0Hz",
        "volume": "+0%",
    }
    assert _edge_tts_options(prosody, 2)["rate"] == "-5%"
    assert _edge_tts_options(prosody, 3) == {}


def test_dub_mux_replaces_original_audio(tmp_path):
    args = _dub_mux_args(
        tmp_path / "video.mp4",
        tmp_path / "voice.wav",
        tmp_path / "output.mp4",
    )

    maps = [args[index + 1] for index, value in enumerate(args) if value == "-map"]
    assert maps == ["0:v:0", "1:a:0"]
    assert "-filter_complex" not in args
    assert "0:a:0" not in args


def test_tts_speed_is_natural_and_bounded():
    assert _bounded_tts_ratio(800, 1_000, 1.25) == 1.0
    assert _bounded_tts_ratio(1_070, 1_000, 1.25) == 1.0
    assert _bounded_tts_ratio(1_100, 1_000, 1.25) == 1.1
    assert _bounded_tts_ratio(2_000, 1_000, 1.25) == 1.25


def test_natural_tts_target_borrows_following_silence():
    item = Subtitle(1, 1_000, 2_000, "Xin chào")
    next_item = Subtitle(2, 2_500, 3_000, "Bạn khỏe không")

    assert _natural_tts_target_ms(item, next_item, 4_000) == 1_320


def test_natural_tts_target_does_not_cross_next_line():
    item = Subtitle(1, 1_000, 2_000, "Xin chào")
    next_item = Subtitle(2, 2_100, 3_000, "Bạn khỏe không")

    assert _natural_tts_target_ms(item, next_item, 4_000) == 1_100


def test_strict_tts_target_matches_original_subtitle():
    item = Subtitle(1, 1_000, 2_000, "Xin chào")
    next_item = Subtitle(2, 2_500, 3_000, "Bạn khỏe không")

    assert _strict_tts_target_ms(item, next_item) == 1_000


def test_strict_tts_target_never_overlaps_next_subtitle():
    item = Subtitle(1, 1_000, 2_000, "Xin chào")
    next_item = Subtitle(2, 1_900, 3_000, "Bạn khỏe không")

    assert _strict_tts_target_ms(item, next_item) == 900


@pytest.mark.parametrize("text", ["あと", "你好", "안녕", "गाया", "привет", "غنى", "G�от"])
def test_non_vietnamese_scripts_are_retranslated(text):
    assert _needs_vietnamese_retranslation(text)


def test_vietnamese_text_does_not_need_retranslation():
    assert not _needs_vietnamese_retranslation("Đó là tôi.")


def test_unrepaired_characters_are_removed_before_vietnamese_tts():
    assert _clean_vietnamese_tts_text("G�от") == "G"
    assert _clean_vietnamese_tts_text("đã hát गाया") == "đã hát"


@pytest.mark.parametrize("text", ["%,", "...", "?! —", "🎵"])
def test_punctuation_only_text_is_not_speakable(text):
    assert not _has_speakable_content(text)


@pytest.mark.parametrize("text", ["Xin chào!", "2026", "câu 7"])
def test_letters_and_numbers_are_speakable(text):
    assert _has_speakable_content(text)


def test_silence_wav_matches_subtitle_duration(tmp_path):
    audio = tmp_path / "silence.wav"
    _write_silence_wav(audio, 1_700)

    with wave.open(str(audio), "rb") as source:
        assert source.getframerate() == 24_000
        assert source.getnchannels() == 1
        assert source.getnframes() == 40_800


@pytest.mark.asyncio
async def test_audio_duration_accepts_valid_wav(tmp_path):
    audio = tmp_path / "valid.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x00\x00" * 2_400)

    assert 90 <= await _audio_duration_ms(audio) <= 110


@pytest.mark.asyncio
async def test_audio_duration_rejects_corrupt_audio(tmp_path):
    audio = tmp_path / "corrupt.mp3"
    audio.write_bytes(b"not an mp3")

    with pytest.raises(RuntimeError, match="không phải audio hợp lệ"):
        await _audio_duration_ms(audio)
