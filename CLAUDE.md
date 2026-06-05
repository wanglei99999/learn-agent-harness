# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

Personal learning notes and hands-on experiments following the [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code) course — a 12-session progressive study of agent harness engineering. Each `experiments/sNN_*.py` file is a standalone reimplementation of one session's mechanism.

## Environment Setup

```sh
pip install -r requirements.txt       # anthropic, python-dotenv, pyyaml
pip install requests                  # only needed for s013_nga_capture.py
cp .env.example .env                  # if not present, create manually
```

Required `.env` variables:
- `ANTHROPIC_API_KEY` — Anthropic key
- `MODEL_ID` — e.g. `claude-sonnet-4-6` or `deepseek-v4-flash`
- `ANTHROPIC_BASE_URL` — optional; set for third-party compatible providers (DeepSeek, Kimi, etc.)

When `ANTHROPIC_BASE_URL` is set, the code pops `ANTHROPIC_AUTH_TOKEN` to avoid SDK conflicts with third-party providers.

## Running Experiments

Each experiment is a self-contained REPL — run it directly and type `q` or `exit` to quit:

```sh
python experiments/s01_agent_loop.py
python experiments/s12_worktree_task_isolation.py
python experiments/s013_nga_capture.py   # needs NGA_UID + NGA_CID in .env
```

## Structure

```
experiments/   standalone reimplementations, one per session (s01–s12 + s013 bonus)
notes/         learning notes per session (day1-s01-*.md … day11-s12-*.md)
```

## Experiment Architecture Pattern

Every experiment follows the same invariant core loop. Only the surrounding mechanisms change between sessions:

```python
def agent_loop(messages: list):
    while True:
        response = client.messages.create(model=MODEL, system=SYSTEM,
                                          messages=messages, tools=TOOLS, max_tokens=8000)
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                output = TOOL_HANDLERS[block.name](**block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
        messages.append({"role": "user", "content": results})
```

Key conventions shared across all files:
- **`TOOL_HANDLERS`** — `{name: callable}` dispatch map; new tools = new entry here
- **`TOOLS`** — Anthropic tool definitions list (JSON Schema per tool)
- Tool output is capped at 50,000 chars to stay within context limits
- Bash tool blocks a hardcoded dangerous-pattern list (`rm -rf /`, `sudo`, etc.)

## Session Progression

| Session | New mechanism | Key pattern |
|---------|--------------|-------------|
| s01 | Agent loop + bash | `while stop_reason == "tool_use"` |
| s02 | Tool dispatch map | `TOOL_HANDLERS = {name: handler}` |
| s03 | TodoManager + nag reminder | Inject `<reminder>` after 3 idle rounds |
| s04 | Subagents | Fresh `messages=[]` per child; parent gets only summary |
| s05 | Skill loading | `SKILL.md` injected via `tool_result`, not system prompt |
| s06 | Context compaction | 3-layer compression; transcripts saved to `.transcripts/` |
| s07 | Task system | File-based CRUD task graph in `.tasks/*.json` |
| s08 | Background tasks | Daemon threads + notification queue |
| s09 | Agent teams | Persistent teammates + JSONL async mailboxes in `.team/` |
| s10 | Team protocols | Request-response FSM for shutdown / plan approval |
| s11 | Autonomous agents | Idle-cycle + auto-claim from task board |
| s12 | Worktree isolation | Each task gets its own git worktree in `.worktrees/` |

## Bonus Experiment

`s013_nga_capture.py` — NGA forum scraper agent. Scans a thread page-by-page, LLM judges which replies are valuable (stock analysis, trade logic), appends keepers to `nga_history.md`, and persists progress in `nga_state.json` for resumable runs. Requires `NGA_UID` and `NGA_CID` cookies in `.env`.
