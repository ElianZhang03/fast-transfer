"""
Fast Transfer — LAN messages & file sharing (FastAPI + desktop)
Developer: Zx
"""
from __future__ import annotations

import io
import itertools
import locale
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote
from pathlib import Path

import qrcode
import uvicorn
import webview
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

PORT = 8000

uvicorn_server: uvicorn.Server | None = None


def bundle_resource_abspath(*relative_parts: str) -> Path:
    """
    打包资源绝对路径（PyInstaller 单文件/单目录均会设置 sys._MEIPASS）。
    - frozen：根目录为解压/应用包内的 _MEIPASS
    - 源码运行：根目录为 main.py 所在目录
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            base = Path(meipass)
        else:
            base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*relative_parts).resolve()


def _data_dir() -> Path:
    """可写目录：源码旁或 exe 同目录（downloads）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DATA_DIR = _data_dir()
DOWNLOADS_DIR = APP_DATA_DIR / "downloads"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def get_lan_ipv4() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def current_lan_url() -> str:
    return f"http://{get_lan_ipv4()}:{PORT}"


def ui_is_chinese() -> bool:
    """与前端 navigator.language 规则一致：语言标签以 zh 开头视为中文 UI。"""
    tags: list[str] = []
    try:
        t = locale.getdefaultlocale()[0]
        if t:
            tags.append(str(t))
    except Exception:
        pass
    try:
        t = locale.getlocale()[0]
        if t:
            tags.append(str(t))
    except Exception:
        pass
    for tag in tags:
        if tag.lower().startswith("zh"):
            return True
    return False


def _loc(zh: str, en: str) -> str:
    return zh if ui_is_chinese() else en


app = FastAPI(
    title="Fast Transfer",
    description=_loc("局域网文本消息与文件互传", "LAN text stream & shared files"),
)


class MessageCreateBody(BaseModel):
    text: str


messages_lock = threading.Lock()
messages: list[dict] = []
message_id_seq = itertools.count(1)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def lang_from_accept_language(accept: str | None) -> str:
    """按 HTTP Accept-Language 首选语言判断（手机浏览器会带系统语言）。"""
    if not accept:
        return "en"
    for part in accept.split(","):
        code = part.split(";")[0].strip().lower()
        if not code:
            continue
        if code.startswith("zh"):
            return "zh"
        return "en"
    return "en"


@app.get("/api/locale")
async def api_locale(request: Request) -> JSONResponse:
    accept = request.headers.get("accept-language")
    return JSONResponse({"lang": lang_from_accept_language(accept)})


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    index_path = bundle_resource_abspath("index.html")
    if not index_path.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"index.html 缺失（已查找: {index_path}）",
        )
    return index_path.read_text(encoding="utf-8")


@app.get("/api/server-info")
async def server_info() -> JSONResponse:
    return JSONResponse({"lan_url": current_lan_url()})


@app.get("/api/qr")
async def qr_png() -> Response:
    img = qrcode.make(current_lan_url(), box_size=5, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/api/messages")
async def get_messages() -> JSONResponse:
    with messages_lock:
        return JSONResponse({"messages": list(messages)})


@app.post("/api/messages")
async def post_message(body: MessageCreateBody) -> JSONResponse:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息不能为空")
    msg = {"id": next(message_id_seq), "text": text, "timestamp": now_iso()}
    with messages_lock:
        messages.append(msg)
        snapshot = list(messages)
    return JSONResponse({"ok": True, "message": msg, "messages": snapshot})


@app.delete("/api/messages/{message_id}")
async def delete_message(message_id: int) -> JSONResponse:
    with messages_lock:
        before = len(messages)
        messages[:] = [m for m in messages if int(m.get("id", -1)) != message_id]
        after = len(messages)
    if before == after:
        raise HTTPException(status_code=404, detail="消息不存在")
    return JSONResponse({"ok": True})


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> JSONResponse:
    if not files:
        raise HTTPException(status_code=400, detail="未选择文件")
    saved: list[str] = []
    for f in files:
        name = f.filename or "unnamed"
        safe = Path(name).name
        if not safe:
            safe = "unnamed"
        dest = DOWNLOADS_DIR / safe
        content = await f.read()
        dest.write_bytes(content)
        saved.append(safe)
    return JSONResponse({"ok": True, "files": saved})


@app.get("/api/files")
async def list_files() -> JSONResponse:
    items: list[dict] = []
    try:
        paths = list(DOWNLOADS_DIR.iterdir())
    except FileNotFoundError:
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        paths = []

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    for p in sorted(paths, key=_mtime, reverse=True):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        ext = (p.suffix[1:] if p.suffix.startswith(".") else p.suffix).lower()
        url_name = quote(p.name)
        items.append(
            {
                "name": p.name,
                "size": stat.st_size,
                "ext": ext,
                "url": f"/downloads/{url_name}",
            }
        )
    return JSONResponse({"files": items})


app.mount("/downloads", StaticFiles(directory=str(DOWNLOADS_DIR)), name="downloads")


def desktop_dir() -> Path:
    home = Path.home()
    d = home / "Desktop"
    return d if d.is_dir() else home


def unique_dest_path(dest_dir: Path, filename: str) -> Path:
    base = Path(filename).name or "file"
    stem = Path(base).stem or "file"
    suffix = Path(base).suffix
    cand = dest_dir / (stem + suffix)
    i = 1
    while cand.exists():
        cand = dest_dir / f"{stem} ({i}){suffix}"
        i += 1
    return cand


class DesktopApi:
    def save_with_dialog(self, name: str) -> dict:
        safe = Path(name).name
        src = DOWNLOADS_DIR / safe
        if not src.is_file():
            return {"ok": False, "error": _loc("文件不存在", "File not found")}

        try:
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return {"ok": False, "error": _loc("窗口未就绪", "Window not ready")}
            picked = win.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(desktop_dir()),
                save_filename=safe,
            )
        except Exception as e:
            return {
                "ok": False,
                "error": _loc(f"无法打开保存对话框：{e}", f"Could not open save dialog: {e}"),
            }

        if not picked:
            return {"ok": False, "cancelled": True}

        dest_str = picked[0] if isinstance(picked, (list, tuple)) else picked
        dest = Path(dest_str)
        if dest.is_dir():
            dest = unique_dest_path(dest, safe)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": str(dest), "dir": str(dest.parent)}


def wait_for_server(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{PORT}/api/server-info"
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.35)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.05)
    raise RuntimeError(f"本地服务在 {timeout:.0f} 秒内未就绪，请检查端口 {PORT} 是否被占用。")


def run_desktop() -> None:
    global uvicorn_server
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="warning",
    )
    uvicorn_server = uvicorn.Server(config)
    server_thread = threading.Thread(target=uvicorn_server.run, daemon=False, name="uvicorn")
    server_thread.start()
    wait_for_server()

    win_title = _loc("Fast Transfer 快传", "Fast Transfer")
    window = webview.create_window(
        win_title,
        f"http://127.0.0.1:{PORT}/",
        width=440,
        height=820,
        min_size=(380, 640),
        js_api=DesktopApi(),
    )

    def on_closing() -> None:
        srv = uvicorn_server
        if srv is not None:
            srv.should_exit = True

    window.events.closing += on_closing
    webview.start()
    if uvicorn_server is not None:
        uvicorn_server.should_exit = True
    server_thread.join(timeout=6.0)


if __name__ == "__main__":
    run_desktop()
