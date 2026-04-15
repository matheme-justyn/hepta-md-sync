# hepta-md-sync agent.md

供 AI agent 協作時快速掌握本專案的背景與規範。

## 技術棧

- Python 3.14，只用標準函式庫（sqlite3、shutil、subprocess）

- SQLite：直接讀 `hepta.db`，讀前複製到 tmp 避免 lock

- Heptabase MCP：push 用，透過 `bunx heptabase-mcp`

- launchd：每小時自動 pull

- bun：取代 npx

## 環境變數配置

專案使用 `.env` 檔案管理配置,避免硬編碼路徑。

### 初次設定
```bash
cp .env.template .env
# 編輯 .env 設定實際路徑（預設值通常不需修改）
```

### 主要配置項
- `HEPTA_DB_PATH` - Heptabase 資料庫路徑
- `OUTPUT_DIR` - Markdown 輸出目錄
- `MCP_BRIDGE_PATH` - MCP 橋接器腳本位置
- `MCP_COMMAND` - MCP 執行指令
- `GIT_COMMIT` - 是否自動 git commit (true/false)

詳見 `.env.template` 完整說明。

## 重要 Tables

```
card(id, title, content, created_time, last_edited_time, is_trashed)
pdf_card(id, title, ...)
journal(created_by, date, content, ...)
whiteboard(id, name, ...)
card_instance(card_id, whiteboard_id, ...)
pdf_card_instance(pdf_card_id, whiteboard_id, ...)
```

`card.content` 為 ProseMirror JSON，用 `pm_to_md()` 遞迴轉換。

## 狀態檔格式

`~/.heptabase_sync_state.json`

```json
{
  "cards": { "{id}": "{last_edited_time}", "pdf:{id}": "...", "journal:{by}:{date}": "..." },
  "pushed_md": ["{絕對路徑}", ...]
}
```

## 已知限制

1. Push 無法指定 whiteboard（MCP 限制）

1. PDF 內容不同步（只有 metadata）

1. 圖片附件不處理

1. 衝突以 Heptabase 為準

## 待辦

- 圖片附件複製到 assets/

- Push 後自動回填 heptabase_id 到 front matter

- 支援 web_card、highlight_element

- 衝突偵測

## 檔案位置

`~/Dropbox/6_digital/hepta-md-sync/`