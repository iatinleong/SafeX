# -*- coding: utf-8 -*-
"""
統一的金鑰/敏感設定讀取工具。

優先順序：
1. 系統環境變數（本機 CLI 執行 main.py 時的原有行為，完全不變）。
2. Streamlit secrets（部署到 Streamlit Community Cloud 時，於 App settings -> Secrets 設定，
   不會寫入任何原始碼或提交到 git）。

之所以獨立成一個小工具模組：避免在 order_executor.py / trading_signal_agent.py /
gemini_client.py 三個地方各自重複判斷「是否在 Streamlit 環境」的邏輯。
"""
import os


def get_secret(name: str):
    """依序嘗試環境變數、Streamlit secrets，找不到則回傳 None（由呼叫端決定要不要報錯）。"""
    value = os.environ.get(name)
    if value:
        return value
    try:
        import streamlit as st
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        # 不在 Streamlit 環境（例如本機 CLI 執行 main.py）或尚未設定 secrets.toml，
        # 這是正常情況，忽略即可，回退到「找不到」。
        pass
    return None
