import os
import re
from pathlib import Path

from fastapi import Header, HTTPException, Query, Request, WebSocket


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACES_DIR = Path(
    os.getenv("MEDIACRAWLER_WORKSPACES_DIR", str(PROJECT_ROOT / "workspaces"))
).resolve()
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
DEFAULT_WORKSPACE_ID = "default"


def validate_workspace_id(value: str | None) -> str:
    workspace_id = (value or DEFAULT_WORKSPACE_ID).strip()
    if workspace_id == DEFAULT_WORKSPACE_ID or WORKSPACE_RE.fullmatch(workspace_id):
        return workspace_id
    raise HTTPException(400, "Workspace ID không hợp lệ")


def get_workspace_id(
    request: Request,
    x_workspace_id: str | None = Header(None, alias="X-Workspace-ID"),
    workspace_id: str | None = Query(None),
) -> str:
    return validate_workspace_id(x_workspace_id or workspace_id or request.cookies.get("mediacrawler_workspace"))


def get_websocket_workspace_id(websocket: WebSocket) -> str:
    return validate_workspace_id(
        websocket.query_params.get("workspace_id")
        or websocket.cookies.get("mediacrawler_workspace")
    )


def workspace_root(workspace_id: str) -> Path:
    validated = validate_workspace_id(workspace_id)
    # Requests from older clients keep using the historical project folders.
    root = PROJECT_ROOT if validated == DEFAULT_WORKSPACE_ID else WORKSPACES_DIR / validated
    root.mkdir(parents=True, exist_ok=True)
    return root


def workspace_data_dir(workspace_id: str) -> Path:
    path = workspace_root(workspace_id) / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_video_dir(workspace_id: str) -> Path:
    path = workspace_root(workspace_id) / "video"
    path.mkdir(parents=True, exist_ok=True)
    return path
