"""Harness 数据层：播报规则 / 互动记录 / 任务缓存。

所有运行时数据落在 demo/harness_data/：
  rules.md          播报规则（AI 用 rule_add 逐步累积，seed 内置）
  interactions.jsonl  每次 MCP 互动一条 JSONL
  tasks.json        codex 任务缓存（list_sessions 后刷新）
  codex_sessions.json  Codex 会话首页缓存
  codex_messages.json  已读取会话的最近消息缓存
  notes/            笔记目录（notes.py 使用）
"""

import json
import logging
import pathlib
import threading
import time
import uuid

BASE = pathlib.Path(__file__).resolve().parent  # demo/harness/
DATA_DIR = BASE.parent / "harness_data"
NOTES_DIR = DATA_DIR / "notes"
RULES_FILE = DATA_DIR / "rules.md"
TASKS_FILE = DATA_DIR / "tasks.json"
CODEX_SESSIONS_FILE = DATA_DIR / "codex_sessions.json"
CODEX_MESSAGES_FILE = DATA_DIR / "codex_messages.json"
INTERACTIONS_FILE = DATA_DIR / "interactions.jsonl"
CHAT_FILE = DATA_DIR / "chat.jsonl"
CHAT_LOAD = 10       # 启动时注入上下文 / UI 展示的条数
CHAT_FILE_CAP = 200  # 磁盘最多保留的条数
CODEX_MESSAGE_CACHE_LIMIT = 30
_CODEX_CACHE_LOCK = threading.RLock()

SEED_RULES = [
    "只播报面向人的安全摘要；原始命令、密钥、文件路径、大段日志不播报。",
    "长内容先总结再播报；每段一般不超过三句话。",
    "面向语音：不用 Markdown 符号、表情、括号注释。",
]


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)


def codex_server_dir() -> pathlib.Path:
    """定位 codex-mcp-server 项目目录（demo/../codex-mcp-server）。"""
    return BASE.parent.parent.parent / "codex-mcp-server"


# --- 播报规则 ------------------------------------------------------------

def load_rules() -> str:
    ensure_dirs()
    if not RULES_FILE.exists():
        RULES_FILE.write_text("\n".join(f"- {r}" for r in SEED_RULES) + "\n", encoding="utf-8")
    return RULES_FILE.read_text(encoding="utf-8")


def add_rule(rule: str) -> list[str]:
    ensure_dirs()
    load_rules()
    rules = [ln for ln in RULES_FILE.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
    rule = rule.strip().strip("- ").strip()
    if not rule:
        return rules
    if any(r == f"- {rule}" for r in rules):
        return rules
    rules.append(f"- {rule}")
    RULES_FILE.write_text("\n".join(rules) + "\n", encoding="utf-8")
    return rules


# --- 互动记录 ------------------------------------------------------------

def append_interaction(record: dict) -> bool:
    ensure_dirs()
    record.setdefault("id", uuid.uuid4().hex[:12])
    record.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        with open(INTERACTIONS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logging.warning("Interaction history was not saved: %s", e)
        return False
    return True


def recent_interactions(n: int = 8) -> list[dict]:
    ensure_dirs()
    if not INTERACTIONS_FILE.exists():
        return []
    lines = INTERACTIONS_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# --- 任务缓存 ------------------------------------------------------------

def load_tasks() -> list[dict]:
    ensure_dirs()
    if not TASKS_FILE.exists():
        return []
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_tasks(sessions: list[dict]) -> bool:
    ensure_dirs()
    try:
        TASKS_FILE.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logging.warning("Task cache was not saved: %s", e)
        return False
    return True


# --- Codex UI cache -----------------------------------------------------

def _write_json_cache(path: pathlib.Path, data: object) -> bool:
    """Replace a cache file atomically so a refresh cannot leave broken JSON."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logging.warning("Codex cache was not saved (%s): %s", path.name, e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def load_codex_sessions() -> list[dict]:
    """Load the newest session list, migrating the older tasks cache if needed."""
    ensure_dirs()
    with _CODEX_CACHE_LOCK:
        paths = [CODEX_SESSIONS_FILE, TASKS_FILE]
        for path in paths:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
    return []


def save_codex_sessions(sessions: list[dict]) -> bool:
    ensure_dirs()
    clean = [item for item in sessions if isinstance(item, dict) and item.get("id")]
    with _CODEX_CACHE_LOCK:
        saved = _write_json_cache(CODEX_SESSIONS_FILE, clean)
        # Keep the original cache name usable by the existing harness tools.
        if saved:
            _write_json_cache(TASKS_FILE, clean)
        return saved


def load_codex_session(thread_id: str) -> dict | None:
    ensure_dirs()
    with _CODEX_CACHE_LOCK:
        if not CODEX_MESSAGES_FILE.exists():
            return None
        try:
            data = json.loads(CODEX_MESSAGES_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        result = data.get(thread_id) if isinstance(data, dict) else None
        return result if isinstance(result, dict) and result.get("ok") else None


def _clean_codex_messages(messages: object) -> list[dict]:
    cleaned = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, dict):
            continue
        text = str(message.get("text") or "")
        if len(text) > 20000:
            text = text[:20000] + "…"
        if not text:
            continue
        cleaned.append({"role": message.get("role", "assistant"), "text": text})
    return cleaned


def _merge_codex_messages(previous: list[dict], incoming: list[dict]) -> list[dict]:
    """Append a newer recent window while removing its overlap with the cache."""
    if not previous:
        return incoming[-CODEX_MESSAGE_CACHE_LIMIT:]
    if not incoming:
        return previous[-CODEX_MESSAGE_CACHE_LIMIT:]

    def key(message: dict) -> tuple[str, str]:
        return (str(message.get("role") or ""), str(message.get("text") or ""))

    overlap = 0
    max_overlap = min(len(previous), len(incoming))
    for size in range(max_overlap, 0, -1):
        if [key(item) for item in previous[-size:]] == [key(item) for item in incoming[:size]]:
            overlap = size
            break
    return (previous + incoming[overlap:])[-CODEX_MESSAGE_CACHE_LIMIT:]


def save_codex_session(result: dict) -> bool:
    """Cache one successful read while bounding disk growth from long replies."""
    if not isinstance(result, dict) or not result.get("ok") or not result.get("threadId"):
        return False
    thread_id = str(result["threadId"])
    with _CODEX_CACHE_LOCK:
        data: dict[str, dict] = {}
        if CODEX_MESSAGES_FILE.exists():
            try:
                raw = json.loads(CODEX_MESSAGES_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = {str(key): value for key, value in raw.items() if isinstance(value, dict)}
            except (json.JSONDecodeError, OSError):
                pass
        previous = _clean_codex_messages((data.get(thread_id) or {}).get("messages"))
        incoming = _clean_codex_messages(result.get("messages"))
        cached = {
            "ok": True,
            "threadId": thread_id,
            "name": result.get("name"),
            "cwd": result.get("cwd"),
            "project": result.get("project"),
            "status": result.get("status"),
            "totalTurns": result.get("totalTurns"),
            "messages": _merge_codex_messages(previous, incoming),
            "cachedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "local-cache",
            "note": "cached recent messages; newer windows are merged",
        }
        data[thread_id] = cached
        return _write_json_cache(CODEX_MESSAGES_FILE, data)


# --- 聊天记录 ------------------------------------------------------------

def append_chat(role: str, content: str):
    content = (content or "").strip()
    if role not in ("user", "assistant") or not content:
        return
    ensure_dirs()
    record = {
        "role": role,
        "content": content,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(CHAT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    lines = CHAT_FILE.read_text(encoding="utf-8").splitlines()
    if len(lines) > CHAT_FILE_CAP:
        CHAT_FILE.write_text("\n".join(lines[-CHAT_FILE_CAP:]) + "\n", encoding="utf-8")


def recent_chat(n: int = CHAT_LOAD) -> list[dict]:
    ensure_dirs()
    if not CHAT_FILE.exists():
        return []
    out = []
    for line in CHAT_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("role") in ("user", "assistant") and (rec.get("content") or "").strip():
            out.append(rec)
    return out[-n:]


def clear_chat() -> bool:
    ensure_dirs()
    try:
        if CHAT_FILE.exists():
            CHAT_FILE.write_text("", encoding="utf-8")
    except OSError as e:
        logging.warning("Chat history was not cleared: %s", e)
        return False
    return True
