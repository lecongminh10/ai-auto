import asyncio
import hashlib
import json
import os
import re
import shutil
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

from .gemini_service import _translate_with_google_fallback, translate_chinese_subtitles


@dataclass
class Subtitle:
    index: int
    start_ms: int
    end_ms: int
    text: str


TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")

# Giới hạn toàn process, không phải riêng từng video. Hai job đồng thời vẫn chỉ
# dùng tối đa số worker này nên Whisper/Edge không làm cạn CPU, RAM hoặc bị CDN
# rate-limit. Semaphore riêng của mỗi job bên dưới vẫn giữ fairness nội bộ.
_GLOBAL_WHISPER_SEMAPHORE = asyncio.Semaphore(
    max(1, int(os.getenv("WHISPER_GLOBAL_PARALLELISM", os.getenv("WHISPER_PARALLELISM", "3"))))
)
_GLOBAL_TTS_SEMAPHORE = asyncio.Semaphore(
    max(1, int(os.getenv("TTS_GLOBAL_PARALLELISM", os.getenv("TTS_PARALLELISM", "4"))))
)


def _is_non_latin_letter(character: str) -> bool:
    return unicodedata.category(character).startswith("L") and not unicodedata.name(
        character, ""
    ).startswith("LATIN")


def _is_unsupported_tts_character(character: str) -> bool:
    name = unicodedata.name(character, "")
    return (
        character == "\ufffd"
        or _is_non_latin_letter(character)
        or (unicodedata.category(character).startswith("M") and not name.startswith("COMBINING"))
    )


def _needs_vietnamese_retranslation(text: str) -> bool:
    """Edge's Vietnamese voices return no audio for many non-Latin scripts."""
    return any(_is_unsupported_tts_character(char) for char in text)


def _clean_vietnamese_tts_text(text: str) -> str:
    """Remove unrepaired characters that the Vietnamese Edge voice cannot read."""
    cleaned = "".join(" " if _is_unsupported_tts_character(char) else char for char in text)
    return " ".join(cleaned.split()).strip(" ,.;:!?-–—")


def _has_speakable_content(text: str) -> bool:
    """Punctuation-only subtitles cannot produce audio in Edge-TTS."""
    return any(unicodedata.category(char)[0] in {"L", "N"} for char in text)


def _write_silence_wav(path: Path, duration_ms: int) -> None:
    sample_rate = 24_000
    frame_count = max(1, int(duration_ms * sample_rate / 1000))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x00\x00" * frame_count)


def _milliseconds(value: str) -> int:
    match = TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError(f"Mốc thời gian SRT không hợp lệ: {value}")
    hour, minute, second, millis = map(int, match.groups())
    return ((hour * 60 + minute) * 60 + second) * 1000 + millis


def parse_srt(content: str) -> list[Subtitle]:
    result: list[Subtitle] = []
    blocks = re.split(r"\n\s*\n", content.strip().replace("\r\n", "\n"))
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            raise ValueError(f"Khối SRT không hợp lệ: {block[:80]}")
        start, end = lines[1].split("-->", 1)
        start_ms, end_ms = _milliseconds(start), _milliseconds(end)
        if end_ms <= start_ms:
            raise ValueError(f"Câu {lines[0]} có thời lượng không hợp lệ")
        result.append(Subtitle(int(lines[0]), start_ms, end_ms, " ".join(lines[2:])))
    if not result:
        raise ValueError("Whisper không nhận diện được câu thoại nào")
    return result


def render_srt(items: list[Subtitle]) -> str:
    def stamp(ms: int) -> str:
        hour, rest = divmod(ms, 3_600_000)
        minute, rest = divmod(rest, 60_000)
        second, millis = divmod(rest, 1000)
        return f"{hour:02}:{minute:02}:{second:02},{millis:03}"
    return "\n\n".join(
        f"{item.index}\n{stamp(item.start_ms)} --> {stamp(item.end_ms)}\n{item.text}" for item in items
    ) + "\n"


async def _run(*args: str) -> None:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError(f"Lệnh {args[0]} quá thời gian 15 phút") from exc
    if process.returncode:
        message = stderr.decode("utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"Lệnh {args[0]} thất bại: {message}")


def check_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe", "whisper") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"Thiếu công cụ: {', '.join(missing)}")
    try:
        import edge_tts  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Thiếu edge-tts; chạy uv sync để cài dependencies") from exc


async def _audio_duration_ms(path: Path) -> int:
    """Chỉ chấp nhận file âm thanh mà FFprobe thực sự đọc được."""
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        message = stderr.decode("utf-8", errors="replace")[-500:].strip()
        raise RuntimeError(f"File TTS không phải audio hợp lệ: {message}")
    try:
        duration_ms = int(float(stdout.decode().strip()) * 1000)
    except ValueError as exc:
        raise RuntimeError("FFprobe không trả về thời lượng audio hợp lệ") from exc
    if duration_ms <= 0:
        raise RuntimeError("File TTS có thời lượng bằng 0")
    return duration_ms


def cleanup_media_intermediates(work_dir: Path, output: Path) -> None:
    """Sau khi output hoàn chỉnh, chỉ giữ lại duy nhất output_vi.mp4."""
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("Không dọn file vì output_vi.mp4 chưa hợp lệ")
    for path in work_dir.rglob("*"):
        if not path.is_file() or path == output:
            continue
        try:
            path.unlink()
        except OSError:
            # Output đã an toàn; một file đang bị OS giữ không nên làm job
            # hoàn tất biến thành lỗi. Lần dọn sau có thể xử lý phần còn lại.
            pass
    # Xóa các thư mục chunk/cache đã rỗng, từ cấp sâu nhất trở lên.
    directories = sorted(
        (path for path in work_dir.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


async def transcribe(video_path: Path, work_dir: Path, on_progress: Callable[[int, int], Awaitable[None]] | None = None) -> str:
    check_tools()
    audio = work_dir / "audio.wav"
    await _run("ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio))
    # Whisper CLI mặc định chỉ chọn CUDA/CPU và bỏ qua Apple MPS. MPS nhanh
    # hơn đáng kể trên Mac Apple Silicon; máy không có MPS vẫn dùng CPU.
    device = "cpu"
    try:
        import torch
        if torch.backends.mps.is_available():
            device = "mps"
    except (ImportError, AttributeError):
        pass
    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(audio),
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await probe.communicate()
    duration = float(stdout.decode().strip() or 0)
    # Đoạn 60 giây là điểm cân bằng: đủ nhỏ để chạy song song nhưng không
    # lãng phí thời gian nạp model Whisper cho quá nhiều đoạn.
    chunk_seconds = max(30, int(os.getenv("WHISPER_CHUNK_SECONDS", "60")))
    chunks = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    model = os.getenv("WHISPER_MODEL", "small")
    whisper_parallelism = max(1, int(os.getenv("WHISPER_PARALLELISM", "3")))
    semaphore = asyncio.Semaphore(whisper_parallelism)

    async def transcribe_chunk(index: int) -> list[Subtitle]:
        start = index * chunk_seconds
        chunk_dir = work_dir / f"whisper_chunk_{index:03}"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_audio = chunk_dir / "chunk.wav"
        args = [
            "whisper", str(chunk_audio), "--model", model, "--language", "Chinese", "--device", "cpu",
            "--output_format", "srt", "--word_timestamps", "False", "--beam_size", "1", "--best_of", "1",
            "--output_dir", str(chunk_dir),
        ]
        async with semaphore:
            async with _GLOBAL_WHISPER_SEMAPHORE:
                await _run("ffmpeg", "-y", "-ss", str(start), "-t", str(chunk_seconds), "-i", str(audio), str(chunk_audio))
                await _run(*args)
        files = list(chunk_dir.glob("*.srt"))
        if not files:
            raise RuntimeError(f"Whisper không tạo SRT cho đoạn {index + 1}/{chunks}")
        items = parse_srt(files[0].read_text("utf-8"))
        return [Subtitle(item.index, item.start_ms + start * 1000, item.end_ms + start * 1000, item.text) for item in items]

    merged: list[Subtitle] = []
    completed = 0
    # Đưa toàn bộ chunk vào hàng đợi; semaphore quyết định đúng số tiến trình
    # Whisper chạy đồng thời thay vì bị ghi cứng tối đa hai chunk mỗi lượt.
    tasks = [asyncio.create_task(transcribe_chunk(index)) for index in range(chunks)]
    for task in asyncio.as_completed(tasks):
        merged.extend(await task)
        completed += 1
        if on_progress:
            await on_progress(completed, chunks)
    merged.sort(key=lambda item: item.start_ms)
    for index, item in enumerate(merged, 1):
        item.index = index
    transcript = render_srt(merged)
    (work_dir / "audio.srt").write_text(transcript, "utf-8")
    return transcript


def _atempo_chain(ratio: float) -> str:
    values: list[float] = []
    while ratio > 2:
        values.append(2.0)
        ratio /= 2
    while ratio < 0.5:
        values.append(0.5)
        ratio /= 0.5
    values.append(ratio)
    return ",".join(f"atempo={value:.6f}" for value in values)


def _bounded_tts_ratio(raw_duration_ms: int, target_ms: int, max_speedup: float) -> float:
    """Keep speech natural by allowing tiny overruns before speeding it up."""
    requested = raw_duration_ms / max(1, target_ms)
    if requested <= 1.08:
        return 1.0
    return max(1.0, min(requested, max_speedup))


def _natural_tts_target_ms(
    item: Subtitle,
    next_item: Subtitle | None,
    video_duration_ms: int,
    max_breath_ms: int = 320,
) -> int:
    """Allow TTS to borrow a little following silence instead of sounding rushed."""
    original_ms = item.end_ms - item.start_ms
    boundary_ms = next_item.start_ms if next_item else video_duration_ms
    available_ms = max(original_ms, boundary_ms - item.start_ms)
    return min(available_ms, original_ms + max(0, max_breath_ms))


def _edge_tts_options(prosody: tuple[str, str], attempt: int) -> dict[str, str]:
    """Fall back to Edge-TTS defaults when a text/prosody pair returns no audio."""
    if attempt < 3:
        return {"rate": prosody[0], "pitch": prosody[1], "volume": "+0%"}
    return {}


async def dub(
    video_path: Path,
    work_dir: Path,
    chinese_srt: str,
    voice: str,
    mood: str = "natural",
    on_progress: Callable[[str, int, int], Awaitable[None]] | None = None,
) -> Path:
    check_tools()
    from edge_tts import Communicate
    from edge_tts.exceptions import NoAudioReceived

    subtitles = parse_srt(chinese_srt)
    # Số thứ tự SRT do người dùng sửa có thể trùng hoặc không liên tục. Chuẩn
    # hóa thành ID nội bộ duy nhất để các task không cùng ghi một file seg_*.
    for internal_index, item in enumerate(subtitles, 1):
        item.index = internal_index
    (work_dir / "audio.srt").write_text(chinese_srt, "utf-8")
    translated: list[str] = []
    translation_prompt_version = "natural-dubbing-v2"
    # Chia nhỏ để prompt không quá dài nhưng vẫn giữ ngữ cảnh theo từng nhóm câu.
    translation_batch_size = max(30, int(os.getenv("TRANSLATION_BATCH_SIZE", "60")))
    translation_batches = list(range(0, len(subtitles), translation_batch_size))
    for batch_number, start in enumerate(translation_batches, 1):
        if on_progress:
            await on_progress("translate", batch_number - 1, len(translation_batches))
        source_lines = [item.text for item in subtitles[start:start + translation_batch_size]]
        source_durations_ms = [
            item.end_ms - item.start_ms
            for item in subtitles[start:start + translation_batch_size]
        ]
        batch_key = hashlib.sha256(
            json.dumps(
                [translation_prompt_version, source_lines, source_durations_ms],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        cache_path = work_dir / f"translation_{batch_key}.json"
        cached: list[str] = []
        if cache_path.exists():
            try:
                value = json.loads(cache_path.read_text("utf-8"))
                if isinstance(value, list) and len(value) == len(source_lines) and all(value):
                    cached = [str(item) for item in value]
            except (OSError, ValueError, TypeError):
                pass
        if not cached:
            async def translation_status(message: str):
                if on_progress:
                    await on_progress(message, 0, 0)

            cached = await translate_chinese_subtitles(
                source_lines,
                translation_status,
                source_durations_ms,
            )
        invalid_indexes = [
            index for index, value in enumerate(cached)
            if _needs_vietnamese_retranslation(value)
        ]
        if invalid_indexes:
            if on_progress:
                await on_progress(
                    f"Dịch lại {len(invalid_indexes)} câu có ký tự giọng Việt không đọc được...",
                    0,
                    0,
                )
            repaired = await _translate_with_google_fallback(
                [cached[index] for index in invalid_indexes]
            )
            for index, value in zip(invalid_indexes, repaired):
                # Bản dịch dự phòng đôi lúc giữ nguyên ký tự hỏng. Loại phần
                # còn sót để một câu lỗi không làm dừng toàn bộ video.
                cached[index] = _clean_vietnamese_tts_text(value)
        cache_path.write_text(json.dumps(cached, ensure_ascii=False), "utf-8")
        translated.extend(cached)
    if on_progress:
        await on_progress("translate", len(translation_batches), len(translation_batches))
    vi_items = [Subtitle(item.index, item.start_ms, item.end_ms, text) for item, text in zip(subtitles, translated)]
    (work_dir / "audio_vi.srt").write_text(render_srt(vi_items), "utf-8")

    final_end = max(item.end_ms for item in subtitles)
    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video_path),
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await probe.communicate()
    if probe.returncode:
        raise RuntimeError("Không đọc được thời lượng video")
    duration_ms = max(final_end, int(float(stdout.decode().strip()) * 1000))
    prosody = {
        "natural": ("-8%", "+0Hz"),
        "energetic": ("+3%", "+1Hz"),
        "warm": ("-12%", "-1Hz"),
    }.get(mood, ("-8%", "+0Hz"))
    max_tts_speedup = max(1.0, float(os.getenv("TTS_MAX_SPEEDUP", "1.12")))
    timing_targets = {
        item.index: _natural_tts_target_ms(
            item,
            vi_items[index + 1] if index + 1 < len(vi_items) else None,
            duration_ms,
        )
        for index, item in enumerate(vi_items)
    }

    async def synthesize_segment(item: Subtitle) -> tuple[Subtitle, Path]:
        raw = work_dir / f"seg_{item.index:04}.mp3"
        adjusted = work_dir / f"seg_{item.index:04}_adjusted.wav"
        raw_temp = work_dir / f"seg_{item.index:04}.tmp.mp3"
        adjusted_temp = work_dir / f"seg_{item.index:04}_adjusted.tmp.wav"
        cache_key_path = work_dir / f"seg_{item.index:04}.sha256"
        target_ms = timing_targets[item.index]
        cache_key = hashlib.sha256(
            f"natural-v3\0{item.text}\0{voice}\0{mood}\0{target_ms}\0{max_tts_speedup}".encode("utf-8")
        ).hexdigest()
        if (
            adjusted.exists() and adjusted.stat().st_size > 44
            and cache_key_path.exists()
            and cache_key_path.read_text("ascii", errors="ignore").strip() == cache_key
        ):
            return item, adjusted
        # Edge-TTS đôi lúc tạo file rỗng khi mạng/CDN chập chờn. Thử lại
        # từng câu để một lỗi tạm thời không làm pipeline dừng im lặng.
        last_error: Exception | None = None
        async with tts_semaphore, _GLOBAL_TTS_SEMAPHORE:
            adjusted.unlink(missing_ok=True)
            cache_key_path.unlink(missing_ok=True)
            if not _has_speakable_content(item.text):
                raw.unlink(missing_ok=True)
                _write_silence_wav(adjusted_temp, target_ms)
                adjusted_temp.replace(adjusted)
                cache_key_path.write_text(cache_key, "ascii")
                if on_progress:
                    await on_progress(
                        f"Câu {item.index} chỉ có dấu/ký tự rỗng; chèn khoảng im lặng.",
                        0,
                        0,
                    )
                return item, adjusted
            for attempt in range(6):
                raw_temp.unlink(missing_ok=True)
                adjusted_temp.unlink(missing_ok=True)
                try:
                    await asyncio.wait_for(
                        Communicate(
                            item.text, voice, **_edge_tts_options(prosody, attempt)
                        ).save(str(raw_temp)),
                        timeout=45,
                    )
                    if not raw_temp.exists() or raw_temp.stat().st_size == 0:
                        raise RuntimeError("Edge-TTS tạo file rỗng")
                    raw_duration_ms = await _audio_duration_ms(raw_temp)
                    ratio = _bounded_tts_ratio(raw_duration_ms, target_ms, max_tts_speedup)
                    # Giải mã ngay trong vòng retry. File có header MP3 nhưng
                    # frame bị cụt cũng sẽ bị loại và TTS được gọi lại.
                    await _run(
                        "ffmpeg", "-y", "-i", str(raw_temp),
                        "-filter:a", _atempo_chain(ratio),
                        "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
                        str(adjusted_temp),
                    )
                    if not adjusted_temp.exists() or adjusted_temp.stat().st_size <= 44:
                        raise RuntimeError("FFmpeg tạo segment WAV rỗng")
                    raw_temp.replace(raw)
                    adjusted_temp.replace(adjusted)
                    cache_key_path.write_text(cache_key, "ascii")
                    return item, adjusted
                except Exception as exc:
                    last_error = exc
                    if on_progress:
                        fallback_note = (
                            "; chuyển sang prosody mặc định"
                            if attempt == 2 else ""
                        )
                        await on_progress(
                            f"Câu {item.index} TTS lỗi lần {attempt + 1}/6: "
                            f"{type(exc).__name__}{fallback_note}; đang thử lại...",
                            0,
                            0,
                        )
                    await asyncio.sleep(min(2 ** attempt, 10))
        raw_temp.unlink(missing_ok=True)
        adjusted_temp.unlink(missing_ok=True)
        if isinstance(last_error, NoAudioReceived):
            raw.unlink(missing_ok=True)
            _write_silence_wav(adjusted_temp, target_ms)
            adjusted_temp.replace(adjusted)
            cache_key_path.write_text(cache_key, "ascii")
            if on_progress:
                await on_progress(
                    f"Câu {item.index} không được Edge-TTS hỗ trợ sau 6 lần; "
                    "đã chèn khoảng im lặng và tiếp tục.",
                    0,
                    0,
                )
            return item, adjusted
        raise RuntimeError(f"Không tạo được giọng đọc cho câu {item.index}: {last_error}")

    # Tự chia chapter để phân phối câu TTS của video dài qua hàng đợi chung.
    # Tổng concurrency được giới hạn để Edge-TTS không bị rate-limit quá mức.
    if duration_ms <= 10 * 60_000:
        chapter_ms, automatic_parallelism = duration_ms + 1, 3
    elif duration_ms <= 30 * 60_000:
        chapter_ms, automatic_parallelism = 5 * 60_000, 4
    else:
        chapter_ms, automatic_parallelism = 4 * 60_000, 4
    tts_parallelism = max(1, int(os.getenv("TTS_PARALLELISM", str(automatic_parallelism))))
    chapters: dict[int, list[Subtitle]] = {}
    for item in vi_items:
        chapters.setdefault(item.start_ms // chapter_ms, []).append(item)
    if on_progress:
        await on_progress(
            f"Tự chia {len(chapters)} chapter; job dùng tối đa {tts_parallelism} tác vụ "
            "và tự xếp hàng theo giới hạn TTS toàn server.",
            0,
            0,
        )

    tts_semaphore = asyncio.Semaphore(tts_parallelism)
    synthesized: list[tuple[Subtitle, Path]] = []
    # Xếp xen kẽ câu của các chapter để chapter dài không giữ toàn bộ worker.
    pending_items: list[Subtitle] = []
    chapter_values = list(chapters.values())
    max_chapter_items = max((len(items) for items in chapter_values), default=0)
    for offset in range(max_chapter_items):
        pending_items.extend(items[offset] for items in chapter_values if offset < len(items))
    tasks = [asyncio.create_task(synthesize_segment(item)) for item in pending_items]
    try:
        for task in asyncio.as_completed(tasks):
            synthesized.append(await task)
            if on_progress:
                await on_progress("tts", len(synthesized), len(vi_items))
    except BaseException:
        # Không để các task cũ tiếp tục ghi seg_* sau khi job đã báo lỗi hoặc
        # người dùng bắt đầu retry một job mới.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    audio_path = work_dir / "final_audio_vi.wav"
    # Ghi WAV tuần tự để RAM và thời gian ghép tăng tuyến tính theo độ dài
    # video. pydub.overlay sao chép lại track dài cho từng câu và rất chậm.
    sample_rate, sample_width = 24_000, 2
    cursor_frames = 0
    total_frames = int(duration_ms * sample_rate / 1000)
    silence = b"\x00" * sample_width
    with wave.open(str(audio_path), "wb") as output_wav:
        output_wav.setnchannels(1)
        output_wav.setsampwidth(sample_width)
        output_wav.setframerate(sample_rate)
        for item, adjusted in sorted(synthesized, key=lambda value: value[0].start_ms):
            start_frame = int(item.start_ms * sample_rate / 1000)
            if start_frame > cursor_frames:
                output_wav.writeframes(silence * (start_frame - cursor_frames))
                cursor_frames = start_frame
            with wave.open(str(adjusted), "rb") as segment_wav:
                frames = segment_wav.readframes(segment_wav.getnframes())
            # Không cắt đuôi câu nếu bản dịch vẫn dài hơn timestamp. Câu sau
            # nối tiếp sau câu hiện tại; các khoảng nghỉ kế tiếp sẽ hấp thụ độ trễ.
            output_wav.writeframes(frames)
            cursor_frames += len(frames) // sample_width
        if total_frames > cursor_frames:
            output_wav.writeframes(silence * (total_frames - cursor_frames))
    if on_progress:
        await on_progress("mux", 0, 1)
    output = work_dir / "output_vi.mp4"
    await _run(
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path), "-c:v", "copy",
        "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(output),
    )
    cleanup_media_intermediates(work_dir, output)
    if on_progress:
        await on_progress("mux", 1, 1)
    return output
