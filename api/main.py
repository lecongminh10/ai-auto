# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/main.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

"""
MediaCrawler WebUI API Server
Start command: uvicorn api.main:app --port 8080 --reload
Or: python -m api.main
"""
import asyncio
import os
import sys
import subprocess
from pathlib import Path
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .routers import crawler_router, data_router, websocket_router, videos_router

# Project root directory (used for running subprocesses like uv run main.py)
PROJECT_ROOT = Path(__file__).parent.parent

app = FastAPI(
    title="Video Crawler API",
    description="API điều khiển crawler và tải video đa nền tảng",
    version="1.0.0"
)

# Get webui static files directory
WEBUI_DIR = os.path.join(os.path.dirname(__file__), "webui")

# CORS configuration - allow frontend dev server access
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://localhost:3000",  # Backup port
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(crawler_router, prefix="/api")
app.include_router(data_router, prefix="/api")
app.include_router(websocket_router, prefix="/api")
app.include_router(videos_router, prefix="/api")


@app.get("/")
async def serve_frontend():
    """Return frontend page"""
    index_path = os.path.join(WEBUI_DIR, "index.html")
    if os.path.exists(index_path):
        # index.html trỏ tới bundle có hash. Không cache HTML để sau mỗi lần
        # deploy trình duyệt luôn nhận đúng tên bundle mới thay vì tiếp tục
        # chạy JavaScript cũ và báo lỗi dù API hiện tại vẫn hoạt động.
        return FileResponse(
            index_path,
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )
    return {
        "message": "Video Crawler API",
        "version": "1.0.0",
        "docs": "/docs",
        "note": "WebUI not found, please build it first: cd webui && npm run build"
    }


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.get("/api/env/check")
async def check_environment():
    """Check if MediaCrawler environment is configured correctly"""
    try:
        # Run uv run main.py --help command to check environment
        # Use PROJECT_ROOT so it works regardless of where uvicorn was started
        if sys.platform == "win32":
            loop = asyncio.get_running_loop()
            process = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["uv", "run", "main.py", "--help"],
                    capture_output=True,
                    timeout=30.0,
                    cwd=str(PROJECT_ROOT)
                )
            )
            stdout, stderr = process.stdout, process.stderr  # bytes
        else:
            process = await asyncio.create_subprocess_exec(
                "uv", "run", "main.py", "--help",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(PROJECT_ROOT)  # Project root directory
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=30.0  # 30 seconds timeout
            )
        if process.returncode == 0:
            return {
                "success": True,
                "message": "Môi trường MediaCrawler đã được cấu hình chính xác",
                "output": stdout.decode("utf-8", errors="ignore")[:500]  # Truncate to first 500 characters
            }
        else:
            error_msg = stderr.decode("utf-8", errors="ignore") or stdout.decode("utf-8", errors="ignore")
            return {
                "success": False,
                "message": "Kiểm tra môi trường thất bại",
                "error": error_msg[:500]
            }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "message": "Kiểm tra môi trường quá thời gian",
            "error": "Lệnh chạy quá 30 giây"
        }
    except FileNotFoundError:
        return {
            "success": False,
            "message": "Không tìm thấy lệnh uv",
            "error": "Hãy đảm bảo uv đã được cài đặt và có trong PATH của hệ thống"
        }
    except Exception as e:
        return {
            "success": False,
            "message": "Lỗi kiểm tra môi trường",
            "error": f"{type(e).__name__}: {str(e) or 'Không xác định'}"
        }


@app.get("/api/config/platforms")
async def get_platforms():
    """Get list of supported platforms"""
    return {
        "platforms": [
            {"value": "xhs", "label": "Tiểu Hồng Thư", "icon": "book-open"},
            {"value": "dy", "label": "Douyin (TikTok Trung Quốc)", "icon": "music"},
            {"value": "bili", "label": "Bilibili", "icon": "tv"},
        ]
    }


@app.get("/api/config/options")
async def get_config_options():
    """Get all configuration options"""
    return {
        "login_types": [
            {"value": "qrcode", "label": "Đăng nhập bằng mã QR"},
            {"value": "cookie", "label": "Đăng nhập bằng Cookie"},
        ],
        "crawler_types": [
            {"value": "search", "label": "Tìm kiếm"},
            {"value": "detail", "label": "Chi tiết bài đăng"},
            {"value": "creator", "label": "Nhà sáng tạo"},
        ],
        "save_options": [
            {"value": "jsonl", "label": "Tệp JSONL"},
            {"value": "json", "label": "Tệp JSON"},
            {"value": "csv", "label": "Tệp CSV"},
            {"value": "excel", "label": "Tệp Excel"},
            {"value": "sqlite", "label": "Cơ sở dữ liệu SQLite"},
            {"value": "db", "label": "Cơ sở dữ liệu MySQL"},
            {"value": "mongodb", "label": "Cơ sở dữ liệu MongoDB"},
        ],
    }


# Mount static resources - must be placed after all routes
if os.path.exists(WEBUI_DIR):
    assets_dir = os.path.join(WEBUI_DIR, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")
    # Mount logos directory
    logos_dir = os.path.join(WEBUI_DIR, "logos")
    if os.path.exists(logos_dir):
        app.mount("/logos", StaticFiles(directory=logos_dir), name="logos")
    # Mount other static files (e.g., vite.svg)
    app.mount("/static", StaticFiles(directory=WEBUI_DIR), name="webui-static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
