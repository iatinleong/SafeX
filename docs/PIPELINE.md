# SafeW Pipeline 流程說明

本文件說明整個「聊天截圖 → 交易訊號 → Binance 下單參數」流程的架構、資料流、
以及每個檔案/元件的職責，供之後開發或除錯時快速對照。

## 專案結構

```
SafeW/
├── main.py                      # 統一入口，依子命令分派給 app/ 底下的元件
├── app/
│   ├── config.py                # 所有設定常數（視窗/裁切/Gemini 參數/檔案路徑）
│   ├── capture.py               # 視窗尋找、截圖(PrintWindow)、裁切、縮放、存檔/清理
│   ├── gemini_client.py         # Gemini Stage 1（截圖萃取）/ Stage 2（純文字分類）
│   ├── structuring.py           # 直接結構化狀態管理 + 去重安全網 + 逐輪處理
│   ├── monitor_loop.py          # 持續監聽模式（每 2 秒截圖一次）
│   ├── scroll_capture.py        # 深度回溯滾動擷取模式（上滑到舊訊息→慢慢下滑擷取）
│   ├── trading_signal_agent.py  # Stage 3：結構化 JSONL -> 可交易訊號 JSONL
│   ├── signal_to_order_params.py# Stage 4：可交易訊號 JSONL -> 下單參數 JSONL
│   ├── order_executor.py        # Stage 5：下單參數 -> 實際下單（互動式逐筆確認）
│   └── validate_and_execute_order_params.py  # Testnet 下單參數驗證與成交報告
├── docs/PIPELINE.md             # 本文件
├── 阿佛禁言群B_結構化.jsonl        # Stage 1+2 輸出（只含 trading_signal 類別）
├── 阿佛禁言群B_可交易訊號.jsonl     # Stage 3 輸出（嚴格 schema 的可執行訂單意圖）
└── 阿佛禁言群B_下單參數.jsonl       # Stage 4 輸出（可直接餵給 Binance API 的參數）
```

## 整體資料流（兩種截圖模式，共用同一套下游 Stage）

```
                      ┌──────────────────────────┐
                      │   SafeW Desktop 聊天視窗   │
                      └────────────┬─────────────┘
                                   │ PrintWindow 截圖 + 裁切聊天區域
                                   ▼
        ┌───────────────────────────────────────────────────┐
        │  擷取模式（二選一，取決於呼叫的 main.py 子命令）         │
        │                                                     │
        │  A) python main.py monitor   （持續監聽模式）           │
        │     while True 迴圈，每輪流程：                         │
        │       1. 每隔 POLL_INTERVAL_SEC=2 秒醒來一次            │
        │       2. capture_window() 截取整個視窗                  │
        │       3. crop_chat_area() 裁切出聊天區域(CROP_RATIO)     │
        │       4. images_look_same(前一張,這一張,閾值=30)？       │
        │          → 是：跳過本輪 Gemini 呼叫(省成本)，睡眠後重來    │
        │          → 否：呼叫 process_screenshot_direct_          │
        │            structuring()（見下方 Stage1+2），存截圖       │
        │            （SAVE_SCREENSHOTS_ONLY_ON_CHANGE=True，      │
        │            只在真的寫入新訊號時才存檔）                    │
        │       5. 印出結果或每60秒印一次心跳，睡眠後重來             │
        │                                                     │
        │  B) python main.py scroll  或  python main.py       │
        │     pipeline （一次性深度回溯滾動擷取，DELAY_SEC=0.6） │
        │     └─ 階段1：往上滑 20 步 × 每步5格＝共100格           │
        │        （純滾動，不截圖/不處理，只移動到較舊的起點）      │
        │     └─ 基準截圖 step_00_base.png：滑完後先存一張        │
        │        並跑一次結構化（回溯起點，避免起點訊息被漏記）      │
        │     └─ 階段2：往下滑 20 步 × 每步5格＝共100格            │
        │        （總量務必等於階段1的100格，否則滑不回最底部/       │
        │        對不上原本畫面；每步都真的截圖 step_01~20_        │
        │        HHMMSS.png +處理，不做 images_look_same 判斷）    │
        └───────────────────────┬─────────────────────────────┘
                                 │ 每一張「裁切後聊天區域」截圖
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │  app/structuring.py: process_screenshot_direct_    │
        │  structuring(chat_img, source_screenshot=檔名)        │
        │  ← 每一張截圖都呼叫這個函式                              │
        │                                                     │
        │  數字輔助 OCR（app/digit_hint.py，2026-07-05 新增）       │
        │    extract_digit_hints(chat_img)                    │
        │    [Tesseract 5.4，限定辨識數字字元 0-9]                 │
        │    用途：Gemini 視覺模型會把圖片中的小字體數字看錯             │
        │    （實測案例：ETH1840 誤讀成 ETH3400），改用傳統 OCR         │
        │    引擎額外抓一次「畫面上出現過的連續數字序列」，過濾長度        │
        │    <3 或以 0 開頭的雜訊，整理成清單附加進 Stage 1 prompt，      │
        │    請 Gemini 視覺判讀的數字跟清單對不上時優先採信清單             │
        │        │                                           │
        │        ▼                                           │
        │  Stage 1（圖片 + 近期已記錄文字 + 數字提示 -> 新增乾淨文字） │
        │    app.gemini_client.gemini_extract_new_target_    │
        │    messages(chat_img, recent_texts, digit_hints)     │
        │    [模型: gemini-3.5-flash，回傳 JSON array]          │
        │    輸入：本張截圖 + recent_texts（最近已記錄的            │
        │         GEMINI_RECENT_CONTEXT_SIZE=30 筆文字，          │
        │         告訴模型「這些已經記錄過，不要重複回傳」）           │
        │         + digit_hints（Tesseract 抓到的數字清單）        │
        │    輸出：畫面上「尚未記錄過」的阿佛發言清單                  │
        │         每筆含 time / text / is_confirmed_target_     │
        │         speaker（發言人再次確認，false 則整筆丟棄）        │
        │    ⚠只萃取「阿佛本人打字輸入的純文字」（2026-07-05 新增規則）：│
        │      若阿佛發送的是圖片/截圖/K線圖/語音/貼圖等附件，        │
        │      即使附件上印刷/顯示著文字或數字（例如交易所價格截圖），    │
        │      一律整則忽略，不當成阿佛的發言擷取出來                │
        │        │ 逐筆處理，每筆都會被加入 recent_texts           │
        │        │ （不論下面 Stage2 判斷結果為何）                 │
        │        ▼                                           │
        │  Stage 2（純文字 -> 分類 + 是否具體可交易，逐筆呼叫）        │
        │    app.gemini_client.gemini_classify_signal(text)   │
        │    [模型: gemini-3.1-flash-lite，回傳 JSON]           │
        │    輸出：category ∈ {trading_signal,sharing,          │
        │         chitchat,other} + is_specific(布林)           │
        │    只有 category=="trading_signal" 且 is_specific     │
        │    =True 才繼續往下走，其餘直接丟棄（不寫檔）              │
        │        │                                           │
        │        ▼                                           │
        │  去重/合併判斷（依序判斷，命中就停）：                     │
        │   1. _is_truncation_continuation(新文字,上一筆文字)？    │
        │      → 是：其中一句是另一句前綴（滑動時訊息被視窗切半），     │
        │        取較長版本覆寫最後一行，不新增 index               │
        │      ⚠已知限制：只抓「前綴關係」，若因數字誤讀導致兩次        │
        │        擷取的文字在中間某處不同（非前綴關係，例如            │
        │        ETH3400 vs ETH1840），不會被判定為續接，可能         │
        │        產生兩筆重複記錄，需人工核對 source_screenshot 抓出   │
        │   2. text in written_texts（永不過期的完整文字集合）？    │
        │      → 是：與「歷史上任何一筆已寫入內容」完全相同，          │
        │        代表 recent_texts 視窗(只留最近30筆)已經把它        │
        │        擠出去、Stage1 才會誤判成新內容，直接丟棄不寫檔       │
        │        （這是 2026-07-05 修正重複寫入 bug 的關鍵防線）      │
        │   3. 都不是 → 全新內容，written_count+=1，append 新行     │
        └───────────────────────┬─────────────────────────────┘
                                 │ append（或覆寫最後一行 / 丟棄）
                                 ▼
                 阿佛禁言群B_結構化.jsonl
     {index, time, speaker, text, category, source_screenshot?}
             （index 是跨執行累加的序號，不代表滾動步驟數；
              source_screenshot 為選填欄位，僅 scroll/pipeline 模式的
              呼叫端有傳入截圖檔名時才會出現，monitor 模式目前未傳，
              方便事後核對某筆訊號是從哪張截圖擷取出來的）
                                 │
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │  Stage 3: app/trading_signal_agent.py               │
        │  對每則「尚未處理過」的結構化訊息（用獨立 state 檔追蹤      │
        │  已處理到哪個 index，重跑不會重複處理）：                  │
        │    1. 查詢 Binance Testnet /fapi/v2/positionRisk       │
        │       目前持倉 + /fapi/v1/ticker/price 目前現價          │
        │    2. 把持倉/現價資訊放進 prompt，讓模型重新從原始文字       │
        │       判斷 position_action（不沿用 Stage2 的粗略分類）：   │
        │       开多/开空/加多/加空/减多/减空/平多/平空/            │
        │       止盈多/止盈空/止损多/止损空                        │
        │    3. 沒有對應倉位的指令一律不產生訂單（例如沒有多單          │
        │       卻叫你「止盈多」）                                 │
        └───────────────────────┬─────────────────────────────┘
                                 │ append
                                 ▼
              阿佛禁言群B_可交易訊號.jsonl
     {source_index, text, orders:[{position_action, symbol,
      trigger_price, position_size_pct, ...}]}
                                 │
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │  Stage 4: app/signal_to_order_params.py             │
        │  把每個 order 轉換成 Binance 下單參數：                │
        │    quantity 計算公式（僅開倉/加倉類需要）：              │
        │      保證金 = 帳戶可用USDT餘額 ÷ N個標的                │
        │              （同一則訊息若提到多個幣種，均分）            │
        │      notional = 保證金 × DEFAULT_LEVERAGE(槓桿倍數)     │
        │      quantity = notional ÷ 現價，再依該symbol的          │
        │                LOT_SIZE stepSize 無條件捨去精度          │
        │      （忽略 Stage3 產出的 position_size_pct，固定規則）   │
        │    - 產生 preflight_requests（例如 /fapi/v1/leverage    │
        │      設定槓桿等下單前必須成功的前置檢查）                  │
        │    - 產生 order_request（實際下單 body：symbol/side/     │
        │      type/quantity/stopPrice/reduceOnly等）             │
        │    - status: READY(可下單)/SKIPPED(略過)/ERROR(有問題)   │
        └───────────────────────┬─────────────────────────────┘
                                 │ append
                                 ▼
               阿佛禁言群B_下單參數.jsonl
   {source_index, order_idx, symbol, position_action,
    status, preflight_requests, order_request, error}
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
   app/validate_and_execute_        app/order_executor.py
   order_params.py（Testnet 專用）    （Stage 5：互動式逐筆確認）
   1. 先對每筆送 /fapi/v1/order/test   --list    列出所有待處理訂單
      （不成交，純驗證參數格式）         --show    顯示某筆完整細節+試算quantity
   2. 全部通過才逐筆送 /fapi/v1/order  --execute 實際送出下單（人工逐筆確認後才用）
      做真正的 Testnet 成交驗證        --skip    標記略過，不下單
   3. 產出 下單驗證與成交報告.json     絕不自動連續下單，狀態記錄在
                                    order_execution_state.jsonl，
                                    重跑不會對同一筆訂單重複下單
```

## 兩種截圖模式的差異

| | `monitor` | `scroll` |
|---|---|---|
| 觸發方式 | `python main.py monitor`，常駐 | `python main.py scroll`（只做擷取）或 `python main.py pipeline`（擷取+自動接著跑 Stage3+Stage4） |
| 用途 | 即時盯盤，新訊息一出現就記錄 | 一次性把過去一段時間的舊訊息「補回來」 |
| 截圖頻率 | 每 2 秒一次（`POLL_INTERVAL_SEC = 2`） | 上滑 20 步（純滾動不截圖）→ 基準截圖 → 下滑 20 步，每步截圖 |
| 是否跳過無變化畫面 | 是（`images_look_same` 比對像素差異，門檻 `OCR_SKIP_DIFF_THRESHOLD = 30`） | 否，下滑每一步都真的截圖+處理 |
| 截圖存檔內容 | 裁切後聊天區域（`screenshots/`） | 裁切後聊天區域（`test_scroll_captures_deep/`），與 monitor 一致 |

### `scroll` / `pipeline` 的滾動參數細節（`app/scroll_capture.py`）

| 參數 | 數值 | 說明 |
|---|---|---|
| `SCROLL_UP_STEPS` | **20 步** | 階段1：往上滑的步數 |
| `SCROLL_UP_NOTCHES` | **5 格/步** | 階段1每一步滾輪滾動的格數（1格 = 1 個 `WHEEL_DELTA`=120） |
| 階段1 總滾動量 | 20 × 5 = **100 格** | 純滾動，過程中**不截圖、不處理**，只是把畫面移動到較舊的起點 |
| `SCROLL_DOWN_STEPS` | **20 步** | 階段2：往下滑的步數 |
| `SCROLL_DOWN_NOTCHES` | **5 格/步** | 階段2每一步滾動的格數 |
| 階段2 總滾動量 | 20 × 5 = **100 格** | **務必等於階段1總量（100格）**，否則滑不回最底部、對不上原始畫面；每一步都真的截圖+呼叫 Gemini 結構化 |
| `DELAY_SEC` | **0.6 秒** | 每次滾動後、擷取畫面前的等待時間（讓 UI 有時間重繪完成，避免截到滾動中的殘影） |
| 基準截圖 | `step_00_base.png` | 階段1滑完後，正式開始階段2之前，先存一張並跑一次結構化（作為這次回溯的起點，避免起點那批訊息被漏記） |
| 每步截圖檔名 | `step_{01~20}_{HHMMSS}.png` | 存於 `test_scroll_captures_deep/`，內容是**裁切後的聊天區域**（跟餵給 Gemini 的畫面一致） |

### `monitor` 模式的其他參數（`app/config.py`）

| 參數 | 數值 | 說明 |
|---|---|---|
| `CROP_RATIO` | `(0.50, 0.082, 1.0, 1.0)` | 視窗客戶區裁切比例 (left, top, right, bottom)，取右側聊天區，排除左側聯絡人清單與頂部標題列 |
| `AUTO_RESIZE_WINDOW` / `TARGET_WINDOW_SIZE` | `True` / `(1100, 1200)` | 啟動時自動把視窗調整到 1100×1200，讓一次截圖能看到更多訊息，降低漏抓機率 |
| `SAVE_SCREENSHOTS_ONLY_ON_CHANGE` | `True` | 只在偵測到新交易訊號時才存截圖，避免長時間監控累積大量無用檔案 |
| `SCREENSHOT_KEEP_DAYS` | **7 天** | 自動清理超過 7 天的舊截圖，避免佔滿磁碟（設 0 表示不清理） |
| `GEMINI_RECENT_CONTEXT_SIZE` | **30 筆** | 傳給 Stage 1 的「最近已記錄訊息」筆數（詳見下方去重機制說明） |
| Stage 1 模型 | `gemini-3.5-flash` | 需要細膩視覺 grouping 判斷（誰講的），用較強模型 |
| Stage 2 模型 | `gemini-3.1-flash-lite` | 純文字分類，呼叫頻率高、風險低，用較便宜模型 |

### `images_look_same` 的「閾值」到底是什麼（`app/capture.py`）

只有 `monitor` 模式會用到這個判斷（`scroll`/`pipeline` 模式刻意每一步都截圖處理，不跳過）：

1. 把「上一次截圖」與「這次截圖」都轉成灰階（0=全黑, 255=全白）。
2. 逐像素相減取絕對值，得到一張「差異圖」。
3. `OCR_SKIP_DIFF_THRESHOLD = 30`：取這張差異圖中**差異最大的那個像素值**，
   若最大差異 < 30（滿分255），代表兩張截圖幾乎一模一樣（沒有新訊息滑入畫面、
   沒有游標閃爍等實質變化），就跳過本輪 Gemini 呼叫，省下一次 API 成本。
   若 ≥ 30，代表畫面有實質變化，照常呼叫 Stage 1+2 處理。
4. 這只是「省成本的捷徑」，不是判斷「有沒有新交易訊號」——就算通過這關、
   真的呼叫了 Gemini，最終還是由 Stage 1（發言人確認）+ Stage 2（分類）
   決定要不要寫入 JSONL。


## 核心去重機制（2026-07-05 修正的 bug）

**問題**：早期版本只靠 `recent_texts`（固定大小 deque，`GEMINI_RECENT_CONTEXT_SIZE`）
告訴 Stage 1「哪些內容已經記錄過」。當滑動幅度很小（早期參數 `SCROLL_DOWN_NOTCHES=2`）
時，同一則訊息會連續停留在畫面上超過視窗大小的輪數，
一旦被擠出視窗，Stage 1 就會誤判成「新內容」而重複萃取、重複寫入 JSONL。
即使現在調大每步滾動幅度（`SCROLL_DOWN_NOTCHES=5`）降低了發生機率，
`written_texts` 安全網仍然保留作為根本防線，不因滾動參數調整而失效。

**修正**：
1. `GEMINI_RECENT_CONTEXT_SIZE` 從 6 調高到 30，降低視窗被擠爆的機率。
2. 新增 `written_texts`（`app/structuring.py`）：一個**永不過期**的完整文字集合，
   紀錄「所有曾經寫入過的原始文字」。即使 `recent_texts` 視窗已經把某句話擠出去，
   `written_texts` 仍會在真正寫檔前擋下重複，這是真正的防線。

## 驗證每一階段輸出是否正確的建議做法

1. **結構化 JSONL**：打開對應的截圖（`test_scroll_captures_deep/step_XX_*.png`
   或 `screenshots/` 底下的檔案），確認畫面上真的有出現該筆 `text`，且發言人是
   `阿佛`。**注意 index 是跨執行累加的**，不是「第幾個滾動步驟」的意思。
2. **可交易訊號 JSONL**：核對 `position_action` 是否符合原文語意（例如原文說
   「止盈」不應該被判斷成「开仓」），以及 `trigger_price`/`position_size_pct`
   是否真的出現在原文中。
3. **下單參數 JSONL**：核對 `symbol`/`side`/`quantity`/`stopPrice` 等是否符合
   Binance API 規格，且 `status` 欄位（READY/SKIPPED/ERROR）合理。
