# -*- coding: utf-8 -*-
"""SafeW 監聽 App 套件：把原本散落在單一 ocr_monitor.py 的功能拆成獨立元件。

模組總覽：
    config.py            所有設定常數（視窗/裁切/Gemini/檔案路徑）
    capture.py            視窗尋找、截圖、裁切、縮放、截圖存檔/清理
    gemini_client.py       Gemini Stage 1（截圖萃取）/ Stage 2（純文字分類）呼叫
    structuring.py         直接結構化狀態管理 + 逐輪處理（含去重安全網）
    monitor_loop.py        持續監聽模式（原 ocr_monitor.py 的 main() 迴圈）
    scroll_capture.py      深度回溯滾動擷取模式（原 deep_scroll_pipeline.py）
    trading_signal_agent.py    Stage 3：結構化 -> 可交易訊號
    signal_to_order_params.py  Stage 4：可交易訊號 -> 下單參數
    order_executor.py          Stage 5：下單參數 -> 實際下單（互動式逐筆確認）
    validate_and_execute_order_params.py  Testnet 下單參數驗證與成交報告

統一入口見專案根目錄的 main.py。
"""
