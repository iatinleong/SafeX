# -*- coding: utf-8 -*-
"""
深度回溯滾動擷取（原 deep_scroll_pipeline.py 的滾動+截圖+結構化部分）：
  階段1：先往上滑到較舊的訊息（純滾動，滑動過程中「不」截圖、「不」處理，
         只是把畫面移動到起點）。
  階段2：往上滑完成後才開始慢慢往下滑，模擬「新訊息陸續出現」，
         每一步都真的截圖並呼叫正式的 Gemini 直接結構化流程
         （app.structuring.process_screenshot_direct_structuring），把新萃取到的交易訊號
         真正 append 進正式的結構化 JSONL（不是寫到測試用的隔離檔案）。

存檔的截圖一律是「裁切後的聊天區域」（即真正餵給 Gemini 的畫面），
而不是整個視窗，確保人工核對截圖與 JSONL 內容時看到的是同一份資料，避免核對失真。

Stage 3/Stage 4 不在此模組執行，交由 main.py 的 pipeline 子命令在滾動擷取完成後另外呼叫。

【非同步佇列架構】
截圖與 OCR/結構化（呼叫 Gemini）分離成兩個執行緒：
  - 主執行緒（producer）：只負責滑動 + 截圖 + 存檔，不等待 Gemini 回應，
    全速跑完整個滾動流程，把每張截圖依序放進佇列（Queue）。
  - 背景執行緒（consumer）：從佇列依「先進先出」順序取出截圖，逐一呼叫
    process_screenshot_direct_structuring 寫入正式 JSONL。全程單執行緒消費，
    確保處理順序與截圖順序完全一致，避免併發呼叫導致 dedup（written_texts /
    recent_texts）邏輯因處理順序錯亂而誤判。
  - 主執行緒滑動完成後放入結束訊號（None），並等待背景執行緒把佇列處理完
    （queue.join()）才算真正結束，回傳總計新寫入筆數。

用法（透過 main.py）：
    python main.py scroll
"""
import time
import queue
import threading
from pathlib import Path
from datetime import datetime

import win32gui
import win32api
import win32con

from app.config import WINDOW_TITLE_KEYWORDS, CROP_RATIO, AUTO_RESIZE_WINDOW, TARGET_WINDOW_SIZE
from app.capture import find_window, capture_window, crop_chat_area, resize_window_if_needed
from app.structuring import process_screenshot_direct_structuring

SCREENSHOT_DIR = Path(r"C:\Users\user\Desktop\SafeW\test_scroll_captures_deep")

SCROLL_UP_STEPS = 20       # 往上滑步數，總量 = 20*5 = 100 格
SCROLL_UP_NOTCHES = 5      # 每步滑幾格
SCROLL_DOWN_STEPS = 20     # 往下滑步數，總量必須等於往上滑總量(100格)，否則滑不回最底部/對不上原本畫面
SCROLL_DOWN_NOTCHES = 5    # 每步滑幾格；20*5 = 100 = 與上滑總量一致
DELAY_SEC = 0.6

WM_MOUSEWHEEL = 0x020A
WHEEL_DELTA = 120


def _scroll(hwnd, notches: int):
    """notches > 0 = 向上滾動（看舊訊息），notches < 0 = 向下滾動（看新訊息）。"""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    screen_x, screen_y = win32gui.ClientToScreen(hwnd, (cx, cy))
    lparam = win32api.MAKELONG(screen_x, screen_y)
    wparam = win32api.MAKELONG(0, notches * WHEEL_DELTA)
    win32gui.PostMessage(hwnd, WM_MOUSEWHEEL, wparam, lparam)


def _ocr_worker(q: "queue.Queue", result_holder: dict):
    """背景消費者執行緒：依序（FIFO）處理佇列中的截圖，呼叫正式 Gemini 結構化流程。
    全程單執行緒消費，保證處理順序與截圖順序一致，避免 dedup 邏輯因併發亂序誤判。"""
    total_new_signals = 0
    while True:
        item = q.get()
        if item is None:
            q.task_done()
            break
        step_label, chat_img, filename = item
        try:
            n = process_screenshot_direct_structuring(chat_img, source_screenshot=filename)
            total_new_signals += n
            print(f"[OCR/{step_label}] 處理完成，本步新寫入交易訊號 {n} 筆（來源截圖：{filename}）")
        except Exception as e:
            print(f"[OCR/{step_label}] 處理失敗：{e}")
        finally:
            q.task_done()
    result_holder["total_new_signals"] = total_new_signals


def run_scroll_capture() -> int:
    """執行完整滾動擷取，回傳本次新寫入的交易訊號筆數。找不到視窗時回傳 -1。
    採生產者/消費者非同步架構：本函式（生產者）只管滑動+截圖+存檔，全速跑完，
    不等待 Gemini 回應；背景執行緒（消費者）依序處理佇列中的截圖並寫入 JSONL。"""
    print("尋找 SafeW 視窗中...")
    hwnd = find_window(WINDOW_TITLE_KEYWORDS, verbose=True)
    if not hwnd:
        print("找不到視窗，請確認 SafeW 已開啟該聊天室且未最小化。")
        return -1
    if win32gui.IsIconic(hwnd):
        print("視窗最小化，還原中（不搶焦點）...")
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        time.sleep(0.5)
    if AUTO_RESIZE_WINDOW:
        resize_window_if_needed(hwnd, TARGET_WINDOW_SIZE)
        time.sleep(0.3)

    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    ocr_queue: "queue.Queue" = queue.Queue()
    result_holder = {"total_new_signals": 0}
    worker = threading.Thread(target=_ocr_worker, args=(ocr_queue, result_holder), daemon=False)
    worker.start()

    # ---- 階段1：先往上滑到較舊的訊息（純滾動，不截圖、不處理）----
    print(f"\n[階段1] 往上滑 {SCROLL_UP_STEPS} 步（每步 {SCROLL_UP_NOTCHES} 格），"
          f"滑動過程中不截圖不處理，只是移動到較舊的起點...")
    for i in range(SCROLL_UP_STEPS):
        _scroll(hwnd, SCROLL_UP_NOTCHES)
        time.sleep(DELAY_SEC)
    print("往上滑完成。")

    # 往上滑完成後，先存一張基準截圖，丟進佇列（不在這裡等 Gemini 回應）。
    # 存檔用裁切後的聊天區域畫面（跟餵給 Gemini 的是同一張），方便事後核對。
    img0 = capture_window(hwnd)
    chat0 = crop_chat_area(img0, CROP_RATIO)
    chat0.save(SCREENSHOT_DIR / "step_00_base.png")
    ocr_queue.put(("base", chat0, "step_00_base.png"))
    print("[基準截圖] 已存檔並丟進佇列，交由背景執行緒處理...")

    # ---- 階段2：慢慢往下滑，模擬新訊息陸續出現；每步只截圖存檔+丟進佇列，不等待OCR ----
    print(f"\n[階段2] 開始往下滑，共 {SCROLL_DOWN_STEPS} 步（每步 {SCROLL_DOWN_NOTCHES} 格），"
          f"每一步截圖存檔後立即丟進佇列，不等待 Gemini 回應，全速滑完...")
    for step in range(1, SCROLL_DOWN_STEPS + 1):
        _scroll(hwnd, -SCROLL_DOWN_NOTCHES)
        time.sleep(DELAY_SEC)

        img = capture_window(hwnd)
        chat_img = crop_chat_area(img, CROP_RATIO)

        ts = datetime.now().strftime("%H%M%S")
        filename = f"step_{step:02d}_{ts}.png"
        chat_img.save(SCREENSHOT_DIR / filename)
        ocr_queue.put((f"step_{step:02d}", chat_img, filename))
        print(f"第 {step} 步：已往下滑 {SCROLL_DOWN_NOTCHES} 格並存檔，丟進佇列（背景處理中）")

    print(f"\n滾動階段完成，共 {SCROLL_DOWN_STEPS + 1} 張截圖已存於 {SCREENSHOT_DIR}。"
          f"等待背景執行緒處理完佇列中剩餘的 OCR/結構化工作...")
    ocr_queue.put(None)  # 結束訊號
    worker.join()

    total_new_signals = result_holder["total_new_signals"]
    print(f"\n全部完成，累計新寫入交易訊號 {total_new_signals} 筆。")
    return total_new_signals


if __name__ == "__main__":
    run_scroll_capture()
