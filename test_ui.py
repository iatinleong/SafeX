# -*- coding: utf-8 -*-
"""
Streamlit 測試介面：讓使用者手動輸入一句 source_text，即時跑過
Stage 3（trading_signal_agent.convert_to_tradable_orders）與
Stage 4（order_executor.compute_binance_params）完整流程，
顯示結果格式與 阿佛禁言群B_可交易訊號.jsonl / 阿佛禁言群B_下單參數.jsonl 一致，
方便快速驗證 prompt/邏輯修改是否符合預期，不需要真的跑批次檔案。

用法：
    streamlit run test_ui.py
"""
import time

import streamlit as st

from app import trading_signal_agent as tsa
from app import order_executor as oe

st.set_page_config(page_title="阿佛訊號轉換測試台", layout="wide")
st.title("阿佛禁言群 訊號 → 可交易訂單 測試台")
st.caption("輸入一句原始訊息，實際呼叫 Stage3（Gemini）+ Stage4（下單參數試算），結果與批次 jsonl 格式一致。")


def _fmt_num(val, digits=4):
    if val is None or val == "":
        return "-"
    try:
        return f"{float(val):,.{digits}f}"
    except (TypeError, ValueError):
        return str(val)


def _fetch_testnet_details():
    """查詢 Testnet 資產、持倉明細與 open orders。"""
    assets = {}
    detailed_positions = []
    open_orders = []
    error = None

    try:
        account = oe._signed_request("GET", "/fapi/v2/account", {})
        usdt = next((a for a in account.get("assets", []) if a.get("asset") == "USDT"), {})
        assets = {
            "walletBalance": usdt.get("walletBalance"),
            "availableBalance": usdt.get("availableBalance"),
            "crossWalletBalance": usdt.get("crossWalletBalance"),
            "unrealizedProfit": usdt.get("unrealizedProfit"),
        }

        raw_positions = oe._signed_request("GET", "/fapi/v2/positionRisk", {})
        for p in raw_positions:
            symbol = p.get("symbol")
            if symbol not in tsa.SUPPORTED_SYMBOLS:
                continue
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            side = "多" if amt > 0 else "空"
            detailed_positions.append({
                "symbol": symbol,
                "side": side,
                "positionSide": p.get("positionSide"),
                "positionAmt": abs(amt),
                "entryPrice": p.get("entryPrice"),
                "markPrice": p.get("markPrice"),
                "unRealizedProfit": p.get("unRealizedProfit"),
                "leverage": p.get("leverage"),
                "marginType": p.get("marginType"),
            })

        for symbol in tsa.SUPPORTED_SYMBOLS:
            for o in oe._signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol}):
                open_orders.append({
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
                })
    except Exception as e:
        error = str(e)

    return assets, detailed_positions, open_orders, error


@st.cache_data(ttl=15, show_spinner="查詢 Binance Testnet 帳戶資料...")
def _load_account_snapshot():
    positions = tsa.fetch_positions()
    prices = tsa.fetch_prices()
    assets, detailed_positions, open_orders, error = _fetch_testnet_details()
    return positions, prices, assets, detailed_positions, open_orders, error


with st.sidebar:
    st.subheader("Binance Testnet 帳戶")
    if st.button("重新整理帳戶資料"):
        _load_account_snapshot.clear()
    positions, prices, assets, detailed_positions, open_orders, api_error = _load_account_snapshot()

    if api_error:
        st.warning(f"帳戶資料查詢失敗：{api_error}")

    st.markdown("**資產 (USDT)**")
    if assets:
        c1, c2 = st.columns(2)
        c1.metric("可用餘額", _fmt_num(assets.get("availableBalance"), 2))
        c2.metric("錢包餘額", _fmt_num(assets.get("walletBalance"), 2))
        c3, c4 = st.columns(2)
        c3.metric("全倉餘額", _fmt_num(assets.get("crossWalletBalance"), 2))
        c4.metric("未實現盈虧", _fmt_num(assets.get("unrealizedProfit"), 2))
    else:
        st.caption("無法取得資產資料")

    with st.expander("持倉 (Positions)", expanded=True):
        if detailed_positions:
            st.dataframe(detailed_positions, use_container_width=True, hide_index=True)
        else:
            st.caption("目前無持倉")

    with st.expander("掛單 (Open Orders)", expanded=True):
        if open_orders:
            st.dataframe(open_orders, use_container_width=True, hide_index=True)
        else:
            st.caption("目前無掛單")

    st.markdown("**危險操作**")

    cancel_confirm = st.checkbox("確認撤銷全部掛單", key="confirm_cancel_all", disabled=not open_orders)
    if st.button("撤銷所有掛單", disabled=not (open_orders and cancel_confirm)):
        cancel_results = []
        symbols_with_orders = sorted({o["symbol"] for o in open_orders})
        with st.spinner("撤銷掛單中..."):
            for symbol in symbols_with_orders:
                try:
                    resp = oe._signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
                    cancel_results.append({"symbol": symbol, "status": "OK", "response": resp})
                except Exception as e:
                    cancel_results.append({"symbol": symbol, "status": "ERROR", "error": str(e)})
        st.session_state["cancel_all_results"] = cancel_results
        _load_account_snapshot.clear()
        st.rerun()

    close_confirm = st.checkbox("確認一鍵全平倉（市價）", key="confirm_close_all", disabled=not detailed_positions)
    if st.button("一鍵全平倉", disabled=not (detailed_positions and close_confirm)):
        close_results = []
        hedge = oe.is_hedge_mode()
        with st.spinner("送出平倉單中..."):
            for p in detailed_positions:
                symbol = p["symbol"]
                side = "SELL" if p["side"] == "多" else "BUY"
                params = {
                    "symbol": symbol, "side": side, "type": "MARKET",
                    "quantity": p["positionAmt"], "newOrderRespType": "RESULT",
                }
                if hedge:
                    params["positionSide"] = "LONG" if p["side"] == "多" else "SHORT"
                else:
                    params["reduceOnly"] = "true"
                try:
                    resp = oe.place_order(params)
                    close_results.append({"symbol": symbol, "status": "OK", "response": resp})
                except Exception as e:
                    close_results.append({"symbol": symbol, "status": "ERROR", "error": str(e)})
        st.session_state["close_all_results"] = close_results
        _load_account_snapshot.clear()
        st.rerun()

    if st.session_state.get("cancel_all_results"):
        with st.expander("撤銷掛單結果", expanded=True):
            for r in st.session_state["cancel_all_results"]:
                if r["status"] == "OK":
                    st.success(f"{r['symbol']}：已撤銷")
                else:
                    st.error(f"{r['symbol']}：{r['error']}")

    if st.session_state.get("close_all_results"):
        with st.expander("全平倉結果", expanded=True):
            for r in st.session_state["close_all_results"]:
                if r["status"] == "OK":
                    resp = r["response"] or {}
                    st.success(f"{r['symbol']}：已平倉 orderId={resp.get('orderId')} status={resp.get('status')}")
                else:
                    st.error(f"{r['symbol']}：{r['error']}")

    st.markdown("**Stage3 帳戶上下文**")
    st.text(tsa.build_account_context_text(positions, prices))

source_text = st.text_area(
    "輸入 source_text（模擬阿佛禁言群的一則訊息）",
    value="这两天大家保持关注消息，插针我们就接多，BTC55000-57000分批接，合约和现货",
    height=100,
)

run = st.button("轉換 (Stage3 + Stage4)", type="primary")

if run:
    if not source_text.strip():
        st.warning("請先輸入 source_text。")
    else:
        snapshot_time = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            client = tsa._get_client()
        except Exception as e:
            st.error(f"無法建立 Gemini client：{e}")
            st.stop()

        with st.spinner(f"呼叫 Stage3（模型：{tsa.MODEL_ID}）..."):
            record = {"text": source_text}
            orders = tsa.convert_to_tradable_orders(client, record, positions, prices, snapshot_time)

        stage3_record = {
            "source_index": None,
            "source_text": source_text,
            "orders": orders,
        }

        if orders:
            symbol_count = oe._symbol_count_needing_quantity(orders)
            stage4_results = []
            for order_idx, order in enumerate(orders):
                try:
                    detail = oe.compute_binance_params(order, symbol_count=symbol_count)
                    stage4_results.append({
                        "order_idx": order_idx,
                        "symbol": order.get("symbol"),
                        "position_action": order.get("position_action"),
                        "status": "READY",
                        "order_request": {
                            "method": "POST",
                            "endpoint": "/fapi/v1/order",
                            "params": detail["binance_params"],
                        },
                        "calc_detail": detail,
                        "error": None,
                    })
                except Exception as e:
                    stage4_results.append({
                        "order_idx": order_idx,
                        "symbol": order.get("symbol"),
                        "position_action": order.get("position_action"),
                        "status": "ERROR",
                        "order_request": None,
                        "error": str(e),
                    })
        else:
            stage4_results = []

        # 新一輪 Stage3+4 結果存進 session_state，供下方下單按鈕使用（按鈕點擊會觸發 rerun，
        # 若不存進 session_state，這裡算出來的 orders/stage4_results 會在 rerun 後消失）。
        st.session_state["stage3_record"] = stage3_record
        st.session_state["stage4_results"] = stage4_results
        st.session_state["order_exec_results"] = None  # 新一輪轉換，清掉舊的下單結果

if st.session_state.get("stage3_record") is not None:
    stage3_record = st.session_state["stage3_record"]
    stage4_results = st.session_state["stage4_results"]
    orders = stage3_record["orders"]

    st.subheader("Stage3 結果（可交易訊號，對照 阿佛禁言群B_可交易訊號.jsonl）")
    if not orders:
        st.info("模型判斷：此訊息目前無可執行的具體交易指令（略過，不產生訂單）。")
    st.json(stage3_record, expanded=True)

    st.subheader("Stage4 結果（下單參數試算，對照 阿佛禁言群B_下單參數.jsonl）")
    if not orders:
        st.info("沒有 Stage3 訂單可供試算。")
    else:
        for r in stage4_results:
            label = f"order_idx={r['order_idx']} {r['symbol']} {r['position_action']} -> {r['status']}"
            if r["status"] == "ERROR":
                st.error(f"{label}：{r['error']}")
            else:
                st.success(label)
                st.json(r["order_request"]["params"])
                with st.expander("計算明細（含忽略的 LLM position_size_pct、槓桿、保證金等）"):
                    st.json(r["calc_detail"])

    ready_results = [r for r in stage4_results if r["status"] == "READY"]
    exec_results = st.session_state.get("order_exec_results")

    if ready_results and exec_results is None:
        st.subheader("Stage5：送出下單（Binance Testnet）")
        st.warning(f"點擊後會對上面 {len(ready_results)} 筆 READY 訂單依序：設定槓桿 -> "
                   f"/fapi/v1/order/test 驗證參數 -> 全部通過才送出 /fapi/v1/order 真正下單，"
                   f"會在你的 Testnet 帳戶產生真實部位/掛單。")
        if st.button("送出下單 (Testnet)", type="primary"):
            new_exec_results = []
            with st.spinner("送出訂單中..."):
                for r in ready_results:
                    detail = r["calc_detail"]
                    params = r["order_request"]["params"]
                    try:
                        if detail.get("leverage_will_be_set_before_order"):
                            oe.set_leverage(detail["symbol"], oe.DEFAULT_LEVERAGE)
                        oe._signed_request("POST", "/fapi/v1/order/test", params)  # 不成交驗證
                        resp = oe.place_order(params)  # 實際送出
                        new_exec_results.append({
                            "order_idx": r["order_idx"], "symbol": r["symbol"],
                            "status": "OK", "response": resp,
                        })
                    except Exception as e:
                        new_exec_results.append({
                            "order_idx": r["order_idx"], "symbol": r["symbol"],
                            "status": "ERROR", "error": str(e),
                        })
            st.session_state["order_exec_results"] = new_exec_results
            _load_account_snapshot.clear()  # 讓側邊欄下一次讀到最新的資產/持倉/掛單
            st.rerun()

    if exec_results:
        st.subheader("下單結果")
        for r in exec_results:
            label = f"order_idx={r['order_idx']} {r['symbol']} -> {r['status']}"
            if r["status"] == "OK":
                resp = r["response"] or {}
                st.success(f"{label}  orderId={resp.get('orderId')} status={resp.get('status')}")
                st.json(resp)
            else:
                st.error(f"{label}：{r['error']}")
        st.caption("已送出，側邊欄「Binance Testnet 帳戶」會顯示最新持倉/掛單（如未自動更新可點左側「重新整理帳戶資料」）。")