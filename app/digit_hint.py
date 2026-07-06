# -*- coding: utf-8 -*-
"""
數字輔助 OCR：用 Tesseract（傳統 OCR 引擎，對規則印刷數字辨識穩定度較高）
從截圖中額外抓出「連續數字序列」，作為 Stage 1（Gemini 視覺萃取）的輔助提示，
降低 Gemini 把圖片中數字看錯的機率（例如把 ETH1840 誤讀成 ETH3400）。

不取代 Gemini 的視覺判斷——Tesseract 對中文/複雜排版辨識能力較差，只提供
「畫面上實際出現過哪些數字」的第二意見，讓 Gemini 有機會自我修正數字部分。
"""
import re

import pytesseract

from app.config import TESSERACT_CMD, DIGIT_HINT_MIN_LENGTH

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

_DIGIT_RE = re.compile(r"\d+")


def extract_digit_hints(img) -> list:
    """
    對截圖跑 Tesseract（限定辨識數字字元），回傳畫面上出現過的「連續數字序列」清單，
    已過濾：
      - 長度 < DIGIT_HINT_MIN_LENGTH 的（太短通常是雜訊、樓層編號等無意義數字）
      - 開頭是 '0' 的（交易點位/目標價不會以 0 開頭，通常是時間戳記殘留，如 09:00 的 "09"）
      - 重複值（保留原始出現順序，去重）
    Tesseract 未安裝或呼叫失敗時回傳空 list（呼叫端應視為「本輪沒有數字提示」，
    不影響主流程，Stage 1 仍會照常執行，只是少了輔助提示）。
    """
    try:
        raw_text = pytesseract.image_to_string(img, config="--psm 11 -c tessedit_char_whitelist=0123456789")
    except Exception as e:
        print(f"[警告] Tesseract 數字輔助 OCR 呼叫失敗，本輪略過：{e}")
        return []

    seen = set()
    hints = []
    for match in _DIGIT_RE.findall(raw_text):
        if len(match) < DIGIT_HINT_MIN_LENGTH:
            continue
        if match.startswith("0"):
            continue
        if match in seen:
            continue
        seen.add(match)
        hints.append(match)
    return hints
