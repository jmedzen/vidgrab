# vidgrab · 關鍵字找素材

打幾個關鍵字 → 看得到實際畫面 → **只下載你要的那幾秒**，連同授權資訊整理成一份清單。

給剪片用的：找 B 站／YouTube 上的長素材（街景、空景、寺廟、人物走動…），
不必把兩小時的片整支抓下來，只切需要的片段，順手記下出處與授權狀態。

---

## 快速開始

```bash
cd vidgrab

./vidgrab web          # 開網頁介面（推薦）
./vidgrab login        # 掃碼登入 B 站（高畫質／會員片需要）
```

第一次執行會自己建 venv、裝 yt-dlp，約一分鐘。需要 `ffmpeg` 與 `uv`：

```bash
brew install ffmpeg uv
```

---

## 網頁介面

`./vidgrab web` 會開 <http://127.0.0.1:8310>。

左邊輸入關鍵字（一行一個）和篩選條件，按「搜尋素材」，
右邊就出現候選清單：每支都有**實際畫面縮圖**、時長、觀看數、授權標記。
勾選要的 → 設定每段秒數（預設 15 秒）→ 按「切片段」。
切好的檔案在 `projects/<專案>/clips/`，直接拖進剪輯軟體。

- **抓畫面**：不必下載整支，只抓那個時間點的 1 秒畫面（成本極低）
- **指定秒數**：填 `90, 3:20, 1:05:30`，就從這些位置各切一段
- **只要聲音**：勾起來就只留音軌（做環境音、白噪音）
- **重新確認授權**：回頭再查一次平台上的授權欄位與影片是否還在

---

## 命令列

```bash
# 搜一輪（多個關鍵字分開搜再合併）
./vidgrab search "台北 街景" "北歐 空景" --platform both --min-duration 20

# 看結果
./vidgrab show demo-taipei

# 切片段：前 3 支各切一段，順便抓畫面
./vidgrab pick demo-taipei --top 3 --per 15 --frames

# 指定時間點
./vidgrab pick demo-taipei --select 1,3-4 --at 2:00 --at 25:30 --per 20

# 只要聲音
./vidgrab pick demo-taipei --select 2 --audio --per 60

# 列出所有專案
./vidgrab list
```

常用篩選：

| 參數 | 意思 |
|---|---|
| `--min-duration 20` | 只要 20 分鐘以上的長素材（預設 10） |
| `--max-duration 90` | 最長 90 分鐘（0 = 不限） |
| `--order views` | 先看熱門的 |
| `--fresh-days 365` | 只看最近一年發布 |
| `--exclude 合集 --exclude 直播` | 標題含這些詞就跳過 |
| `--only-free` | 只留授權明確的 |
| `--drop-reposts` | 丟掉轉載／非原始出處 |
| `--verify-license` | 逐支抓詳情確認授權（慢，但準） |

---

## 產出的東西

```
projects/<專案名>/
├── index.html      # 清單頁（雙擊就能看，有縮圖、授權、片段連結）
├── clips.csv       # 丟進試算表篩選
├── README.md
├── clips/          # 切好的片段
├── frames/         # 抓下來的畫面與拼版
├── manifest.json   # 搜尋條件
└── results.json    # 完整資料
```

---

## 授權這件事（重要）

工具會自動標記授權狀態，但那是**從標題、說明、平台欄位推測的，不能當法律依據**：

- `可用需標註`：YouTube 頁面標示 CC BY，可營利用，但要標作者與授權
- `可自由使用`：CC0／公共領域
- `需確認授權`：自稱免費、素材站、CC 但沒說哪一種
- `未知`：絕大多數情況，**預設當作不能直接用**

實務上的做法：公開使用的素材，要嘛找明確授權的（Pexels、Pixabay、YouTube CC BY），
要嘛只用短片段、以評論或教學形式引用，並且在影片說明標明出處、作者、原片連結。
`index.html` 底部也寫了同樣的提醒。

---

## 登入 B 站

```bash
./vidgrab login     # 產 QR → 手機 B 站 App 掃 → 自動存 cookie
./vidgrab logout
```

cookie 存在 `auth/bilibili-cookies.txt`（已列在 `.gitignore`）。
登入後搜尋結果更完整、比較不會被要求驗證，也能抓 1080p 以上的畫質。

---

## 它怎麼運作的

- **B 站搜尋**：直接打 `wbi/search/type`（wbi 簽名），拿到完整 metadata；遇到驗證頁會自動換 cookie 重試
- **YouTube 搜尋**：走 yt-dlp 的 `ytsearch`
- **切片**：yt-dlp `--download-sections` + `--force-keyframes-at-cuts`，只下載那幾秒
- **抓畫面**：每個時間點只下載 1 秒，再用 ffmpeg 抽一格
- **去雜訊**：標題／說明沒命中關鍵字詞的結果會濾掉（B 站常混進不相干的歌）

---

## 疑難排解

| 症狀 | 解法 |
|---|---|
| B 站搜尋 0 支 | 多半是被要求驗證，重跑一次；或 `./vidgrab login` |
| B 站只抓到 360p | 沒登入。`./vidgrab login` |
| YouTube 說要驗證機器人 | `./vidgrab pick ... --cookies-from-browser chrome` |
| 抓畫面失敗 | 該片可能需要登入或有地區限制，先 `./vidgrab login` |
| 想換 port | `./vidgrab web --port 8080` |
