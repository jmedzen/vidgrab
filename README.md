# vidgrab · 關鍵字找素材

打幾個關鍵字 → 看到實際畫面 → **整支下載，或只切你要的那幾秒**，連同授權資訊整理成一份清單。

給剪片用的：找 B 站／YouTube 上的長素材（街景、空景、寺廟、人物走動…），
要留檔就整支抓，只要幾秒就只切那幾秒，順手記下出處與授權狀態。

- **三種下載方式**：整支、固定長度切片、自訂區段（`1:00-2:30`）
- **介面內直接播放**：下載完不必離開網頁就能預覽、跳到你想確認的那一格
- **6 組主題**（3 深 3 淺）＋ 可拖曳側欄寬度，選擇會記住
- **網頁介面 + 完整 REST API**：外部工具可以直接調用所有功能
- **設定存一次就變預設**：CLI 與 API 共用同一份 `config.json`
- **可包成 Docker**：`docker compose up -d` 就跑起來

---

## 目錄

- [快速開始](#快速開始)
- [網頁介面](#網頁介面)
- [命令列](#命令列)
- [給 AI agent 用](#給-ai-agent-用)
- [設定](#設定)
- [REST API](#rest-api)
- [Docker](#docker)
- [授權這件事](#授權這件事重要)
- [版本與 build number](#版本與-build-number)
- [開發](#開發)
- [疑難排解](#疑難排解)

---

## 快速開始

### Docker（推薦）

```bash
docker compose up -d
```

開 <http://127.0.0.1:8310>。專案、下載的素材、B 站 cookie、設定都在 `vidgrab-data` 這個 volume 裡。

```bash
# 不用 compose
docker build -t vidgrab .
docker run --rm -p 8310:8310 -v vidgrab-data:/data vidgrab
```

### 本機 Python

```bash
cd vidgrab
./vidgrab web          # 開網頁介面
./vidgrab login        # 掃碼登入 B 站（高畫質／會員片需要）
```

第一次執行會自己建 venv、照 `pyproject.toml` 裝依賴。需要 `ffmpeg` 與 `uv`：

```bash
brew install ffmpeg uv
```

---

## 網頁介面

`./vidgrab web` → <http://127.0.0.1:8310>

左邊輸入關鍵字（一行一個）和篩選條件，按「搜尋素材」，
右邊出現候選清單：每支都有**實際畫面縮圖**、時長、觀看數、授權標記。
勾選要的 → 選下載方式 → 按「切片段」。檔案在 `projects/<專案>/clips/`，直接拖進剪輯軟體。

### 下載方式

| 模式 | 意思 |
|---|---|
| **切片（每段固定）** | 從自動挑的時間點（或你指定的 `90, 3:20`）切固定長度 |
| **切片（自訂區段）** | 填 `1:00-2:30, 5:00-6:00`，只抓這幾段 |
| **整支下載** | 整支影片原樣抓下來，不切 |

其他開關：**抓畫面**（不下載整支，只抓那秒的畫面）、**只要聲音**（環境音、白噪音）、
**重新確認授權**（回頭查平台欄位與影片是否還在）、
**記住這些設定**（這次用的下載參數存成預設）。

### 播放已下載的片段

每支已下載的檔案後面有 **▶ 播放** 和 **下載**。按播放，影片會固定在畫面上方（捲動時不會跑掉），
下面是這支影片的幀圖——**點幀圖就跳到那個時間點**，確認你要的畫面在不在，不用整支看完。

播放器顯示解析度與長度；按 **關閉** 收起。整支下載的檔案標示為「整支」，切片則標示實際區間（如 `1:00–1:15`）。

### 素材庫（大型面板）

側欄的「我的素材專案」按 **⤢ 展開**，會打開一個大面板，一次看清所有專案：
卡片上有支數、已下載段數、佔用空間、建立日期。

- **改名**：雙擊卡片，或按「改名」。名稱只是顯示用的標籤，**資料夾不會被搬動**，下載紀錄與片段路徑完全不受影響
- **釘選**：按「釘選」，重要的專案永遠排在最前面（任何排序方式下都成立）
- **排序**：手動順序／建立時間／名稱／片段數／候選數。手動拖曳卡片就能改順序，拖過之後自動切回「手動」
- 排序方式存在伺服器（`library.json`），換瀏覽器、重開容器都一樣

### 清單是「權威記錄」，不會因為資料夾不見就消失

`projects/library.json` 是一份**獨立的穩定記錄**。重要規則：

- 專案資料夾不見了（換 `VIDGRAB_HOME`、誤搬、外接碟沒插）→ 記錄**保留**，
  標記 `missing`，卡片還在，只是變成虛線框
- 資料夾放回來 → **自動復原**，不需要任何操作
- 只有你明確按「移除記錄」才會刪

面板右上角有 **⟳ 掃描**：比對磁碟與記錄，列出兩種不一致並各自給一個按鈕 ——
「磁碟有、記錄沒有」可以**全部納入**；「記錄有、磁碟沒有」可以移除空記錄
（只刪記錄，不動任何檔案）。

### 主題與側欄

右上角的 ◐ 按鈕切換主題，六組分別是：

| 深色 | 淺色 |
|---|---|
| **Midnight** 深藍，預設 | **Paper** 米白，像紙 |
| **Ink** 近黑配金，長時間看最舒服 | **Mist** 冷灰藍，乾淨 |
| **Forest** 墨綠，柔和 | **Sakura** 暖粉 |

沒選過的話會跟著系統的深淺色走。選擇存在瀏覽器裡，下次打開還是同一套。

深淺色的判定有兩種：`prefers-color-scheme` 決定**預設值**，
選單裡的分組標籤（深色／淺色）決定**分類**。所以就算系統是深色，
你仍可以挑 Paper（淺色）並固定住——不會被系統蓋回去。

側欄邊界可以**拖曳調寬**（240–620px），雙擊還原成 330px，也能用鍵盤 ←/→ 調整。
寬度一樣會記住。視窗變窄時會自動收成上下排列。

> **主題選單的兩個坑（都已修，並有測試盯著）**
>
> 1. **被壓成一條縫**：`aside * { max-width:100% }` 原本是防止側欄內容溢出，
>    但它也會把 `position:absolute` 的浮層壓成父元素寬度（◐ 按鈕的 32px），
>    選單只剩 32px。現在浮層明確寫 `min-width` / `max-width`。
>    → `test_theme_menu_has_explicit_width`、UI 斷言「選單寬度 ≥180px」
> 2. **淺色主題下文字隱形**：選單按鈕原本用 `color:var(--text)`，
>    而 `--text` 會跟著目前主題變。切到 Paper／Mist／Sakura 時，
>    深色文字配深色浮層，對比只有 **1.2:1**（幾乎看不見）。
>    現在固定用淺色字，實測 **15.86:1**。
>    → `test_theme_menu_labels_use_fixed_light_color`、UI 斷言「對比 ≥4.5」

---

## 命令列

```bash
# 搜一輪（多個關鍵字分開搜再合併）
./vidgrab search "台北 街景" "北歐 空景" --platform both --min-duration 20

# 看結果
./vidgrab show demo-taipei

# 切片：前 3 支各切一段，順便抓畫面
./vidgrab pick demo-taipei --top 3 --per 15 --frames

# 指定時間點
./vidgrab pick demo-taipei --select 1,3-4 --at 2:00 --at 25:30 --per 20

# 整支下載
./vidgrab pick demo-taipei --select 2 --full

# 自訂區段（可重複、可逗號分隔）
./vidgrab pick demo-taipei --select 1 --range 1:00-2:30 --range 8:00-8:30

# 只要聲音
./vidgrab pick demo-taipei --select 2 --audio --per 60

./vidgrab purge demo-taipei        # 篩掉教學／課程
./vidgrab prune demo-taipei        # 清孤兒檔案
./vidgrab list          # 列出所有專案
./vidgrab login         # 掃碼登入 B 站
./vidgrab build         # 版本與 build number
./vidgrab test          # 跑測試 + Dockerfile 檢查
```

---

## 給 AI agent 用

人看的是 HTML／CSV，機器要的是另一套東西。這幾個指令專為 agent 設計，
**stdout 只有 JSON**，不含任何給人看的裝飾文字。

```bash
./vidgrab vault                      # 整個素材庫的快照（主要入口）
./vidgrab vault <專案>                # 單一專案（含每支素材與每個片段）
./vidgrab vault --query "夜晚 街景"    # 在「已下載的素材」裡搜尋
./vidgrab batch <專案> --top 3 --range 1:00-1:30   # 批次抓片
./vidgrab find "bilibili:BV1xx#60-75"              # 用 id 找回素材
./vidgrab purge <專案> --dry-run                   # 篩掉教學（先看會刪什麼）
./vidgrab prune <專案> --dry-run                   # 清孤兒檔案
```

### 穩定 id

每個素材與片段都有**可重算的 id**：重新掃描、搬專案、重建清單都不會變。
agent 可以把它存進自己的記憶，之後永遠找得回來。

| 對象 | id 長相 |
|---|---|
| 一支影片 | `bilibili:BV1EE411k75J` |
| 一個片段 | `bilibili:BV1EE411k75J#60-75` |
| 整支 | `bilibili:BV1EE411k75J#full` |

用的是「平台 + 平台自己的編號」與「秒數範圍」，不是資料庫序號——
所以就算 `library.json` 被砍掉重練，id 還是同一個。

### 一次呼叫就拿到全部

`vault` 刻意做成「一個指令回完整狀態」，因為 agent 不擅長多輪探索：

```json
{
  "schema": "vidgrab.vault/1",
  "root": "/絕對路徑/projects",
  "totals": {"projects": 5, "clips": 31, "bytes": 193881525},
  "projects": [{
    "name": "20261105-handpan",
    "pinned": true,
    "counts": {"candidates": 11, "with_clips": 5, "clips": 5},
    "items": [{
      "id": "bilibili:BV1xx",
      "title": "…",
      "license": {"kind": "unknown", "label": "未標示"},
      "search_terms": ["手碟", "音樂"],
      "credit_hint": "授權未標示，使用前請先聯繫 …",
      "clips": [{
        "id": "bilibili:BV1xx#60-75",
        "path": "/絕對路徑/…mp4",
        "exists": true,
        "start": 60, "end": 75, "duration": 15.0,
        "cached": false
      }]
    }]
  }]
}
```

幾個刻意的設計：

- **路徑一律絕對路徑**，agent 拿到就能直接用，不用自己拼
- **`exists` 旗標**：清單有記錄但檔案被刪掉時，agent 看得出來
- **`cached` 旗標**：分辨「這次真的下載」與「本來就在」
- **`credit_hint` 直接给可讀句子**，agent 不用自己判斷授權風險

### 冪等：重跑不會重複下載

`batch` 對「同一個片段的同一個範圍」是冪等的。重跑只會回 `new_clips: []`，
不會重新下載、也不會產生重複檔案。所以 agent 可以安全地重試。

```json
{"schema": "vidgrab.batch/1", "requested": 3, "ok": 3, "failed": 0,
 "elapsed_seconds": 12.4, "dry_run": false,
 "results": [{"index": 0, "asset_id": "bilibili:BV1xx",
              "new_clips": [...], "error": null}]}
```

- **部分成功**：某支失敗不影響其他支，每支各自回報
- **`--timeout`**：批次最多跑幾秒，避免 agent 被單一請求卡死
- **`--dry-run`**：只回報要抓什麼，不動網路

### 錯誤也是結構化的

失敗時不會吐 traceback，而是：

```json
{"ok": false, "error": {
  "code": "bad_input",
  "message": "區段要寫成 開始-結束（收到 abc）",
  "retryable": false,
  "hint": "例如 --range 1:00-2:30"
}}
```

`retryable` 讓 agent 知道該重試還是該換方法。可用的 code：

| code | 意思 | 可重試 |
|---|---|---|
| `not_found` | 找不到專案／素材 | 否 |
| `bad_input` | 參數寫錯 | 否 |
| `ambiguous_project` | 專案名對到多個 | 否 |
| `no_project` | 還沒有任何專案 | 否 |
| `no_targets` | 沒指定要抓哪幾支 | 否 |
| `login_required` | 需要登入 B 站 | 否 |
| `download_failed` | 下載失敗 | **是** |
| `timeout` | 批次逾時 | **是** |
| `missing_ffmpeg` | 缺 ffmpeg | 否 |
| `internal` | 未預期錯誤 | **是** |

### 在已下載的素材裡搜尋

`search` 是去平台找**新片**；`vault --query` 是在你**手上的素材**裡找。

```bash
./vidgrab vault --query "台北 夜晚"
```

比對標題、作者、標籤、素材關鍵字，回傳帶 `score` 與 `matched` 的排序結果。
這是 agent 最常用的一步——先問「我手上有什麼可以用」，再決定要不要去抓新的。

### 繁體關鍵字會自動轉簡體

B 站是簡體站，用繁體字搜尋常常一支都搜不到：

```
搜「寺廟 建築」→ 0 支      ← 繁體
搜「寺庙 建筑」→ 12 支     ← 簡體
```

現在 `search` 會**原文與簡體都搜、取聯集**。原文一定排第一，
所以就算對照表不完整，原本能搜到的結果也不會消失。
英文與已經是簡體的關鍵字不會多搜（不浪費請求）。

內建對照表在 `zh.py`，刻意不引入 opencc／zhconv——那會讓 Docker 映像大一倍。

### 自動濾掉教學／課程

素材庫要的是**可剪的畫面**，不是公開課。標題含「教程」「公開課」「第 N 講」
「建模」「零基礎」這類字眼的會自動排除：

| 會濾掉 | 會保留 |
|---|---|
| 【清华大学公开课】中国建筑史 全集 | 山西悬空寺 航拍 4K 空镜 |
| 【3dmax建模】古建筑场景制作教程 | 雨后深山鸟鸣声，云雾缭绕 |
| 佛教建筑第一讲｜亚洲佛教建筑总序 | 冬夜寺院燃烧的香烛 |

舊專案（過濾規則加入前搜的）可以用 `./vidgrab purge` 重新整理，
`--dry-run` 先看會刪什麼。

### 下載進度與即時更新

按下「切片段」之後，畫面下緣會出現進度卡：

```
第 2 / 3 支   臺灣老街 4K 空鏡素材             47%
████████████████░░░░░░░░░░░░░░░░░░░░░░░
1.9MB/s · 分段 12/25
```

- **兩條進度**：細的是當前影片，粗的是整個作業（3 支裡的第幾支）
- **下載途中就會出現新片段與截圖**，不必等整個作業跑完
- 頂部細條保留，滾動時也看得到有事在跑

#### 一個實話：切片下載沒有真實百分比

`--download-sections` 讓 yt-dlp 把工作交給 ffmpeg 直接從位元流截，
yt-dlp 只在**結束時**吐一行進度，中間完全是空的。

所以切片時用「心跳」估算：確實有東西在下載，就照已花時間往前走，
標成 `estimated`，最多走到 95%，最後那 5% 留給真正的完成事件。

整支下載（不切片）走的是正常路徑，百分比是量出來的。

> 進度卡出現時，底部控制台會自動往上讓，不會互相遮住。

### 清理孤兒檔案

`purge` 只清清單，磁碟上的檔案還在。`prune` 清掉沒被清單追蹤的孤兒：

```bash
./vidgrab prune <專案> --dry-run   # 先看會刪什麼
./vidgrab prune <專案>             # 實際清（含 .info.json sidecar）
```

實測：一個專案從 17MB 降到 3MB，清單在用的檔案一個都沒動。

> **風控要注意**
>
> 短時間搜太多次，B 站會回 `v_voucher`（風控），這時**換 cookie 沒用**，
> 要等冷卻。原本的訊息寫「換 cookie」會把人帶去重掃碼，已經改正。
> 繁簡雙搜會讓請求數翻倍，搜尋量大時要留意。

### 常用篩選

| 參數 | 意思 |
|---|---|
| `--min-duration 20` | 只要 20 分鐘以上的長素材（出廠 10） |
| `--max-duration 90` | 最長 90 分鐘（0 = 不限） |
| `--order views` | 先看熱門的 |
| `--fresh-days 365` | 只看最近一年發布 |
| `--exclude 合集 --exclude 直播` | 標題含這些詞就跳過 |
| `--only-free` | 只留授權明確的 |
| `--drop-reposts` | 丟掉轉載／非原始出處 |
| `--verify-license` | 逐支抓詳情確認授權（慢，但準） |

---

## 設定

**設定一次，之後就是預設值。** 存在資料根目錄的 `config.json`（Docker 裡是 `/data/config.json`），
CLI 和 API 共用。重開容器也還在。

```bash
./vidgrab config                        # 看目前設定（改過的會標「已改」）
./vidgrab config --set per=30 --set height=720
./vidgrab config --json                 # 給程式讀
./vidgrab config --reset per            # 還原單項
./vidgrab config --reset                # 全部還原
```

也可以在搜尋或下載時順手存下來：

```bash
./vidgrab search "街景" --per 20 --min-duration 15 --height 720 --save
./vidgrab pick demo-taipei --select 1 --per 30 --save
```

### 可設定的項目

| 項目 | 預設 | 說明 |
|---|---|---|
| `platform` | `both` | `both` / `bilibili` / `youtube` |
| `per_keyword` | `12` | 每個關鍵字抓幾支 |
| `order` | `smart` | `smart` / `relevance` / `views` / `new` |
| `duration` | `any` | `any` / `short` / `medium` / `long` / `verylong` |
| `min_duration` | `10` | 最短幾分鐘 |
| `max_duration` | `0` | 最長幾分鐘（0 = 不限） |
| `fresh_days` | `0` | 只看最近 N 天（0 = 不限） |
| `min_views` | `0` | 至少幾個觀看 |
| `exclude` | `[]` | 標題含這些詞就跳過 |
| `only_free` | `false` | 只留授權明確的 |
| `drop_reposts` | `false` | 丟掉轉載 |
| `verify_license` | `false` | 逐支確認授權 |
| `sort` | `smart` | 清單排序 |
| `per` | `15` | 每段幾秒 |
| `height` | `1080` | 最高解析度 |
| `audio` | `false` | 只要聲音 |
| `audio_format` | `m4a` | m4a / mp3 / wav / opus |
| `frames` | `true` | 順便抓畫面 |
| `frame_count` | `3` | 抓幾格 |
| `host` / `port` | `127.0.0.1` / `8310` | 服務位址 |

環境變數的優先序比設定檔高：`VIDGRAB_HOST`、`VIDGRAB_PORT`、`VIDGRAB_BUILD`、
`VIDGRAB_HOME`、`VIDGRAB_AUTH_DIR`、`VIDGRAB_CONFIG`。

---

## REST API

所有功能都有 API。完整規格在 **<http://127.0.0.1:8310/api/openapi.json>**
（OpenAPI 3.0，可直接匯入 Postman，或餵給 LLM 讓它自己學會怎麼呼叫）。

長時間的工作（搜尋、下載）是**非同步**的：POST 拿 `job` id → 輪詢 `/api/job/{id}`，
或訂閱 `/api/stream/{id}` 的 SSE 看即時進度。

### 範例

```bash
# 搜尋（非同步）
JOB=$(curl -s -X POST http://127.0.0.1:8310/api/search \
  -H 'Content-Type: application/json' \
  -d '{"keywords":["台北 街景"],"min_duration":20,"per_keyword":10}' \
  | jq -r .job)

# 等它做完（SSE 即時進度）
curl -N http://127.0.0.1:8310/api/stream/$JOB

# 看結果
curl -s http://127.0.0.1:8310/api/project/20261105-台北_街景 | jq '.items[].title'

# 整支下載第 0 支
curl -s -X POST http://127.0.0.1:8310/api/pick \
  -H 'Content-Type: application/json' \
  -d '{"project":"20261105-台北_街景","indexes":[0],"full":true}' | jq -r .job

# 只切 1:00–2:30 這一段
curl -s -X POST http://127.0.0.1:8310/api/pick \
  -H 'Content-Type: application/json' \
  -d '{"project":"20261105-台北_街景","indexes":[0],"ranges":[[60,150]]}'

# 改設定，之後都吃這個
curl -s -X POST http://127.0.0.1:8310/api/config \
  -H 'Content-Type: application/json' \
  -d '{"set":{"per":30,"height":720}}'
```

### 端點一覽

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/version` | 版本與 build number |
| GET | `/api/openapi.json` | OpenAPI 規格 |
| GET | `/api/config` | 讀設定與預設值 |
| POST | `/api/config` | 改設定（`{"set":{...}}` 或 `{"reset":[...]}`） |
| POST | `/api/search` | 搜尋並建立專案（回 `job`） |
| POST | `/api/pick` | 整支／切片／抓畫面（回 `job`） |
| GET | `/api/job/{id}` | 工作狀態與完整訊息 |
| GET | `/api/stream/{id}` | SSE 即時進度（`log` / `done` / `error`） |
| GET | `/api/projects` | 所有專案索引 |
| GET | `/api/project/{name}` | 單一專案（含候選與已下載片段） |
| GET | `/api/item/{name}/{i}` | 單一候選影片 |
| GET | `/api/library` | 素材庫（改名／釘選／排序後的清單） |
| GET | `/api/library/scan` | 掃描 projects/，找出未納入與遺失的專案 |
| GET | `/api/asset/{專案}/{索引}` | 單支素材的完整 metadata |
| POST | `/api/library` | 改名、釘選、排序、拖曳（見下） |
| POST | `/api/project/delete` | 刪除整個專案 |
| POST | `/api/delete-clip` | 刪除單一片段檔 |
| POST | `/api/refresh` | 重新確認授權（回 `job`） |
| GET | `/api/export` | 匯出設定與專案索引（備份用） |
| POST | `/api/login/start` | 產生登入 QR |
| GET | `/api/login/qr` | QR 圖（PNG） |
| POST | `/api/login/poll` | 查掃碼結果 |
| GET | `/api/login/status` | 目前登入帳號 |
| POST | `/api/login/logout` | 登出 |
| POST | `/api/open` | 用瀏覽器打開 HTML 清單 |
| GET | `/file/{專案}/{路徑}` | 取回片段或縮圖（支援 Range） |

`/api/library` 的動作：

```bash
# 改名（只改顯示名稱，不動資料夾）
curl -X POST http://127.0.0.1:8310/api/library -H 'Content-Type: application/json' \
  -d '{"action":"rename","project":"20261105-台北_街景","title":"台北街景素材"}'

# 釘選（永遠排最前）
curl -X POST http://127.0.0.1:8310/api/library -H 'Content-Type: application/json' \
  -d '{"action":"pin","project":"20261105-台北_街景","pinned":true}'

# 排序：manual / created / name / clips / count
curl -X POST http://127.0.0.1:8310/api/library -H 'Content-Type: application/json' \
  -d '{"action":"sort","mode":"clips"}'

# 手動指定順序，或上／下移一格
curl -X POST http://127.0.0.1:8310/api/library -H 'Content-Type: application/json' \
  -d '{"action":"order","order":["專案A","專案B"]}'
curl -X POST http://127.0.0.1:8310/api/library -H 'Content-Type: application/json' \
  -d '{"action":"move","project":"專案A","delta":-1}'
```

> 在 `/api/search` 或 `/api/pick` 帶 `save_settings: true`，
> 這次呼叫用的參數就會同時變成預設值。

---

## Docker

```bash
./tools/docker-build.sh          # build number 自動取 git
docker compose up -d
```

三段建置：wheel → **測試** → runtime。測試不通過，映像就建不起來。

```bash
docker exec -it vidgrab vidgrab login --no-open   # 容器內登入
docker exec vidgrab vidgrab config --set per=30   # 容器內改設定
docker exec vidgrab vidgrab build                 # 確認跑的是哪次建置
```

資料都掛在 `/data`：`projects/`（素材）、`auth/`（cookie）、`config.json`（設定）。
容器以非 root（uid 1000）執行。

---

## 授權這件事（重要）

工具會自動標記授權，但那是**從標題、說明、平台欄位推測的，不能當法律依據**：

| 標記 | 意思 |
|---|---|
| `可自由使用` | CC0／公共領域 |
| `可用需標註` | YouTube 標示 CC BY，可營利用，要標作者 |
| `需確認授權` | 自稱免費、素材站、CC 但沒說哪一種 |
| `未知` | 絕大多數情況，**預設當作不能直接用** |

公開使用的素材，要嘛找明確授權的（Pexels、Pixabay、YouTube CC BY），
要嘛只用短片段、以評論或教學形式引用，並在影片說明標明出處、作者、原片連結。
`index.html` 底部也寫了同樣的提醒。

### 素材 metadata

**下載的當下**就把完整資訊寫進 `results.json`（以及 yt-dlp 的 `.info.json` sidecar），
不必事後回頭爬——影片可能下架、改標題、改授權，那時就補不回來了。

每支素材都有：

| 欄位 | 內容 |
|---|---|
| `tags` / `categories` | 平台標籤與分類（已過濾 `yt:cc=on` 這種技術標記） |
| `search_terms` | **找素材用的關鍵字**＝標籤＋分類＋你的搜尋詞 |
| `author` / `channel` / `author_url` | 作者與其頁面（要標註或聯繫時用） |
| `webpage_url` / `thumbnail` | 原始頁面與封面 |
| `pubdate` / `language` | 發布日與語言 |
| `width` / `height` / `fps` | 解析度 |
| `license` | 授權類別、原始授權名稱 |
| `contact.credit_hint` | 一句話的標註建議 |

`CSV` 也補上了這些欄位（作者頁面／素材關鍵字／分類／標註建議／解析度），
可以直接匯進素材管理工具。

既有專案（metadata 功能上線前建立的）跑一次回填：

```bash
./vidgrab refresh demo-taipei     # 重新抓一次詳情，補齊所有欄位
./vidgrab scrub                   # 只清技術標記，不重新抓取
```

介面上不再對每支影片掛「未知」膠囊（幾乎每支都是，掛了等於雜訊）。
只有真的查出授權的才會顯示標籤；其餘在清單上方用一句「N 支未標示授權」帶過。

---

## 版本與 build number

```bash
./vidgrab build          # vidgrab 0.2.0+2.8436bba
./vidgrab build --json   # {"version":"0.2.0","build":"2.8436bba","full":"..."}
```

來源依序：`VIDGRAB_BUILD` 環境變數 → **git commit 數＋短 hash** → 打包時寫入的 `_build_info.py` → `dev`。
Docker 建置會自動帶入，容器內 `vidgrab build` 就知道跑的是哪一次建置。網頁左上角也會顯示。

> **為什麼 git 排在 stamp 檔前面**
>
> `_build_info.py` 是 stamp 當下寫的快照。它一旦存在、又排在 git 前面，
> 就會**永遠蓋掉 git**：明明已經有 11 個 commit，`./vidgrab build` 還一直顯示 `3.d79b3e3`。
> 正確的順序是「有 git 就信 git（最新），沒有 git（Docker／pip 安裝）才看 stamp 檔」。
> 三種來源各有一條測試：`test_build_env_wins`、`test_build_prefers_git_over_stale_stamp`、
> `test_build_falls_back_to_stamp_without_git`。
>
> 改了 `version.py` 之後若行為沒變，先清 `__pycache__`——
> 用 `cp` 還原檔案會保留舊 mtime，Python 會繼續用過期的 `.pyc`（我就被這個騙過一次）。

---

## 開發

```bash
./vidgrab test                     # pytest + Dockerfile 靜態檢查
./vidgrab test --ui                # 另外驗證介面（自動起臨時 Chromium）
python tools/stamp_build.py        # 手動寫入 build number
python tools/check_docker.py       # 只跑 Dockerfile 檢查
```

### 關於瀏覽器

vidgrab 開介面、開報告、開 QR 圖，一律用 `webbrowser.open()`，
也就是**你系統的預設瀏覽器** —— 不需要 Chrome，也沒有任何寫死的瀏覽器路徑。
（有測試 `test_no_hardcoded_browser` 盯著這件事。）

只有 `--ui` 的介面自我驗證需要 CDP，那是 Chromium 系專有的協定
（Firefox／Safari 不支援）。它會自己起一個**臨時的、用完就關**的 Chromium；
找不到就直接跳過並說明原因，不會擅自幫你開瀏覽器、也不會改動你的 HOME
或既有瀏覽器設定。

```bash
python tools/check_ui.py                 # 連現有的 CDP（9222）
python tools/check_ui.py --launch        # 自己起一個臨時的（建議）
python tools/check_ui.py --launch --headful   # 想看到畫面就加這個
```

專案結構：

```
src/vidgrab/
├── cli.py       命令列
├── web.py       Web UI + REST API（含 OpenAPI）
├── tasks.py     搜尋／下載流程（CLI 與 Web 共用）
├── sources.py   B 站 wbi 簽名搜尋、YouTube yt-dlp 搜尋
├── media.py     下載、切片、抓畫面
├── config.py    持久化設定
├── library.py   素材庫（改名／排序／釘選）
├── auth.py      B 站掃碼登入
├── report.py    HTML／CSV 清單
└── models.py    資料結構與工具
```

CI（`.github/workflows/test.yml`）在 Python 3.9／3.12 上跑測試，
並真的建一次 Docker 映像、起容器驗證健康。

---

## 疑難排解

| 症狀 | 解法 |
|---|---|
| B 站搜尋 0 支 | 多半被要求驗證，重跑一次；或 `./vidgrab login` |
| B 站只抓到 360p | 沒登入。`./vidgrab login` |
| YouTube 說要驗證機器人 | `--cookies-from-browser chrome` |
| 整支下載 567 錯誤 | 已內建 `--http-chunk-size` 繞過 B 站音訊 CDN 限制；仍失敗先確認登入有效 |
| 抓畫面失敗 | 該片可能要登入或有地區限制 |
| 容器裡抓不到 1080p | 容器內也要登入：網頁按「掃碼登入」 |
| 設定沒生效 | `./vidgrab config` 看實際值；環境變數優先序比設定檔高 |
| 想換 port | `./vidgrab web --port 8080` 或 `./vidgrab config --set port=8080` |
