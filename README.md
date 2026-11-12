# oneflow：三種後勤助手，一次只做一家、一個流程

**English summary.** Three small back-office assistants for one company at a time, on one shared
base: order entry (PDF/Excel orders to a confirmed ERP export), receivables (overdue items,
customer mail and reminder drafts) and repair intake (LINE messages or a web form to a booked
visit). The interface is Traditional Chinese. Every outward step needs a person: nothing is
exported, sent or booked by the program alone. **No model is called anywhere; reading is done by
rules. All test data is invented by the author of the rules, so the figures below show that the
rules behave as designed, not how they do on a real company's documents.**

## 現況先講清楚

| 項目 | 現況 |
|---|---|
| 測試資料 | 全部是虛構的，而且和規則出自同一人。下面的數字只說明「規則照設計運作」，**不是**真實文件上的準確率。 |
| 語言模型 | **完全沒有呼叫**。訂單讀取、郵件分類、報修欄位都是程式規則。讀不懂就轉人工，不會猜。 |
| 節省時間 | **沒有量測**。這需要真人用新舊兩種方式各做一次。訂單畫面已記錄「開啟到確認」的秒數，可供日後量測。 |
| ERP 格式 | 目前是通用 CSV（UTF-8 或 Big5）。實際欄位要拿到該公司 ERP 的匯入規格才能對。 |
| 寄送提醒 | 「寄送」是把 `.eml` 寫入寄件匣資料夾，由使用者用自己的信箱寄出。沒有 SMTP。 |
| LINE | Webhook 簽章驗證與回覆呼叫都寫了，測試用替身跑過，**從未連上真正的 LINE**。 |
| 真實使用者 | 沒有。沒有部署，沒有任何一家公司用過。 |

## 三個流程

| | 訂單輸入助手 | 應收帳款助手 | 維修接單助手 |
|---|---|---|---|
| 使用者 | 業務助理 | 會計 | 調度人員 |
| 輸入 | PDF、Excel 訂單；商品表、客戶料號表 | 應收帳款匯出（CSV／Excel）；`.eml` 郵件 | 網頁表單；LINE 官方帳號文字訊息 |
| 程式做的事 | 找表頭、讀料號／數量／交期，比對商品表，列出問題 | 算逾期、列缺件、把來信分堆並暫停相關帳款、產生提醒草稿 | 固定問四題：地區、設備、狀況、方便時間 |
| 人做的事 | 修正、確認、按匯出 | 看信、解除暫停、核准每一封、寄出 | 聯絡客戶、確認到府時間 |
| 程式不做 | 出貨、改價、寫入正式帳 | 認定欠款、催討有爭議的款項 | 診斷、報價、預約、處理緊急事故 |

### 共用底座（`oneflow/base.py`、`oneflow/rules.py`）

- **繁體中文介面**與**登入**：密碼以 scrypt 雜湊；角色決定能進哪個流程。
- **原文對照**：每個畫面左邊是收到的原文，右邊是程式讀出的結果，訂單明細會標出對應的原文列。
- **修改紀錄**：誰在什麼時候把哪個欄位從什麼改成什麼。
- **重複處理防護**：同一個檔案只處理一次；同客戶同單號的訂單不能確認；一張訂單只匯出一次；
  一筆帳款每個階段只寄一次；LINE 重送的事件只處理一次；一筆報修只確認一次。
- **失敗人工接手**：讀不懂的檔案、歸不了戶的來信、可能的緊急狀況、問不齊的報修，都進「人工接手」清單。
- **金額和日期用程式規則驗證**：民國年、全形數字、千分位、不存在的日期（2/30）、
  交期早於訂單日、已收超過應收等。不合規則的值不會被採用，會留給人填。
- **資料使用同意**：匯入時標示「模擬」或「真實」。真實文件必須指定一筆涵蓋該流程、未撤回的同意紀錄，否則拒絕。

## 驗收結果（虛構資料）

完整輸出在 [`docs/acceptance-run.txt`](docs/acceptance-run.txt)，可用 `uv run python scripts/accept.py` 重跑。

### 訂單輸入：50 個檔案、208 列明細

五種版面（三種 PDF、兩種 Excel），41 列故意埋了問題：客戶自己的料號、寫法不同、打錯料號、
寫成別的尺寸的料號、停用料號、以盒／包訂購、全形數字、壞掉的數量、不明單位、缺交期、
交期早於訂單日、不存在的日期、單價與牌價不同。

| | 料號 | 數量 | 交期 |
|---|---|---|---|
| 正確，沒有提醒 | 193 | 196 | 175 |
| 正確，附提醒請人確認 | 6 | 9 | 26 |
| 程式不填，留給人 | 6 | 3 | 7 |
| 填錯，但有提醒 | 3 | 0 | 0 |
| **填錯，沒有任何提醒** | **0** | **0** | **0** |

- 47 張待確認的訂單中，21 張沒有任何問題可直接確認，26 張有提醒或必須處理的項目。
- 2 張重送的訂單（同客戶同單號）都被擋下；1 個沒有文字的掃描檔轉人工。
- 「填錯但有提醒」的 3 列：客戶寫了另一個尺寸的有效料號，品名卻是原本要的那個。程式照料號比對，
  再用品名裡的數字發現不一致。
- 交期「附提醒」多數是只寫月日、沒有年份的訂單，年份由訂單日期推定。

### 應收帳款：一個月 60 筆帳款、16 封來信

| 項目 | 結果 |
|---|---|
| 逾期帳款 | 36 筆中找到 36 筆，沒有多報 |
| 埋入的資料問題與缺件 | 12 筆全部標出 |
| 來信分堆正確 | 15 封中 14 封（另 1 封是同一封信匯出兩次，只讀一次） |
| 可以催收的帳款 | 11 筆，全部產生草稿；重跑一次不會多產生 |
| 寄給已付款、暫停中或有爭議帳款的提醒 | **0** |
| 重複寄送 | **0**（9 封寄出後各再試一次，9 次都被拒絕） |
| 核准後才入帳的 2 筆 | 匯入新的帳款檔時，2 封待寄的提醒自動取消 |

- 分錯堆的那封信寫的是「是否收到我們的匯款？」，關鍵字沒有認出它是「已付款」，放進了「有來信待閱讀」。
  **帳款仍然被暫停**：只要客戶有來信，相關帳款就停，分堆只影響清單上的標題。
- 有一封信從私人信箱寄來、沒寫發票號碼，程式無法歸戶。這時該客戶的 3 筆帳款還是產生了草稿，
  但**全部 14 份草稿都不能核准**，直到有人把那封信歸戶；歸戶後那 3 份草稿自動取消。
- 「缺件」在這裡的意思是：缺發票號碼、缺聯絡信箱、缺簽收單。這是我的解讀，需要和實際的會計確認。

### 維修接單：30 個模擬詢問

| 項目 | 結果 |
|---|---|
| 可能的緊急狀況（瓦斯味、冒煙、燒焦味） | 3 件全部立即轉人工；沒有誤判（「瓦斯爐點不著火」不算） |
| 一般報修 | 27 件 |
| 四項資料齊全 | 24 件，**資料完整率 88.9%** |
| 齊全但欄位有錯 | 0 件 |
| 沒問齊的 3 件 | 1 件客戶不再回覆、1 件不願說地區（問兩次後轉人工）、1 件只是問價錢 |
| 非營業時間進線 | 11 件，其中 10 件問齊，經人工確認後成為 10 張有效工單 |
| 問價錢或故障原因 | 3 件，都回覆「無法報價或判斷原因」 |

模擬的客戶會乖乖回答被問到的問題，真人不一定。「今天下午或明天上午都可以」這類說法，
程式只保留原話，不替客戶選日期。

## 執行

需要 Python 3.12 與 [uv](https://docs.astral.sh/uv/)。不需要另外安裝資料庫：沒有設定 `DATABASE_URL` 時，
會用 `pgserver` 套件在 `./.pgdata` 啟動一個 PostgreSQL。

```bash
uv sync
uv run pytest                                   # 102 個測試，使用真正的 PostgreSQL
uv run python scripts/accept.py                 # 三個流程的驗收數字
ONEFLOW_DEMO_PASSWORD=自訂密碼 uv run python scripts/seed_demo.py   # 建立示範帳號並載入虛構資料
uv run uvicorn oneflow.app:app --port 8813      # 開啟 http://127.0.0.1:8813
```

示範帳號：`admin@example.test`（全部流程）、`sales@`、`acct@`、`dispatch@`（各一個流程）。
應收帳款頁面的基準日請選 2026-11-30。一次實際啟動伺服器的紀錄在 [`docs/live-run.txt`](docs/live-run.txt)。

| 環境變數 | 用途 |
|---|---|
| `DATABASE_URL` | 使用自己的 PostgreSQL |
| `ONEFLOW_OUTBOX` | 提醒信 `.eml` 的寄件匣資料夾（預設 `./outbox`） |
| `ONEFLOW_COMPANY` | 提醒信的署名 |
| `ONEFLOW_LINE_SECRET` | LINE channel secret；沒有設定時 webhook 回 503 |
| `ONEFLOW_LINE_TOKEN` | LINE channel access token；沒有設定時回覆只存檔、不送出 |

## 導入一家公司之前還缺什麼

1. **資料使用同意**，然後才是真實文件：50 張歷史訂單、一個月帳款與已知爭議、有限度的 LINE 試用。
2. **該公司的版面**。訂單讀取靠表頭字詞與欄位位置；沒見過的版面會轉人工，掃描檔一律轉人工。
   這裡才是值得評估語言模型或 OCR 的地方，而且要用同一份驗收表來比。
3. **ERP 匯入規格**，以及由誰、怎麼把匯出檔放進 ERP。
4. **基準量測**：同一批訂單，人工輸入要多久、錯幾筆；用這個系統含修正要多久、錯幾筆。
5. 帳號管理畫面、密碼重設、資料庫遷移、HTTPS 部署、備份。目前帳號只能用腳本建立。

## 檔案

| 路徑 | 內容 |
|---|---|
| `oneflow/base.py`、`rules.py`、`models.py`、`db.py`、`tables.py` | 共用底座、驗證規則、資料表 |
| `oneflow/orders.py`、`receivables.py`、`repairs.py` | 三個流程 |
| `oneflow/app.py`、`templates/` | 網頁介面 |
| `oneflow/acceptance.py`、`scripts/accept.py` | 驗收計數 |
| `scripts/make_*_fixtures.py`、`fixtures/` | 虛構測試資料與產生它的程式 |
| `tests/` | 102 個測試 |

## 套件與授權

fastapi 0.162.5（MIT）、jinja2 3.1.6（BSD）、sqlalchemy 2.1.8（MIT）、psycopg 3.3.9（LGPL-3.0）、
pgserver 0.1.4（Apache-2.0，內含 PostgreSQL）、pdfplumber 0.11.10（MIT）、pdfminer.six 20260107（MIT）、
openpyxl 3.1.5（MIT）、reportlab 5.1.0（BSD，只用來產生測試 PDF）、python-multipart 0.0.33（Apache-2.0）、
uvicorn 0.54.0（BSD）。測試 PDF 內嵌 Noto Sans TC 字型子集（SIL Open Font License）。
