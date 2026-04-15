#!/usr/bin/env python3
"""
md_to_heptabase.py  ── MCP Bridge（Markdown → Heptabase）
把一個 .md 檔透過 Heptabase MCP save_to_note_card 建立成 card。
用法：python3 md_to_heptabase.py path/to/note.md
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

load_env()

mcp_cmd_str = os.environ.get("MCP_COMMAND", "bunx heptabase-mcp")
MCP_COMMAND = mcp_cmd_str.split()


def strip_frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        kv = line.split(":", 1)
        if len(kv) == 2:
            k, v = kv[0].strip(), kv[1].strip()
            if v.startswith('"') and v.endswith('"'):
                try: v = json.loads(v)
                except: pass
            fm[k] = v
    return fm, m.group(2).strip()


def build_content(md_path):
    raw = md_path.read_text(encoding="utf-8")
    fm, body = strip_frontmatter(raw)
    title = fm.get("title") or md_path.stem or "untitled"
    content = body if body.startswith("#") else f"# {title}\n\n{body}"
    hid = fm.get("heptabase_id", "")
    wbs = fm.get("whiteboards", "")
    meta = []
    if wbs:  meta.append(f"> 原始 Whiteboard：{wbs}")
    if hid:  meta.append(f"> Heptabase ID：{hid}（已同步，勿重複）")
    if meta: content += "\n\n" + "\n".join(meta)
    return content


def call_mcp(content):
    req = {"jsonrpc":"2.0","id":1,"method":"tools/call",
           "params":{"name":"save_to_note_card","arguments":{"content":content}}}
    try:
        proc = subprocess.run(MCP_COMMAND, input=json.dumps(req)+"\n",
                              capture_output=True, text=True, timeout=30,
                              env={**os.environ})
    except FileNotFoundError:
        print("錯誤：找不到 bun，請安裝：brew install bun", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("錯誤：MCP 逾時", file=sys.stderr)
        return False

    if proc.returncode != 0:
        print(f"MCP 失敗：{proc.stderr[:300]}", file=sys.stderr)
        return False

    for line in proc.stdout.splitlines():
        try:
            resp = json.loads(line.strip())
            if "error" in resp:
                print(f"MCP 錯誤：{resp['error']}", file=sys.stderr)
                return False
            if "result" in resp:
                for item in (resp["result"].get("content") or []):
                    if item.get("type") == "text":
                        print(f"MCP 回應：{item['text'][:200]}")
                return True
        except json.JSONDecodeError:
            continue
    return bool(proc.stdout.strip())


def main():
    if len(sys.argv) < 2:
        print("用法：python3 md_to_heptabase.py <file.md>", file=sys.stderr)
        sys.exit(1)
    md_path = Path(sys.argv[1])
    if not md_path.exists():
        print(f"找不到：{md_path}", file=sys.stderr)
        sys.exit(1)
    print(f"推送：{md_path.name}")
    ok = call_mcp(build_content(md_path))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
