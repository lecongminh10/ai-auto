import asyncio
import hashlib
import json
import os
import re
import shutil
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Awaitable

from .gemini_service import translate_chinese_subtitles


@dataclass
class Subtitle:
    index: int
    start_ms: int
    end_ms: int
    text: str


TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")


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
        from pydub import AudioSegment  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("Thiếu edge-tts hoặc pydub; chạy uv sync để cài dependencies") from exc


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
    from pydub import AudioSegment

    subtitles = parse_srt(chinese_srt)
    (work_dir / "audio.srt").write_text(chinese_srt, "utf-8")
    translated: list[str] = []
    # Chia nhỏ để prompt không quá dài nhưng vẫn giữ ngữ cảnh theo từng nhóm câu.
    translation_batch_size = max(30, int(os.getenv("TRANSLATION_BATCH_SIZE", "60")))
    translation_batches = list(range(0, len(subtitles), translation_batch_size))
    for batch_number, start in enumerate(translation_batches, 1):
        if on_progress:
            await on_progress("translate", batch_number - 1, len(translation_batches))
        source_lines = [item.text for item in subtitles[start:start + translation_batch_size]]
        batch_key = hashlib.sha256(
            json.dumps(source_lines, ensure_ascii=False).encode("utf-8")
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

            cached = await translate_chinese_subtitles(source_lines, translation_status)
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
        "natural": ("-5%", "+0Hz"),
        "energetic": ("+8%", "+2Hz"),
        "warm": ("-10%", "-1Hz"),
    }.get(mood, ("-5%", "+0Hz"))

    async def synthesize_segment(item: Subtitle) -> tuple[Subtitle, Path]:
        raw = work_dir / f"seg_{item.index:04}.mp3"
        adjusted = work_dir / f"seg_{item.index:04}_adjusted.wav"
        cache_key_path = work_dir / f"seg_{item.index:04}.sha256"
        target_ms = item.end_ms - item.start_ms
        cache_key = hashlib.sha256(
            f"{item.text}\0{voice}\0{mood}\0{target_ms}".encode("utf-8")
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
        async with tts_semaphore:
            adjusted.unlink(missing_ok=True)
            cache_key_path.unlink(missing_ok=True)
            for attempt in range(6):
                raw.unlink(missing_ok=True)
                try:
                    await asyncio.wait_for(
                        Communicate(item.text, voice, rate=prosody[0], pitch=prosody[1], volume="+0%").save(str(raw)),
                        timeout=45,
                    )
                    if raw.exists() and raw.stat().st_size > 0:
                        break
                    raise RuntimeError("Edge-TTS tạo file rỗng")
                except Exception as exc:
                    last_error = exc
                    if on_progress:
                        await on_progress(
                            f"Câu {item.index} TTS lỗi lần {attempt + 1}/6: "
                            f"{type(exc).__name__}; đang thử lại...",
                            0,
                            0,
                        )
                    await asyncio.sleep(min(2 ** attempt, 10))
        if not raw.exists() or raw.stat().st_size == 0:
            raise RuntimeError(f"Không tạo được giọng đọc cho câu {item.index}: {last_error}")
        segment = AudioSegment.from_file(raw)
        ratio = max(0.01, len(segment) / target_ms)
        await _run(
            "ffmpeg", "-y", "-i", str(raw), "-filter:a", _atempo_chain(ratio),
            "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(adjusted),
        )
        cache_key_path.write_text(cache_key, "ascii")
        return item, adjusted

    # Tự chia chapter để phân phối câu TTS của video dài qua hàng đợi chung.
    # Tổng concurrency được giới hạn để Edge-TTS không bị rate-limit quá mức.
    if duration_ms <= 10 * 60_000:
        chapter_ms, automatic_parallelism = duration_ms + 1, 4
    elif duration_ms <= 30 * 60_000:
        chapter_ms, automatic_parallelism = 5 * 60_000, 6
    else:
        chapter_ms, automatic_parallelism = 4 * 60_000, 8
    tts_parallelism = max(1, int(os.getenv("TTS_PARALLELISM", str(automatic_parallelism))))
    chapters: dict[int, list[Subtitle]] = {}
    for item in vi_items:
        chapters.setdefault(item.start_ms // chapter_ms, []).append(item)
    if on_progress:
        await on_progress(
            f"Tự chia {len(chapters)} chapter; chạy tối đa {tts_parallelism} tác vụ TTS song song.",
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
    for task in asyncio.as_completed(tasks):
        synthesized.append(await task)
        if on_progress:
            await on_progress("tts", len(synthesized), len(vi_items))

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
            target_frames = int((item.end_ms - item.start_ms) * sample_rate / 1000)
            frames = frames[: target_frames * sample_width]
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
