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


@st.cache_data(ttl=15, show_spinner="查詢 Binance Testnet 持倉與現價...")
def _load_account_snapshot():
    positions = tsa.fetch_positions()
    prices = tsa.fetch_prices()
    return positions, prices


with st.sidebar:
    st.subheader("目前帳戶快照")
    if st.button("重新整理持倉/現價"):
        _load_account_snapshot.clear()
    positions, prices = _load_account_snapshot()
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

        st.subheader("Stage3 結果（可交易訊號，對照 阿佛禁言群B_可交易訊號.jsonl）")
        if not orders:
            st.info("模型判斷：此訊息目前無可執行的具體交易指令（略過，不產生訂單）。")
        st.json(stage3_record, expanded=True)

        st.subheader("Stage4 結果（下單參數試算，對照 阿佛禁言群B_下單參數.jsonl）")
        if not orders:
            st.info("沒有 Stage3 訂單可供試算。")
        else:
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

            for r in stage4_results:
                label = f"order_idx={r['order_idx']} {r['symbol']} {r['position_action']} -> {r['status']}"
                if r["status"] == "ERROR":
                    st.error(f"{label}：{r['error']}")
                else:
                    st.success(label)
                    st.json(r["order_request"]["params"])
                    with st.expander("計算明細（含忽略的 LLM position_size_pct、槓桿、保證金等）"):
                        st.json(r["calc_detail"])
