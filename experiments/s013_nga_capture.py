"""
s013_nga_capture.py — NGA 帖子扫描 Agent

功能：
  - 逐页扫描指定帖子，LLM 判断每条回复是否有价值
  - 有价值的内容实时追加到 nga_history.md
  - 进度保存到 nga_state.json，随时可中断、恢复

三个核心工具：
  scan_page(tid, page)  — 抓取指定页，返回结构化楼层列表
  save_valuable(content) — 把有价值的内容追加到历史文件
  scan_status(tid)       — 查看当前扫描进度

判断"有价值"的标准（由 system prompt 约束）：
  保留：具体股票分析、买卖逻辑、仓位变化、市场判断
  丢弃：打卡、+1、闲聊、无实质内容的回复
"""

import json
import os
import time
import random
from datetime import datetime
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

NGA_UID = os.getenv("NGA_UID")
NGA_CID = os.getenv("NGA_CID")

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

STATE_FILE = Path("nga_state.json")
HISTORY_FILE = Path("nga_history.md")

SYSTEM = """你是一个 NGA 股票帖子分析助手。

你的工作流程：
1. 用 scan_page 抓取下一页（不传 page 参数，自动从上次中断处继续）
2. 逐条判断哪些回复有价值：
   - 保留：具体股票分析、买卖逻辑、仓位变化、对市场的判断、有依据的观点
   - 丢弃：打卡、+1、"好帖"、闲聊、无实质内容
3. 把有价值的内容用 save_valuable 保存（保留楼层号和原文）
4. 继续调用 scan_page 翻下一页，直到用户让你停止

注意：scan_page 不传 page 时自动接续上次进度；第一次扫描从第1页开始。
扫描时不需要每页都汇报，遇到有价值内容时简短说一句即可。
"""

# ── NGA 请求 ──────────────────────────────────────────────────────────────────

HEADERS = {
    "Cookie": f"ngaPassportUid={NGA_UID}; ngaPassportCid={NGA_CID}",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://bbs.nga.cn/",
}

def _nga_request(tid: str, page: int) -> dict:
    url = f"https://bbs.nga.cn/read.php?tid={tid}&page={page}&lite=js"
    # 随机间隔 2~4 秒，模拟正常浏览节奏，避免触发限速
    time.sleep(random.uniform(2, 4))
    r = requests.get(url, headers=HEADERS, timeout=10)
    text = r.text
    prefix = "window.script_muti_get_var_store="
    if text.startswith(prefix):
        text = text[len(prefix):]
    return json.loads(text, strict=False)

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def scan_page(tid: str, page: int = None) -> str:
    try:
        if page is None:
            state = _load_state()
            page = state.get("current_page", 0) + 1
        data = _nga_request(tid, page)
        d = data.get("data", {})
        posts = d.get("__R", {})
        users = d.get("__U", {})
        total_pages = d.get("__PAGE", {}).get("_PAGECOUNT", 1)

        # 保存/更新进度
        state = _load_state()
        state.update({"tid": tid, "current_page": page, "total_pages": total_pages})
        _save_state(state)

        lines = [f"=== 第 {page}/{total_pages} 页 ==="]
        for key in sorted(posts.keys(), key=lambda x: int(x) if str(x).isdigit() else 0):
            p = posts[key]
            floor = p.get("lou", key)
            uid = str(p.get("authorid", ""))
            username = users.get(uid, {}).get("username", uid)
            content = p.get("content", "").strip()
            if content:
                lines.append(f"[{floor}楼 @{username}]\n{content[:600]}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def save_valuable(content: str) -> str:
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n---\n<!-- {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n")
            f.write(content + "\n")
        return f"已保存到 {HISTORY_FILE}"
    except Exception as e:
        return f"Error: {e}"

def scan_status(tid: str = None) -> str:
    state = _load_state()
    if not state:
        return "尚未开始扫描"
    current = state.get("current_page", 0)
    total = state.get("total_pages", "?")
    history_lines = len(HISTORY_FILE.read_text(encoding="utf-8").splitlines()) if HISTORY_FILE.exists() else 0
    return f"进度：第 {current}/{total} 页 | 历史记录：{history_lines} 行 | 文件：{HISTORY_FILE}"

# ── 状态管理 ──────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}

def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

# ── 工具注册 ──────────────────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "scan_page":     lambda **kw: scan_page(kw["tid"], int(kw.get("page", 1))),
    "save_valuable": lambda **kw: save_valuable(kw["content"]),
    "scan_status":   lambda **kw: scan_status(kw.get("tid")),
}

TOOLS = [
    {
        "name": "scan_page",
        "description": "抓取 NGA 帖子指定页的所有回复，返回楼层列表",
        "input_schema": {
            "type": "object",
            "properties": {
                "tid":  {"type": "string", "description": "帖子 ID"},
                "page": {"type": "integer", "description": "页码，从 1 开始"}
            },
            "required": ["tid"]
        }
    },
    {
        "name": "save_valuable",
        "description": "把有价值的楼层内容保存到历史文件",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要保存的内容，建议包含楼层号"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "scan_status",
        "description": "查看当前扫描进度和历史文件大小",
        "input_schema": {
            "type": "object",
            "properties": {
                "tid": {"type": "string"}
            }
        }
    },
]

# ── Agent 主循环 ──────────────────────────────────────────────────────────────

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                tool = TOOL_HANDLERS.get(block.name)
                result = tool(**block.input) if tool else f"未知工具：{block.name}"
                print(f"> {block.name}  →  {str(result)[:150]}")
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(result)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36mnga >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        last = history[-1]["content"]
        if isinstance(last, list):
            for block in last:
                if hasattr(block, "text"):
                    print(block.text)
        print()
