# Cách chạy MediaCrawler

Thư mục dự án:

```bash
cd /Users/lecongminh/code/job/auto-ai/MediaCrawler
```

## Chạy WebUI hằng ngày

Mở **2 cửa sổ Terminal**.

Terminal 1 — chạy backend (cổng `8080`):

```bash
cd /Users/lecongminh/code/job/auto-ai/MediaCrawler
uv run uvicorn api.main:app --port 8080
```

Terminal 2 — chạy frontend (cổng `5173`):

```bash
cd /Users/lecongminh/code/job/auto-ai/MediaCrawler/webui
npm run dev
```

Sau đó mở: <http://localhost:5173>

Với Douyin, lần chạy đầu tiên WebUI sẽ hiện QR ngay trên trang để quét bằng ứng dụng Douyin. Phiên đăng nhập được lưu trong `browser_data/dy_user_data_dir`; các lần sau crawler chạy ẩn và không mở website Douyin. Chỉ cần quét lại khi Douyin làm phiên đăng nhập hết hạn.

## Cho nhiều người dùng cùng lúc

Build frontend và chạy một server lắng nghe trong mạng LAN:

```bash
cd /Users/lecongminh/code/job/auto-ai/MediaCrawler/webui
npm run build

cd ..
uv run uvicorn api.main:app --host 0.0.0.0 --port 8080
```

Người dùng cùng mạng mở `http://<IP-của-máy-chạy-server>:8080`. Mỗi trình duyệt
được cấp một workspace ngẫu nhiên và chỉ thấy tiến trình, log, QR đăng nhập,
kết quả crawl, video tải về và job lồng tiếng của workspace đó. Dữ liệu nằm tại
`workspaces/<workspace_id>/{data,video,browser_data}`. Xóa localStorage của trang
sẽ tạo workspace mới và không còn thấy workspace cũ.

Workspace ID là mã truy cập ngẫu nhiên, phù hợp cho nhóm tin cậy trong LAN nhưng
không thay thế tài khoản/mật khẩu. Nếu mở ra Internet, cần đặt server sau HTTPS
và lớp đăng nhập/reverse proxy; không công khai trực tiếp cổng `8080`.

Để dừng, nhấn `Ctrl+C` ở cả hai Terminal.

## Cài đặt lần đầu hoặc sau khi cập nhật code

Yêu cầu: Python `>= 3.11`, `uv`, Node.js và Chrome.

```bash
cd /Users/lecongminh/code/job/auto-ai/MediaCrawler

# Cài/cập nhật thư viện Python theo uv.lock
uv sync

# Dự án hiện dùng Playwright (ENABLE_CDP_MODE = False), nên cần Chromium
uv run playwright install chromium

# Cài thư viện frontend theo package-lock.json
cd webui
npm ci
```

Pipeline lồng tiếng cần `ffmpeg` và `ffprobe`. Trên macOS cài bằng:

```bash
brew install ffmpeg
```

Kiểm tra trước khi dùng:

```bash
ffmpeg -version
uv run whisper --help
```

Lần đầu nhận diện giọng nói, Whisper sẽ tải model `large-v3` nên có thể mất nhiều thời gian và dung lượng.

Nếu chưa có file `.env`:

```bash
cd /Users/lecongminh/code/job/auto-ai/MediaCrawler
cp .env.example .env
```

Điền `GEMINI_API_KEYS` trong `.env` nếu dùng tính năng Gemini. Không commit hoặc gửi file `.env` cho người khác.

Whisper mặc định dùng model `small` để chạy ổn định trên Mac, chia audio 60 giây và chạy 3 đoạn song song. Có thể điều chỉnh trong `.env` bằng `WHISPER_CHUNK_SECONDS` và `WHISPER_PARALLELISM`; chia dưới 30 giây thường không nhanh hơn vì mỗi đoạn phải nạp model.

## Chạy crawler bằng dòng lệnh (không dùng WebUI)

```bash
cd /Users/lecongminh/code/job/auto-ai/MediaCrawler

# Tìm kiếm trên Xiaohongshu, đăng nhập bằng QR
uv run main.py --platform xhs --lt qrcode --type search --keywords "từ khóa"

# Xem toàn bộ tùy chọn
uv run main.py --help
```

Mã nền tảng: `xhs` (Xiaohongshu), `dy` (Douyin), `ks` (Kuaishou), `bili` (Bilibili), `wb` (Weibo), `tieba`, `zhihu`.

Dữ liệu mặc định được lưu trong thư mục `data/`; video tải về nằm trong `video/`.

### Lồng tiếng một video Douyin sang tiếng Việt

1. Crawl Douyin và đợi danh sách video xuất hiện.
   Mặc định WebUI giữ mọi thời lượng. Có thể đặt ngưỡng tại ô
   **Video dài hơn (phút)** trước khi bắt đầu crawl nếu chỉ cần video dài.
2. Tích chọn **đúng một video**.
3. Nhấn **Lồng tiếng Việt**.
4. Đợi Whisper tạo transcript, sau đó đọc và sửa nội dung tiếng Trung trong ô SRT. Giữ nguyên số thứ tự và timestamp.
5. Chọn giọng Hoài My hoặc Nam Minh, rồi nhấn **Transcript đúng — tiến hành lồng tiếng**.
6. Khi hoàn tất, nhấn **Tải output_vi.mp4**.

Hệ thống tự chia video dài thành chapter nội bộ để phân phối TTS song song:
video 10–30 phút dùng chapter 5 phút và tối đa 4 tác vụ; video trên 30 phút
dùng chapter 4 phút và tối đa 4 tác vụ để hạn chế Edge-TTS trả audio rỗng. Có thể ghi đè bằng
`TTS_PARALLELISM` trong `.env`. Khi nhiều người chạy đồng thời, toàn server còn
được chặn bởi `TTS_GLOBAL_PARALLELISM` (mặc định 4),
`WHISPER_GLOBAL_PARALLELISM` (mặc định 3) và
`TRANSLATION_GLOBAL_PARALLELISM` (mặc định 1). Vì vậy job có thể hiện trạng thái
chờ lượt thay vì cùng dội request vào Gemini/Google/Edge-TTS và bị rate-limit.

Khi hoàn tất, thư mục `video/dy/<video_id>/` chỉ giữ `output_vi.mp4`; toàn bộ
chapter, audio, transcript và cache trung gian được tự động xóa.

### Gửi video sang MediaSocial

Chạy MediaSocial trước trên cổng `8090`, sau đó cấu hình trong `.env`:

```env
MEDIA_SOCIAL_URL=http://127.0.0.1:8090
MEDIASOCIAL_INTAKE_KEY=thay-bang-mot-khoa-bi-mat
```

Đặt cùng `MEDIASOCIAL_INTAKE_KEY` trong `MediaSocial/.env`. MediaCrawler chỉ gửi
`output_vi.mp4` sau khi quá trình lồng tiếng hoàn tất. Nếu MediaSocial chưa chạy
lúc đó, tích chọn video đã lồng tiếng và nhấn **Gửi MediaSocial** để gửi lại.

## Chạy bản frontend đã build

Cách này chỉ cần chạy một server sau khi build:

```bash
cd /Users/lecongminh/code/job/auto-ai/MediaCrawler/webui
npm run build

cd ..
uv run uvicorn api.main:app --port 8080
```

Mở: <http://localhost:8080>

## Lỗi thường gặp

- `uv: command not found`: cài `uv`, sau đó mở Terminal mới.
- `npm: command not found`: cài Node.js, sau đó mở Terminal mới.
- Thiếu browser executable: chạy `uv run playwright install chromium` tại thư mục `MediaCrawler`.
- Cổng đang được dùng: kiểm tra bằng `lsof -i :8080` hoặc `lsof -i :5173`, rồi dừng tiến trình cũ.
- Backend lỗi nhưng frontend vẫn mở: xem log ở Terminal 1; frontend cần backend tại `http://localhost:8080`.
- Trang yêu cầu đăng nhập: làm theo cửa sổ Chrome được mở ra và quét QR/xác minh thủ công.
