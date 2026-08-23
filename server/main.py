"""FastWrite 本地后端：静态服务 + 流式识别 WebSocket + 热词管理。

启动：server/.venv/Scripts/python.exe -m uvicorn main:app --port 8964 --app-dir server
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import webbrowser

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

import asr_engine
import llm_proxy

ROOT = pathlib.Path(__file__).resolve().parent.parent          # 工作区根目录
HOTWORDS_FILE = pathlib.Path(__file__).resolve().parent / "hotwords.json"
PORT = 8964

app = FastAPI(title="FastWrite local backend")

_engine_task: asyncio.Task | None = None
_engine = None            # 加载完成前为 None


def load_hotwords() -> dict:
    try:
        data = json.loads(HOTWORDS_FILE.read_text(encoding="utf-8"))
        if isinstance(data.get("words"), list):
            return data
    except Exception:
        pass
    return {"words": []}


def save_hotwords(words: list[str]) -> dict:
    words = [w.strip() for w in words if isinstance(w, str) and w.strip()]
    HOTWORDS_FILE.write_text(json.dumps({"words": words}, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return {"words": words}


@app.on_event("startup")
async def startup():
    global _engine_task
    loop = asyncio.get_running_loop()
    # 模型加载放后台线程，不阻塞 HTTP/WS 立即可用；/health 反映加载进度
    _engine_task = loop.run_in_executor(None, asr_engine.get_engine)

    async def _open_browser_when_ready():
        engine = await asyncio.wrap_future(_engine_task)
        webbrowser.open(f"http://localhost:{PORT}/")
        return engine

    asyncio.create_task(_open_browser_when_ready())


@app.get("/")
async def index():
    # no-store：禁止浏览器缓存入口页，避免旧的 FastWrite.html 副本被缓存导致「打开还是旧界面」
    return FileResponse(ROOT / "Handheld.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/health")
async def health():
    ready = _engine_task is not None and _engine_task.done()
    return {"status": "ok", "engine": "funasr", "ready": ready,
            "llm": llm_proxy.load_config() is not None}


@app.get("/hotwords")
async def get_hotwords():
    return load_hotwords()


@app.post("/hotwords")
async def set_hotwords(body: dict):
    words = body.get("words")
    if not isinstance(words, list):
        return JSONResponse(status_code=400, content={"error": "body must be {words: [...]}"})
    return save_hotwords(words)


@app.post("/rewrite")
async def rewrite(body: dict):
    text = body.get("text")
    mode = body.get("mode", "polish")
    hotwords = body.get("hotwords") or load_hotwords()["words"]
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(status_code=400, content={"error": "body must be {text: ...}"})
    # 同步网络调用放线程池，避免阻塞事件循环
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, llm_proxy.rewrite, text, mode, hotwords)


@app.websocket("/ws/asr")
async def ws_asr(ws: WebSocket):
    global _engine
    await ws.accept()
    if _engine is None:
        _engine = await asyncio.wrap_future(_engine_task)   # 等模型加载完成

    hotwords = load_hotwords()["words"]
    try:
        init_raw = await asyncio.wait_for(ws.receive_text(), timeout=5)
        init = json.loads(init_raw)
        if isinstance(init.get("hotwords"), list) and init["hotwords"]:
            hotwords = init["hotwords"]
    except Exception:
        pass  # 没有 init 消息也继续，直接收音频

    session = asr_engine.Session(_engine, hotwords)
    await ws.send_text(json.dumps({"type": "ready", "hotwords": len(hotwords)},
                                  ensure_ascii=False))

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("text") or msg.get("bytes")
            if data is None:
                continue
            if isinstance(data, str):
                ctrl = json.loads(data)
                if ctrl.get("type") == "stop":
                    loop = asyncio.get_running_loop()
                    for m in await loop.run_in_executor(None, session.finish):
                        await ws.send_text(json.dumps(m, ensure_ascii=False))
                    await ws.send_text(json.dumps({"type": "done"}))
                    break
                continue
            # 二进制帧 = PCM；识别在线程池执行，避免阻塞事件循环
            loop = asyncio.get_running_loop()
            for m in await loop.run_in_executor(None, session.feed, data):
                await ws.send_text(json.dumps(m, ensure_ascii=False))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
