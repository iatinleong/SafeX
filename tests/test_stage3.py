# -*- coding: utf-8 -*-
"""Stage 3（trading_signal_agent.py）離線測試：
filter_valid_orders 純函式的防禦性過濾、API 失敗不寫檔（下次重試）、損壞輸入行容錯。
Gemini/Binance 呼叫全部 monkeypatch。"""
import json

import app.trading_signal_agent as tsa


def _positions(**kw):
    base = {s: {"side": None, "amt": 0.0} for s in tsa.SUPPORTED_SYMBOLS}
    base.update(kw)
    return base


def _order(**kw):
    o = {"symbol": "BTCUSDT", "position_action": "开多", "execution_mode": "现在执行",
         "trigger_price": None, "position_size_pct": None, "explanation": "測試"}
    o.update(kw)
    return o


PRICES = {"BTCUSDT": 60000.0, "ETHUSDT": 1800.0, "SOLUSDT": 80.0}


# ---------------- filter_valid_orders ----------------

def test_open_long_passes_and_price_overwritten():
    got = tsa.filter_valid_orders([_order()], _positions(), PRICES)
    assert len(got) == 1
    assert got[0]["trigger_price"] == 60000.0  # 现在执行 → 一律覆寫成實際查到的現價


def test_tp_without_position_filtered():
    o = _order(position_action="止盈多", execution_mode="到价触发", trigger_price=65000)
    assert tsa.filter_valid_orders([o], _positions(), PRICES) == []


def test_tp_with_position_passes():
    o = _order(position_action="止盈多", execution_mode="到价触发", trigger_price=65000)
    pos = _positions(BTCUSDT={"side": "多", "amt": 0.5})
    assert len(tsa.filter_valid_orders([o], pos, PRICES)) == 1


def test_tp_must_be_trigger_mode_with_price():
    pos = _positions(BTCUSDT={"side": "多", "amt": 0.5})
    assert tsa.filter_valid_orders(
        [_order(position_action="止盈多", execution_mode="现在执行", trigger_price=65000)], pos, PRICES) == []
    assert tsa.filter_valid_orders(
        [_order(position_action="止盈多", execution_mode="到价触发", trigger_price=None)], pos, PRICES) == []


def test_invalid_enum_or_missing_fields_filtered():
    assert tsa.filter_valid_orders([_order(symbol="DOGEUSDT")], _positions(), PRICES) == []
    assert tsa.filter_valid_orders([_order(position_action="梭哈")], _positions(), PRICES) == []
    assert tsa.filter_valid_orders([_order(explanation="")], _positions(), PRICES) == []
    assert tsa.filter_valid_orders(["not a dict"], _positions(), PRICES) == []


def test_add_short_requires_short_position():
    o = _order(position_action="加空", execution_mode="现在执行")
    assert tsa.filter_valid_orders([o], _positions(), PRICES) == []
    pos = _positions(BTCUSDT={"side": "空", "amt": -0.5})
    assert len(tsa.filter_valid_orders([o], pos, PRICES)) == 1


def test_stale_entry_price_empties_whole_message():
    # BTC 進場價 55000 離現價 60000 已達 1000 點門檻 → 整則訊息清空，
    # 連同同一則訊息裡其他本來合格的訂單（现在执行的 ETH 開多）也一併不產生。
    stale = _order(execution_mode="到价触发", trigger_price=55000)
    fresh = _order(symbol="ETHUSDT", execution_mode="现在执行")
    assert tsa.filter_valid_orders([stale, fresh], _positions(), PRICES) == []


def test_entry_price_within_limit_passes():
    o = _order(execution_mode="到价触发", trigger_price=59100)  # 差 900 < 1000
    assert len(tsa.filter_valid_orders([o], _positions(), PRICES)) == 1
    exact = _order(execution_mode="到价触发", trigger_price=59000)  # 差恰好 1000 → 達門檻即過時
    assert tsa.filter_valid_orders([exact], _positions(), PRICES) == []


def test_stale_check_uses_per_symbol_limit():
    o = _order(symbol="ETHUSDT", execution_mode="到价触发", trigger_price=1700)  # 差 100 >= 30
    assert tsa.filter_valid_orders([o], _positions(), PRICES) == []


def test_tp_sl_far_price_not_treated_as_stale():
    # 止盈/止損的價位本來就該離現價很遠，不適用陳舊訊號防線
    pos = _positions(BTCUSDT={"side": "多", "amt": 0.5})
    tp = _order(position_action="止盈多", execution_mode="到价触发", trigger_price=67500)
    sl = _order(position_action="止损多", execution_mode="到价触发", trigger_price=55000)
    assert len(tsa.filter_valid_orders([tp, sl], pos, PRICES)) == 2


def test_stale_add_also_blocked():
    pos = _positions(BTCUSDT={"side": "多", "amt": 0.5})
    o = _order(position_action="加多", execution_mode="到价触发", trigger_price=58000)  # 差 2000
    assert tsa.filter_valid_orders([o], pos, PRICES) == []


def test_size_grade_normalized_to_full_when_missing_or_invalid():
    # 舊記錄沒有 size_grade 欄位 / 模型填了 enum 外的值 → 一律正規化為全仓（維持原本全倉行為）
    got = tsa.filter_valid_orders([_order()], _positions(), PRICES)
    assert got[0]["size_grade"] == "全仓"
    got = tsa.filter_valid_orders([_order(size_grade="重仓")], _positions(), PRICES)
    assert got[0]["size_grade"] == "全仓"
    got = tsa.filter_valid_orders([_order(size_grade="轻仓")], _positions(), PRICES)
    assert got[0]["size_grade"] == "轻仓"  # 合法值原樣保留


# ---------------- main()：失敗不寫檔、下次重試 ----------------

def _setup_main(monkeypatch, tmp_path, structured_records):
    structured = tmp_path / "structured.jsonl"
    tradable = tmp_path / "tradable.jsonl"
    structured.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in structured_records) + "\n", encoding="utf-8")
    monkeypatch.setattr(tsa, "STRUCTURED_INPUT_JSONL", structured)
    monkeypatch.setattr(tsa, "TRADABLE_OUTPUT_JSONL", tradable)
    monkeypatch.setattr(tsa, "fetch_positions", lambda: _positions())
    monkeypatch.setattr(tsa, "fetch_prices", lambda: dict(PRICES))
    monkeypatch.setattr(tsa, "_get_client", lambda: object())
    return tradable


def test_api_failure_not_written_and_retried(monkeypatch, tmp_path):
    recs = [{"index": 1, "text": "訊號一"}, {"index": 2, "text": "訊號二"}]
    tradable = _setup_main(monkeypatch, tmp_path, recs)

    # 第一次執行：index 1 成功、index 2 API 失敗（回傳 None）→ 只寫入 index 1
    def convert_first(client, record, positions, prices, snapshot_time):
        return [_order()] if record["index"] == 1 else None
    monkeypatch.setattr(tsa, "convert_to_tradable_orders", convert_first)
    tsa.main()
    written = [json.loads(l) for l in tradable.read_text(encoding="utf-8").splitlines()]
    assert [r["source_index"] for r in written] == [1]

    # 第二次執行：index 2 這次成功 → 自動補上，index 1 不重複處理
    monkeypatch.setattr(tsa, "convert_to_tradable_orders",
                        lambda client, record, positions, prices, snapshot_time: [_order()])
    tsa.main()
    written = [json.loads(l) for l in tradable.read_text(encoding="utf-8").splitlines()]
    assert [r["source_index"] for r in written] == [1, 2]


def test_genuine_empty_orders_marked_processed(monkeypatch, tmp_path):
    tradable = _setup_main(monkeypatch, tmp_path, [{"index": 1, "text": "只是閒聊"}])
    monkeypatch.setattr(tsa, "convert_to_tradable_orders",
                        lambda *a, **kw: [])  # 真的判斷為無訂單（非失敗）
    tsa.main()
    written = [json.loads(l) for l in tradable.read_text(encoding="utf-8").splitlines()]
    assert written[0]["orders"] == []           # 寫入空 orders = 已處理，不會重試
    assert tsa.load_already_processed_indexes() == {1}


def test_corrupt_input_line_skipped_not_crash(monkeypatch, tmp_path, capsys):
    structured = tmp_path / "structured.jsonl"
    structured.write_text(
        json.dumps({"index": 1, "text": "正常"}, ensure_ascii=False) + "\n"
        + "{broken line\n"
        + json.dumps({"time": "9:00"}, ensure_ascii=False) + "\n",  # 缺 index/text
        encoding="utf-8")
    tradable = tmp_path / "tradable.jsonl"
    monkeypatch.setattr(tsa, "STRUCTURED_INPUT_JSONL", structured)
    monkeypatch.setattr(tsa, "TRADABLE_OUTPUT_JSONL", tradable)
    monkeypatch.setattr(tsa, "fetch_positions", lambda: _positions())
    monkeypatch.setattr(tsa, "fetch_prices", lambda: dict(PRICES))
    monkeypatch.setattr(tsa, "_get_client", lambda: object())
    monkeypatch.setattr(tsa, "convert_to_tradable_orders", lambda *a, **kw: [])
    tsa.main()  # 不應拋例外
    out = capsys.readouterr().out
    assert "無法解析" in out and "缺少 index/text" in out
    written = [json.loads(l) for l in tradable.read_text(encoding="utf-8").splitlines()]
    assert [r["source_index"] for r in written] == [1]
