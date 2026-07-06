# -*- coding: utf-8 -*-
"""
把「阿佛禁言群B_可交易訊號.jsonl」轉換成 Binance 真正會使用的 API request，
輸出到「阿佛禁言群B_下單參數.jsonl」，供人工逐筆核對（本程式**不會下單**）。

轉換邏輯與 order_executor.py 的 --show 完全一致（重用同一套計算函式，避免邏輯分歧）：
- 同一則訊息裡有 N 個需要計算數量的標的（開倉/加倉類），可用 USDT 餘額均分給這 N 個標的
  （每檔保證金 = 可用餘額 ÷ N），槓桿固定 DEFAULT_LEVERAGE 倍，
  notional = 保證金 × 槓桿，quantity = notional ÷ 現價，並依 LOT_SIZE stepSize 捨位。
- 减多/减空 屬於部分減倉，quantity 必須基於目前持倉比例；目前沒有明確比例規則時會輸出 ERROR。
- 止盈/止損必須有 trigger_price，否則輸出 ERROR，不會產生下單 request。
- 「現在平倉」不能用 closePosition=true；Binance MARKET 平倉必須查目前倉位後傳 quantity+reduceOnly。
- closePosition=true 只用在 STOP_MARKET / TAKE_PROFIT_MARKET 這類條件平倉單。
- orders 為空的紀錄會被跳過（不輸出）。

用法：
    python signal_to_order_params.py
    # 輸出：阿佛禁言群B_下單參數.jsonl（每行一筆可送 API 的 request 或 ERROR）
"""
import json
from pathlib import Path

from app import order_executor as oe

OUTPUT_JSONL = Path(r"C:\Users\user\Desktop\SafeW\阿佛禁言群B_下單參數.jsonl")


def _build_output_record(rec: dict, order_idx: int, order: dict, symbol_count: int) -> dict:
    try:
        detail = oe.compute_binance_params(order, symbol_count=symbol_count)
    except Exception as e:
        return {
            "source_index": rec["source_index"],
            "order_idx": order_idx,
            "symbol": order.get("symbol"),
            "position_action": order.get("position_action"),
            "status": "ERROR",
            "preflight_requests": [],
            "order_request": None,
            "error": str(e),
        }

    preflight_requests = []
    if detail.get("leverage_will_be_set_before_order"):
        preflight_requests.append({
            "method": "POST",
            "endpoint": "/fapi/v1/leverage",
            "params": {
                "symbol": detail["symbol"],
                "leverage": oe.DEFAULT_LEVERAGE,
            },
        })

    return {
        "source_index": rec["source_index"],
        "order_idx": order_idx,
        "symbol": order.get("symbol"),
        "position_action": order.get("position_action"),
        "status": "READY",
        "preflight_requests": preflight_requests,
        "order_request": {
            "method": "POST",
            "endpoint": "/fapi/v1/order",
            "params": detail["binance_params"],
        },
        "error": None,
    }


def main():
    records = oe._load_records()
    results = []
    skipped = 0
    for rec in records:
        orders = rec.get("orders") or []
        if not orders:
            skipped += 1
            continue
        n = oe._symbol_count_needing_quantity(orders)
        for order_idx, order in enumerate(orders):
            results.append(_build_output_record(rec, order_idx, order, n))

    with OUTPUT_JSONL.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"已轉換 {len(results)} 筆訂單（跳過 {skipped} 筆 orders 為空的紀錄），輸出至：{OUTPUT_JSONL}")
    for r in results:
        status = "OK" if r["status"] == "READY" else f"[錯誤] {r['error']}"
        params = r["order_request"]["params"] if r["order_request"] else None
        print(f"  source_index={r['source_index']} order_idx={r['order_idx']} "
              f"{r['symbol']} {r['position_action']} -> {status} {params}")


if __name__ == "__main__":
    main()
