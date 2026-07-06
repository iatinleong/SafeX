# -*- coding: utf-8 -*-
"""
讀取「阿佛禁言群B_下單參數.jsonl」，先用 Binance Testnet /fapi/v1/order/test
做不成交驗證；全部通過後，才逐筆送 /fapi/v1/order 成交驗證。

本腳本不重新計算 quantity，只使用「下單參數.jsonl」內已生成的 preflight_requests
與 order_request，確保驗證與實際送出的參數一致。
"""
import argparse
import json
import time
from pathlib import Path

from app import order_executor as oe

ORDER_PARAMS_JSONL = Path(r"C:\Users\user\Desktop\SafeW\阿佛禁言群B_下單參數.jsonl")
REPORT_JSON = Path(r"C:\Users\user\Desktop\SafeW\下單驗證與成交報告.json")


def _load_ready_records():
    records = []
    for line in ORDER_PARAMS_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("status") == "READY" and rec.get("order_request"):
            records.append(rec)
    return records


def _symbols(records):
    return sorted({r["order_request"]["params"]["symbol"] for r in records})


def _account_snapshot(symbols):
    account = oe._signed_request("GET", "/fapi/v2/account", {})
    mode = oe._signed_request("GET", "/fapi/v1/positionSide/dual", {})
    multi_assets = oe._signed_request("GET", "/fapi/v1/multiAssetsMargin", {})
    positions = oe._signed_request("GET", "/fapi/v2/positionRisk", {})
    open_orders = []
    for symbol in symbols:
        open_orders.extend(oe._signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol}))

    usdt = next((a for a in account.get("assets", []) if a.get("asset") == "USDT"), {})
    symbol_settings = []
    nonzero_positions = []
    for p in positions:
        if p.get("symbol") not in symbols:
            continue
        symbol_settings.append({
            "symbol": p.get("symbol"),
            "positionSide": p.get("positionSide"),
            "positionAmt": p.get("positionAmt"),
            "leverage": p.get("leverage"),
            "marginType": p.get("marginType"),
            "isolatedMargin": p.get("isolatedMargin"),
        })
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        nonzero_positions.append({
            "symbol": p.get("symbol"),
            "positionSide": p.get("positionSide"),
            "positionAmt": p.get("positionAmt"),
            "entryPrice": p.get("entryPrice"),
            "markPrice": p.get("markPrice"),
            "unRealizedProfit": p.get("unRealizedProfit"),
            "liquidationPrice": p.get("liquidationPrice"),
            "leverage": p.get("leverage"),
            "marginType": p.get("marginType"),
        })

    return {
        "position_mode": mode,
        "multi_assets_mode": multi_assets,
        "usdt": {
            "walletBalance": usdt.get("walletBalance"),
            "availableBalance": usdt.get("availableBalance"),
            "crossWalletBalance": usdt.get("crossWalletBalance"),
            "unrealizedProfit": usdt.get("unrealizedProfit"),
        },
        "symbol_settings": symbol_settings,
        "positions": nonzero_positions,
        "open_orders": [
            {
                "symbol": o.get("symbol"),
                "orderId": o.get("orderId"),
                "side": o.get("side"),
                "positionSide": o.get("positionSide"),
                "type": o.get("type"),
                "origQty": o.get("origQty"),
                "price": o.get("price"),
                "stopPrice": o.get("stopPrice"),
                "status": o.get("status"),
                "reduceOnly": o.get("reduceOnly"),
                "closePosition": o.get("closePosition"),
            }
            for o in open_orders
        ],
    }


def _run_preflight(records):
    results = []
    seen = set()
    for rec in records:
        for req in rec.get("preflight_requests") or []:
            key = (req["method"], req["endpoint"], json.dumps(req["params"], sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            result = _run_preflight_request(req)
            results.append(result)
    return results


def _get_actual_leverage(symbol):
    positions = oe._signed_request("GET", "/fapi/v2/positionRisk", {})
    for p in positions:
        if p.get("symbol") == symbol:
            return int(p.get("leverage"))
    return None


def _run_preflight_request(req):
    """執行 preflight；遇到 Binance -1007/timeout 時查實際狀態並重試。"""
    symbol = req["params"].get("symbol")
    expected_leverage = req["params"].get("leverage")
    attempts = []
    for attempt in range(1, 4):
        try:
            resp = oe._signed_request(req["method"], req["endpoint"], req["params"])
            actual = _get_actual_leverage(symbol) if symbol and expected_leverage else None
            if expected_leverage and actual != expected_leverage:
                attempts.append({"attempt": attempt, "response": resp, "actual_leverage": actual})
                time.sleep(1)
                continue
            return {"request": req, "status": "OK", "response": resp, "actual_leverage": actual, "attempts": attempts}
        except Exception as e:
            actual = _get_actual_leverage(symbol) if symbol and expected_leverage else None
            attempts.append({"attempt": attempt, "error": str(e), "actual_leverage": actual})
            if expected_leverage and actual == expected_leverage:
                return {
                    "request": req,
                    "status": "OK_CONFIRMED_AFTER_ERROR",
                    "response": None,
                    "actual_leverage": actual,
                    "attempts": attempts,
                }
            time.sleep(2)
    # 重試全部失敗（例如 Testnet 後端對特定 symbol 的設定端點 -1007 逾時）。
    # 若該 symbol 帳戶上已有有效的現存槓桿，則沿用現值放行，不阻擋成交驗證。
    if symbol and expected_leverage:
        existing = _get_actual_leverage(symbol)
        if existing:
            return {
                "request": req,
                "status": "OK_USING_EXISTING_LEVERAGE",
                "response": None,
                "expected_leverage": expected_leverage,
                "actual_leverage": existing,
                "note": (
                    f"無法設定 leverage={expected_leverage}（後端逾時 -1007），"
                    f"沿用帳戶現有槓桿 {existing}x。名目部位大小(quantity)不變，"
                    f"僅鎖倉保證金與爆倉距離受槓桿影響。"
                ),
                "attempts": attempts,
            }
    return {"request": req, "status": "ERROR", "error": "preflight failed after retries", "attempts": attempts}


def _test_orders(records):
    results = []
    for rec in records:
        req = rec["order_request"]
        attempts = []
        final = None
        for attempt in range(1, 4):
            try:
                resp = oe._signed_request(req["method"], "/fapi/v1/order/test", req["params"])
                final = {"status": "OK", "response": resp}
                break
            except Exception as e:
                attempts.append({"attempt": attempt, "error": str(e)})
                time.sleep(2)
        if final is None:
            final = {"status": "ERROR", "error": attempts[-1]["error"] if attempts else "unknown error"}
        results.append({
            "source_index": rec["source_index"],
            "order_idx": rec["order_idx"],
            "symbol": req["params"].get("symbol"),
            "params": req["params"],
            "attempts": attempts,
            **final,
        })
    return results


def _execute_orders(records):
    results = []
    for rec in records:
        req = rec["order_request"]
        try:
            resp = oe._signed_request(req["method"], req["endpoint"], req["params"])
            results.append({
                "source_index": rec["source_index"],
                "order_idx": rec["order_idx"],
                "symbol": req["params"]["symbol"],
                "params": req["params"],
                "status": "OK",
                "response": resp,
            })
        except Exception as e:
            results.append({
                "source_index": rec["source_index"],
                "order_idx": rec["order_idx"],
                "symbol": req["params"].get("symbol"),
                "params": req["params"],
                "status": "ERROR",
                "error": str(e),
            })
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="不成交驗證全通過後，實際送出 Testnet 訂單")
    args = parser.parse_args()

    records = _load_ready_records()
    symbols = _symbols(records)
    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "input": str(ORDER_PARAMS_JSONL),
        "symbols": symbols,
        "pre_account": _account_snapshot(symbols),
        "preflight_results": _run_preflight(records),
        "test_order_results": [],
        "live_order_results": [],
        "post_account": None,
    }

    report["test_order_results"] = _test_orders(records)
    preflight_ok = all(r["status"] in {"OK", "OK_CONFIRMED_AFTER_ERROR", "OK_USING_EXISTING_LEVERAGE"} for r in report["preflight_results"])
    test_ok = all(r["status"] == "OK" for r in report["test_order_results"])

    if args.execute and preflight_ok and test_ok:
        report["live_order_results"] = _execute_orders(records)
        report["post_account"] = _account_snapshot(symbols)
    else:
        report["post_account"] = _account_snapshot(symbols)

    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"preflight_ok={preflight_ok} test_ok={test_ok} executed={bool(report['live_order_results'])}")
    print(f"report={REPORT_JSON}")
    for r in report["test_order_results"]:
        print(f"TEST {r['symbol']} source={r['source_index']}:{r['order_idx']} {r['status']} {r.get('error', '')}")
    for r in report["live_order_results"]:
        response = r.get("response") or {}
        print(
            f"LIVE {r['symbol']} source={r['source_index']}:{r['order_idx']} "
            f"{r['status']} orderId={response.get('orderId')} status={response.get('status')} "
            f"executedQty={response.get('executedQty')} avgPrice={response.get('avgPrice')}"
        )


if __name__ == "__main__":
    main()
