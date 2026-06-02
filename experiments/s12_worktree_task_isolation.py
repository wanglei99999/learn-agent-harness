"""
s12_worktree_task_isolation.py — Worktree + 任务隔离

s12 在 s11 基础上新增一个机制：
  目录级隔离——每个任务拥有独立的 git worktree（独立目录 + 独立分支），
  并行执行时互不踩踏。

═══════════════════════════════════════════════════════════════
 整体架构
═══════════════════════════════════════════════════════════════

  三个管理器互相配合：

    TaskManager          WorktreeManager         EventBus
    .tasks/*.json   ←→  .worktrees/index.json   .worktrees/events.jsonl
         │                      │                       │
         │  bind_worktree()     │  create/remove        │  emit()
         └──────────────────────┘                       │
                                                        │
                         所有操作都向 EventBus 写入事件日志 ─┘

═══════════════════════════════════════════════════════════════
 目录结构
═══════════════════════════════════════════════════════════════

  repo/
  ├── .tasks/
  │   ├── task_12.json      ← 任务元信息（含 worktree 字段）
  │   └── task_7.json
  ├── .worktrees/
  │   ├── index.json        ← 所有 worktree 的元信息汇总
  │   ├── events.jsonl      ← 生命周期事件日志（JSONL 格式）
  │   ├── auth-refactor/    ← 任务12 的独立工作目录
  │   └── payment-fix/      ← 任务7  的独立工作目录
  └── main 分支代码

  git 分支：
    main              ← 主分支
    wt/auth-refactor  ← 任务12 的专属分支（在 auth-refactor/ 目录 checkout）
    wt/payment-fix    ← 任务7  的专属分支

═══════════════════════════════════════════════════════════════
 两层数据结构对照
═══════════════════════════════════════════════════════════════

  task_12.json                    index.json（一条 worktree 记录）
  {                               {
    "id": 12,                       "name": "auth-refactor",
    "subject": "...",               "path": ".../.worktrees/auth-refactor",
    "status": "in_progress",        "branch": "wt/auth-refactor",
    "worktree": "auth-refactor", ←→ "task_id": 12,
    "owner": "agent"                "status": "active"
  }                               }

  两边通过 task_id / worktree 字段互相指向。

═══════════════════════════════════════════════════════════════
 工作流程
═══════════════════════════════════════════════════════════════

  1. task_create     → 创建任务文件（status=pending）
  2. worktree_create → git worktree add，创建独立目录和分支
                       同时 bind_worktree()，任务写入 worktree 名
  3. worktree_run    → 在该目录执行 shell 命令（测试、构建等）
  4. worktree_status → 查看该目录的 git 状态
  5a. worktree_remove（complete_task=True）→ 删除目录，任务标记 completed
  5b. worktree_keep  → 保留目录（分支留着，待后续合并）

═══════════════════════════════════════════════════════════════
 s12 vs s11 关键区别
═══════════════════════════════════════════════════════════════

  s11：多个 teammate 共享同一目录，并发写文件会冲突
  s12：每个任务独占一个 worktree 目录，天然隔离，不会冲突

  s11：没有事件日志，无法回溯操作历史
  s12：EventBus 记录每次 create/remove/keep，可审计
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]


def detect_repo_root(cwd: Path) -> Path | None:
    """
    找到当前目录所在的 git 仓库根目录。
    `git rev-parse --show-toplevel` 会向上遍历父目录直到找到 .git。
    不在 git 仓库中时返回 None，调用方退回到 WORKDIR。
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            return None
        root = Path(r.stdout.strip())
        return root if root.exists() else None
    except Exception:
        return None


# 优先使用 git 仓库根目录，不在仓库中则退回当前目录
REPO_ROOT = detect_repo_root(WORKDIR) or WORKDIR

SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Use task + worktree tools for multi-task work. "
    "For parallel or risky changes: create tasks, allocate worktree lanes, "
    "run commands in those lanes, then choose keep/remove for closeout. "
    "Use worktree_events when you need lifecycle visibility."
)


# ── EventBus：生命周期事件日志 ────────────────────────────────────────────────

class EventBus:
    """
    把所有关键操作写入 events.jsonl，每行一条事件（JSONL 格式）。
    用途：出错时回溯、审计操作历史、调试。
    事件名约定：<对象>.<动作>.<阶段>，如 worktree.create.before / task.completed
    """

    def __init__(self, event_log_path: Path):
        self.path = event_log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("")

    def emit(
        self,
        event: str,
        task: dict | None = None,
        worktree: dict | None = None,
        error: str | None = None,
    ):
        """向事件日志追加一条记录，包含时间戳、关联任务和 worktree 信息。"""
        payload = {
            "event": event,
            "ts": time.time(),
            "task": task or {},
            "worktree": worktree or {},
        }
        if error:
            payload["error"] = error
        # 追加写入，不覆盖历史记录
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    def list_recent(self, limit: int = 20) -> str:
        """读取最近 N 条事件，用于 LLM 查看操作历史。"""
        n = max(1, min(int(limit or 20), 200))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        recent = lines[-n:]  # 取最后 N 行
        items = []
        for line in recent:
            try:
                items.append(json.loads(line))
            except Exception:
                items.append({"event": "parse_error", "raw": line})
        return json.dumps(items, indent=2)


# ── TaskManager：任务板（从 s07 继承，新增 worktree 绑定字段）─────────────────

class TaskManager:
    """
    管理 .tasks/*.json 任务文件。
    s12 新增：任务结构里多了 "worktree" 字段，用于绑定关联的工作目录。
    """

    def __init__(self, tasks_dir: Path):
        self.dir = tasks_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        # 启动时扫描现有任务，确保新 ID 不重复
        self._next_id = self._max_id() + 1

    def _max_id(self) -> int:
        """找到现有任务中最大的 ID，新任务从 max+1 开始。"""
        ids = []
        for f in self.dir.glob("task_*.json"):
            try:
                ids.append(int(f.stem.split("_")[1]))
            except Exception:
                pass
        return max(ids) if ids else 0

    def _path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"

    def _load(self, task_id: int) -> dict:
        path = self._path(task_id)
        if not path.exists():
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())

    def _save(self, task: dict):
        self._path(task["id"]).write_text(json.dumps(task, indent=2))

    def create(self, subject: str, description: str = "") -> str:
        """创建新任务，worktree 字段初始为空，等待 bind_worktree 绑定。"""
        task = {
            "id": self._next_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "owner": "",
            "worktree": "",       # s12 新增：关联的 worktree 名
            "blockedBy": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._save(task)
        self._next_id += 1
        return json.dumps(task, indent=2)

    def get(self, task_id: int) -> str:
        return json.dumps(self._load(task_id), indent=2)

    def exists(self, task_id: int) -> bool:
        return self._path(task_id).exists()

    def update(self, task_id: int, status: str = None, owner: str = None) -> str:
        task = self._load(task_id)
        if status:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
        if owner is not None:
            task["owner"] = owner
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2)

    #新增：绑定工作空间，本质是更新状态和worktree赋值
    def bind_worktree(self, task_id: int, worktree: str, owner: str = "") -> str:
        """
        将任务与 worktree 绑定：写入 worktree 名，状态自动从 pending 升为 in_progress。
        由 WorktreeManager.create() 在创建 worktree 后调用。
        """
        task = self._load(task_id)
        task["worktree"] = worktree
        if owner:
            task["owner"] = owner
        if task["status"] == "pending":
            task["status"] = "in_progress"  # 有了 worktree 就算开工了
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2)
    #新增：解绑工作空间
    def unbind_worktree(self, task_id: int) -> str:
        """解绑 worktree，worktree 删除后调用，把任务的 worktree 字段清空。"""
        task = self._load(task_id)
        task["worktree"] = ""
        task["updated_at"] = time.time()
        self._save(task)
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        """列出所有任务，格式：[ ]/[>]/[x] #id: subject owner=xxx wt=xxx"""
        tasks = []
        for f in sorted(self.dir.glob("task_*.json")):
            tasks.append(json.loads(f.read_text()))
        if not tasks:
            return "No tasks."
        lines = []
        for t in tasks:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }.get(t["status"], "[?]")
            owner = f" owner={t['owner']}" if t.get("owner") else ""
            wt = f" wt={t['worktree']}" if t.get("worktree") else ""
            lines.append(f"{marker} #{t['id']}: {t['subject']}{owner}{wt}")
        return "\n".join(lines)


# 全局单例，在模块加载时初始化
TASKS = TaskManager(REPO_ROOT / ".tasks")
EVENTS = EventBus(REPO_ROOT / ".worktrees" / "events.jsonl")


# ── WorktreeManager：git worktree 生命周期管理 ────────────────────────────────

class WorktreeManager:
    """
    管理 git worktree 的创建、运行、删除和保留。
    元信息存在 .worktrees/index.json，实际目录由 git 管理。
    依赖 TaskManager 做双向绑定，依赖 EventBus 记录操作历史。
    """

    def __init__(self, repo_root: Path, tasks: TaskManager, events: EventBus):
        self.repo_root = repo_root
        self.tasks = tasks
        self.events = events
        self.dir = repo_root / ".worktrees"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        # 首次运行时初始化空索引文件
        if not self.index_path.exists():
            self.index_path.write_text(json.dumps({"worktrees": []}, indent=2))
        # 启动时检查一次 git 可用性，后续操作直接查这个标志
        self.git_available = self._is_git_repo()

    def _is_git_repo(self) -> bool:
        """检查 repo_root 是否在 git 仓库中，结果缓存到 self.git_available。"""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
        except Exception:
            return False

    def _run_git(self, args: list[str]) -> str:
        """
        统一的 git 命令执行器。
        调用方只传参数列表，不需要关心错误处理和输出合并。
        ["git", *args] 把参数展开拼成完整命令，如 ["worktree", "add", ...] → git worktree add ...
        stdout 和 stderr 合并，因为 git 有时把信息放 stderr。
        """
        if not self.git_available:
            raise RuntimeError("Not in a git repository. worktree tools require git.")
        r = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            msg = (r.stdout + r.stderr).strip()
            raise RuntimeError(msg or f"git {' '.join(args)} failed")
        return (r.stdout + r.stderr).strip() or "(no output)"

    def _load_index(self) -> dict:
        """从磁盘读取 index.json，返回字典。"""
        return json.loads(self.index_path.read_text())

    def _save_index(self, data: dict):
        """把修改后的字典写回 index.json。"""
        self.index_path.write_text(json.dumps(data, indent=2))

    def _find(self, name: str) -> dict | None:
        """按名字在 index.json 中查找 worktree 记录，找不到返回 None。"""
        idx = self._load_index()
        for wt in idx.get("worktrees", []):
            if wt.get("name") == name:
                return wt
        return None

    def _validate_name(self, name: str):
        """
        worktree 名即目录名和分支名的一部分，限制字符集避免路径/git 特殊字符问题。
        合法：字母、数字、点、下划线、连字符，1-40 个字符。
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            raise ValueError(
                "Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -"
            )

    def create(self, name: str, task_id: int = None, base_ref: str = "HEAD") -> str:
        """
        创建 git worktree，流程：
          1. 校验名字合法、不重复、关联任务存在
          2. 发 before 事件
          3. git worktree add -b wt/<name> .worktrees/<name> <base_ref>
             → 真实创建分支和目录
          4. 把记录追加到 index.json
          5. 绑定关联任务（task.worktree = name, status → in_progress）
          6. 发 after 事件
        失败时发 failed 事件并重新抛出异常。
        """
        self._validate_name(name)
        if self._find(name):
            raise ValueError(f"Worktree '{name}' already exists in index")
        # 提前验证任务存在，避免创建 worktree 后发现任务不存在变成孤儿
        if task_id is not None and not self.tasks.exists(task_id):
            raise ValueError(f"Task {task_id} not found")

        path = self.dir / name
        branch = f"wt/{name}"  # 分支命名约定：wt/ 前缀方便识别

        self.events.emit(
            "worktree.create.before",
            task={"id": task_id} if task_id is not None else {},
            worktree={"name": name, "base_ref": base_ref},
        )
        try:
            # 真实 git 操作：新建分支并在指定目录 checkout
            # base_ref 决定从哪个提交分叉，默认 HEAD（当前最新）
            self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])

            # 构造 index 记录
            entry = {
                "name": name,
                "path": str(path),
                "branch": branch,
                "task_id": task_id,
                "status": "active",
                "created_at": time.time(),
            }

            # 读-改-写 index.json，追加这条新记录
            idx = self._load_index()
            idx["worktrees"].append(entry)
            self._save_index(idx)

            # 双向绑定：任务的 worktree 字段写入 name
            if task_id is not None:
                self.tasks.bind_worktree(task_id, name)

            self.events.emit(
                "worktree.create.after",
                task={"id": task_id} if task_id is not None else {},
                worktree={"name": name, "path": str(path), "branch": branch, "status": "active"},
            )
            return json.dumps(entry, indent=2)
        except Exception as e:
            self.events.emit(
                "worktree.create.failed",
                task={"id": task_id} if task_id is not None else {},
                worktree={"name": name, "base_ref": base_ref},
                error=str(e),
            )
            raise

    def list_all(self) -> str:
        """列出 index.json 中所有 worktree 记录及其状态。"""
        idx = self._load_index()
        wts = idx.get("worktrees", [])
        if not wts:
            return "No worktrees in index."
        lines = []
        for wt in wts:
            suffix = f" task={wt['task_id']}" if wt.get("task_id") else ""
            lines.append(
                f"[{wt.get('status', 'unknown')}] {wt['name']} -> "
                f"{wt['path']} ({wt.get('branch', '-')}){suffix}"
            )
        return "\n".join(lines)

    def status(self, name: str) -> str:
        """
        在指定 worktree 目录下运行 git status，查看未提交的改动。
        --short：简洁格式  --branch：显示当前分支名
        """
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = (r.stdout + r.stderr).strip()
        return text or "Clean worktree"

    def run(self, name: str, command: str) -> str:
        """
        在指定 worktree 目录下执行 shell 命令。
        关键：cwd=path，命令在该 worktree 的目录里跑，
        改动只影响这个 worktree，不影响主目录或其他 worktree。
        """
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            return "Error: Dangerous command blocked"
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"
        path = Path(wt["path"])
        if not path.exists():
            return f"Error: Worktree path missing: {path}"
        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=path,          # 在 worktree 目录里执行，隔离效果的关键
                capture_output=True,
                text=True,
                timeout=300,
            )
            out = (r.stdout + r.stderr).strip()
            return out[:50000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return "Error: Timeout (300s)"

    def remove(self, name: str, force: bool = False, complete_task: bool = False) -> str:
        """
        删除 worktree，有两种模式：
          - complete_task=False：只删目录，任务状态不变（可以手动合并）
          - complete_task=True ：删目录同时把关联任务标记为 completed

        force=True：目录里有未提交改动时强制删除（会丢失改动，慎用）。
        注意：删除 worktree 不会删除对应的 git 分支，分支仍然存在。
        """
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"

        self.events.emit(
            "worktree.remove.before",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={"name": name, "path": wt.get("path")},
        )
        try:
            # git worktree remove [--force] <path>
            args = ["worktree", "remove"]
            if force:
                args.append("--force")
            args.append(wt["path"])
            self._run_git(args)

            # 可选：同时完成关联任务
            if complete_task and wt.get("task_id") is not None:
                task_id = wt["task_id"]
                before = json.loads(self.tasks.get(task_id))  # 记录完成前的状态
                self.tasks.update(task_id, status="completed")
                self.tasks.unbind_worktree(task_id)  # 清空任务的 worktree 字段
                self.events.emit(
                    "task.completed",
                    task={"id": task_id, "subject": before.get("subject", ""), "status": "completed"},
                    worktree={"name": name},
                )

            # 更新 index.json：不删记录，只改状态为 removed（保留历史）
            idx = self._load_index()
            for item in idx.get("worktrees", []):
                if item.get("name") == name:
                    item["status"] = "removed"
                    item["removed_at"] = time.time()
            self._save_index(idx)

            self.events.emit(
                "worktree.remove.after",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path"), "status": "removed"},
            )
            return f"Removed worktree '{name}'"
        except Exception as e:
            self.events.emit(
                "worktree.remove.failed",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path")},
                error=str(e),
            )
            raise

    def keep(self, name: str) -> str:
        """
        保留 worktree，只改 index.json 里的状态为 kept，不做任何 git 操作。
        适用场景：代码写完了，但还没合并，先标记"留着"，之后手动 git merge。
        remove 是"干净收尾"，keep 是"暂时搁置"。
        """
        wt = self._find(name)
        if not wt:
            return f"Error: Unknown worktree '{name}'"

        idx = self._load_index()
        kept = None
        for item in idx.get("worktrees", []):
            if item.get("name") == name:
                item["status"] = "kept"
                item["kept_at"] = time.time()
                kept = item
        self._save_index(idx)

        self.events.emit(
            "worktree.keep",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={"name": name, "path": wt.get("path"), "status": "kept"},
        )
        return json.dumps(kept, indent=2) if kept else f"Error: Unknown worktree '{name}'"


# 全局单例，三个管理器在模块加载时初始化并互相注入
WORKTREES = WorktreeManager(REPO_ROOT, TASKS, EVENTS)


# ── 基础工具实现（与前几节相同）──────────────────────────────────────────────

def safe_path(p: str) -> Path:
    """路径安全检查，防止 LLM 传入 ../../ 之类的路径逃逸。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# ── 工具分发表 ────────────────────────────────────────────────────────────────
# 工具名 → lambda，LLM 调用工具时通过名字查到对应函数

TOOL_HANDLERS = {
    # 基础文件操作
    "bash":               lambda **kw: run_bash(kw["command"]),
    "read_file":          lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":         lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":          lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # 任务管理
    "task_create":        lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    "task_list":          lambda **kw: TASKS.list_all(),
    "task_get":           lambda **kw: TASKS.get(kw["task_id"]),
    "task_update":        lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("owner")),
    "task_bind_worktree": lambda **kw: TASKS.bind_worktree(kw["task_id"], kw["worktree"], kw.get("owner", "")),
    # worktree 管理（s12 新增）
    "worktree_create":    lambda **kw: WORKTREES.create(kw["name"], kw.get("task_id"), kw.get("base_ref", "HEAD")),
    "worktree_list":      lambda **kw: WORKTREES.list_all(),
    "worktree_status":    lambda **kw: WORKTREES.status(kw["name"]),
    "worktree_run":       lambda **kw: WORKTREES.run(kw["name"], kw["command"]),
    "worktree_keep":      lambda **kw: WORKTREES.keep(kw["name"]),
    "worktree_remove":    lambda **kw: WORKTREES.remove(kw["name"], kw.get("force", False), kw.get("complete_task", False)),
    "worktree_events":    lambda **kw: EVENTS.list_recent(kw.get("limit", 20)),
}

TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command in the current workspace (blocking).",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "task_create",
        "description": "Create a new task on the shared task board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["subject"],
        },
    },
    {
        "name": "task_list",
        "description": "List all tasks with status, owner, and worktree binding.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "task_get",
        "description": "Get task details by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "task_update",
        "description": "Update task status or owner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                },
                "owner": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_bind_worktree",
        "description": "Bind a task to a worktree name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "worktree": {"type": "string"},
                "owner": {"type": "string"},
            },
            "required": ["task_id", "worktree"],
        },
    },
    {
        "name": "worktree_create",
        "description": "Create a git worktree and optionally bind it to a task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task_id": {"type": "integer"},
                "base_ref": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "worktree_list",
        "description": "List worktrees tracked in .worktrees/index.json.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "worktree_status",
        "description": "Show git status for one worktree.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "worktree_run",
        "description": "Run a shell command in a named worktree directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "command": {"type": "string"},
            },
            "required": ["name", "command"],
        },
    },
    {
        "name": "worktree_remove",
        "description": "Remove a worktree and optionally mark its bound task completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "force": {"type": "boolean"},
                "complete_task": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "worktree_keep",
        "description": "Mark a worktree as kept in lifecycle state without removing it.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "worktree_events",
        "description": "List recent worktree/task lifecycle events from .worktrees/events.jsonl.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
]


def agent_loop(messages: list):
    """标准 agent loop，s12 无特殊改动，单 agent 直接使用 worktree 工具。"""
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print(f"Repo root for s12: {REPO_ROOT}")
    if not WORKTREES.git_available:
        print("Note: Not in a git repo. worktree_* tools will return errors.")

    history = []
    while True:
        try:
            query = input("\033[36ms12 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
