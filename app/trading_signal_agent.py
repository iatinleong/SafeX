# -*- coding: utf-8 -*-
"""
Stage 3：可交易訊號 Agent（獨立於 Stage 1/2，使用不同模型 gemini-3.5-flash）。

職責：讀取「阿佛禁言群B_結構化.jsonl」（Stage 2 已篩選為「具體可交易」的 trading_signal，
只含 index/time/speaker/text/category，不含任何欄位抽取結果），對每一則尚未處理過的訊息，
完全獨立地重新從原始文字判斷是否存在可執行的具體交易指令，並轉換成嚴格 enum 限定、
可直接餵給下單邏輯的結構化訂單清單，寫入「阿佛禁言群B_可交易訊號.jsonl」。

之所以獨立成第三個 agent、用不同模型：
- Stage 2（gemini-3.1-flash-lite）只負責「這是不是交易訊號」+「內容是否具體」兩個粗略判斷，
  完全不做欄位抽取，避免同一組欄位被兩個 agent 各自解讀出不一致的結果。
- Stage 3 的職責是「把自然語言交易訊號，從頭開始轉換成嚴格 schema 的可執行訂單」，
  且需要對照「目前帳戶實際持倉」與「目前市場現價」才能判斷是否該下單/該填什麼觸發價，
  這需要額外呼叫 Binance Testnet API，是完全不同的任務，值得用獨立模型與獨立 prompt 維護。

【本版修正重點（依使用者回饋 2026-07-05）】
1. 帳戶感知：每次執行前，先查詢 Binance Testnet 目前的「持倉」與「現價」，並把這些資訊
   放進每一則訊息的 prompt 中，讓模型可以判斷「這個止盈/加倉指令，我現在到底有沒有對應的倉位」，
   沒有對應倉位的指令（例如根本沒有多單卻叫你「止盈多」）一律不產生訂單。
2. position_action 欄位取代舊的 position_side + trade_action 兩個欄位：
   舊設計「position_side=做多, trade_action=止盈」語意混淆（止盈多單其實是「賣出」動作，
   容易讓人誤解成也是做多方向的操作），改用單一欄位直接講清楚「方向+動作」：
     - 开多／开空：目前沒有對應倉位，建立一個全新倉位（不需要先持有倉位）。
     - 加多／加空：已經持有同方向倉位，再加碼擴大（需要先持有對應倉位，否則不產生訂單）。
     - 减多／减空：已經持有同方向倉位，賣出/買回「部分」倉位縮小部位，倉位還在，
       尚未完全出場（需要先持有對應倉位）。
     - 平多／平空：把「全部」對應倉位平倉出場，沒有特別強調「因為獲利/因為停損」這種原因，
       單純因為時間到了/看法改變而全部出場（需要先持有對應倉位）。
     - 止盈多／止盈空：因為「已經獲利」而設定一個未來價位，價格到了就自動全部/部分平倉獲利了結，
       這是「到價觸發」的條件單，必須有明確的未來價位（需要先持有對應倉位）。
     - 止损多／止损空：設定一個未來價位，價格到了就自動平倉停損出場，同樣是「到價觸發」的條件單，
       必須有明確的未來價位（需要先持有對應倉位）。
3. hedge 用詞更嚴格排除：「至少要...以上」「起码要」這類帶保留/模糊語氣的條件句，
   視為不夠明確的訊號，不產生訂單（orders 回傳空陣列），不再視為條件觸發單。
4. 同一則訊息可能包含多個獨立的操作暗示（例如同時提到「這幾天下跌是加倉機會」與「目標67500」），
   要求模型針對每一個獨立可判斷的操作各自輸出，不要因為其中一部分模糊就連明確的部分也一起放棄。
5. position_size_pct：新增欄位，若原文有提到倉位大小（例如「总仓位1%」），填入數字 1，
   沒有提到則為 null。若同一句「总仓位」描述涵蓋多個標的（例如「大饼，eth，sol入多，总仓位1%」），
   代表這是三個標的合計共佔1%，而非每個標的各自1%，須按標的數量平均分配
   （2026-07-06 修正：先前版本會讓每個標的都填入完整的1%，導致實際下單總倉位變成3倍）。
6. trigger_price：
   - "到价触发" 的訂單，價位一律以原文明確提到的數字為準。
   - "现在执行" 的訂單，直接使用本程式查詢到的目前 testnet 現價（由 Python 事後覆寫，
     不完全依賴模型自行填寫，確保是真實查到的數字而不是模型憑空生成）。
7. size_grade（2026-07-07 新增）：倉位程度分級 全仓/半仓/轻仓，只看語氣詞
   （「一点点/轻仓/试探」→轻仓、「一半仓位」→半仓、沒描述→全仓），
   下游 order_executor 依 SIZE_GRADE_FACTORS（1.0/0.5/0.25）縮放開倉保證金與加倉數量。
   position_size_pct 仍只是記錄用，不參與下單量計算。
8. 陳舊訊號防線（2026-07-07 新增）：到價觸發的開倉/加倉進場價離目前現價達
   STALE_PRICE_LIMITS 門檻（BTC 1000 點，ETH/SOL 按比例）時，整則訊息的訂單清空，
   避免把回溯擷取的舊喊單/行情早已走遠的價位當成新單掛出去。

用法：
    python trading_signal_agent.py
"""
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

STRUCTURED_INPUT_JSONL = Path(r"C:\Users\user\Desktop\SafeW\阿佛禁言群B_結構化.jsonl")
TRADABLE_OUTPUT_JSONL = Path(r"C:\Users\user\Desktop\SafeW\阿佛禁言群B_可交易訊號.jsonl")
MODEL_ID = "gemini-3.5-flash"
API_KEY_ENV_VAR = "GEMINI_API_KEY"

BINANCE_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
BINANCE_KEY_ENV_VAR = "BINANCE_TESTNET_API_KEY"
BINANCE_SECRET_ENV_VAR = "BINANCE_TESTNET_API_SECRET"

SUPPORTED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
POSITION_ACTIONS = [
    "开多", "开空",
    "加多", "加空",
    "减多", "减空",
    "平多", "平空",
    "止盈多", "止损多",
    "止盈空", "止损空",
]
# 需要「先持有對應倉位」才可能成立的動作（開多/開空是建立新倉位，不需要先持有）。
ACTIONS_REQUIRE_EXISTING_LONG = {"加多", "减多", "平多", "止盈多", "止损多"}
ACTIONS_REQUIRE_EXISTING_SHORT = {"加空", "减空", "平空", "止盈空", "止损空"}
# 一定是「到價觸發」條件單的動作（止盈/止損本質上就是設定未來價位）。
ACTIONS_MUST_BE_TRIGGER = {"止盈多", "止损多", "止盈空", "止损空"}
EXECUTION_MODES = ["现在执行", "到价触发"]
# 倉位程度分級（2026-07-07 依使用者規則新增）：只看語氣詞（一点点/轻仓/试探→轻仓、
# 一半仓位→半仓），明確的百分比數字一律不影響分級——「总仓位1%」也是全仓
# （阿佛帳戶規模與使用者不同，他的百分比對使用者沒有意義，只記錄在 position_size_pct）。
# 對應的下單量係數在 order_executor.SIZE_GRADE_FACTORS（全仓=1.0、半仓=0.5、轻仓=0.25）。
SIZE_GRADES = ["全仓", "半仓", "轻仓"]

# 陳舊訊號防線（2026-07-07 依使用者規則新增）：
# 「到价触发」的開倉/加倉訂單，訊息裡要代入下單的進場價位若離目前現價差達下列點數，
# 代表這句話是舊訊息（回溯擷取的積壓、或行情早已走遠的過時喊單），
# 整則訊息的 orders 一律清空、不產生任何訂單（含同訊息裡其他本來合格的訂單）。
# 只檢查開倉/加倉的進場價：止盈/止損/平倉/減倉的價位本來就應該離現價很遠，不適用。
# 「现在执行」的訂單價位一律由程式代入即時現價，永遠不會觸發此防線。
# 門檻以「點數」計：BTC=1000 是使用者設定的基準，ETH/SOL 按現價比例換算（約1.7%），
# 行情大幅變動後應回頭調整這組數字。達到門檻（>=）即視為過時。
STALE_PRICE_LIMITS = {"BTCUSDT": 1000.0, "ETHUSDT": 30.0, "SOLUSDT": 1.3}
STALE_CHECK_ACTIONS = {"开多", "开空", "加多", "加空"}

POSITION_ACTION_DEFINITIONS = (
    "- 开多：目前沒有多單倉位，建立一筆全新的多單（買入開倉），不需要先持有倉位。\n"
    "- 开空：目前沒有空單倉位，建立一筆全新的空單（賣出開倉），不需要先持有倉位。\n"
    "- 加多：已經持有多單，再加碼買入擴大多單部位——必須先持有多單才成立。\n"
    "- 加空：已經持有空單，再加碼賣出擴大空單部位——必須先持有空單才成立。\n"
    "- 减多：已經持有多單，賣出「部分」多單縮小部位，倉位還在、尚未全部出場——必須先持有多單才成立。\n"
    "- 减空：已經持有空單，買回「部分」空單縮小部位，倉位還在、尚未全部出場——必須先持有空單才成立。\n"
    "- 平多：把「全部」多單平倉出場，單純因為時間到/看法改變而出場，"
    "不是因為「已經獲利」或「停損」的理由——必須先持有多單才成立。\n"
    "- 平空：把「全部」空單平倉出場，同上，不強調獲利/停損理由——必須先持有空單才成立。\n"
    "- 止盈多：因為「已經獲利」而設定一個未來價位，價格漲到該價位時自動平掉多單獲利了結，"
    "屬於「到價觸發」的條件單，一定要有明確的未來價位——必須先持有多單才成立。\n"
    "- 止损多：設定一個未來價位，價格跌到該價位時自動平掉多單停損出場，"
    "屬於「到價觸發」的條件單，一定要有明確的未來價位——必須先持有多單才成立。\n"
    "- 止盈空：因為「已經獲利」而設定一個未來價位，價格跌到該價位時自動平掉空單獲利了結，"
    "屬於「到價觸發」的條件單，一定要有明確的未來價位——必須先持有空單才成立。\n"
    "- 止损空：設定一個未來價位，價格漲到該價位時自動平掉空單停損出場，"
    "屬於「到價觸發」的條件單，一定要有明確的未來價位——必須先持有空單才成立。\n"
)

PROMPT_TEMPLATE = (
    "以下是一則已被判定為「交易訊號」的中文訊息：\n\n"
    "原始文字：「{text}」\n\n"
    "【目前帳戶實際狀況（testnet，查詢時間 {snapshot_time}）】\n"
    "{account_context}\n\n"
    "請你判斷這則文字裡，是否存在『真正可以直接下單執行』的具體交易指令。\n\n"
    "【position_action 欄位定義（方向+動作合併，避免「做多+止盈」這種語意混淆）】\n"
    f"{POSITION_ACTION_DEFINITIONS}\n"
    "【重要判斷原則】\n"
    "1. 如果文字本質是「觀望/等待/暫不進場」而沒有給出之後要在什麼價位進場的具體條件，"
    "這不是可執行指令，該部分不要輸出。\n"
    "2. 帶有「至少要...以上」「起码要」「差不多」這類保留/模糊語氣的條件句，"
    "視為不夠明確的訊號，不要輸出訂單，即使它有提到價位也一樣——這種語氣代表發言者自己都不確定，"
    "不應該被視為可以直接執行的明確指令。\n"
    "3. 只有下列三種幣種算是可交易範圍：BTCUSDT、ETHUSDT、SOLUSDT"
    "（文字中可能寫成 BTC/大饼、ETH/以太、SOL），其餘幣種或無法判斷幣種的內容，"
    "該筆不要輸出（略過，不要用其他字串硬塞進 symbol）。\n"
    "4. 若同一則文字對不同幣種給了不同的價位或方向（例如「BTC64000，ETH1840以上」），"
    "請拆成多筆各自獨立的訂單，每筆的 trigger_price 只對應該筆的 symbol，不要混用。\n"
    "5. 同一則文字裡可能同時包含好幾個獨立的操作暗示（例如「這幾天下跌就是加倉機會」是一個暗示、"
    "「目标67500」又是另一個止盈的暗示），請針對每一個能獨立判斷清楚的操作各自輸出一筆訂單，"
    "不要因為訊息裡有一部分模糊的敘述文字，就連另一部分已經明確的操作也一起放棄不輸出。\n"
    "6. position_action 必須先對照上面提供的「目前帳戶實際狀況」：\n"
    "   - 開多/開空不需要先持有倉位。\n"
    "   - 加多/減多/平多/止盈多/止损多，必須目前帳戶「已經持有多單」才成立，"
    "如果帳戶目前沒有多單，即使文字有提到，也不要輸出這筆訂單。\n"
    "   - 加空/減空/平空/止盈空/止损空，必須目前帳戶「已經持有空單」才成立，"
    "如果帳戶目前沒有空單，即使文字有提到，也不要輸出這筆訂單。\n"
    "7. 止盈多/止损多/止盈空/止损空 一定是「到價觸發」（execution_mode=到价触发），且一定要有"
    "明確的未來價位，沒有明確價位就不要用這幾個分類（改用平多/平空，或該部分不輸出）。\n"
    "8. execution_mode：如果文字要求「現在」「现价」「立刻」執行，填「现在执行」；"
    "如果是「漲到/跌到/回踩/掛在某價位」才觸發，填「到价触发」。\n"
    "9. trigger_price 請填實際數字（例如 64000）；「到价触发」一定要有值；"
    "「现在执行」如果原文沒有明確價位，可以先填 null（程式會事後用目前現價覆寫）。\n"
    "10. position_size_pct：如果原文有提到倉位大小（例如「总仓位1%」「一半仓位」），"
    "請填入對應的百分比數字（例如 1、50），沒有提到則填 null。"
    "特別注意「总仓位」「总共」這類字眼代表的是「這整句話提到的所有標的合計的倉位大小」，"
    "不是「每一個標的各自」的倉位大小——例如「大饼，eth，sol入多，总仓位1%」是指三個標的"
    "加起來共佔1%倉位，此時每一筆訂單的 position_size_pct 應該填入 1 除以本則訊息中"
    "同一句「总仓位」描述所涵蓋的標的數量（此例為 1/3，四捨五入到小數點後兩位即 0.33），"
    "不要每個標的都各自填滿整個「总仓位」的數字，那樣會讓實際下單的總倉位變成好幾倍，"
    "超出使用者原本講的風險上限。\n"
    "11. size_grade（倉位程度分級，決定實際下單量的大小，只影響開倉/加倉類動作）——"
    "只看「語氣詞」，明確的百分比數字一律不影響分級（那些只記錄在 position_size_pct）：\n"
    "   - 轻仓：原文用「一点点」「一点儿」「轻仓」「小仓位」「试探」這類字眼描述很小的倉位。\n"
    "   - 半仓：原文說「一半仓位」「半仓」。\n"
    "   - 全仓：其餘一律填全仓——包括完全沒描述倉位大小（「现价买入」「直接开多」）、"
    "只給了明確百分比數字（例如「总仓位1%」也是全仓，不因數字小就當轻仓）、"
    "或描述大倉位（「重仓」「梭哈」也填全仓，不會放大超過全仓）。\n"
    "12. explanation：請用一句白話中文解釋你為什麼這樣判斷（包含為何符合/不符合帳戶持倉條件），"
    "讓人工可以快速核對這筆判斷是否合理。\n\n"
    "如果整則文字完全沒有任何符合以上條件的可執行內容，orders 請回傳空陣列 []。"
)


# ---------------- Binance Testnet：帳戶持倉 + 現價查詢 ----------------

def _binance_signed_get(path: str, params: dict) -> dict:
    """對 Binance USDT-M Futures Testnet 發送簽名 GET 請求，回傳解析後的 JSON。"""
    from app.env_config import get_secret
    api_key = get_secret(BINANCE_KEY_ENV_VAR)
    api_secret = get_secret(BINANCE_SECRET_ENV_VAR)
    if not api_key or not api_secret:
        raise RuntimeError(f"找不到環境變數/Streamlit secrets {BINANCE_KEY_ENV_VAR}/{BINANCE_SECRET_ENV_VAR}，請先設定 Binance Testnet 金鑰。")
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    query = urllib.parse.urlencode(params)
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{BINANCE_TESTNET_BASE_URL}{path}?{query}&signature={signature}"
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": api_key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _binance_public_get(path: str, params: dict) -> dict:
    """呼叫 Binance 公開行情端點（不需要簽名/金鑰）。"""
    query = urllib.parse.urlencode(params)
    url = f"{BINANCE_TESTNET_BASE_URL}{path}?{query}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_positions() -> dict:
    """
    查詢 testnet 目前的持倉狀況，回傳 {symbol: {"side": "多"/"空"/None, "amt": float}}。
    positionAmt > 0 視為多單、< 0 視為空單、== 0 視為無持倉。
    查詢失敗時回傳「全部無持倉」，並印出警告（保守處理：查不到就當作沒有持倉，
    這樣加倉/止盈類指令會被正確排除，不會因為查詢失敗而誤下單）。
    """
    result = {sym: {"side": None, "amt": 0.0} for sym in SUPPORTED_SYMBOLS}
    try:
        data = _binance_signed_get("/fapi/v2/positionRisk", {})
        for item in data:
            symbol = item.get("symbol")
            if symbol not in result:
                continue
            amt = float(item.get("positionAmt", 0) or 0)
            if amt > 0:
                result[symbol] = {"side": "多", "amt": amt}
            elif amt < 0:
                result[symbol] = {"side": "空", "amt": amt}
            else:
                result[symbol] = {"side": None, "amt": 0.0}
    except Exception as e:
        print(f"[警告] 查詢 Binance Testnet 持倉失敗，視為目前無任何持倉：{e}")
    return result


def fetch_prices() -> dict:
    """查詢 testnet 目前每個 symbol 的最新成交價，回傳 {symbol: float}，查詢失敗則該 symbol 為 None。"""
    result = {sym: None for sym in SUPPORTED_SYMBOLS}
    for sym in SUPPORTED_SYMBOLS:
        try:
            data = _binance_public_get("/fapi/v1/ticker/price", {"symbol": sym})
            result[sym] = float(data["price"])
        except Exception as e:
            print(f"[警告] 查詢 {sym} 現價失敗：{e}")
    return result


def build_account_context_text(positions: dict, prices: dict) -> str:
    lines = []
    for sym in SUPPORTED_SYMBOLS:
        pos = positions.get(sym) or {"side": None, "amt": 0.0}
        price = prices.get(sym)
        price_text = f"現價≈{price}" if price is not None else "現價查詢失敗"
        if pos["side"] is None:
            pos_text = "目前無持倉"
        else:
            pos_text = f"目前持有{pos['side']}單（數量 {abs(pos['amt'])}）"
        lines.append(f"{sym}：{pos_text}；{price_text}")
    return "\n".join(lines)


# ---------------- Gemini Stage 3：文字 -> 可交易訂單 ----------------

def _build_schema():
    from google.genai import types as t
    order_schema = t.Schema(
        type="OBJECT",
        properties={
            "symbol": t.Schema(type="STRING", enum=SUPPORTED_SYMBOLS, description="交易對，僅限 BTCUSDT/ETHUSDT/SOLUSDT"),
            "position_action": t.Schema(type="STRING", enum=POSITION_ACTIONS, description="方向+動作合併欄位，詳見定義"),
            "execution_mode": t.Schema(
                type="STRING", enum=EXECUTION_MODES,
                description="现在执行＝用市價單立刻下單；到价触发＝掛條件單，等價格到 trigger_price 才成交",
            ),
            "trigger_price": t.Schema(type="NUMBER", nullable=True, description="觸發/進場價位，到价触发時必填"),
            "position_size_pct": t.Schema(type="NUMBER", nullable=True, description="倉位大小百分比，原文沒提到則為 null"),
            "size_grade": t.Schema(
                type="STRING", enum=SIZE_GRADES,
                description="倉位程度分級（只看語氣詞，明確百分比不影響）：一点点/轻仓/试探=轻仓；一半/半仓=半仓；其餘含明確百分比=全仓",
            ),
            "explanation": t.Schema(type="STRING", description="一句白話中文解釋這筆判斷依據，含帳戶持倉條件判斷"),
        },
        required=["symbol", "position_action", "execution_mode", "size_grade", "explanation"],
    )
    return t.Schema(
        type="OBJECT",
        properties={"orders": t.Schema(type="ARRAY", items=order_schema)},
        required=["orders"],
    )


def _get_client():
    from google import genai
    from app.env_config import get_secret
    api_key = get_secret(API_KEY_ENV_VAR)
    if not api_key:
        raise RuntimeError(f"找不到環境變數/Streamlit secrets {API_KEY_ENV_VAR}，請先設定 Gemini API Key。")
    return genai.Client(api_key=api_key)


def _position_requirement_satisfied(action: str, positions: dict, symbol: str) -> bool:
    """防禦性二次檢查：不完全信任模型，Python 端也依照實際查到的持倉再驗證一次。"""
    pos = positions.get(symbol) or {"side": None, "amt": 0.0}
    if action in ACTIONS_REQUIRE_EXISTING_LONG:
        return pos["side"] == "多"
    if action in ACTIONS_REQUIRE_EXISTING_SHORT:
        return pos["side"] == "空"
    return True  # 开多/开空 不需要先持有倉位


def filter_valid_orders(orders: list, positions: dict, prices: dict) -> list:
    """
    防禦性二次過濾（純函式，不打任何 API，供離線測試 harness 直接驗證）：
    即使 schema 限制了 enum，仍再檢查一次必要欄位完整性，並用 Python 端實際查到的
    持倉再驗證一次「需要先持有倉位」的條件，避免模型誤判（沒有持倉卻輸出加倉/止盈類
    指令）流入下游下單邏輯。「现在执行」的訂單一律用查到的實際現價覆寫 trigger_price，
    不依賴模型自行填寫的數字。另含陳舊訊號防線：到價觸發的開倉/加倉進場價離現價
    達 STALE_PRICE_LIMITS 門檻時，整則訊息的訂單全部清空（見常數定義處說明）。
    """
    valid_orders = []
    for o in orders:
        if not isinstance(o, dict):
            continue
        symbol = o.get("symbol")
        action = o.get("position_action")
        execution_mode = o.get("execution_mode")
        if not (symbol in SUPPORTED_SYMBOLS
                and action in POSITION_ACTIONS
                and execution_mode in EXECUTION_MODES
                and o.get("explanation")):
            continue
        if not _position_requirement_satisfied(action, positions, symbol):
            print(f"    [略過] {symbol} {action}：目前帳戶無對應持倉，不產生此訂單。")
            continue
        if action in ACTIONS_MUST_BE_TRIGGER and (execution_mode != "到价触发" or o.get("trigger_price") is None):
            print(f"    [略過] {symbol} {action}：止盈/止損類指令必須是到價觸發且有明確價位。")
            continue
        if execution_mode == "现在执行":
            live_price = prices.get(symbol)
            if live_price is not None:
                o["trigger_price"] = live_price
        if o.get("size_grade") not in SIZE_GRADES:
            o["size_grade"] = "全仓"  # 模型漏填/舊記錄無此欄位時的預設，維持原本全倉行為
        valid_orders.append(o)

    # 陳舊訊號防線：任何一筆「到价触发」開倉/加倉的進場價離現價達 STALE_PRICE_LIMITS 門檻，
    # 就判定整則訊息過時，全部訂單清空（寫入 orders=[] 即視為已處理，不會重試——過時訊號
    # 之後再跑只會更過時，這正是要的行為）。現價查詢失敗（None）時無法判斷，不觸發此防線。
    for o in valid_orders:
        if o["position_action"] not in STALE_CHECK_ACTIONS or o["execution_mode"] != "到价触发":
            continue
        live = prices.get(o["symbol"])
        limit = STALE_PRICE_LIMITS.get(o["symbol"])
        tp = o.get("trigger_price")
        if live is not None and limit is not None and tp is not None and abs(tp - live) >= limit:
            print(f"    [整則略過] {o['symbol']} {o['position_action']} 進場價 {tp} 離現價 {live} "
                  f"已達 {limit} 點門檻，判定為過時訊號，整則訊息不產生任何訂單。")
            return []
    return valid_orders


def convert_to_tradable_orders(client, record: dict, positions: dict, prices: dict, snapshot_time: str):
    """
    對單一結構化訊號記錄呼叫 gemini-3.5-flash，回傳可交易訂單清單（可能為空）。
    回傳 None 代表「呼叫失敗」（非「判斷結果為無訂單」）——呼叫端不應寫入輸出檔，
    讓該筆在下次執行時自動重試；若失敗也寫入 orders=[]，這筆訊號會被
    load_already_processed_indexes 永久視為已處理，一次暫時性 API 失敗就永久漏單。
    """
    from google.genai import types as t
    from app.gemini_client import generate_content_with_retry

    account_context = build_account_context_text(positions, prices)
    prompt = PROMPT_TEMPLATE.format(text=record["text"], account_context=account_context, snapshot_time=snapshot_time)
    try:
        resp = generate_content_with_retry(
            client,
            model=MODEL_ID,
            contents=[prompt],
            config=t.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_build_schema(),
            ),
            stage_name="Stage3（可交易訊號轉換）",
        )
        raw = (resp.text or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        orders = parsed.get("orders", [])
        if not isinstance(orders, list):
            return []
        return filter_valid_orders(orders, positions, prices)
    except Exception as e:
        print(f"[警告] Stage3（可交易訊號轉換）重試後仍失敗，本則不寫入、下次執行重試：{e}")
        return None


def load_already_processed_indexes() -> set:
    """讀取既有輸出檔案，取得已處理過的 source_index 集合，確保重跑時不會重複處理。"""
    if not TRADABLE_OUTPUT_JSONL.exists():
        return set()
    processed = set()
    for line in TRADABLE_OUTPUT_JSONL.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            processed.add(json.loads(line)["source_index"])
        except (json.JSONDecodeError, KeyError):
            continue
    return processed


def main():
    if not STRUCTURED_INPUT_JSONL.exists():
        print(f"找不到輸入檔案：{STRUCTURED_INPUT_JSONL}")
        return

    records = []
    for lineno, line in enumerate(STRUCTURED_INPUT_JSONL.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            # 單一損壞行不應讓整個 Stage 3 崩潰（其餘正常記錄照常處理），但必須顯式警告，
            # 讓人工知道有記錄被跳過、可回頭修復該行後重跑補上。
            print(f"[警告] 結構化檔案第 {lineno} 行無法解析為 JSON，已略過：{e}")
            continue
        if not (isinstance(rec.get("index"), int) and rec.get("text")):
            print(f"[警告] 結構化檔案第 {lineno} 行缺少 index/text 欄位，已略過。")
            continue
        records.append(rec)
    processed_indexes = load_already_processed_indexes()
    new_records = [r for r in records if r["index"] not in processed_indexes]

    if not new_records:
        print("沒有新的結構化訊號需要轉換。")
        return

    print("查詢 Binance Testnet 目前持倉與現價...")
    positions = fetch_positions()
    prices = fetch_prices()
    snapshot_time = time.strftime("%Y-%m-%d %H:%M:%S")
    print(build_account_context_text(positions, prices))

    client = _get_client()
    print(f"\n共 {len(new_records)} 則新的結構化訊號待轉換（模型：{MODEL_ID}）...")

    failed_count = 0
    with TRADABLE_OUTPUT_JSONL.open("a", encoding="utf-8") as f:
        for record in new_records:
            orders = convert_to_tradable_orders(client, record, positions, prices, snapshot_time)
            if orders is None:
                # 呼叫失敗（非「無訂單」）：不寫入，保留為未處理狀態，下次執行自動重試。
                failed_count += 1
                continue
            out = {
                "source_index": record["index"],
                "source_text": record["text"],
                "orders": orders,
            }
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            if orders:
                print(f"[index {record['index']}] {record['text'][:40]} -> {len(orders)} 筆可交易訂單")
                for o in orders:
                    size_text = f"{o['position_size_pct']}%" if o.get("position_size_pct") is not None else "未指定"
                    print(f"    {o['symbol']} {o['position_action']} "
                          f"({o['execution_mode']}, 價位={o.get('trigger_price')}, "
                          f"分級={o.get('size_grade', '全仓')}, 原文倉位={size_text})")
                    print(f"      理由：{o['explanation']}")
            else:
                print(f"[index {record['index']}] {record['text'][:40]} -> 無可執行訂單（略過）")

    if failed_count:
        print(f"\n[注意] 有 {failed_count} 則因 API 失敗未轉換（未寫入），下次執行 stage3 會自動重試。")
    print(f"\n完成，結果已寫入 {TRADABLE_OUTPUT_JSONL}")


if __name__ == "__main__":
    main()
