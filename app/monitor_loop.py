# -*- coding: utf-8 -*-
"""
持續監聽模式：每隔 POLL_INTERVAL_SEC 秒截圖一次，呼叫直接結構化流程，
把新偵測到的交易訊號即時 append 進 STRUCTURED_OUTPUT_JSONL。

從 ocr_monitor.py 的 main() 搬移並簡化：原本依 GEMINI_DIRECT_STRUCTURING 開關
在「直接結構化」與「舊版 Tesseract/規則式組裝」兩條路徑間切換，
因為舊路徑已確認是死代碼（實際上線一直使用直接結構化模式），此處只保留直接結構化路徑。

用法：
    python main.py monitor
"""
import time
from datetime import datetime

import win32gui

from app.config import (
    WINDOW_TITLE_KEYWORDS, TARGET_PROCESS_NAME, CROP_RATIO, POLL_INTERVAL_SEC,
    AUTO_RESIZE_WINDOW, TARGET_WINDOW_SIZE, SAVE_SCREENSHOTS, SAVE_SCREENSHOTS_ONLY_ON_CHANGE,
    SCREENSHOT_KEEP_DAYS, OCR_SKIP_ON_NO_PIXEL_CHANGE, OCR_SKIP_DIFF_THRESHOLD,
    STRUCTURED_OUTPUT_JSONL, TARGET_SPEAKER,
)
from app.capture import (
    find_window, capture_window, crop_chat_area, resize_window_if_needed,
    save_screenshot, cleanup_old_screenshots, images_look_same,
)
from app.structuring import process_screenshot_direct_structuring


def run():
    STRUCTURED_OUTPUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    print(f"[模式] Gemini 直接結構化（兩階段：OCR萃取 -> 分類/交易訊號抽取），"
          f"只監聽並記錄「{TARGET_SPEAKER}」的 trading_signal 訊息。")

    print("尋找 SafeW 視窗中...")
    hwnd = find_window(WINDOW_TITLE_KEYWORDS, verbose=True)
    if not hwnd:
        print(f"找不到符合條件的視窗（進程={TARGET_PROCESS_NAME}, 標題關鍵字「{WINDOW_TITLE_KEYWORDS}」）。"
              f" 請確認 SafeW 已開啟、未最小化，且目前開啟的是該聊天室。")
        return
    print(f"找到視窗 hwnd={hwnd}，標題：{win32gui.GetWindowText(hwnd)}")

    if AUTO_RESIZE_WINDOW:
        resize_window_if_needed(hwnd, TARGET_WINDOW_SIZE)

    last_cleanup = 0
    last_heartbeat = 0
    prev_chat_img = None
    while True:
        try:
            hwnd = find_window(WINDOW_TITLE_KEYWORDS, verbose=True)  # 視窗可能重建，重新尋找較保險
            if not hwnd:
                print("視窗遺失，等待重新出現...")
                time.sleep(POLL_INTERVAL_SEC)
                continue

            now = datetime.now()
            img = capture_window(hwnd)
            chat_img = crop_chat_area(img, CROP_RATIO)

            skip_call = (
                OCR_SKIP_ON_NO_PIXEL_CHANGE
                and prev_chat_img is not None
                and images_look_same(prev_chat_img, chat_img, OCR_SKIP_DIFF_THRESHOLD)
            )

            new_signal_count = 0 if skip_call else process_screenshot_direct_structuring(chat_img)
            prev_chat_img = chat_img
            if SAVE_SCREENSHOTS and (new_signal_count or not SAVE_SCREENSHOTS_ONLY_ON_CHANGE):
                save_screenshot(chat_img, now)
            if new_signal_count:
                print(f"[{now}] 新增 {new_signal_count} 則交易訊號")
            elif time.time() - last_heartbeat > 60:
                # 定期心跳輸出：這個模式下沒有新交易訊號時完全不會印任何東西，
                # 長時間監控畫面時容易誤以為程式卡住/沒在運作，故每分鐘印一次存活狀態。
                print(f"[{now}] 監聽中（畫面{'無變化' if skip_call else '有變化但無新交易訊號'}）")
                last_heartbeat = time.time()

            if SCREENSHOT_KEEP_DAYS > 0 and time.time() - last_cleanup > 3600:
                cleanup_old_screenshots(SCREENSHOT_KEEP_DAYS)
                last_cleanup = time.time()

        except Exception as e:
            print(f"[錯誤] {e}")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    run()
