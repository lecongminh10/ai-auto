# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/routers/websocket.py
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

import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services import get_crawler_manager
from ..workspace import get_websocket_workspace_id

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    """Stream only logs belonging to the requesting browser workspace."""
    workspace_id = get_websocket_workspace_id(websocket)
    crawler = get_crawler_manager(workspace_id)
    await websocket.accept()
    last_log_id = 0
    try:
        while True:
            for log in crawler.logs:
                if log.id <= last_log_id:
                    continue
                await websocket.send_json(log.model_dump())
                last_log_id = log.id
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=0.5)
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@router.websocket("/ws/status")
async def websocket_status(websocket: WebSocket):
    """WebSocket status stream"""
    workspace_id = get_websocket_workspace_id(websocket)
    crawler = get_crawler_manager(workspace_id)
    await websocket.accept()

    try:
        while True:
            # Send status every second
            status = crawler.get_status()
            await websocket.send_json(status)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
