# -*- coding: utf-8 -*-
"""
所有設定常數集中於此，供 app/ 底下其他模組與 main.py 匯入。
原本分散在 ocr_monitor.py 頂部的 CONFIG 區塊，拆分套件時原封不動搬移，
僅移除已刪除的舊版 Tesseract/規則式管線專用設定（OCR_LANG、TESSERACT_EXE、
SAVE_SCREENSHOTS_ONLY_ON_CHANGE 之外的舊 txt 輸出路徑等）。
"""
import ctypes
from pathlib import Path

# 宣告本進程為 DPI-aware，讓 GetWindowRect/GetClientRect/MoveWindow 回傳「實際物理像素」，
# 與 PrintWindow 擷取到的實際畫面像素一致（否則在螢幕縮放不是 100% 時，座標與裁切比例會對不齊）。
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

# ============ 視窗 / 截圖設定 ============
TARGET_PROCESS_NAME = "SafeW.exe"    # 只在此進程底下的視窗尋找，避免誤抓到其他程式（VS Code/瀏覽器等）
WINDOW_CLASS_PREFIX = "Qt"           # SafeW 主視窗的 Win32 類別名稱前綴（實際類別如 Qt51517QWindowIcon，含隨版本變動的數字，故用前綴比對）
WINDOW_TITLE_KEYWORDS = ["阿佛禁言群B", "阿佛Family财富群B"]  # SafeW 視窗標題會顯示「目前開啟的聊天室名稱」，需保持該聊天室在畫面上（可在背景，不可最小化）。
                                                              # 可同時監聽多個聊天室名稱，只要目前開啟的聊天室標題符合其中一個關鍵字即可（依序嘗試，找到第一個相符的就使用）。
POLL_INTERVAL_SEC = 2                # 每隔幾秒截圖辨識一次（縮短間隔降低訊息被新訊息推出畫面外而漏抓的機率）
# 聊天內容區域裁切比例 (left, top, right, bottom)，以視窗客戶區寬高的比例表示
# 預設抓右側聊天區（去掉左側聯絡人清單與頂部標題列），可依實際視窗調整。
CROP_RATIO = (0.50, 0.082, 1.0, 1.0)
SAVE_SCREENSHOTS = True               # 是否保留 OCR 前的截圖（供除錯/事後核對辨識是否正確）
SCREENSHOT_DIR = Path(r"C:\Users\user\Desktop\SafeW\screenshots")
SAVE_SCREENSHOTS_ONLY_ON_CHANGE = True  # True: 只在偵測到新內容時存圖；False: 每次截圖都存（會快速累積大量檔案）
SCREENSHOT_KEEP_DAYS = 7               # 自動清理超過幾天的舊截圖，避免佔滿磁碟（設 0 表示不自動清理）
AUTO_RESIZE_WINDOW = True              # 啟動時自動把視窗調整到合適大小，讓一次截圖能看到更多訊息（減少漏抓）
TARGET_WINDOW_SIZE = (1100, 1200)      # 調整後的視窗寬高（僅放大聊天顯示範圍，裁切比例仍會排除左側清單/頂部標題等無關內容）

# ---- OCR 效能優化設定 ----
OCR_SKIP_ON_NO_PIXEL_CHANGE = True     # True: 若本次截圖與上次幾乎沒有像素差異，跳過本次辨識呼叫（畫面靜止時省下最貴的步驟）
OCR_SKIP_DIFF_THRESHOLD = 30           # 像素差異強度門檻（0-255），低於此視為「沒有實質變化」

# ---- 發言人過濾設定 ----
# 此群為禁言群（廣播型），只有以下兩人能發言。
TARGET_SPEAKER = "阿佛"                       # 只保留這個人的言論
OTHER_SPEAKERS = ["一根巧乐滋"]                # 群內其他已知會發言的人（非目標對象），出現時代表切換掉，之後內容不保留

STRUCTURED_OUTPUT_JSONL = Path(r"C:\Users\user\Desktop\SafeW\阿佛禁言群B_結構化.jsonl")  # 供 LLM 讀取的結構化訊息（每行一個 JSON 物件）

# ---- Gemini 設定 ----
GEMINI_API_KEY_ENV_VAR = "GEMINI_API_KEY"  # API Key 從環境變數讀取，不寫死在程式碼中（避免密鑰外洩）
GEMINI_MODEL_ID = "gemini-3.1-flash-lite"
# Stage 1（截圖 -> 發言人辨識/萃取）需要的視覺推理較細膩：連續同一人發的多則短訊息，
# 聊天 UI 通常只在「這組訊息的第一則」顯示頭像/名字，後面幾則不會重複顯示，
# 模型必須靠左右對齊位置/頭像去推斷歸屬，flash-lite 在這種細膩視覺 grouping 判斷上
# 容易產生幻覺（誤將他人訊息歸到目標發言人名下，2026-07-05 實測發現「一根巧乐滋」的
# 「头仓进多」被誤判成「阿佛」發的），因此 Stage 1 改用較強的 gemini-3.5-flash；
# Stage 2（純文字分類）風險較低、呼叫頻率也高很多，繼續用 GEMINI_MODEL_ID 省成本。
GEMINI_VISION_MODEL_ID = "gemini-3.5-flash"

GEMINI_RECENT_CONTEXT_SIZE = 30  # 傳給 Stage 1 的「最近已記錄訊息」筆數，讓它知道哪些已經記錄過、不要重複（不論分類為何都要納入，避免同一句話被重複萃取）
# 註：原本是 6，但滑動幅度小（如 scroll_capture.py 的 SCROLL_DOWN_NOTCHES=2）時，
# 同一則訊息可能連續停留在畫面上超過 6 輪，導致視窗把它擠出去、Stage 1 誤判為新內容而重複萃取。
# 調高至 30 降低這個機率；真正的防線是 structuring.py 裡的 written_texts 永不過期安全網。

# ---- 數字輔助 OCR（Tesseract）設定 ----
# 背景：2026-07-05 實測發現 Gemini 視覺模型會把截圖中的數字看錯（例如把「ETH1840」誤讀成「ETH3400」），
# 這是視覺 LLM 對圖片中小字體數字辨識的固有風險，無法單靠去重/規則邏輯避免。
# 解法：額外用傳統 OCR 引擎 Tesseract（對規則印刷數字辨識穩定度較高）從同一張截圖抓出所有
# 「連續數字序列」，過濾掉開頭是 0 的（通常是時間戳記如 09:00 的殘留或雜訊，交易點位不會以 0 開頭），
# 整理成一份「本畫面出現過的數字」清單，附加進 Stage 1 的 prompt 提示 Gemini：
# 如果視覺辨識到的數字跟這份清單中「開頭相同、長度相近」的數字對不上，優先採信清單中的數字。
# 這不是取代 Gemini 的視覺判斷（Tesseract 對中文/複雜排版辨識能力較差，不能單獨依賴），
# 而是提供「數值層面」的第二意見，讓 Gemini 有機會自我修正。
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"  # Tesseract 執行檔路徑
DIGIT_HINT_MIN_LENGTH = 3   # 只保留長度 >= 3 的數字序列（例如 3400/64000/1840），太短的通常是雜訊或無意義編號

# ---- Stage 1：OCR 清洗 + 去重 + 發言人再次確認（圖片輸入）----
GEMINI_EXTRACTION_PROMPT_TEMPLATE = (
    "這是一張聊天截圖，群組內只有「{target}」的發言需要保留，其餘發言人（例如群管理員）的訊息一律忽略。\n"
    "以下是「最近已經記錄過」的 {target} 發言內容（由舊到新），只是給你參考「已經記錄過什麼」，"
    "不需要在輸出中重複這些內容：\n"
    "{recent_block}\n"
    "請你判斷這張截圖中，屬於「{target}」的發言裡，有哪些是「尚未出現在上面清單中的全新內容」，"
    "針對每一則全新內容，依照以下規則處理並輸出：\n\n"
    "【清洗規則】\n"
    "1. 忽略表情符號反應圖示列（如「👍4 🔥2」），不要當成訊息內容，也不要放進 text 欄位。\n"
    "2. 移除因螢幕擷取造成的雜訊字元、對齊空白。\n"
    "3. 如果同一則訊息在截圖中因捲動被重複看到，只回傳一次（去重）。\n"
    "4. 只萃取「{target}」本人親自打字輸入的純文字訊息內容。如果「{target}」發送的是圖片/"
    "截圖/K線圖/語音訊息/貼圖等非文字內容附件，一律忽略整則不要輸出，即使圖片附件上印刷/顯示著"
    "文字或數字（例如交易所 App 的價格截圖、K線圖上的價位標籤）也不要把那些文字當成「{target}」"
    "說的話擷取出來——那是圖片裡的內容，不是「{target}」本人打的訊息，兩者必須明確區分。\n\n"
    "【發言人再次確認——這是最容易出錯的地方，請特別仔細】\n"
    "這個聊天群組有多個人發言（例如「{target}」、群管理員、其他成員），"
    "聊天介面的規則是：同一人「連續」發送的多則短訊息，通常只有「這組訊息中的第一則」會顯示"
    "頭像與名字標籤，後面緊接著的訊息不會重複顯示頭像/名字，只能靠「左右對齊位置是否一致」"
    "去判斷是否仍屬於同一人。因此請務必：\n"
    "  a. 先找出每則訊息「所屬的那一組」最靠近、且清楚可見的頭像/名字標籤是誰，"
    "不要只因為訊息內容look起來像交易訊號、或跟「{target}」最近的發言主題相關，就假設是「{target}」講的。\n"
    "  b. 如果一組連續訊息的頭像/名字標籤在畫面中不可見或被截斷（例如捲動到一半、"
    "標籤在畫面上緣被切掉），而你無法確定這組訊息屬於誰，請保守處理：is_confirmed_target_speaker 填 false，"
    "不要用「猜的」或「最近提到的人」來填補不確定性。\n"
    "  c. 特別注意左右對齊：不同發言人的訊息氣泡通常左右對齊、顏色不同，"
    "同一人的連續訊息一定維持相同的左右對齊/顏色，如果對齊或顏色跟「{target}」已知的訊息不一致，"
    "就不是「{target}」講的。\n\n"
    "is_confirmed_target_speaker：再次確認這則訊息的發言人「真的」是「{target}」本人，"
    "不是群管理員或其他人被 OCR 誤認成「{target}」、也不是別人訊息的段落被誤黏過來。"
    "若無法確認是「{target}」本人，請不要輸出這則訊息（直接略過，不要放進結果陣列）。\n\n"
    "如果畫面上完全沒有「{target}」的新發言（例如都跟已記錄清單重複，或畫面上都是其他人的發言/系統訊息），"
    "回傳空陣列 []。"
)

# ---- Stage 2：純分類（純文字輸入，不需要圖片）----
# 注意：Stage 2 只負責「這是不是交易訊號」+「內容是否具體到值得轉交 Stage 3」，
# 不再抽取 action/direction/symbol/price_level/timing 這些細節欄位——
# 那些欄位完全交給 Stage 3（trading_signal_agent.py）從原始文字重新判斷，
# 避免同一組欄位被兩個 agent 各自解讀一次、互相打架或讓人誤以為 Stage 2 的粗略值可直接拿來下單。
GEMINI_MESSAGE_CATEGORIES = ["trading_signal", "sharing", "chitchat", "other"]

GEMINI_CLASSIFICATION_PROMPT_TEMPLATE = (
    "以下是「{target}」在交易訊號群組中的一則發言文字，請你判斷其類別。\n\n"
    "訊息內容：「{text}」\n\n"
    "【訊息分類】category 欄位，從以下四種擇一：\n"
    "- trading_signal：內容是具體的交易操作指示或訊號，通常會提到「開倉/平倉/加倉/減倉/止盈/止損/觀望」"
    "其中一種動作，並且/或提到具體幣種（如 BTC、ETH）與價位/點位（如「64000以上」）。\n"
    "- sharing：心得分享、知識性內容、教學文章、市場分析評論（沒有明確具體的下單指示）。\n"
    "- chitchat：閒聊、問候、與交易無關的日常對話。\n"
    "- other：無法歸類到以上任何一種。\n\n"
    "【是否具體可交易】is_specific 欄位（布林值），只有當 category 是 trading_signal 時才需要判斷"
    "（其他 category 一律填 false）：\n"
    "- true：文字包含具體的價位/點位（如「64000以上」「1840」），或明確的操作動作"
    "（開倉/平倉/加倉/減倉/止盈/止損/觀望等），提供了「做什麼」或「在哪裡做」的具體依據。\n"
    "- false：只是空泛的喊單、打氣、重複性語句，沒有任何具體點位或明確動作"
    "（例如「多单拿住，多单拿住，多单拿住」「多单接着拿，不要瞎下车」這類沒有新資訊的內容）。\n"
)
