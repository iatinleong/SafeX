# -*- coding: utf-8 -*-
"""
「直接結構化」模式的狀態管理與逐輪處理入口。從 ocr_monitor.py 搬移，邏輯不變，
唯一新增的是 written_texts 永不過期去重安全網（2026-07-05 修正重複萃取 bug）。

  Stage 1（圖片）：萃取畫面上「尚未記錄過」的 TARGET_SPEAKER 新發言（已清洗/去重）。
  Stage 2（純文字，逐則）：判斷每則新發言的 category；只有 trading_signal 才會被附加寫入
                          結構化 JSONL（使用者只關心交易訊號，其餘分類一律丟棄不寫檔）。
"""
import json
from collections import deque

from app.config import STRUCTURED_OUTPUT_JSONL, TARGET_SPEAKER, GEMINI_RECENT_CONTEXT_SIZE
from app.gemini_client import gemini_extract_new_target_messages, gemini_classify_signal
from app.digit_hint import extract_digit_hints

_DIRECT_STRUCTURING_STATE = None  # 延遲初始化，跨多次 poll 延續「最近已萃取文字」與「已寫入筆數」狀態


def _get_direct_structuring_state():
    """
    初始化（或程式重啟後復原）直接結構化模式的狀態。
    因為只保留一份「只含交易訊號」的結構化 JSONL，沒有原始逐字稿可還原完整發言記錄，
    重啟後只能把既有 JSONL 中的 text 當作「最近已記錄」的種子（僅涵蓋交易訊號，
    sharing/chitchat 過去說過什麼會遺忘）——這是拿掉中間檔案換來的已知取捨，
    重啟後短暫時間內可能重複萃取到剛好在重啟前已判斷為 sharing/chitchat 的內容，
    屬於可接受範圍（不會誤寫入輸出，只是 Stage 2 API 多呼叫幾次）。
    """
    global _DIRECT_STRUCTURING_STATE
    if _DIRECT_STRUCTURING_STATE is not None:
        return _DIRECT_STRUCTURING_STATE

    recent_texts = deque(maxlen=GEMINI_RECENT_CONTEXT_SIZE)
    written_texts = set()  # 「已寫入過」的完整文字集合，永不過期（不像 recent_texts 只留最近N筆），
                            # 用來擋下「同一則訊息因為 recent_texts 視窗太小而被 Stage 1 誤判成新內容」的重複寫入
    written_count = 0
    if STRUCTURED_OUTPUT_JSONL.exists():
        # 用 utf-8-sig 讀取：若檔案開頭有 UTF-8 BOM（例如曾被 PowerShell Set-Content -Encoding utf8
        # 寫入過）會自動去除，避免 BOM 字元卡在第一行開頭導致該行 JSON 解析失敗、被靜默跳過，
        # 進而讓第一筆記錄「憑空消失」於 written_texts/recent_texts，造成後續被誤判重複寫入。
        for raw in STRUCTURED_OUTPUT_JSONL.read_text(encoding="utf-8-sig").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            written_count += 1
            text = record.get("text")
            if text:
                recent_texts.append(text)
                written_texts.add(text)

    # 復原「最後一筆已寫入的交易訊號」，供后續判斷「截斷續接」時可以覆寫而非重複新增。
    last_written_record = None
    if STRUCTURED_OUTPUT_JSONL.exists():
        for raw in STRUCTURED_OUTPUT_JSONL.read_text(encoding="utf-8-sig").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                last_written_record = json.loads(raw)
            except json.JSONDecodeError:
                continue

    _DIRECT_STRUCTURING_STATE = {
        "recent_texts": recent_texts,
        "written_texts": written_texts,
        "written_count": written_count,
        "last_written_record": last_written_record,
    }
    return _DIRECT_STRUCTURING_STATE


def _is_truncation_continuation(new_text: str, prev_text: str) -> bool:
    """
    判斷 new_text 是否為 prev_text 的「截斷續接」版本——
    畫面往下滑動時，一則訊息可能先被截到一半就擷取（視窗邊界剛好切在訊息中間），
    下一輪捲動後同一則訊息完整出現，兩次擷取的文字不同（一個是前半截、一個是完整版），
    會被 Stage1 的精確文字比對誤判成「兩則新內容」。
    這裡用「其中一句是另一句的前綴」當判準：只要其中一個字串是另一個的開頭子字串
    （且兩者不完全相同），就視為同一則訊息的兩種擷取版本，應合併成一筆，取較長/較完整的那個。
    """
    if not new_text or not prev_text or new_text == prev_text:
        return False
    return new_text.startswith(prev_text) or prev_text.startswith(new_text)


def _overwrite_last_jsonl_line(new_record: dict):
    """把結構化 JSONL 檔案的最後一行覆寫成 new_record（用於「截斷續接」合併，不新增一行）。"""
    lines = []
    if STRUCTURED_OUTPUT_JSONL.exists():
        lines = [ln for ln in STRUCTURED_OUTPUT_JSONL.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    if lines:
        lines[-1] = json.dumps(new_record, ensure_ascii=False)
    else:
        lines = [json.dumps(new_record, ensure_ascii=False)]
    STRUCTURED_OUTPUT_JSONL.write_text("\n".join(lines) + "\n", encoding="utf-8")


def process_screenshot_direct_structuring(chat_img, source_screenshot: str = None) -> int:
    """
    直接結構化模式的每輪處理入口：
      Stage 1（圖片）：萃取畫面上「尚未記錄過」的 TARGET_SPEAKER 新發言（已清洗/去重）。
      Stage 2（純文字，逐則）：判斷每則新發言的 category；只有 trading_signal 才會被附加寫入
                              結構化 JSONL（使用者只關心交易訊號，其餘分類一律丟棄不寫檔）。
    不論分類為何，Stage 1 萃取到的文字都會加入 recent_texts 視窗，避免同一句話在下一輪被重複萃取。
    若新訊號文字與「最後一筆已寫入的訊號」互為前綴關係（截斷續接，常見於滑動過程中訊息被視窗邊界切半），
    視為同一則訊息的更新，覆寫最後一筆而非新增一筆，避免重複/半殘缺的紀錄。
    written_texts（永不過期）作為額外安全網：即使 recent_texts 視窗已把某句話擠出去、
    Stage 1 因此誤判為「新內容」，這裡仍會擋下重複寫入。

    source_screenshot：可選，呼叫端傳入本次截圖的檔名（例如 "step_07_234113.png"），
    會附加寫入 JSONL 的 "source_screenshot" 欄位，方便事後人工核對「這筆訊號是從哪張截圖
    抓出來的」，不需要再靠猜測 index/時間去反推對應截圖（2026-07-05 使用者要求新增）。
    未提供時該欄位省略（例如舊資料或非截圖來源的呼叫）。

    回傳本輪新寫入（含覆寫合併）的交易訊號筆數（供呼叫端記錄/印出訊息）。
    """
    state = _get_direct_structuring_state()
    digit_hints = extract_digit_hints(chat_img)  # Tesseract 輔助提示：畫面上實際出現過的數字序列
    extracted = gemini_extract_new_target_messages(chat_img, list(state["recent_texts"]), digit_hints=digit_hints)

    new_signal_count = 0
    for item in extracted:
        text = item["text"]
        state["recent_texts"].append(text)  # 不論分類為何都要記錄，避免重複萃取

        classification = gemini_classify_signal(text)
        if classification["category"] != "trading_signal":
            continue  # 只監聽交易訊號，sharing/chitchat/other 一律丟棄
        if not classification["is_specific"]:
            continue  # 沒有明確點位也沒有明確動作的空泛喊單（如「多单拿住」），不值得寫入

        last = state["last_written_record"]
        if last is not None and _is_truncation_continuation(text, last.get("text", "")):
            # 同一則訊息的截斷/完整兩種擷取版本，取較長的合併覆寫，不新增筆數/index
            merged_text = text if len(text) >= len(last.get("text", "")) else last["text"]
            record = {
                "index": last["index"],
                "time": item["time"] or last.get("time"),
                "speaker": TARGET_SPEAKER,
                "text": merged_text,
                "category": "trading_signal",
            }
            if source_screenshot:
                record["source_screenshot"] = source_screenshot
            elif "source_screenshot" in last:
                record["source_screenshot"] = last["source_screenshot"]
            _overwrite_last_jsonl_line(record)
            state["written_texts"].discard(last.get("text", ""))
            state["written_texts"].add(merged_text)
        elif text in state["written_texts"]:
            # 安全網：recent_texts 視窗只留最近 GEMINI_RECENT_CONTEXT_SIZE 筆給 Stage 1 參考，
            # 滑動幅度小、同一則訊息連續多輪停留在畫面上時，視窗可能已經把它擠出去，
            # 導致 Stage 1 誤判為「新內容」而重複萃取。written_texts 永不過期，在這裡擋下重複寫入，
            # 不新增筆數、不覆寫既有紀錄（避免打亂已存在的 index/time）。
            continue
        else:
            state["written_count"] += 1
            record = {
                "index": state["written_count"],
                "time": item["time"],
                "speaker": TARGET_SPEAKER,
                "text": text,
                "category": "trading_signal",
            }
            if source_screenshot:
                record["source_screenshot"] = source_screenshot
            with STRUCTURED_OUTPUT_JSONL.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            state["written_texts"].add(text)

        state["last_written_record"] = record
        new_signal_count += 1

    return new_signal_count
