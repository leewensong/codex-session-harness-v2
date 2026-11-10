"""流式语音 demo 服务端：sherpa-onnx 流式 ASR + 流式 TTS。

用法:
    pip install -r requirements.txt
    ./start-service.ps1              # 后台启动，http://127.0.0.1:8050
    ./start-service.ps1 -Foreground   # 前台启动并查看日志

首次启动会自动下载模型：
  - ASR: sherpa-onnx-streaming-paraformer-bilingual-zh-en (int8, ~230MB)
  - TTS: vits-melo-tts-zh_en (~163MB)
  - 标点: sherpa-onnx-punct-ct-transformer-zh-en (int8, ~72MB)

浏览器交互:
  按住「说话」-> 实时流式转写(partial) -> 松手出最终文本(final，自动加标点)
  -> 自动(或手动)发起流式合成，边合成边播放。
  勾选「实时跟读」后，partial 每增长约 6 字符就增量合成并播放，边说边播。
  勾选「AI 对话」后，松手自动把 final 发给 Ollama 本地模型，回复边生成边按句朗读。

协议(单个 WebSocket /ws):
  客户端 -> 服务端:
    binary  = Int16 PCM 16kHz 单声道麦克风数据
    JSON    = {"type":"finalize"} / {"type":"speak","text":...} / {"type":"stop_speak"}
              / {"type":"echo","on":bool} / {"type":"llm","on":bool}
              / {"type":"chat","text":...} / {"type":"reset"}
              / {"type":"llm.context","context":...}
              / {"type":"ollama.models.list"}
              / {"type":"ollama.model.set","model":...}
              / {"type":"codex.sessions.list"} / {"type":"codex.session.create","cwd":...,"project":...}
              / {"type":"codex.session.read","thread_id":...}
              / {"type":"codex.session.send","thread_id":...,"text":...}
              / {"type":"codex.reply.summarize","thread_id":...,"text":...}
  服务端 -> 客户端:
    binary  = Int16 PCM 44100Hz 单声道 TTS 音频块
    JSON    = {"type":"asr.partial","text":...} / {"type":"asr.final","text":...}
              / {"type":"tts.started"} / {"type":"tts.done"}
              / {"type":"llm.started"} / {"type":"llm.partial","text":...}
              / {"type":"llm.done","text":...} / {"type":"llm.error","message":...}
              / {"type":"ollama.models","models":[...],"current":...}
              / {"type":"ollama.model.changed","model":...}
              / {"type":"codex.session.created","session":{...}}
              / {"type":"codex.reply.summary","thread_id":...,"text":...}
"""

import argparse
import asyncio
import json
import logging
import ntpath
import pathlib
import queue
import shutil
import ssl
import tarfile
import time
import threading
import urllib.request

import aiohttp
import numpy as np
import sherpa_onnx
from aiohttp import web

from llm_agent import LLMError, check_models
from harness import store
from harness.agent import HarnessAgent, summarize_codex_reply_for_speech
from harness.codex_client import CodexMcp
import certs as local_certs
import media

BASE = pathlib.Path(__file__).resolve().parent
MODELS_DIR = BASE / "models"
INDEX_FILE = BASE / "index.html"
INSTALL_FILE = BASE / "install.html"
AVATAR_FILE = BASE / "avatar.html"

ASR_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2"
ASR_DIRNAME = "sherpa-onnx-streaming-paraformer-bilingual-zh-en"

TTS_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2"
TTS_DIRNAME = "vits-melo-tts-zh_en"

PUNCT_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/punctuation-models/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8.tar.bz2"
PUNCT_DIRNAME = "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8"

MEDIA_SHOT_DIR = media.SHOT_DIR
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
_harness_connections: dict[web.WebSocketResponse, HarnessAgent] = {}

async def call_codex_json(name: str, args: dict, timeout_seconds: float = 20) -> dict:
    """Call the bridge without truncating its JSON response for the browser UI."""
    raw = ""
    try:
        raw = await mcp_client.call_tool(
            name,
            args,
            result_cap=None,
            timeout_seconds=timeout_seconds,
        )
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        detail = raw.strip() if isinstance(raw, str) else ""
        logging.warning("[codex-ui] invalid %s response: %s; raw=%r", name, exc, detail[:2000])
        return {
            "ok": False,
            "error": detail[:1200] or "Codex bridge returned an invalid response.",
        }
    if not isinstance(data, dict):
        logging.warning("[codex-ui] unexpected %s response type: %s", name, type(data).__name__)
        return {"ok": False, "error": "Codex bridge returned an unexpected response."}
    return data


async def list_all_codex_sessions() -> dict:
    """Fetch every page exposed by the bridge, preserving the desktop ordering."""
    sessions = []
    cursor = None
    for _ in range(100):
        args = {"limit": 200}
        if cursor:
            args["cursor"] = cursor
        result = await call_codex_json("codex_list_sessions", args, timeout_seconds=30)
        if not result.get("ok"):
            return result
        sessions.extend(result.get("sessions") or [])
        cursor = result.get("nextCursor")
        if not cursor:
            return {"ok": True, "sessions": sessions}
    return {"ok": False, "error": "Too many session pages to display."}


def _project_name(session: dict) -> str:
    cwd = session.get("cwd")
    if not cwd:
        return "未指定项目"
    return session.get("project") or ntpath.basename(str(cwd).rstrip("\\/")) or str(cwd)


def _enrich_codex_sessions(sessions: list[dict]) -> list[dict]:
    enriched = []
    for session in sessions:
        if not isinstance(session, dict) or not session.get("id"):
            continue
        item = dict(session)
        item["project"] = _project_name(item)
        enriched.append(item)
    return enriched


async def _send_codex_event(ws: web.WebSocketResponse, event: dict) -> bool:
    if ws.closed:
        return False
    try:
        await ws.send_str(json.dumps(event, ensure_ascii=False))
    except (ConnectionResetError, RuntimeError):
        return False
    return True


async def list_ollama_models() -> list[dict]:
    """Return the models installed in the local Ollama instance.

    The browser deliberately talks to this service over the existing WebSocket
    instead of calling Ollama directly.  That keeps the Ollama endpoint local
    and gives every connected UI the same view of the installed models.
    """
    timeout = aiohttp.ClientTimeout(total=8, connect=3)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(OLLAMA_TAGS_URL) as response:
            if response.status != 200:
                body = (await response.text())[:300]
                raise RuntimeError(f"Ollama HTTP {response.status}: {body}")
            data = await response.json()

    models = []
    raw_models = data.get("models", []) if isinstance(data, dict) else []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or raw.get("model") or "").strip()
        if not name:
            continue
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        models.append({
            "name": name,
            "size": size,
            "modifiedAt": str(raw.get("modified_at") or ""),
            "family": str(details.get("family") or ""),
            "parameterSize": str(details.get("parameter_size") or ""),
            "quantization": str(details.get("quantization_level") or ""),
        })
    models.sort(key=lambda item: item["name"].lower())
    return models


async def send_ollama_models(ws: web.WebSocketResponse):
    """Send the current Ollama inventory to one browser connection."""
    try:
        models = await list_ollama_models()
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        logging.warning("[ollama-ui] list models failed: %s", exc)
        await _send_codex_event(ws, {
            "type": "ollama.error",
            "action": "list",
            "message": f"无法读取 Ollama 模型列表：{exc}",
        })
        return
    await _send_codex_event(ws, {
        "type": "ollama.models",
        "models": models,
        "current": llm_model,
    })


async def set_ollama_model(ws: web.WebSocketResponse, requested_model):
    """Validate and apply a service-wide Ollama model selection."""
    global llm_model
    model = str(requested_model or "").strip()
    if not model:
        await _send_codex_event(ws, {
            "type": "ollama.error",
            "action": "set",
            "message": "模型名称不能为空。",
        })
        return

    try:
        models = await list_ollama_models()
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        logging.warning("[ollama-ui] validate model failed: %s", exc)
        await _send_codex_event(ws, {
            "type": "ollama.error",
            "action": "set",
            "message": f"无法验证 Ollama 模型：{exc}",
        })
        return

    available = {item["name"] for item in models}
    if model not in available:
        await _send_codex_event(ws, {
            "type": "ollama.error",
            "action": "set",
            "message": f"模型不存在或尚未安装：{model}",
            "current": llm_model,
        })
        return

    previous = llm_model
    llm_model = model
    # Existing sockets own their HarnessAgent, so update those instances as
    # well as the global default used by future connections.
    for agent in list(_harness_connections.values()):
        agent.model = model
    logging.info("[ollama-ui] model changed %s -> %s", previous, model)

    event = {
        "type": "ollama.model.changed",
        "model": model,
        "previous": previous,
    }
    for connection in list(_harness_connections):
        await _send_codex_event(connection, event)


async def _refresh_codex_sessions(ws: web.WebSocketResponse):
    result = await list_all_codex_sessions()
    if result.get("ok"):
        sessions = _enrich_codex_sessions(result.get("sessions") or [])
        await asyncio.to_thread(store.save_codex_sessions, sessions)
        await _send_codex_event(ws, {
            "type": "codex.sessions",
            "sessions": sessions,
            "cached": False,
        })
        return
    await _send_codex_event(ws, {
        "type": "codex.error",
        "action": "list",
        "background": True,
        "message": result.get("error", "无法刷新 Codex 会话。"),
    })


async def _refresh_codex_session(ws: web.WebSocketResponse, thread_id: str):
    result = await call_codex_json("codex_read_session", {
        "thread_id": thread_id,
        "max_messages": 12,
    })
    if result.get("ok"):
        result["project"] = _project_name(result)
        await asyncio.to_thread(store.save_codex_session, result)
        await _send_codex_event(ws, {"type": "codex.session", **result, "cached": False})
        return
    await _send_codex_event(ws, {
        "type": "codex.error",
        "action": "read",
        "thread_id": thread_id,
        "background": True,
        "message": result.get("error", "无法刷新该会话。"),
    })


def _normalized_project_path(value) -> str:
    raw = str(value or "").strip()
    return ntpath.normcase(ntpath.normpath(raw)) if raw else ""


async def _create_codex_session(ws: web.WebSocketResponse, cwd, requested_project):
    """Create an empty Codex thread in a project already visible to this UI."""
    requested_cwd = str(cwd or "").strip()
    project = str(requested_project or "").strip()
    normalized_cwd = _normalized_project_path(requested_cwd)
    if not normalized_cwd or not project or project == "全部项目":
        await _send_codex_event(ws, {
            "type": "codex.error",
            "action": "create",
            "message": "请先在项目下拉框中选择一个明确的项目。",
        })
        return

    listed = await list_all_codex_sessions()
    if not listed.get("ok"):
        await _send_codex_event(ws, {
            "type": "codex.error",
            "action": "create",
            "message": listed.get("error", "无法验证 Codex 项目目录。"),
        })
        return

    matching = []
    for raw in listed.get("sessions") or []:
        if not isinstance(raw, dict):
            continue
        session_cwd = str(raw.get("cwd") or "").strip()
        if _normalized_project_path(session_cwd) != normalized_cwd:
            continue
        if _project_name(raw).casefold() != project.casefold():
            continue
        matching.append(raw)
    if not matching:
        await _send_codex_event(ws, {
            "type": "codex.error",
            "action": "create",
            "message": "该项目目录不在当前 Codex 会话列表中，请刷新项目后重试。",
        })
        return

    actual_cwd = str(matching[0].get("cwd") or requested_cwd)
    if not await asyncio.to_thread(pathlib.Path(actual_cwd).is_dir):
        await _send_codex_event(ws, {
            "type": "codex.error",
            "action": "create",
            "message": f"项目目录不存在：{actual_cwd}",
        })
        return

    name = f"新会话 {time.strftime('%m-%d %H:%M')}"
    result = await call_codex_json(
        "codex_start_session",
        {"cwd": actual_cwd, "name": name},
        timeout_seconds=30,
    )
    thread_id = str(result.get("threadId") or "").strip()
    if not result.get("ok") or not thread_id:
        await _send_codex_event(ws, {
            "type": "codex.error",
            "action": "create",
            "message": result.get("error", "Codex MCP 未返回新会话 ID。"),
        })
        return

    session = {
        "id": thread_id,
        "name": result.get("name") or name,
        "cwd": result.get("cwd") or actual_cwd,
        "project": project,
        "status": result.get("status") or "idle",
        "updatedAt": int(time.time()),
        "preview": "",
    }
    cached = [
        item for item in await asyncio.to_thread(store.load_codex_sessions)
        if isinstance(item, dict) and item.get("id") != thread_id
    ]
    await asyncio.to_thread(store.save_codex_sessions, [session, *cached])
    await _send_codex_event(ws, {
        "type": "codex.session.created",
        "session": session,
    })


CODEX_STREAM_POLL_SECONDS = 0.8
CODEX_SPEECH_WINDOW_SECONDS = 5.0


async def _start_codex_stream(ws: web.WebSocketResponse, thread_id: str, text: str):
    result = await call_codex_json("codex_start_stream_message", {
        "thread_id": thread_id,
        "message": text,
    }, timeout_seconds=30)
    await _send_codex_event(ws, {
        "type": "codex.session.sent",
        "thread_id": thread_id,
        "result": result,
    })
    if result.get("ok"):
        stream_id = result.get("streamId")
        await _send_codex_event(ws, {
            "type": "codex.reply.stream.started",
            "thread_id": thread_id,
            "stream_id": stream_id,
        })
        if stream_id:
            await _stream_codex_reply(ws, thread_id, stream_id)


async def _summarize_codex_reply(
    ws: web.WebSocketResponse,
    thread_id: str,
    text: str,
    *,
    incremental: bool = False,
    final: bool = True,
):
    await _send_codex_event(ws, {
        "type": "codex.reply.summarizing",
        "thread_id": thread_id,
        "streaming": incremental,
        "final": final,
    })
    summary = await summarize_codex_reply_for_speech(
        llm_model,
        thread_id,
        text,
        incremental=incremental,
    )
    await _send_codex_event(ws, {
        "type": "codex.reply.summary",
        "thread_id": thread_id,
        "text": summary,
        "streaming": incremental,
        "final": final,
    })


async def _stream_codex_reply(ws: web.WebSocketResponse, thread_id: str, stream_id: str):
    """Poll bridge deltas and summarize them in five-second speech windows."""
    cursor = 0
    full_text = ""
    buffered = ""
    buffer_started_at = None
    last_summary_at = None

    while not ws.closed:
        result = await call_codex_json(
            "codex_poll_stream",
            {"stream_id": stream_id, "cursor": cursor},
            timeout_seconds=15,
        )
        if not result.get("ok"):
            await _send_codex_event(ws, {
                "type": "codex.error",
                "action": "stream",
                "thread_id": thread_id,
                "message": result.get("error", "无法读取 Codex 流式回复。"),
            })
            return

        chunks = result.get("chunks") or []
        cursor = int(result.get("nextCursor") or cursor)
        for chunk in chunks:
            delta = chunk.get("text") if isinstance(chunk, dict) else str(chunk or "")
            if not delta:
                continue
            full_text += delta
            buffered += delta
            if buffer_started_at is None:
                buffer_started_at = time.monotonic()
            await _send_codex_event(ws, {
                "type": "codex.reply.delta",
                "thread_id": thread_id,
                "stream_id": stream_id,
                "text": delta,
                "full_text": full_text[-40000:],
            })

        status = str(result.get("status") or "running")
        terminal = status not in {"starting", "running"}
        now = time.monotonic()
        gap_ready = last_summary_at is None or now - last_summary_at >= CODEX_SPEECH_WINDOW_SECONDS
        window_ready = buffer_started_at is not None and now - buffer_started_at >= CODEX_SPEECH_WINDOW_SECONDS
        if buffered and gap_ready and (window_ready or terminal):
            piece = buffered
            buffered = ""
            buffer_started_at = None
            await _summarize_codex_reply(
                ws,
                thread_id,
                piece,
                incremental=not terminal,
                final=terminal,
            )
            last_summary_at = time.monotonic()

        if terminal and not buffered:
            await _refresh_codex_session(ws, thread_id)
            await _send_codex_event(ws, {
                "type": "codex.reply.stream.done",
                "thread_id": thread_id,
                "stream_id": stream_id,
                "status": status,
            })
            return
        await asyncio.sleep(CODEX_STREAM_POLL_SECONDS)


def ensure_model(url: str, dirname: str) -> pathlib.Path:
    folder = MODELS_DIR / dirname
    if folder.is_dir():
        return folder
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    archive = MODELS_DIR / f"{dirname}.tar.bz2"
    if not archive.exists():
        logging.info("Downloading model %s ...", url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        tmp = archive.with_suffix(".part")
        with urllib.request.urlopen(req) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        tmp.rename(archive)
    logging.info("Extracting %s ...", archive)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(MODELS_DIR)
    return folder


def load_models():
    asr_dir = ensure_model(ASR_URL, ASR_DIRNAME)
    recognizer = sherpa_onnx.OnlineRecognizer.from_paraformer(
        tokens=str(asr_dir / "tokens.txt"),
        encoder=str(asr_dir / "encoder.int8.onnx"),
        decoder=str(asr_dir / "decoder.int8.onnx"),
        num_threads=2,
        sample_rate=16000,
        feature_dim=80,
        # 按住说话模式：关闭端点检测，只在松手(finalize)时出最终结果。
        # 否则说话停顿 0.6s 就会被误判为说完而提前截断。
        enable_endpoint_detection=False,
    )
    tts_dir = ensure_model(TTS_URL, TTS_DIRNAME)
    tts = sherpa_onnx.OfflineTts(
        sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(tts_dir / "model.onnx"),
                    lexicon=str(tts_dir / "lexicon.txt"),
                    tokens=str(tts_dir / "tokens.txt"),
                ),
                num_threads=2,
            ),
            rule_fsts=",".join(str(tts_dir / f) for f in ("date.fst", "number.fst")),
            max_num_sentences=1,
        )
    )
    punct_dir = ensure_model(PUNCT_URL, PUNCT_DIRNAME)
    punct = sherpa_onnx.OfflinePunctuation(
        sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(
                ct_transformer=str(punct_dir / "model.int8.onnx"),
            ),
        )
    )
    return recognizer, tts, punct


class TTSJob:
    """在 C++ 回调线程里收音频块，由 async 发送任务取出并通过 WebSocket 发出。"""

    def __init__(self):
        self.q = queue.Queue()  # bytes 或 None(结束)
        self.stop = threading.Event()

    def callback(self, samples: np.ndarray, progress: float):
        if self.stop.is_set():
            return 0
        arr = np.clip(samples, -1.0, 1.0)
        self.q.put((arr * 32767.0).astype(np.int16).tobytes())
        return 1


async def run_tts(ws: web.WebSocketResponse, job: TTSJob, text: str):
    await ws.send_str(json.dumps({"type": "tts.started"}))

    def synth():
        try:
            gen = sherpa_onnx.GenerationConfig()
            tts.generate(text, gen, callback=job.callback)
        except Exception as e:  # noqa: BLE001 - 让前端感知结束
            logging.warning("TTS synthesis failed: %s", e)
        finally:
            job.q.put(None)

    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(None, synth)
    try:
        while True:
            chunk = await asyncio.to_thread(job.q.get)
            if chunk is None or job.stop.is_set():
                break
            await ws.send_bytes(chunk)
    finally:
        if not task.done():
            task.cancel()
        try:
            await ws.send_str(json.dumps({"type": "tts.done"}))
        except Exception:  # noqa: BLE001 - 连接可能已关闭
            pass


async def persist_chat(role: str, content: str):
    """Best-effort chat persistence; a locked history file must not block a reply."""
    for attempt in range(3):
        try:
            await asyncio.to_thread(store.append_chat, role, content)
            return
        except OSError as e:
            if attempt == 2:
                logging.warning("Chat history was not saved (%s): %s", role, e)
                return
            await asyncio.sleep(0.1 * (attempt + 1))


async def run_reply(ws: web.WebSocketResponse, agent, user_text: str):
    """LLM 对话管线：追加历史 → 流式生成 → 逐句合成朗读 → 事件回传。"""
    await ws.send_str(json.dumps({"type": "llm.started"}))
    await ws.send_str(json.dumps({"type": "llm.status", "text": "正在调用本地模型…"}))
    full = ""

    async def on_status(text: str):
        if not ws.closed:
            await ws.send_str(json.dumps({"type": "llm.status", "text": text}))

    try:
        await persist_chat("user", user_text)
        async for chunk in agent.stream(user_text, on_status=on_status):
            full += chunk
            await ws.send_str(json.dumps({"type": "llm.partial", "text": full}))
            job = TTSJob()
            if agent.reply_job is not None:
                agent.reply_job.stop.set()
            agent.reply_job = job
            await run_tts(ws, job, chunk)
        if full.strip():
            await persist_chat("assistant", full)
        await ws.send_str(json.dumps({"type": "llm.done", "text": full}))
        media_events = getattr(agent, "media_events", None)
        if media_events:
            await ws.send_str(json.dumps({"type": "media", "items": media_events}))
    except asyncio.CancelledError:
        raise
    except LLMError as e:
        logging.warning("LLM error: %s", e)
        await ws.send_str(json.dumps({"type": "llm.error", "message": str(e)}))
    except Exception as e:  # noqa: BLE001 - 让前端感知结束
        logging.exception("LLM reply failed: %s", e)
        await ws.send_str(json.dumps({"type": "llm.error", "message": "回复生成失败，请稍后再试。"}))
    finally:
        agent.reply_job = None


ECHO_CHUNK = 6  # 实时跟读：partial 每增长这么多字符就合成一次
ECHO_MAX_QUEUE = 4  # 队列上限，防止合成跟不上说话而无限积压


class EchoState:
    """实时跟读（echo）模式：把 partial 的增量文本排队交给 worker 流式合成。"""

    def __init__(self):
        self.on = False
        self.spoken_len = 0  # 已交给合成的 partial 长度
        self.q = queue.Queue()  # 当前 worker 的队列
        self.workers = []  # [(task, queue)]，每个 worker 持有自己的队列，避免开关竞态
        self.current_job = None


async def echo_worker(ws: web.WebSocketResponse, state: EchoState, q: queue.Queue):
    while True:
        text = await asyncio.to_thread(q.get)
        if text is None:
            break
        state.current_job = TTSJob()
        await run_tts(ws, state.current_job, text)
        state.current_job = None


def push_echo(state: EchoState, text: str):
    if state.q.qsize() >= ECHO_MAX_QUEUE:
        try:
            state.q.get_nowait()  # 丢弃最旧，保证跟读贴近最新
        except queue.Empty:
            pass
    state.q.put(text)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    stream = recognizer.create_stream()
    state = EchoState()
    agent = HarnessAgent(llm_model, mcp=mcp_client)
    history = agent.load_recent_chat(store.CHAT_LOAD)
    await ws.send_str(json.dumps({
        "type": "hello",
        "model": llm_model,
    }, ensure_ascii=False))
    await ws.send_str(json.dumps({"type": "history", "messages": history}))
    logging.info("[ws] connected model=%s history=%d", llm_model, len(history))
    last_partial = ""
    job = None
    tts_task = None
    reply_task = None
    codex_tasks: set[asyncio.Task] = set()
    codex_list_task = None

    def spawn_codex(coro):
        task = asyncio.create_task(coro)
        codex_tasks.add(task)
        task.add_done_callback(codex_tasks.discard)
        return task

    async def start_reply(user_text: str):
        nonlocal reply_task
        await cancel_reply()
        reply_task = asyncio.create_task(run_reply(ws, agent, user_text))

    async def cancel_reply():
        nonlocal reply_task
        if agent.reply_job is not None:
            agent.reply_job.stop.set()
        if reply_task is not None:
            task, reply_task = reply_task, None
            task.cancel()
            # 等旧任务收尾（含 finally 清理 reply_job），避免新任务启动后误清
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    _harness_connections[ws] = agent
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                samples = np.frombuffer(msg.data, dtype=np.int16).astype(np.float32) / 32768.0
                if samples.size == 0:
                    continue
                stream.accept_waveform(16000, samples)
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                partial = recognizer.get_result(stream).strip()
                if partial != last_partial:
                    last_partial = partial
                    await ws.send_str(json.dumps({"type": "asr.partial", "text": partial}))
                    if state.on:
                        if len(partial) < state.spoken_len:
                            state.spoken_len = len(partial)  # 识别修订导致变短，跳过已说部分
                        delta = partial[state.spoken_len:]
                        if len(delta) >= ECHO_CHUNK:
                            state.spoken_len = len(partial)
                            push_echo(state, delta)
            elif msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                mtype = data.get("type")
                if mtype == "finalize":
                    partial = recognizer.get_result(stream).strip()
                    if partial:
                        final_text = await asyncio.to_thread(punct.add_punctuation, partial)
                        await ws.send_str(json.dumps({"type": "asr.final", "text": final_text}))
                        if state.on:
                            tail = partial[state.spoken_len:]
                            if tail:
                                push_echo(state, tail)
                        elif agent.on:
                            await start_reply(final_text)
                    last_partial = ""
                    state.spoken_len = 0
                    recognizer.reset(stream)
                elif mtype == "echo":
                    state.on = bool(data.get("on"))
                    if state.on and not state.workers:
                        state.q = queue.Queue()
                        task = asyncio.create_task(echo_worker(ws, state, state.q))
                        state.workers.append((task, state.q))
                    elif not state.on and state.workers:
                        for _task, q in state.workers:
                            q.put(None)
                        state.workers = []
                elif mtype == "llm":
                    agent.on = bool(data.get("on"))
                    logging.info("[ws] llm mode=%s", agent.on)
                    if not agent.on:
                        await cancel_reply()
                elif mtype == "llm.context":
                    agent.set_codex_context(data.get("context"))
                    context = agent.codex_context
                    logging.info(
                        "[ws] llm context thread=%s messages=%d",
                        context.get("threadId") if context else "",
                        len(context.get("messages") or []) if context else 0,
                    )
                elif mtype == "ollama.models.list":
                    await send_ollama_models(ws)
                elif mtype == "ollama.model.set":
                    # A model change should not leave the requesting browser
                    # waiting for a reply generated with the previous model.
                    await cancel_reply()
                    await set_ollama_model(ws, data.get("model"))
                elif mtype == "chat":
                    text = (data.get("text") or "").strip()
                    logging.info("[ws] chat %s", text[:80])
                    if text:
                        await start_reply(text)
                elif mtype == "codex.sessions.list":
                    if codex_list_task is not None and not codex_list_task.done():
                        codex_list_task.cancel()
                    cached_sessions = _enrich_codex_sessions(
                        await asyncio.to_thread(store.load_codex_sessions)
                    )
                    await _send_codex_event(ws, {
                        "type": "codex.sessions",
                        "sessions": cached_sessions,
                        "cached": True,
                    })
                    codex_list_task = spawn_codex(_refresh_codex_sessions(ws))
                elif mtype == "codex.session.create":
                    await _send_codex_event(ws, {"type": "codex.session.creating"})
                    spawn_codex(_create_codex_session(
                        ws,
                        data.get("cwd"),
                        data.get("project"),
                    ))
                elif mtype == "codex.session.read":
                    thread_id = (data.get("thread_id") or "").strip()
                    if not thread_id:
                        continue
                    cached_result = await asyncio.to_thread(store.load_codex_session, thread_id)
                    if cached_result:
                        await _send_codex_event(ws, {
                            "type": "codex.session",
                            **cached_result,
                            "cached": True,
                        })
                    spawn_codex(_refresh_codex_session(ws, thread_id))
                elif mtype == "codex.session.send":
                    thread_id = (data.get("thread_id") or "").strip()
                    text = (data.get("text") or "").strip()
                    if not thread_id or not text:
                        continue
                    await _send_codex_event(ws, {"type": "codex.session.sending", "thread_id": thread_id})
                    spawn_codex(_start_codex_stream(ws, thread_id, text))
                elif mtype == "codex.reply.summarize":
                    thread_id = (data.get("thread_id") or "").strip()
                    text = (data.get("text") or "").strip()
                    if not thread_id or not text:
                        continue
                    spawn_codex(_summarize_codex_reply(ws, thread_id, text))
                elif mtype == "reset":
                    await cancel_reply()
                    agent.reset_conversation()
                    await ws.send_str(json.dumps({"type": "history", "messages": []}))
                    await ws.send_str(json.dumps({"type": "llm.done", "text": ""}))
                elif mtype == "shot":
                    try:
                        url = await asyncio.to_thread(media.take_screenshot)
                    except Exception as e:  # noqa: BLE001
                        logging.warning("screenshot failed: %s", e)
                        url = ""
                    if url:
                        await ws.send_str(json.dumps({
                            "type": "media",
                            "items": [{"kind": "image", "url": url, "caption": "桌面截图"}],
                        }))
                elif mtype == "speak":
                    text = (data.get("text") or "").strip()
                    if not text:
                        continue
                    if tts_task is not None:
                        if job is not None:
                            job.stop.set()
                        tts_task.cancel()
                        try:
                            await tts_task
                        except (asyncio.CancelledError, Exception):
                            pass
                    job = TTSJob()
                    tts_task = asyncio.create_task(run_tts(ws, job, text))
                elif mtype == "stop_speak":
                    if job is not None:
                        job.stop.set()
                    if state.current_job is not None:
                        state.current_job.stop.set()
                    while not state.q.empty():
                        try:
                            state.q.get_nowait()
                        except queue.Empty:
                            break
                    await cancel_reply()
            elif msg.type == web.WSMsgType.ERROR:
                break
    finally:
        _harness_connections.pop(ws, None)
        logging.info("[ws] disconnected")
        if job is not None:
            job.stop.set()
        if tts_task is not None:
            tts_task.cancel()
        for _task, q in state.workers:
            q.put(None)
        if reply_task is not None:
            reply_task.cancel()
        for task in list(codex_tasks):
            task.cancel()
        if codex_tasks:
            await asyncio.gather(*codex_tasks, return_exceptions=True)
    return ws


async def index_handler(request: web.Request) -> web.FileResponse:
    resp = web.FileResponse(INDEX_FILE)
    resp.headers["Cache-Control"] = "no-cache"  # 开发期避免浏览器用旧页面
    return resp


async def avatar_handler(request: web.Request) -> web.FileResponse:
    resp = web.FileResponse(AVATAR_FILE)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def static_handler(request: web.Request) -> web.FileResponse:
    # 白名单静态资源（防路径穿越）：avatar.html 引用的本地依赖
    name = pathlib.Path(request.match_info["name"]).name
    if name not in {"three.min.js", "three.module.js", "GLTFLoader.js", "BufferGeometryUtils.js", "brunette.glb"}:
        raise web.HTTPNotFound()
    resp = web.FileResponse(BASE / name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def media_handler(request: web.Request) -> web.FileResponse:
    # 媒体资源（截图等）：只取 basename 防路径穿越，按扩展名白名单限流
    name = pathlib.Path(request.match_info["name"]).name
    path = MEDIA_SHOT_DIR / name
    if not path.is_file() or path.suffix.lower() not in MEDIA_TYPES:
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    resp.headers["Content-Type"] = MEDIA_TYPES[path.suffix.lower()]
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def install_handler(request: web.Request) -> web.FileResponse:
    resp = web.FileResponse(INSTALL_FILE)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def ca_handler(request: web.Request) -> web.FileResponse:
    name = "ca.cer" if request.path.endswith(".cer") else "ca.crt"
    resp = web.FileResponse(local_certs.CA_CRT_FILE)
    resp.content_type = "application/x-x509-ca-cert"
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def main():
    parser = argparse.ArgumentParser(description="流式语音 demo（sherpa-onnx + Ollama + codex Harness）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；手机局域网访问需要 HTTPS，默认仅供本机")
    parser.add_argument("--port", type=int, default=8060, help="HTTPS 主端口（--no-ssl 时退化为唯一 HTTP 端口）")
    parser.add_argument("--http-port", type=int, default=8050, help="HTTP 端口，与 HTTPS 同实例同时监听（无证书环境访问）")
    parser.add_argument("--llm-model", default="qwen3.8:latest", help="Ollama 模型名（AI 对话用）")
    parser.add_argument("--mcp-server-dir", default=None, help="codex-mcp-server 项目目录（默认自动定位）")
    parser.add_argument("--no-ssl", action="store_true", help="关闭 HTTPS，仅监听 --port 一个 HTTP 端口")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    global recognizer, tts, punct, llm_model, mcp_client
    recognizer, tts, punct = load_models()
    llm_model = args.llm_model
    mcp_client = CodexMcp(pathlib.Path(args.mcp_server_dir) if args.mcp_server_dir else store.codex_server_dir())
    ssl_ctx = None
    scheme = "http"
    if not args.no_ssl:
        ips = local_certs.ensure_certs()
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(local_certs.SERVER_CRT_FILE, local_certs.SERVER_KEY_FILE)
        scheme = "https"
        logging.info("HTTPS on. Install CA via HTTP: http://<lan-ip>:%s/install  IPs=%s", args.http_port, ",".join(ips))
    logging.info("Models ready. LLM=%s  Open %s://%s:%s/ to test.", llm_model, scheme, args.host, args.port)
    warn = asyncio.run(check_models(llm_model))
    if warn:
        logging.warning(warn)

    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/install", install_handler)
    app.router.add_get("/avatar.html", avatar_handler)
    app.router.add_get("/ca.crt", ca_handler)
    app.router.add_get("/ca.cer", ca_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/media/screenshots/{name}", media_handler)  # 放在 /{name} 兜底之前
    app.router.add_get("/{name}", static_handler)  # 白名单静态资源，放在最后避免遮蔽
    if ssl_ctx:
        lan = next((ip for ip in ips if ip != "127.0.0.1"), "127.0.0.1")
        logging.info("同一实例双端口：HTTPS=%s://%s:%s  HTTP=%s://%s:%s（HTTP 用于无证书环境访问/装 CA）",
                     scheme, lan, args.port, "http", lan, args.http_port)
        asyncio.run(_serve_dual(app, args.host, args.port, args.http_port, ssl_ctx))
    else:
        web.run_app(app, host=args.host, port=args.port)


async def _serve_dual(app: web.Application, host: str, ssl_port: int, http_port: int, ssl_ctx: ssl.SSLContext):
    """同一份 app 同时监听 HTTPS 与 HTTP 两个端口（共享模型、WS、内存状态）。"""
    runner = web.AppRunner(app)
    await runner.setup()
    https_site = web.TCPSite(runner, host, ssl_port, ssl_context=ssl_ctx)
    http_site = web.TCPSite(runner, host, http_port, ssl_context=None)
    await https_site.start()
    await http_site.start()
    logging.info("Serving https://%s:%s and http://%s:%s", host, ssl_port, host, http_port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    main()
