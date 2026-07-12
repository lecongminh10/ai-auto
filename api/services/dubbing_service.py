import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

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


async def transcribe(video_path: Path, work_dir: Path) -> str:
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
    whisper_args = [
        "whisper", str(audio), "--model", os.getenv("WHISPER_MODEL", "small"), "--language", "Chinese",
        "--device", device, "--output_format", "srt", "--word_timestamps", "True", "--output_dir", str(work_dir),
    ]
    # FP16 trên MPS có thể treo ở bước decode với large-v3; FP32 ổn định hơn.
    if device == "mps":
        whisper_args.extend(["--fp16", "False"])
    await _run(*whisper_args)
    srt_files = sorted(work_dir.glob("*.srt"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not srt_files and device == "mps":
        # Một số bản torch/Whisper trên MPS trả mã 0 nhưng không ghi output.
        # Chạy lại CPU để đảm bảo pipeline có transcript thay vì kết thúc mơ hồ.
        cpu_args = [
            "whisper", str(audio), "--model", os.getenv("WHISPER_MODEL", "small"),
            "--language", "Chinese", "--device", "cpu", "--output_format", "srt",
            "--word_timestamps", "True", "--output_dir", str(work_dir),
        ]
        await _run(*cpu_args)
        srt_files = sorted(work_dir.glob("*.srt"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not srt_files:
        raise RuntimeError("Whisper đã chạy nhưng không tạo file SRT")
    srt_path = srt_files[0]
    return srt_path.read_text("utf-8")


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


async def dub(video_path: Path, work_dir: Path, chinese_srt: str, voice: str) -> Path:
    check_tools()
    from edge_tts import Communicate
    from pydub import AudioSegment

    subtitles = parse_srt(chinese_srt)
    (work_dir / "audio.srt").write_text(chinese_srt, "utf-8")
    translated: list[str] = []
    # Chia nhỏ để prompt không quá dài nhưng vẫn giữ ngữ cảnh theo từng nhóm câu.
    for start in range(0, len(subtitles), 30):
        translated.extend(await translate_chinese_subtitles([item.text for item in subtitles[start:start + 30]]))
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
    final_audio = AudioSegment.silent(duration=duration_ms, frame_rate=44_100)

    for item in vi_items:
        raw = work_dir / f"seg_{item.index:04}.mp3"
        adjusted = work_dir / f"seg_{item.index:04}_adjusted.wav"
        # Edge-TTS đôi lúc tạo file rỗng khi mạng/CDN chập chờn. Thử lại
        # từng câu để một lỗi tạm thời không làm pipeline dừng im lặng.
        last_error: Exception | None = None
        for attempt in range(3):
            raw.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(Communicate(item.text, voice).save(str(raw)), timeout=30)
                if raw.exists() and raw.stat().st_size > 0:
                    break
                raise RuntimeError("Edge-TTS tạo file rỗng")
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(1 + attempt)
        if not raw.exists() or raw.stat().st_size == 0:
            raise RuntimeError(f"Không tạo được giọng đọc cho câu {item.index}: {last_error}")
        segment = AudioSegment.from_file(raw)
        target_ms = item.end_ms - item.start_ms
        ratio = max(0.01, len(segment) / target_ms)
        await _run("ffmpeg", "-y", "-i", str(raw), "-filter:a", _atempo_chain(ratio), "-ar", "44100", str(adjusted))
        fitted = AudioSegment.from_file(adjusted)[:target_ms]
        final_audio = final_audio.overlay(fitted, position=item.start_ms)

    audio_path = work_dir / "final_audio_vi.wav"
    final_audio.export(audio_path, format="wav")
    output = work_dir / "output_vi.mp4"
    await _run(
        "ffmpeg", "-y", "-i", str(video_path), "-i", str(audio_path), "-c:v", "copy",
        "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(output),
    )
    return output
