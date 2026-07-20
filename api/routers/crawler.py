# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/routers/crawler.py
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

from pathlib import Path
import shutil

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response

from ..schemas import CrawlerStartRequest, CrawlerStatusResponse
from ..services import crawler_manager, get_crawler_manager
from ..workspace import get_workspace_id, workspace_root

router = APIRouter(prefix="/crawler", tags=["crawler"])
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _manager(workspace_id: str):
    return crawler_manager if workspace_id == "default" else get_crawler_manager(workspace_id)


@router.post("/start")
async def start_crawler(
    request: CrawlerStartRequest,
    workspace_id: Annotated[str, Depends(get_workspace_id)],
):
    """Start crawler task"""
    manager = _manager(workspace_id)
    success = await manager.start(request)
    if not success:
        # Handle concurrent/duplicate requests: if process is already running, return 400 instead of 500
        if manager.process and manager.process.poll() is None:
            raise HTTPException(status_code=400, detail="Crawler is already running")
        raise HTTPException(status_code=500, detail="Failed to start crawler")

    return {"status": "ok", "message": "Crawler started successfully"}


@router.post("/stop")
async def stop_crawler(workspace_id: Annotated[str, Depends(get_workspace_id)]):
    """Stop crawler task"""
    manager = _manager(workspace_id)
    success = await manager.stop()
    if not success:
        # Handle concurrent/duplicate requests: if process already exited/doesn't exist, return 400 instead of 500
        if not manager.process or manager.process.poll() is not None:
            raise HTTPException(status_code=400, detail="No crawler is running")
        raise HTTPException(status_code=500, detail="Failed to stop crawler")

    return {"status": "ok", "message": "Crawler stopped successfully"}


@router.get("/status", response_model=CrawlerStatusResponse)
async def get_crawler_status(workspace_id: Annotated[str, Depends(get_workspace_id)]):
    """Get crawler status"""
    return _manager(workspace_id).get_status()


@router.get("/logs")
async def get_logs(
    workspace_id: Annotated[str, Depends(get_workspace_id)],
    limit: int = 100,
):
    """Get recent logs"""
    manager = _manager(workspace_id)
    logs = manager.logs[-limit:] if limit > 0 else manager.logs
    return {"logs": [log.model_dump() for log in logs]}


@router.get("/login-qrcode")
async def get_login_qrcode(workspace_id: Annotated[str, Depends(get_workspace_id)]):
    """QR đăng nhập Douyin hiện tại; chỉ tồn tại trong lúc chờ quét."""
    qr_path = workspace_root(workspace_id) / "browser_data" / "douyin_login_qr.png"
    if not qr_path.exists():
        # Frontend polls while Chromium is starting; an absent QR is an
        # expected temporary state, not a missing API route.
        return Response(status_code=204)
    return FileResponse(
        qr_path,
        media_type="image/png",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/auth-status")
async def get_auth_status(workspace_id: Annotated[str, Depends(get_workspace_id)]):
    marker = workspace_root(workspace_id) / "browser_data" / "douyin_authenticated"
    return {"platform": "dy", "authenticated": marker.exists()}


@router.post("/logout")
async def reset_workspace_login(workspace_id: Annotated[str, Depends(get_workspace_id)]):
    manager = _manager(workspace_id)
    if manager.process and manager.process.poll() is None:
        raise HTTPException(409, "Hãy dừng crawler trước khi đổi tài khoản Douyin")
    browser_data = workspace_root(workspace_id) / "browser_data"
    if browser_data.exists():
        shutil.rmtree(browser_data)
    browser_data.mkdir(parents=True, exist_ok=True)
    return {"success": True, "authenticated": False}
