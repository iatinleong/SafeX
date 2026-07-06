# -*- coding: utf-8 -*-
"""
Gemini API 呼叫：Stage 1（截圖 -> 萃取尚未記錄的目標發言人新發言，含發言人再次確認）
與 Stage 2（純文字 -> 分類是否為交易訊號 + 是否具體可交易）。
從 ocr_monitor.py 搬移，邏輯不變；已移除只有舊版 Tesseract/純 OCR 模式才會用到的
_tesseract_ocr_image / _gemini_ocr_image / ocr_image（GEMINI_DIRECT_STRUCTURING 恆為 True，
純 OCR 模式已刪除，不再需要這些函式）。
"""
import io as _io
import json
import re

from google import genai as _genai
from google.genai.types import Part as _GenaiPart

from app.config import (
    GEMINI_API_KEY_ENV_VAR, GEMINI_VISION_MODEL_ID, GEMINI_MODEL_ID,
    GEMINI_EXTRACTION_PROMPT_TEMPLATE, GEMINI_CLASSIFICATION_PROMPT_TEMPLATE,
    GEMINI_MESSAGE_CATEGORIES, TARGET_SPEAKER,
)

_gemini_client = None  # 延遲初始化的 Gemini API client（首次呼叫時建立）


def _get_gemini_client():
    global _gemini_client
    if _gemini_client is None:
        import os
        api_key = os.environ.get(GEMINI_API_KEY_ENV_VAR)
        if not api_key:
            raise RuntimeError(
                f"找不到環境變數 {GEMINI_API_KEY_ENV_VAR}，請先設定 Gemini API Key"
                f"（例如：[System.Environment]::SetEnvironmentVariable('{GEMINI_API_KEY_ENV_VAR}', '你的key', 'User')，"
                f"設定後需重新開啟終端機/程式才會生效）。"
            )
        _gemini_client = _genai.Client(api_key=api_key)
    return _gemini_client


def _build_gemini_extraction_schema():
    """
    Stage 1（圖片輸入）輸出格式：只負責「新增內容 + 發言人再次確認」，不含分類。
    用 response_schema 強制格式，減少格式漂移。
    """
    from google.genai import types as _t
    item_schema = _t.Schema(
        type="OBJECT",
        properties={
            "time": _t.Schema(type="STRING", nullable=True, description="H:MM 格式，畫面上看不到時間戳記則為 null"),
            "text": _t.Schema(type="STRING", description="清洗後的訊息內容（已移除emoji反應列/雜訊/重複）"),
            "is_confirmed_target_speaker": _t.Schema(type="BOOLEAN", description="再次確認發言人真的是目標對象本人"),
        },
        required=["text", "is_confirmed_target_speaker"],
    )
    return _t.Schema(type="ARRAY", items=item_schema)


def gemini_extract_new_target_messages(img, recent_texts, target_speaker: str = None, digit_hints: list = None):
    """
    Stage 1：把截圖 + 「最近已記錄的 target_speaker 發言（不論分類為何）」交給 Gemini，
    請它回傳「畫面上出現但尚未記錄過」的 target_speaker 發言，只做：
      1. 清洗（去 emoji 反應列/雜訊/去重）
      2. 發言人再次確認（is_confirmed_target_speaker，false 則該筆不會出現在結果中）
    不做分類/訊號抽取——那是 Stage 2（gemini_classify_signal）的職責，兩者分開呼叫，
    因為 Stage 1 需要圖片、Stage 2 只需要文字，拆開後 Stage 2 可以更快更便宜地重複呼叫。

    digit_hints：可選，由 app.digit_hint.extract_digit_hints() 用 Tesseract 額外抓出的
    「畫面上實際出現過的數字序列」清單，作為輔助提示附加進 prompt，降低 Gemini 把圖片中
    數字看錯的機率（2026-07-05 實測發現的 ETH1840 誤讀成 ETH3400 案例）。

    回傳 list[dict]，每個 dict 含 time/text 欄位；解析失敗或呼叫失敗時回傳空 list
    （呼叫端應視為「這一輪沒有新內容」，等下一輪再試，因為 recent_texts 視窗會持續涵蓋
    最近內容，不會因此永久遺漏）。
    """
    from google.genai import types as _genai_types

    target_speaker = target_speaker or TARGET_SPEAKER
    recent_block = "\n".join(f"- {t}" for t in recent_texts) if recent_texts else "（目前尚未記錄任何內容）"
    prompt = GEMINI_EXTRACTION_PROMPT_TEMPLATE.format(target=target_speaker, recent_block=recent_block)

    if digit_hints:
        prompt += (
            "\n\n【數字輔助提示】\n"
            "另外用傳統 OCR 引擎（非本次視覺模型）從同一張截圖額外辨識出以下數字序列，"
            "僅供你核對「畫面上出現的數字」是否與你視覺判讀的一致，這份清單可能包含"
            "與交易內容無關的數字（例如時間、樓層編號），也可能因傳統 OCR 誤判而不完全準確，"
            "請自行判斷是否採用，但如果你視覺判讀出的數字跟這份清單中「開頭相同、長度相近」的"
            "數字對不上，請優先採信這份清單中的數字（傳統 OCR 對規則印刷數字辨識通常較穩定）：\n"
            + ", ".join(digit_hints)
        )

    client = _get_gemini_client()
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    image_part = _GenaiPart.from_bytes(data=buf.getvalue(), mime_type="image/png")

    resp = client.models.generate_content(
        model=GEMINI_VISION_MODEL_ID,
        contents=[prompt, image_part],
        config=_genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_build_gemini_extraction_schema(),
        ),
    )
    raw = (resp.text or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[警告] Gemini Stage1 回應不是合法 JSON，本輪略過：{raw[:200]}")
        return []
    if not isinstance(parsed, list):
        return []

    results = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if not item.get("is_confirmed_target_speaker", False):
            continue  # 發言人再次確認未通過，視為誤判，不收錄
        text = (item.get("text") or "").strip()
        if not text:
            continue
        time_val = item.get("time")
        if isinstance(time_val, str) and not re.match(r"^\d{1,2}:\d{2}$", time_val.strip()):
            time_val = None  # 格式不符預期就當作沒有時間戳，避免污染下游時間欄位
        results.append({"time": time_val, "text": text})
    return results


def _build_gemini_classification_schema():
    """Stage 2（純文字輸入）輸出格式：只有分類 + 是否具體可交易，不再抽取細節欄位。"""
    from google.genai import types as _t
    result_schema = _t.Schema(
        type="OBJECT",
        properties={
            "category": _t.Schema(type="STRING", enum=GEMINI_MESSAGE_CATEGORIES),
            "is_specific": _t.Schema(
                type="BOOLEAN",
                description="是否包含具體點位或明確操作動作，只有 category=trading_signal 時才可能為 true",
            ),
        },
        required=["category", "is_specific"],
    )
    return result_schema


def gemini_classify_signal(text: str, target_speaker: str = None):
    """
    Stage 2：純文字輸入（不需要圖片），只判斷一則訊息的 category 與是否具體可交易（is_specific），
    不抽取 action/direction/symbol/price_level/timing 等細節欄位——那些欄位完全交給 Stage 3
    （trading_signal_agent.py）從原始文字重新判斷，避免兩個 agent 各自解讀同一組欄位造成落差。
    因為不帶圖片，這次呼叫比 Stage 1 快、便宜很多，可視需要獨立升級模型而不影響 Stage 1。

    回傳 dict：{"category": str, "is_specific": bool}；呼叫失敗時回傳
    {"category": "other", "is_specific": False}（保守處理，寧可漏記也不要誤判成交易訊號寫入輸出）。
    """
    from google.genai import types as _genai_types

    target_speaker = target_speaker or TARGET_SPEAKER
    prompt = GEMINI_CLASSIFICATION_PROMPT_TEMPLATE.format(target=target_speaker, text=text)
    try:
        client = _get_gemini_client()
        resp = client.models.generate_content(
            model=GEMINI_MODEL_ID,
            contents=[prompt],
            config=_genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_build_gemini_classification_schema(),
            ),
        )
        raw = (resp.text or "").strip()
        if not raw:
            return {"category": "other", "is_specific": False}
        parsed = json.loads(raw)
        category = parsed.get("category") if parsed.get("category") in GEMINI_MESSAGE_CATEGORIES else "other"
        is_specific = bool(parsed.get("is_specific")) if category == "trading_signal" else False
        return {"category": category, "is_specific": is_specific}
    except Exception as e:
        print(f"[警告] Gemini Stage2（分類）呼叫失敗，本則訊息視為 other 略過：{e}")
        return {"category": "other", "is_specific": False}
