# -*- coding: utf-8 -*-
"""測試共用設定：把專案根目錄加進 sys.path，讓 `import app.xxx` 可用。
整個測試套件完全離線——所有 Gemini/Binance 呼叫都被 monkeypatch 掉，
不消耗任何 API 額度，可隨改隨跑。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
