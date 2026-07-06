# -*- coding: utf-8 -*-
"""
視窗尋找 / 截圖 / 裁切 / 縮放 / 截圖存檔與清理。
從 ocr_monitor.py 搬移，邏輯不變，只是移除了舊版 Tesseract 專用的縮放輔助（已隨死代碼一併刪除）。
"""
import ctypes
import time
from pathlib import Path
from datetime import datetime

import win32gui
import win32ui
import win32con
import win32process
import psutil
from PIL import Image, ImageChops

from app.config import TARGET_PROCESS_NAME, WINDOW_CLASS_PREFIX, SCREENSHOT_DIR


def _get_process_name(hwnd) -> str:
    """取得該視窗所屬進程的執行檔名稱（例如 SafeW.exe）。"""
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name()
    except Exception:
        return ""


def resize_window_if_needed(hwnd, target_size):
    """
    啟動時把視窗調整到指定大小，讓一次截圖能顯示更多訊息，降低漏抓機率。
    只調整寬高，不改變視窗左上角位置（避免視窗跑到螢幕外）。
    注意：只是放大同一個視窗的可視範圍，CROP_RATIO 仍會排除左側聯絡人清單/頂部標題等
    無關內容，不會因為視窗變大而多截到不相關的畫面。
    """
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.3)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        target_w, target_h = target_size
        if (right - left) == target_w and (bottom - top) == target_h:
            return  # 已經是目標大小，不需調整
        win32gui.MoveWindow(hwnd, left, top, target_w, target_h, True)
        time.sleep(0.3)  # 給視窗一點時間重繪
        print(f"已將視窗調整為 {target_w}x{target_h}")
    except Exception as e:
        print(f"[警告] 自動調整視窗大小失敗，將沿用目前視窗大小繼續執行：{e}")


def find_window(keyword, process_name: str = TARGET_PROCESS_NAME,
                 class_prefix: str = WINDOW_CLASS_PREFIX, verbose: bool = False):
    """
    精準尋找 SafeW 聊天視窗：
    1. 先過濾「屬於 SafeW.exe 進程」的可見視窗（避免誤抓其他程式）
    2. 再過濾 ClassName 前綴為 Qt（排除同進程其他非主視窗，如工作列圖示等）
    3. 優先完全比對標題（keyword == title），找不到才退而求其次用包含比對
    找不到時會列出候選視窗供除錯，避免誤判。

    keyword 可傳入單一字串，或字串清單（依序嘗試，只要符合其中一個關鍵字即視為找到）。
    """
    keywords = [keyword] if isinstance(keyword, str) else list(keyword)
    candidates = []  # (hwnd, title)

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        if _get_process_name(hwnd).lower() != process_name.lower():
            return True
        if class_prefix and not win32gui.GetClassName(hwnd).startswith(class_prefix):
            return True
        candidates.append((hwnd, title))
        return True

    win32gui.EnumWindows(callback, None)

    if not candidates:
        if verbose:
            print(f"[find_window] 找不到任何屬於 {process_name} 且 ClassName 前綴為 {class_prefix} 的可見視窗。"
                  f" 請確認 SafeW 已開啟且未被關閉/最小化。")
        return None

    # 優先精準比對（標題與任一關鍵字完全一致）
    exact = [hwnd for hwnd, title in candidates if title in keywords]
    if exact:
        return exact[0]

    # 其次用包含比對（不分大小寫，任一關鍵字符合即可）
    partial = [hwnd for hwnd, title in candidates
               if any(kw.lower() in title.lower() for kw in keywords)]
    if partial:
        return partial[0]

    if verbose:
        titles = "、".join(f"「{t}」" for _, t in candidates)
        kw_display = "、".join(f"「{kw}」" for kw in keywords)
        print(f"[find_window] 找到 {len(candidates)} 個 SafeW 視窗，但標題都不符合關鍵字（{kw_display}）。"
              f" 目前候選標題：{titles}。"
              f" 請確認 SafeW 目前開啟的聊天室是否為上述其中之一。")
    return None


def capture_window(hwnd) -> Image.Image:
    """使用 PrintWindow 擷取指定視窗畫面（背景/被遮擋亦可，最小化則不行）。"""
    if win32gui.IsIconic(hwnd):
        raise RuntimeError("視窗已最小化，PrintWindow 無法擷取，請還原視窗（可在背景，不需最上層）。")

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("視窗大小異常，取得寬高為 0。")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()

    save_bitmap = win32ui.CreateBitmap()
    save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(save_bitmap)

    # PW_RENDERFULLCONTENT = 2，可正確擷取硬體加速繪製的視窗內容
    result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)

    bmp_info = save_bitmap.GetInfo()
    bmp_bits = save_bitmap.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB",
        (bmp_info["bmWidth"], bmp_info["bmHeight"]),
        bmp_bits, "raw", "BGRX", 0, 1,
    )

    win32gui.DeleteObject(save_bitmap.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)

    if result != 1:
        raise RuntimeError("PrintWindow 擷取失敗（回傳值非 1），可能需改用 Windows Graphics Capture 備案。")

    return img


def crop_chat_area(img: Image.Image, ratio) -> Image.Image:
    w, h = img.size
    l, t, r, b = ratio
    return img.crop((int(w * l), int(h * t), int(w * r), int(h * b)))


def save_screenshot(img: Image.Image, ts: datetime) -> Path:
    """把 OCR 前的截圖存檔，檔名帶時間戳，供事後核對辨識是否正確。"""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    filename = ts.strftime("%Y%m%d_%H%M%S_%f") + ".png"
    path = SCREENSHOT_DIR / filename
    img.save(path)
    return path


def cleanup_old_screenshots(keep_days: int):
    """刪除超過保留天數的舊截圖，避免長期執行累積佔滿磁碟。"""
    if keep_days <= 0 or not SCREENSHOT_DIR.exists():
        return
    cutoff = time.time() - keep_days * 86400
    for f in SCREENSHOT_DIR.glob("*.png"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def images_look_same(img_a: Image.Image, img_b: Image.Image, threshold: int) -> bool:
    """
    比較兩張截圖是否幾乎沒有像素差異，用來跳過非必要的 Gemini 呼叫
    （辨識本身是整個輪詢週期中最貴的一步，畫面靜止時沒有理由每 2 秒都重新辨識一次）。
    """
    if img_a is None or img_b is None or img_a.size != img_b.size:
        return False
    diff = ImageChops.difference(img_a.convert("L"), img_b.convert("L"))
    if diff.getbbox() is None:
        return True
    return diff.getextrema()[1] < threshold
