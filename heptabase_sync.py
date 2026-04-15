#!/usr/bin/env python3
"""
heptabase_sync.py  ── 雙向同步主腳本
=======================================
方向 A（預設）：Heptabase → Markdown
  - 直接讀 ~/Library/Application Support/project-meta/hepta.db（SQLite）
  - 先複製到 tmpfile 避免 WAL lock，原本的 db 完全不動
  - 增量比對 last_edited_time，只重寫有變動的檔案
  - 同步後 git commit

方向 B（--push）：Markdown → Heptabase
  - 偵測 heptabase-md/ 裡的新 .md 檔
  - 透過 Heptabase MCP save_to_note_card 建立 card（到主空間 Inbox）

用法：
  python3 heptabase_sync.py              # Heptabase → MD
  python3 heptabase_sync.py --push       # MD → Heptabase
  python3 heptabase_sync.py --both       # 兩個方向
  python3 heptabase_sync.py --dry-run    # 只印不寫
  python3 heptabase_sync.py --force      # 強制全量重跑 A
"""

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# 讀取環境變數（支援 .env 檔案）
def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

load_env()

def get_env(key, default):
    value = os.environ.get(key, default)
    if isinstance(default, bool):
        return value.lower() in ("true", "1", "yes")
    if isinstance(default, Path):
        return Path(os.path.expanduser(value))
    return value

CONFIG = {
    "hepta_db":    get_env("HEPTA_DB_PATH", Path.home() / "Library" / "Application Support" / "project-meta" / "hepta.db"),
    "output_dir":  get_env("OUTPUT_DIR", Path.home() / "heptabase-md"),
    "orphan_dir":  get_env("ORPHAN_DIR", "_Inbox"),
    "git_commit":  get_env("GIT_COMMIT", True),
    "state_file":  get_env("STATE_FILE", Path.home() / ".heptabase_sync_state.json"),
    "log_file":    get_env("LOG_FILE", Path.home() / "Library" / "Logs" / "heptabase_sync.log"),
    "mcp_bridge":  get_env("MCP_BRIDGE_PATH", Path.home() / "Dropbox" / "6_digital" / "hepta-md-sync" / "md_to_heptabase.py"),
}

DRY_RUN  = "--dry-run" in sys.argv
FORCE    = "--force"   in sys.argv
DO_PUSH  = "--push"    in sys.argv or "--both" in sys.argv
DO_PULL  = "--push"    not in sys.argv

CONFIG["log_file"].parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["log_file"], encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def open_db_copy() -> sqlite3.Connection:
    src = CONFIG["hepta_db"]
    if not src.exists():
        log.error(f"找不到 hepta.db：{src}")
        sys.exit(1)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    shutil.copy2(src, tmp_path)
    for ext in ("-wal", "-shm"):
        src_ext = Path(str(src) + ext)
        if src_ext.exists():
            shutil.copy2(src_ext, Path(str(tmp_path) + ext))
    conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn._tmp_path = tmp_path
    return conn


def close_db(conn):
    conn.close()
    try:
        Path(conn._tmp_path).unlink(missing_ok=True)
        for ext in ("-wal", "-shm"):
            Path(str(conn._tmp_path) + ext).unlink(missing_ok=True)
    except Exception:
        pass


def build_card_wb_map(conn):
    rows = conn.execute("""
        SELECT ci.card_id, w.name FROM card_instance ci
        JOIN whiteboard w ON w.id = ci.whiteboard_id
        WHERE w.is_trashed = 0
    """).fetchall()
    m = {}
    for r in rows:
        m.setdefault(r["card_id"], [])
        if r["name"] not in m[r["card_id"]]:
            m[r["card_id"]].append(r["name"])
    return m


def build_pdf_wb_map(conn):
    rows = conn.execute("""
        SELECT pi.pdf_card_id, w.name FROM pdf_card_instance pi
        JOIN whiteboard w ON w.id = pi.whiteboard_id
        WHERE w.is_trashed = 0
    """).fetchall()
    m = {}
    for r in rows:
        m.setdefault(r["pdf_card_id"], [])
        if r["name"] not in m[r["pdf_card_id"]]:
            m[r["pdf_card_id"]].append(r["name"])
    return m


def safe_fn(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", (name or "untitled").strip())
    return (name.strip(". ") or "untitled")[:max_len]


def load_state():
    p = CONFIG["state_file"]
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"cards": {}, "pushed_md": []}


def save_state(state):
    CONFIG["state_file"].write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def pm_to_md(node, depth=0, ordered=False):
    if not isinstance(node, dict):
        return str(node) if node else ""
    ntype   = node.get("type", "")
    attrs   = node.get("attrs") or {}
    marks   = node.get("marks") or []
    content = node.get("content") or []
    text    = node.get("text", "")
    if text:
        for m in marks:
            mt, ma = m.get("type", ""), m.get("attrs") or {}
            if mt == "bold":      text = f"**{text}**"
            elif mt == "italic":  text = f"*{text}*"
            elif mt == "code":    text = f"`{text}`"
            elif mt == "strike":  text = f"~~{text}~~"
            elif mt == "link":    text = f"[{text}]({ma.get('href','')})"
        return text
    def ch(d=depth, o=ordered):
        return "".join(pm_to_md(c, d, o) for c in content)
    if ntype == "doc":         return ch()
    if ntype == "paragraph":
        inner = ch().strip()
        return (inner + "\n\n") if inner else "\n"
    if ntype == "heading":
        return f"{'#' * min(attrs.get('level',1),6)} {ch().strip()}\n\n"
    if ntype == "blockquote":
        return "> " + ch().strip().replace("\n", "\n> ") + "\n\n"
    if ntype == "codeBlock":
        return f"```{attrs.get('language','')}\n{ch()}```\n\n"
    if ntype == "bulletList":
        return "".join(pm_to_md(c, depth, False) for c in content)
    if ntype == "orderedList":
        return "".join(pm_to_md(c, depth, True) for c in content)
    if ntype in ("listItem", "taskItem"):
        checked = attrs.get("checked")
        prefix = ("  " * depth) + (
            f"- [{'x' if checked else ' '}] " if ntype == "taskItem"
            else ("1. " if ordered else "- ")
        )
        parts = []
        for c in content:
            if c.get("type") in ("bulletList","orderedList","taskList"):
                parts.append(pm_to_md(c, depth+1))
            else:
                parts.append(pm_to_md(c, depth).strip())
        return prefix + "\n".join(p for p in parts if p) + "\n"
    if ntype == "taskList":    return "".join(pm_to_md(c, depth) for c in content)
    if ntype == "horizontalRule": return "---\n\n"
    if ntype == "hardBreak":   return "  \n"
    if ntype == "image":
        return f"![{attrs.get('alt','image')}]({attrs.get('src','')})\n\n"
    if ntype == "table":
        rows = [r for r in content if r.get("type") == "tableRow"]
        if not rows: return ch()
        lines = []
        for i, row in enumerate(rows):
            cells = [c for c in (row.get("content") or [])
                     if c.get("type") in ("tableCell","tableHeader")]
            lines.append("| " + " | ".join(
                pm_to_md(c).strip().replace("\n"," ") for c in cells) + " |")
            if i == 0:
                lines.append("|" + "|".join(" --- " for _ in cells) + "|")
        return "\n".join(lines) + "\n\n"
    if ntype in ("tableRow","tableCell","tableHeader"): return ch()
    if ntype == "mention":
        label = attrs.get("label") or (attrs.get("id") or "")[:8]
        return f"[[{label}]]" if attrs.get("type") == "card" else f"[{attrs.get('type','')}:{label}]"
    return ch()


def content_to_md(raw):
    if raw is None: return ""
    if isinstance(raw, str):
        try:   return pm_to_md(json.loads(raw))
        except: return raw
    if isinstance(raw, dict): return pm_to_md(raw)
    return str(raw)


def make_fm(fields):
    lines = ["---"]
    for k, v in fields.items():
        if v is None: continue
        if isinstance(v, (list, dict)):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            lines.append(f'{k}: {json.dumps(str(v), ensure_ascii=False)}')
    lines += ["---", ""]
    return "\n".join(lines)


def resolve_path(folder, title, obj_id, path_idx):
    folder.mkdir(parents=True, exist_ok=True)
    base = safe_fn(title or "untitled")
    fp   = folder / (base + ".md")
    if fp.exists() and path_idx.get(str(fp)) not in (None, obj_id):
        fp = folder / (base + f"_{obj_id[:6]}.md")
    path_idx[str(fp)] = obj_id
    return fp


def pull_heptabase_to_md():
    out_dir  = CONFIG["output_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    state    = load_state()
    cards_st = state.setdefault("cards", {})
    pushed   = state.setdefault("pushed_md", [])
    path_idx = {}
    stats    = {"new": 0, "updated": 0, "skipped": 0, "errors": 0}

    conn = open_db_copy()
    try:
        card_wb  = build_card_wb_map(conn)
        pdf_wb   = build_pdf_wb_map(conn)
        cards    = conn.execute("SELECT id,title,content,created_time,last_edited_time FROM card WHERE is_trashed=0").fetchall()
        pdfs     = conn.execute("SELECT id,title,created_time,last_edited_time FROM pdf_card WHERE is_trashed=0").fetchall()
        journals = conn.execute("SELECT created_by,date,content,created_time,last_edited_time FROM journal").fetchall()
    finally:
        close_db(conn)

    log.info(f"讀取：{len(cards)} cards, {len(pdfs)} PDFs, {len(journals)} journals")

    for row in cards:
        cid, updated = row["id"], row["last_edited_time"]
        if not FORCE and cards_st.get(cid) == updated:
            stats["skipped"] += 1; continue
        try:
            wb = card_wb.get(cid, [])
            folder = out_dir / (safe_fn(wb[0]) if wb else CONFIG["orphan_dir"])
            fp = resolve_path(folder, row["title"], cid, path_idx)
            fm = make_fm({"title": (row["title"] or "untitled").strip(), "type": "card",
                          "created": row["created_time"], "updated": updated,
                          "whiteboards": wb or None, "heptabase_id": cid})
            body = content_to_md(row["content"]).strip()
            md = fm + (body if body else f"# {row['title']}") + "\n"
            if DRY_RUN:
                log.info(f"[DRY] {'NEW' if not fp.exists() else 'UPD'} {fp.relative_to(out_dir)}")
            else:
                is_new = not fp.exists()
                fp.write_text(md, encoding="utf-8")
                stats["new" if is_new else "updated"] += 1
                cards_st[cid] = updated
                if str(fp) not in pushed: pushed.append(str(fp))
        except Exception as e:
            log.warning(f"card {cid}: {e}", exc_info=True); stats["errors"] += 1

    for row in pdfs:
        cid, updated = row["id"], row["last_edited_time"]
        key = f"pdf:{cid}"
        if not FORCE and cards_st.get(key) == updated:
            stats["skipped"] += 1; continue
        try:
            wb = pdf_wb.get(cid, [])
            folder = out_dir / (safe_fn(wb[0]) if wb else CONFIG["orphan_dir"])
            fp = resolve_path(folder, row["title"], cid, path_idx)
            title = (row["title"] or "untitled").strip()
            fm = make_fm({"title": title, "type": "pdfCard",
                          "created": row["created_time"], "updated": updated,
                          "whiteboards": wb or None, "heptabase_id": cid})
            md = fm + f"# {title}\n\n> 📄 PDF Card — 請在 Heptabase 中檢視\n"
            if DRY_RUN:
                log.info(f"[DRY] {'NEW' if not fp.exists() else 'UPD'} PDF {fp.relative_to(out_dir)}")
            else:
                is_new = not fp.exists()
                fp.write_text(md, encoding="utf-8")
                stats["new" if is_new else "updated"] += 1
                cards_st[key] = updated
                if str(fp) not in pushed: pushed.append(str(fp))
        except Exception as e:
            log.warning(f"pdf {cid}: {e}", exc_info=True); stats["errors"] += 1

    journal_folder = out_dir / "_Journal"
    for row in journals:
        jid = f"journal:{row['created_by']}:{row['date']}"
        updated = row["last_edited_time"]
        if not FORCE and cards_st.get(jid) == updated:
            stats["skipped"] += 1; continue
        try:
            fp = resolve_path(journal_folder, f"Journal {row['date']}", jid, path_idx)
            fm = make_fm({"title": f"Journal {row['date']}", "type": "journal",
                          "date": row["date"], "created": row["created_time"], "updated": updated})
            body = content_to_md(row["content"]).strip()
            md = fm + (body if body else f"# Journal {row['date']}") + "\n"
            if DRY_RUN:
                log.info(f"[DRY] {'NEW' if not fp.exists() else 'UPD'} Journal {row['date']}")
            else:
                is_new = not fp.exists()
                fp.write_text(md, encoding="utf-8")
                stats["new" if is_new else "updated"] += 1
                cards_st[jid] = updated
        except Exception as e:
            log.warning(f"journal {jid}: {e}", exc_info=True); stats["errors"] += 1

    if not DRY_RUN:
        save_state(state)

    log.info(f"Pull 完成 | 新增 {stats['new']} 更新 {stats['updated']} "
             f"跳過 {stats['skipped']} 錯誤 {stats['errors']}")

    changed = stats["new"] + stats["updated"]
    if CONFIG["git_commit"] and not DRY_RUN and changed > 0:
        git_commit(out_dir, changed)


def push_md_to_heptabase():
    bridge = CONFIG["mcp_bridge"]
    if not bridge.exists():
        log.error(f"找不到 MCP bridge：{bridge}"); return
    state   = load_state()
    pushed  = set(state.setdefault("pushed_md", []))
    out_dir = CONFIG["output_dir"]
    new_files = [str(p) for p in out_dir.rglob("*.md")
                 if str(p) not in pushed and not p.name.startswith(".")]
    if not new_files:
        log.info("Push：沒有新 .md 需要推送"); return
    log.info(f"Push：{len(new_files)} 個新檔案")
    ok = 0
    for md_path in new_files:
        if DRY_RUN:
            log.info(f"[DRY] PUSH {md_path}"); continue
        try:
            r = subprocess.run([sys.executable, str(bridge), md_path],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                pushed.add(md_path); ok += 1
                log.info(f"Push ✓ {Path(md_path).name}")
            else:
                log.warning(f"Push ✗ {Path(md_path).name}: {r.stderr.strip()[:200]}")
        except Exception as e:
            log.warning(f"Push 錯誤 {md_path}: {e}")
    if not DRY_RUN:
        state["pushed_md"] = list(pushed); save_state(state)
    log.info(f"Push 完成 | 成功 {ok}/{len(new_files)}")


def git_commit(repo_dir, changed):
    if not (repo_dir / ".git").exists():
        subprocess.run(["git","init"], cwd=repo_dir, check=True, capture_output=True)
        (repo_dir / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
    ts  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    msg = f"sync {ts} ({changed} changed)"
    subprocess.run(["git","add","-A"], cwd=repo_dir, check=True, capture_output=True)
    r = subprocess.run(["git","commit","-m",msg], cwd=repo_dir, capture_output=True, text=True)
    if r.returncode == 0:
        log.info(f"Git commit：{msg}")
    elif "nothing to commit" not in (r.stdout + r.stderr):
        log.warning(f"Git：{r.stdout.strip()} {r.stderr.strip()}")


if __name__ == "__main__":
    log.info("=" * 60)
    log.info(f"Heptabase Sync | pull={DO_PULL} push={DO_PUSH} dry={DRY_RUN} force={FORCE}")
    if DO_PULL:
        pull_heptabase_to_md()
    if DO_PUSH:
        push_md_to_heptabase()
