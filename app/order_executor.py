# -*- coding: utf-8 -*-
"""
Stage 4：下單執行模組（讀取「阿佛禁言群B_可交易訊號.jsonl」，逐筆呼叫 Binance Testnet 下單 API）。

設計原則：**絕不自動連續下單**。每一筆訂單都必須經過人工確認（透過本程式的
互動流程，或由呼叫端逐筆核對後才呼叫 --execute），逐行檢視、逐行處理，
orders 為空的紀錄直接跳過。所有「已確認/已略過/已下單/下單失敗」的狀態都會記錄在
order_execution_state.jsonl，重跑本程式不會對同一筆訂單重複下單。

【position_action -> Binance 參數對照】
- 开多／加多：side=BUY，非 reduceOnly（新建立或加碼多單）。
- 开空／加空：side=SELL，非 reduceOnly（新建立或加碼空單）。
- 减多：side=SELL，reduceOnly=true，quantity=部分數量（只縮小部位，倉位還在）。
- 减空：side=BUY，reduceOnly=true，quantity=部分數量。
- 平多／平空（现在执行）：查目前倉位後送 MARKET + quantity + reduceOnly=true（Hedge模式改用 positionSide，不送 reduceOnly）。
- 平多／平空（到价触发）：依觸發價方向送 STOP_MARKET 或 TAKE_PROFIT_MARKET + stopPrice + closePosition=true。
- 止盈多：side=SELL，type=TAKE_PROFIT_MARKET，stopPrice=trigger_price，closePosition=true。
- 止损多：side=SELL，type=STOP_MARKET，stopPrice=trigger_price，closePosition=true。
- 止盈空：side=BUY，type=TAKE_PROFIT_MARKET，stopPrice=trigger_price，closePosition=true。
- 止损空：side=BUY，type=STOP_MARKET，stopPrice=trigger_price，closePosition=true。

quantity 計算（僅開倉/加倉類動作需要，固定寫死規則，**忽略 Stage 3 產出的 position_size_pct**）：
    同一則訊息裡若有 N 個需要計算數量的標的，可用 USDT 餘額「均分」給這 N 個標的
    （每檔保證金 = 帳戶可用餘額 ÷ N，等於 100%/N），
    「槓桿 = DEFAULT_LEVERAGE 倍」，
    notional = 保證金 × 槓桿，quantity = notional ÷ 現價，
    並依照該 symbol 的 LOT_SIZE stepSize 無條件捨去到合法精度（避免下單被交易所拒絕）。
    下單前會先呼叫 /fapi/v1/leverage 把該 symbol 的槓桿設定為 DEFAULT_LEVERAGE 倍。

用法（逐行互動流程，供 agent/使用者逐筆核對後執行）：
    python order_executor.py --list                     # 列出所有待處理訂單與狀態
    python order_executor.py --show <source_index> <order_idx>   # 顯示某一筆訂單完整細節（含試算 quantity）
    python order_executor.py --execute <source_index> <order_idx>  # 實際送出下單
    python order_executor.py --skip <source_index> <order_idx>     # 標記為略過，不下單
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TRADABLE_INPUT_JSONL = Path(r"C:\Users\user\Desktop\SafeW\阿佛禁言群B_可交易訊號.jsonl")
EXECUTION_STATE_JSONL = Path(r"C:\Users\user\Desktop\SafeW\order_execution_state.jsonl")

BINANCE_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
BINANCE_KEY_ENV_VAR = "BINANCE_TESTNET_API_KEY"
BINANCE_SECRET_ENV_VAR = "BINANCE_TESTNET_API_SECRET"

# 固定寫死規則（依使用者要求，忽略 Stage 3 產出的 position_size_pct）：
# 同一則訊息裡若有 N 個需要計算數量的標的，可用 USDT 餘額「均分」給這 N 個標的
# （每檔保證金 = 可用餘額 ÷ N，也就是 100%/N），再乘上 DEFAULT_LEVERAGE 倍槓桿，
# 換算出下單名目金額（notional）與數量。
DEFAULT_LEVERAGE = 5

# 需要「先持有對應倉位才能執行」的動作 -> reduceOnly 訂單
REDUCE_ONLY_ACTIONS = {"减多", "减空", "平多", "平空", "止盈多", "止损多", "止盈空", "止损空"}
# 全部出場（不需要計算 quantity，交易所會用 closePosition 自動平掉目前全部倉位）
CLOSE_POSITION_ACTIONS = {"平多", "平空", "止盈多", "止损多", "止盈空", "止损空"}
# 一定是條件單（STOP_MARKET / TAKE_PROFIT_MARKET）
TRIGGER_ACTIONS = {"止盈多", "止损多", "止盈空", "止损空"}
ENTRY_ACTIONS = {"开多", "加多", "开空", "加空"}
# 开多/开空：全新倉位，用「可用餘額 ÷ symbol_count」計算 quantity。
OPEN_ACTIONS = {"开多", "开空"}
# 加多/加空：已有倉位再加碼，quantity 規則是「目前持倉 × 1」（讓總倉位變成目前的兩倍），
# 不重新按餘額計算（避免跟開倉搶同一份餘額）。
ADD_ACTIONS = {"加多", "加空"}
# 减多/减空：部分減倉，quantity 規則是「目前持倉 × 0.5」（賣出/買回一半，剩下一半倉位還在）。
PARTIAL_REDUCE_ACTIONS = {"减多", "减空"}
QUANTITY_ACTIONS = ENTRY_ACTIONS | PARTIAL_REDUCE_ACTIONS

# position_action -> side（BUY/SELL）
ACTION_TO_SIDE = {
    "开多": "BUY", "加多": "BUY",
    "开空": "SELL", "加空": "SELL",
    "减多": "SELL", "平多": "SELL", "止盈多": "SELL", "止损多": "SELL",
    "减空": "BUY", "平空": "BUY", "止盈空": "BUY", "止损空": "BUY",
}
# 止盈/止損各自對應的 order type
TRIGGER_ACTION_TO_TYPE = {
    "止盈多": "TAKE_PROFIT_MARKET", "止盈空": "TAKE_PROFIT_MARKET",
    "止损多": "STOP_MARKET", "止损空": "STOP_MARKET",
}


# ---------------- Binance Testnet API ----------------

def _get_keys():
    from app.env_config import get_secret
    api_key = get_secret(BINANCE_KEY_ENV_VAR)
    api_secret = get_secret(BINANCE_SECRET_ENV_VAR)
    if not api_key or not api_secret:
        raise RuntimeError(f"找不到環境變數/Streamlit secrets {BINANCE_KEY_ENV_VAR}/{BINANCE_SECRET_ENV_VAR}，請先設定 Binance Testnet 金鑰。")
    return api_key, api_secret


def _signed_request(method: str, path: str, params: dict) -> dict:
    api_key, api_secret = _get_keys()
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query = urllib.parse.urlencode(params)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{BINANCE_TESTNET_BASE_URL}{path}?{query}&signature={signature}"
    req = urllib.request.Request(url, method=method, headers={"X-MBX-APIKEY": api_key})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8").strip()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        raise RuntimeError(f"Binance API 錯誤 {e.code}: {body}")


def _public_get(path: str, params: dict) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{BINANCE_TESTNET_BASE_URL}{path}?{query}"
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


_EXCHANGE_INFO_CACHE = None


def get_symbol_filters(symbol: str) -> dict:
    """回傳 {stepSize, minQty, tickSize}（皆為 float）。"""
    global _EXCHANGE_INFO_CACHE
    if _EXCHANGE_INFO_CACHE is None:
        _EXCHANGE_INFO_CACHE = _public_get("/fapi/v1/exchangeInfo", {})
    for s in _EXCHANGE_INFO_CACHE["symbols"]:
        if s["symbol"] == symbol:
            filt = {f["filterType"]: f for f in s["filters"]}
            return {
                "stepSize": float(filt["LOT_SIZE"]["stepSize"]),
                "minQty": float(filt["LOT_SIZE"]["minQty"]),
                "tickSize": float(filt["PRICE_FILTER"]["tickSize"]),
            }
    raise ValueError(f"找不到 {symbol} 的交易規則")


def get_available_balance_usdt() -> float:
    data = _signed_request("GET", "/fapi/v2/account", {})
    for asset in data.get("assets", []):
        if asset.get("asset") == "USDT":
            return float(asset.get("availableBalance", 0))
    return 0.0


def get_current_price(symbol: str) -> float:
    data = _public_get("/fapi/v1/ticker/price", {"symbol": symbol})
    return float(data["price"])


def get_mark_price(symbol: str) -> float:
    """回傳標記價；條件單使用 workingType=MARK_PRICE 時，方向驗證也必須用標記價。"""
    data = _public_get("/fapi/v1/premiumIndex", {"symbol": symbol})
    return float(data["markPrice"])


def get_position_quantity(symbol: str, action: str) -> float:
    """回傳指定 symbol/action 相關的目前持倉數量（多單用正數絕對值，空單同樣回傳絕對值）；
    沒有對應倉位則回傳 0。用於：平倉/止盈/止損（全部倉位）、減倉（部分倉位基準）、
    加倉（目前倉位，用來算「加碼後變兩倍」需要再買入的數量）。"""
    data = _signed_request("GET", "/fapi/v2/positionRisk", {})
    for pos in data:
        if pos.get("symbol") != symbol:
            continue
        if is_hedge_mode() and pos.get("positionSide") != ACTION_TO_POSITION_SIDE[action]:
            continue
        amt = float(pos.get("positionAmt", 0))
        if action in {"平多", "止盈多", "止损多", "减多", "加多"} and amt > 0:
            return abs(amt)
        if action in {"平空", "止盈空", "止损空", "减空", "加空"} and amt < 0:
            return abs(amt)
    return 0.0


def set_leverage(symbol: str, leverage: int) -> dict:
    """呼叫 POST /fapi/v1/leverage 設定該 symbol 之後下單使用的槓桿倍數。"""
    return _signed_request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})


_POSITION_MODE_CACHE = None


def is_hedge_mode() -> bool:
    """回傳帳戶是否為雙向持倉（Hedge）模式；快取結果避免每筆訂單都查一次。"""
    global _POSITION_MODE_CACHE
    if _POSITION_MODE_CACHE is None:
        data = _signed_request("GET", "/fapi/v1/positionSide/dual", {})
        _POSITION_MODE_CACHE = bool(data.get("dualSidePosition", False))
    return _POSITION_MODE_CACHE


# position_action -> Hedge 模式下要傳的 positionSide（LONG/SHORT）
ACTION_TO_POSITION_SIDE = {
    "开多": "LONG", "加多": "LONG", "减多": "LONG", "平多": "LONG", "止盈多": "LONG", "止损多": "LONG",
    "开空": "SHORT", "加空": "SHORT", "减空": "SHORT", "平空": "SHORT", "止盈空": "SHORT", "止损空": "SHORT",
}


def round_step_size(quantity: float, step_size: float) -> float:
    """依 stepSize 無條件捨去到合法精度，避免下單被交易所以精度不符拒絕。"""
    import math
    precision = max(0, round(-math.log10(step_size)))
    steps = math.floor(quantity / step_size)
    return round(steps * step_size, precision)


# ---------------- 訂單參數計算 ----------------

def _resolve_conditional_order(symbol: str, side: str, execution_mode: str, trigger_price):
    """依 execution_mode/trigger_price 決定「非平倉類」訂單（开多/开空/加多/加空/减多/减空 共用）
    的 type 與價格欄位，回傳 (params_partial, detail_partial) 供呼叫端 update 進 params/detail：
    - 现在执行 -> MARKET
    - 到价触发 -> 觸發價比標記價有利 -> LIMIT；否則 -> STOP_MARKET（依標記價觸發）
    """
    if execution_mode == "现在执行":
        return {"type": "MARKET", "newOrderRespType": "RESULT"}, {}
    if trigger_price is None:
        raise ValueError(f"{symbol}：到價觸發必須有 trigger_price，否則無法決定 price/stopPrice。")
    mark_price = get_mark_price(symbol)
    if (side == "BUY" and trigger_price < mark_price) or (side == "SELL" and trigger_price > mark_price):
        return (
            {"type": "LIMIT", "price": trigger_price, "timeInForce": "GTC"},
            {"price": trigger_price, "order_type_reason": "觸發價比標記價更有利，使用 LIMIT 掛單。"},
        )
    return (
        {"type": "STOP_MARKET", "stopPrice": trigger_price, "workingType": "MARK_PRICE"},
        {"stopPrice": trigger_price, "order_type_reason": "觸發價比標記價更不利，使用 STOP_MARKET 等突破/跌破後觸發。"},
    )


def compute_binance_params(order: dict, symbol_count: int = 1) -> dict:
    """
    把 Stage 3 產出的 order（symbol/position_action/execution_mode/trigger_price/
    position_size_pct/explanation）轉換成可直接送給 /fapi/v1/order 的參數字典，
    並回傳試算細節（quantity 計算過程）供人工核對。

    symbol_count：同一則訊息裡「需要計算數量」的標的總數（開倉/加倉/減倉類）。
    可用餘額會依此數量均分（每檔保證金 = 可用餘額 ÷ symbol_count），
    確保多標的訊息不會讓每檔都各自用滿保證金。
    """
    symbol = order.get("symbol")
    action = order.get("position_action")
    execution_mode = order.get("execution_mode")
    trigger_price = order.get("trigger_price")
    if not symbol:
        raise ValueError("缺少 symbol，無法轉換成 Binance 下單參數。")
    if action not in ACTION_TO_SIDE:
        raise ValueError(f"{symbol}：未知 position_action={action!r}，無法映射到 Binance side/type。")
    if execution_mode not in {"现在执行", "到价触发"}:
        raise ValueError(f"{symbol} {action}：未知 execution_mode={execution_mode!r}。")
    side = ACTION_TO_SIDE[action]

    params = {"symbol": symbol, "side": side}
    detail = {
        "symbol": symbol,
        "position_action": action,
        "side": side,
        "field_mapping": {
            "position_action": {
                "input_value": action,
                "binance_field": "side",
                "binance_value": side,
            }
        },
    }

    if action in TRIGGER_ACTIONS:
        # 止盈/止損：一定是條件單、全部出場，不需要計算 quantity
        if execution_mode != "到价触发":
            raise ValueError(f"{symbol} {action}：止盈/止損必須是 execution_mode=到价触发。")
        if trigger_price is None:
            raise ValueError(f"{symbol} {action}：TP/SL 條件單必須有 trigger_price，否則無法填 Binance stopPrice。")
        # 驗證觸發價方向：條件單使用 MARK_PRICE 觸發，所以必須用標記價判斷是否會立刻觸發。
        mark_price = get_mark_price(symbol)
        if action == "止盈多" and trigger_price <= mark_price:
            raise ValueError(f"{symbol} 止盈多：trigger_price={trigger_price} 必須高於標記價={mark_price}，否則會立刻觸發。")
        if action == "止损多" and trigger_price >= mark_price:
            raise ValueError(f"{symbol} 止损多：trigger_price={trigger_price} 必須低於標記價={mark_price}，否則會立刻觸發。")
        if action == "止盈空" and trigger_price >= mark_price:
            raise ValueError(f"{symbol} 止盈空：trigger_price={trigger_price} 必須低於標記價={mark_price}，否則會立刻觸發。")
        if action == "止损空" and trigger_price <= mark_price:
            raise ValueError(f"{symbol} 止损空：trigger_price={trigger_price} 必須高於標記價={mark_price}，否則會立刻觸發。")
        params["type"] = TRIGGER_ACTION_TO_TYPE[action]
        params["stopPrice"] = trigger_price
        params["closePosition"] = "true"
        params["workingType"] = "MARK_PRICE"  # 用標記價觸發，避免瞬間插針假觸發
        if is_hedge_mode():
            params["positionSide"] = ACTION_TO_POSITION_SIDE[action]
        detail["order_type"] = params["type"]
        detail["stopPrice"] = trigger_price
        detail["closePosition"] = True
        detail["mark_price_at_check"] = mark_price
        detail["field_mapping"].update({
            "trigger_price": {"input_value": trigger_price, "binance_field": "stopPrice", "binance_value": trigger_price},
            "close_position": {"binance_field": "closePosition", "binance_value": "true"},
            "order_type": {"binance_field": "type", "binance_value": params["type"]},
            "working_type": {"binance_field": "workingType", "binance_value": "MARK_PRICE", "note": "用標記價觸發，避免瞬間插針假觸發"},
        })
        if is_hedge_mode():
            detail["field_mapping"]["position_side"] = {"binance_field": "positionSide", "binance_value": params["positionSide"], "note": "Hedge模式必填"}
    elif action in CLOSE_POSITION_ACTIONS:
        # 平多/平空：現在執行時 Binance MARKET 不支援 closePosition=true，必須查目前倉位後傳 quantity+reduceOnly。
        filters = get_symbol_filters(symbol)
        if execution_mode == "现在执行":
            position_qty = get_position_quantity(symbol, action)
            qty = round_step_size(position_qty, filters["stepSize"])
            if qty < filters["minQty"]:
                raise ValueError(f"{symbol} {action}：目前沒有可平的對應倉位，不能產生 MARKET 平倉單。")
            params["type"] = "MARKET"
            params["newOrderRespType"] = "RESULT"
            params["quantity"] = qty
            hedge = is_hedge_mode()
            if hedge:
                params["positionSide"] = ACTION_TO_POSITION_SIDE[action]
            else:
                params["reduceOnly"] = "true"  # Hedge模式不能用 reduceOnly，用 positionSide 代替
            detail["quantity"] = qty
            detail["reduceOnly"] = not hedge
            detail["field_mapping"].update({
                "current_position_quantity": {"binance_field": "quantity", "binance_value": qty},
                "order_type": {"binance_field": "type", "binance_value": "MARKET"},
            })
            if hedge:
                detail["field_mapping"]["position_side"] = {"binance_field": "positionSide", "binance_value": params["positionSide"], "note": "Hedge模式必填"}
            else:
                detail["field_mapping"]["reduce_only"] = {"binance_field": "reduceOnly", "binance_value": "true"}
        else:
            if trigger_price is None:
                raise ValueError(f"{symbol} {action}：到價平倉必須有 trigger_price，否則無法填 Binance stopPrice。")
            mark_price = get_mark_price(symbol)
            if trigger_price == mark_price:
                raise ValueError(f"{symbol} {action}：trigger_price={trigger_price} 等於標記價={mark_price}，條件單可能立刻觸發，請人工確認。")
            if (action == "平多" and trigger_price > mark_price) or (action == "平空" and trigger_price < mark_price):
                params["type"] = "TAKE_PROFIT_MARKET"
            else:
                params["type"] = "STOP_MARKET"
            params["stopPrice"] = trigger_price
            params["closePosition"] = "true"
            params["workingType"] = "MARK_PRICE"
            if is_hedge_mode():
                params["positionSide"] = ACTION_TO_POSITION_SIDE[action]
            detail["stopPrice"] = trigger_price
            detail["closePosition"] = True
            detail["field_mapping"].update({
                "trigger_price": {"input_value": trigger_price, "binance_field": "stopPrice", "binance_value": trigger_price},
                "close_position": {"binance_field": "closePosition", "binance_value": "true"},
                "order_type": {"binance_field": "type", "binance_value": params["type"]},
                "working_type": {"binance_field": "workingType", "binance_value": "MARK_PRICE"},
            })
            if is_hedge_mode():
                detail["field_mapping"]["position_side"] = {"binance_field": "positionSide", "binance_value": params["positionSide"], "note": "Hedge模式必填"}
        detail["order_type"] = params["type"]
    elif action in PARTIAL_REDUCE_ACTIONS:
        # 减多/减空：部分減倉。quantity = 目前持倉 × 0.5（賣出/買回一半，剩下一半倉位還在），
        # 依使用者指示：固定「減半」規則，不採用 LLM 給的 position_size_pct。
        filters = get_symbol_filters(symbol)
        current_qty = get_position_quantity(symbol, action)
        if current_qty <= 0:
            raise ValueError(f"{symbol} {action}：目前沒有對應倉位可減倉，無法計算減倉數量。")
        raw_qty = current_qty * 0.5
        qty = round_step_size(raw_qty, filters["stepSize"])
        if qty < filters["minQty"]:
            raise ValueError(
                f"{symbol} {action}：減半後數量 {qty} 小於交易所最小下單量 {filters['minQty']}，"
                f"請人工確認（目前持倉={current_qty}）。"
            )
        order_type_params, order_type_detail = _resolve_conditional_order(
            symbol, side, execution_mode, trigger_price
        )
        params.update(order_type_params)
        params["quantity"] = qty
        hedge = is_hedge_mode()
        if hedge:
            params["positionSide"] = ACTION_TO_POSITION_SIDE[action]
        else:
            params["reduceOnly"] = "true"  # Hedge模式不能用 reduceOnly，用 positionSide 代替
        detail.update(order_type_detail)
        detail.update({
            "order_type": params["type"],
            "current_position_quantity": current_qty,
            "reduce_ratio": 0.5,
            "raw_quantity": raw_qty,
            "rounded_quantity": qty,
            "step_size": filters["stepSize"],
            "reduceOnly": not hedge,
            "llm_position_size_pct_ignored": order.get("position_size_pct"),
        })
        detail["field_mapping"].update({
            "current_position_quantity": {"binance_field": "quantity", "binance_value": qty, "note": "目前持倉的一半"},
            "order_type": {"binance_field": "type", "binance_value": params["type"]},
        })
        if hedge:
            detail["field_mapping"]["position_side"] = {"binance_field": "positionSide", "binance_value": params["positionSide"], "note": "Hedge模式必填"}
        else:
            detail["field_mapping"]["reduce_only"] = {"binance_field": "reduceOnly", "binance_value": "true"}
    elif action in ADD_ACTIONS:
        # 加多/加空：已有倉位再加碼。quantity = 目前持倉 × 1（買入/賣出與目前持倉相同的數量，
        # 讓總倉位變成目前的兩倍），依使用者指示：固定「加倍」規則，不按可用餘額重新計算。
        filters = get_symbol_filters(symbol)
        current_qty = get_position_quantity(symbol, action)
        if current_qty <= 0:
            raise ValueError(f"{symbol} {action}：目前沒有對應倉位可加倉，無法計算加倉數量。")
        raw_qty = current_qty  # 加碼與目前持倉等量 -> 總倉位變兩倍
        qty = round_step_size(raw_qty, filters["stepSize"])
        if qty < filters["minQty"]:
            raise ValueError(
                f"{symbol} {action}：加倉數量 {qty} 小於交易所最小下單量 {filters['minQty']}，"
                f"請人工確認（目前持倉={current_qty}）。"
            )
        order_type_params, order_type_detail = _resolve_conditional_order(
            symbol, side, execution_mode, trigger_price
        )
        params.update(order_type_params)
        params["quantity"] = qty
        if is_hedge_mode():
            params["positionSide"] = ACTION_TO_POSITION_SIDE[action]
        detail.update(order_type_detail)
        detail.update({
            "order_type": params["type"],
            "current_position_quantity": current_qty,
            "add_ratio": 1.0,
            "raw_quantity": raw_qty,
            "rounded_quantity": qty,
            "step_size": filters["stepSize"],
            "llm_position_size_pct_ignored": order.get("position_size_pct"),
            "leverage_will_be_set_before_order": True,
        })
        detail["field_mapping"].update({
            "current_position_quantity": {"binance_field": "quantity", "binance_value": qty, "note": "與目前持倉等量，加碼後總倉位變兩倍"},
            "order_type": {"binance_field": "type", "binance_value": params["type"]},
        })
        if is_hedge_mode():
            detail["field_mapping"]["position_side"] = {"binance_field": "positionSide", "binance_value": params["positionSide"], "note": "Hedge模式必填"}
    elif action in OPEN_ACTIONS:
        # 开多/开空：全新倉位，需要計算實際 quantity。
        # 固定寫死規則（忽略 LLM 產出的 position_size_pct）：
        #   保證金 = 可用餘額 ÷ symbol_count（同一則訊息的標的數均分，等於100%/N），
        #   槓桿 = DEFAULT_LEVERAGE 倍，notional = 保證金 × 槓桿，quantity = notional ÷ 現價。
        filters = get_symbol_filters(symbol)
        balance = get_available_balance_usdt()
        margin = balance / symbol_count
        notional = margin * DEFAULT_LEVERAGE

        # 先決定 order type/price 欄位，再決定「用哪個價格算數量」：
        #   现在执行 → MARKET，用即時現價算數量；
        #   到价触发 → LIMIT/STOP_MARKET，改用 trigger_price（實際成交價）算數量，
        #             而非即時現價，否則同一則訊息中不同觸發價的分批單會算出相同數量。
        mark_price = None
        if execution_mode == "现在执行":
            params["type"] = "MARKET"
            params["newOrderRespType"] = "RESULT"
            price_for_qty = get_current_price(symbol)
        else:
            if trigger_price is None:
                raise ValueError(f"{symbol} {action}：到價觸發必須有 trigger_price，否則無法決定 price/stopPrice。")
            mark_price = get_mark_price(symbol)
            price_for_qty = trigger_price
            if (side == "BUY" and trigger_price < mark_price) or (side == "SELL" and trigger_price > mark_price):
                params["type"] = "LIMIT"
                params["price"] = trigger_price
                params["timeInForce"] = "GTC"
                detail["price"] = trigger_price
                detail["order_type_reason"] = "觸發價比標記價更有利，使用 LIMIT 掛單。"
            else:
                params["type"] = "STOP_MARKET"
                params["stopPrice"] = trigger_price
                params["workingType"] = "MARK_PRICE"
                detail["stopPrice"] = trigger_price
                detail["order_type_reason"] = "觸發價比標記價更不利，使用 STOP_MARKET 等突破/跌破後市價進場。"

        raw_qty = notional / price_for_qty
        qty = round_step_size(raw_qty, filters["stepSize"])
        if qty < filters["minQty"]:
            raise ValueError(
                f"{symbol}：試算數量 {qty} 小於交易所最小下單量 {filters['minQty']}，"
                f"請確認帳戶餘額（現有可用 USDT={balance}）是否足夠。"
            )
        params["quantity"] = qty
        if is_hedge_mode():
            params["positionSide"] = ACTION_TO_POSITION_SIDE[action]
        detail.update({
            "order_type": params["type"],
            "available_balance_usdt": balance,
            "price_used_for_quantity": price_for_qty,
            "mark_price": mark_price,
            "symbol_count_in_message": symbol_count,
            "margin_pct_used": round(100.0 / symbol_count, 4),
            "leverage_used": DEFAULT_LEVERAGE,
            "margin_usdt": margin,
            "notional_usdt": notional,
            "llm_position_size_pct_ignored": order.get("position_size_pct"),
            "raw_quantity": raw_qty,
            "rounded_quantity": qty,
            "step_size": filters["stepSize"],
            "leverage_will_be_set_before_order": True,
        })
        detail["field_mapping"].update({
            "execution_mode": {"input_value": execution_mode, "binance_field": "type", "binance_value": params["type"]},
            "calculated_quantity": {"binance_field": "quantity", "binance_value": qty},
        })
        if "price" in params:
            detail["field_mapping"]["trigger_price"] = {"input_value": trigger_price, "binance_field": "price", "binance_value": trigger_price}
        if "stopPrice" in params:
            detail["field_mapping"]["trigger_price"] = {"input_value": trigger_price, "binance_field": "stopPrice", "binance_value": trigger_price}
            detail["field_mapping"]["working_type"] = {"binance_field": "workingType", "binance_value": "MARK_PRICE"}
        if is_hedge_mode():
            detail["field_mapping"]["position_side"] = {"binance_field": "positionSide", "binance_value": params["positionSide"], "note": "Hedge模式必填"}
    else:
        raise ValueError(f"{symbol}：未知的 position_action={action!r}，無法計算下單參數。")

    detail["binance_params"] = params
    return detail


def place_order(params: dict) -> dict:
    """實際送出下單請求（POST /fapi/v1/order），回傳交易所回應。"""
    return _signed_request("POST", "/fapi/v1/order", params)


# ---------------- 執行狀態追蹤（逐筆確認，避免重複下單） ----------------

def _load_state() -> dict:
    """回傳 {(source_index, order_idx): {"status":..., ...}}"""
    state = {}
    if not EXECUTION_STATE_JSONL.exists():
        return state
    for line in EXECUTION_STATE_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        state[(rec["source_index"], rec["order_idx"])] = rec
    return state


def _append_state(rec: dict):
    with EXECUTION_STATE_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _load_records():
    if not TRADABLE_INPUT_JSONL.exists():
        return []
    return [json.loads(l) for l in TRADABLE_INPUT_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]


def _symbol_count_needing_quantity(orders: list) -> int:
    """回傳同一則訊息裡要按可用餘額均分的標的數（只包含「开多/开空」全新倉位；
    加多/加空 改用目前持倉計算，不佔用這份餘額）。"""
    count = sum(1 for o in orders if o["position_action"] in OPEN_ACTIONS)
    return max(count, 1)  # 避免除以0


def cmd_list():
    records = _load_records()
    state = _load_state()
    any_order = False
    for rec in records:
        orders = rec.get("orders") or []
        if not orders:
            print(f"\n[source_index {rec['source_index']}] {rec['source_text']}")
            print(f"  狀態=已跳過（orders 為空，無明確可執行下單內容）")
            continue
        any_order = True
        n = _symbol_count_needing_quantity(orders)
        print(f"\n[source_index {rec['source_index']}] {rec['source_text']}")
        for idx, o in enumerate(orders):
            key = (rec["source_index"], idx)
            status = state.get(key, {}).get("status", "待處理")
            if o["position_action"] in ENTRY_ACTIONS:
                margin_note = f"保證金=可用餘額/{n}×槓桿{DEFAULT_LEVERAGE}x"
            elif o["position_action"] in PARTIAL_REDUCE_ACTIONS:
                margin_note = "部分減倉需基於現有倉位比例，未自動換算"
            else:
                margin_note = "無需計算數量"
            print(f"  order_idx={idx}  狀態={status}  {o['symbol']} {o['position_action']} "
                  f"({o['execution_mode']}, 價位={o.get('trigger_price')}, {margin_note}，忽略LLM給的{o.get('position_size_pct')}%)")
    if not any_order:
        print("\n目前沒有任何 orders 非空的紀錄可處理（以上皆已跳過）。")


def cmd_show(source_index: int, order_idx: int):
    records = _load_records()
    rec = next((r for r in records if r["source_index"] == source_index), None)
    if rec is None:
        print(f"找不到 source_index={source_index}")
        return
    orders = rec.get("orders") or []
    if order_idx >= len(orders):
        print(f"source_index={source_index} 沒有 order_idx={order_idx}")
        return
    order = orders[order_idx]
    n = _symbol_count_needing_quantity(orders)
    print(f"原始訊息：{rec['source_text']}")
    print(f"訂單內容：{json.dumps(order, ensure_ascii=False, indent=2)}")
    try:
        detail = compute_binance_params(order, symbol_count=n)
        print("\n【試算下單參數】")
        print(json.dumps(detail, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"\n[警告] 試算失敗：{e}")


def cmd_execute(source_index: int, order_idx: int):
    state = _load_state()
    key = (source_index, order_idx)
    if key in state and state[key]["status"] in ("已下單", "已略過"):
        print(f"source_index={source_index} order_idx={order_idx} 先前已處理過（狀態={state[key]['status']}），不重複執行。")
        return
    records = _load_records()
    rec = next((r for r in records if r["source_index"] == source_index), None)
    if rec is None:
        print(f"找不到 source_index={source_index}")
        return
    orders = rec.get("orders") or []
    if order_idx >= len(orders):
        print(f"source_index={source_index} 沒有 order_idx={order_idx}")
        return
    order = orders[order_idx]
    n = _symbol_count_needing_quantity(orders)
    try:
        detail = compute_binance_params(order, symbol_count=n)
        if detail.get("leverage_will_be_set_before_order"):
            lev_resp = set_leverage(detail["symbol"], DEFAULT_LEVERAGE)
            print(f"已設定槓桿：{detail['symbol']} -> {DEFAULT_LEVERAGE}x，交易所回應：{json.dumps(lev_resp, ensure_ascii=False)}")
        print("送出下單請求，參數：")
        print(json.dumps(detail["binance_params"], ensure_ascii=False, indent=2))
        resp = place_order(detail["binance_params"])
        print("交易所回應：")
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        _append_state({
            "source_index": source_index, "order_idx": order_idx,
            "status": "已下單", "params": detail["binance_params"], "response": resp,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception as e:
        print(f"[失敗] {e}")
        _append_state({
            "source_index": source_index, "order_idx": order_idx,
            "status": "下單失敗", "error": str(e),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })


def cmd_skip(source_index: int, order_idx: int):
    _append_state({
        "source_index": source_index, "order_idx": order_idx,
        "status": "已略過", "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    print(f"已標記 source_index={source_index} order_idx={order_idx} 為略過。")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "--list":
        cmd_list()
    elif cmd == "--show":
        cmd_show(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "--execute":
        cmd_execute(int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "--skip":
        cmd_skip(int(sys.argv[2]), int(sys.argv[3]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
