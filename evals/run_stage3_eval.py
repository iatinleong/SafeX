# -*- coding: utf-8 -*-
"""
Stage 3 真實評測 runner：對 evals/stage3_cases.py 的每個案例，
以「注入的固定持倉/現價」真實呼叫 Gemini（gemini-3.5-flash），
用約束式期望檢查輸出，並把每筆訂單串進 Stage 4（行情 mock 成與注入一致）
驗證「結構化 → 下單參數」全鏈路產出的記錄合法。

會消耗 Gemini API 額度（每案例 1 次呼叫，約 20+ 次），Binance 完全不打。

用法：
    python -m evals.run_stage3_eval                 # 跑全部案例
    python -m evals.run_stage3_eval --case tp_short # 只跑指定案例（可重複）
    python -m evals.run_stage3_eval --repeat 3      # 每個案例跑 N 次（觀察穩定性）

輸出：逐案例 PASS/FAIL + 原因，總結表；同時把完整結果寫進 evals/last_report.json。
exit code = 非 advisory 案例的失敗數（0 = 全過）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

from app import ssl_compat
ssl_compat.apply()  # Avast TLS 攔截環境需要，無 Avast 時不動作

import app.order_executor as oe
import app.signal_to_order_params as sop
import app.trading_signal_agent as tsa
from app.jsonl_schemas import validate_order_params_record
from evals.stage3_cases import CASES, PRICES

REPORT_PATH = Path(__file__).parent / "last_report.json"

_STEP = {"BTCUSDT": 0.001, "ETHUSDT": 0.001, "SOLUSDT": 0.01}


def build_positions(spec: dict) -> dict:
    """{symbol: 數量（正多/負空）} → trading_signal_agent 的 positions 結構。"""
    result = {s: {"side": None, "amt": 0.0} for s in tsa.SUPPORTED_SYMBOLS}
    for sym, amt in spec.items():
        result[sym] = {"side": "多" if amt > 0 else "空", "amt": float(amt)}
    return result


# ---------------- 約束檢查 ----------------

def check_expectations(expect: dict, orders: list) -> list:
    """回傳違反約束的訊息清單（空 = 通過）。"""
    fails = []
    if expect.get("empty"):
        if orders:
            fails.append(f"預期無訂單，實際 {len(orders)} 筆：" +
                         "; ".join(f"{o.get('symbol')} {o.get('position_action')}" for o in orders))
        return fails

    n = len(orders)
    count = expect.get("count")
    if count is not None:
        lo, hi = count if isinstance(count, tuple) else (count, count)
        if not (lo <= n <= hi):
            fails.append(f"訂單筆數 {n} 不在預期範圍 [{lo},{hi}]")

    if orders and "symbols" in expect:
        got = {o.get("symbol") for o in orders}
        if got != set(expect["symbols"]):
            fails.append(f"symbol 集合 {sorted(got)} != 預期 {sorted(expect['symbols'])}")
    for sym in expect.get("forbid_symbols", ()):
        if any(o.get("symbol") == sym for o in orders):
            fails.append(f"出現了不允許的 symbol {sym}")

    if "actions" in expect:
        for o in orders:
            if o.get("position_action") not in expect["actions"]:
                fails.append(f"{o.get('symbol')} 的 position_action={o.get('position_action')!r} "
                             f"不在允許集合 {sorted(expect['actions'])}")
    if "mode" in expect:
        for o in orders:
            if o.get("execution_mode") != expect["mode"]:
                fails.append(f"{o.get('symbol')} execution_mode={o.get('execution_mode')!r} != {expect['mode']!r}")

    for sym, spec in expect.get("triggers", {}).items():
        for o in orders:
            if o.get("symbol") != sym:
                continue
            t = o.get("trigger_price")
            if spec == "live":
                if t != PRICES[sym]:
                    fails.append(f"{sym} trigger_price={t} 應等於注入現價 {PRICES[sym]}（现在执行覆寫）")
            elif isinstance(spec, tuple):
                if t is None or not (spec[0] <= t <= spec[1]):
                    fails.append(f"{sym} trigger_price={t} 不在區間 [{spec[0]},{spec[1]}]")
            else:
                if t != spec:
                    fails.append(f"{sym} trigger_price={t} != 預期 {spec}")

    if "pct_range" in expect:
        lo, hi = expect["pct_range"]
        for o in orders:
            pct = o.get("position_size_pct")
            if pct is None or not (lo <= pct <= hi):
                fails.append(f"{o.get('symbol')} position_size_pct={pct} 不在區間 [{lo},{hi}]"
                             f"（『总仓位』須按標的數均分）")

    if "grades" in expect:
        spec = expect["grades"]
        for o in orders:
            want = spec if isinstance(spec, str) else spec.get(o.get("symbol"))
            if want and o.get("size_grade") != want:
                fails.append(f"{o.get('symbol')} size_grade={o.get('size_grade')!r} != 預期 {want!r}")
    return fails


# ---------------- Stage 4 串接（行情 mock 成與注入一致，不打 Binance） ----------------

def run_stage4_chain(orders: list, position_spec: dict, source_index: int):
    """把 Stage 3 輸出串進正式的 Stage 4 轉換路徑，回傳 (記錄清單, 問題清單)。
    行情/持倉 mock 成與 Stage 3 注入值一致；每筆記錄都必須通過 schema 驗證。"""
    saved = {name: getattr(oe, name) for name in (
        "get_symbol_filters", "get_available_balance_usdt", "get_current_price",
        "get_mark_price", "get_position_quantity", "_POSITION_MODE_CACHE")}
    oe.get_symbol_filters = lambda s: {"stepSize": _STEP[s], "minQty": _STEP[s], "tickSize": 0.1}
    oe.get_available_balance_usdt = lambda: 1000.0
    oe.get_current_price = lambda s: PRICES[s]
    oe.get_mark_price = lambda s: PRICES[s]
    oe.get_position_quantity = lambda s, a: abs(position_spec.get(s, 0.0))
    oe._POSITION_MODE_CACHE = False
    try:
        rec = {"source_index": source_index}
        n = oe._symbol_count_needing_quantity(orders)
        results, problems = [], []
        for idx, order in enumerate(orders):
            r = sop._self_check_record(sop._build_output_record(rec, idx, order, n))
            results.append(r)
            for e in validate_order_params_record(r):
                problems.append(f"order_idx={idx} 記錄不合法：{e}")
        return results, problems
    finally:
        for name, value in saved.items():
            setattr(oe, name, value)


# ---------------- 主流程 ----------------

def run_case(client, case: dict, run_no: int) -> dict:
    positions = build_positions(case["positions"])
    prices = dict(PRICES)
    snapshot_time = time.strftime("%Y-%m-%d %H:%M:%S")
    orders = tsa.convert_to_tradable_orders(
        client, {"text": case["text"]}, positions, prices, snapshot_time)
    if orders is None:
        return {"id": case["id"], "run": run_no, "status": "API_FAIL",
                "advisory": bool(case.get("advisory")),
                "orders": None, "failures": ["Gemini 重試後仍失敗"], "stage4": None}

    failures = check_expectations(case["expect"], orders)

    stage4_records = None
    if orders:
        stage4_records, s4_problems = run_stage4_chain(orders, case["positions"], source_index=run_no)
        failures += s4_problems
        if case["expect"].get("stage4_all_ready"):
            for r in stage4_records:
                if r["status"] != "READY":
                    failures.append(f"Stage4 order_idx={r['order_idx']} 預期 READY，"
                                    f"實際 {r['status']}（{r.get('error')}）")

    return {"id": case["id"], "run": run_no,
            "status": "PASS" if not failures else "FAIL",
            "advisory": bool(case.get("advisory")),
            "orders": orders, "failures": failures, "stage4": stage4_records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", help="只跑指定案例 id（可重複）")
    parser.add_argument("--repeat", type=int, default=1, help="每個案例跑 N 次（觀察穩定性）")
    args = parser.parse_args()

    cases = CASES if not args.case else [c for c in CASES if c["id"] in set(args.case)]
    if not cases:
        print(f"找不到指定案例，可用 id：{[c['id'] for c in CASES]}")
        return 2

    client = tsa._get_client()
    print(f"共 {len(cases)} 個案例 × {args.repeat} 次（模型：{tsa.MODEL_ID}，"
          f"注入現價 BTC={PRICES['BTCUSDT']} ETH={PRICES['ETHUSDT']} SOL={PRICES['SOLUSDT']}）\n")

    all_results = []
    gating_fail = advisory_fail = api_fail = 0
    for case in cases:
        for run_no in range(1, args.repeat + 1):
            r = run_case(client, case, run_no)
            all_results.append(r)
            tag = "ADVISORY" if r["advisory"] else "GATING"
            n_orders = "-" if r["orders"] is None else len(r["orders"])
            print(f"[{r['status']:8s}] {r['id']:32s} ({tag}) 訂單={n_orders}")
            for f in r["failures"]:
                print(f"           - {f}")
            if r["status"] == "API_FAIL":
                api_fail += 1
            elif r["status"] == "FAIL":
                if r["advisory"]:
                    advisory_fail += 1
                else:
                    gating_fail += 1

    total = len(all_results)
    print(f"\n===== 總結：{total} 次執行 =====")
    print(f"  紅燈（gating 失敗）：{gating_fail}")
    print(f"  觀察（advisory 失敗）：{advisory_fail}")
    print(f"  API 失敗：{api_fail}")
    REPORT_PATH.write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完整結果已寫入 {REPORT_PATH}")
    return min(gating_fail + api_fail, 125)


if __name__ == "__main__":
    sys.exit(main())
