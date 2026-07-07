# -*- coding: utf-8 -*-
"""
Stage 3 真實評測案例集：中文喊單文字 → 可交易訂單 的邊界案例。

每個 case：
  id         案例代號
  text       模擬阿佛的原始喊單文字
  positions  注入的持倉（{symbol: 數量}，正=多、負=空、缺=無持倉）——
             不查真實 testnet，讓期望結果可判定
  expect     約束式期望（不是逐字精確比對——模型輸出有合理自由度，
             但準確性紅線用約束卡死）：
      empty=True            必須完全沒有訂單
      count=N 或 (lo,hi)     訂單筆數
      symbols={...}          出現的 symbol 集合必須完全等於此集合（有訂單時才檢查）
      forbid_symbols={...}   絕不允許出現的 symbol
      actions={...}          所有訂單的 position_action 必須落在此集合內
      mode="现在执行"/"到价触发"  所有訂單的 execution_mode
      triggers={sym: 規格}    該 symbol 所有訂單的 trigger_price：
                              數字=精確等於；(lo,hi)=閉區間；"live"=等於注入現價
      pct_range=(lo,hi)      每筆 position_size_pct 落在區間內
      grades="轻仓" 或 {sym: 分級}   所有訂單（或指定 symbol 的訂單）的 size_grade
      stage4_all_ready=True  串進 Stage 4（mock 行情）後每筆都必須是 READY
  advisory   True = 語意本身有模糊空間的案例，失敗只記錄不算紅燈
             （用來觀察模型行為，不阻擋開發）

注入現價固定為 PRICES（BTC 60000 / ETH 1800 / SOL 80），
案例文字中的價位都是圍繞這組現價設計的，改現價前要同步檢查所有案例語意。
"""

PRICES = {"BTCUSDT": 60000.0, "ETHUSDT": 1800.0, "SOLUSDT": 80.0}

LONG_BTC = {"BTCUSDT": 0.5}
LONG_ALL = {"BTCUSDT": 0.5, "ETHUSDT": 2.0, "SOLUSDT": 50.0}
SHORT_ETH = {"ETHUSDT": -2.0}

CASES = [
    # ---------- 必須「不產生任何訂單」的案例（過度觸發 = 誤下單，最危險） ----------
    {"id": "vague_threshold", "positions": {},
     "text": "看了下盘面，目前还是没有涨完，先不要空单至少要BTC64000，ETH1840以上了",
     "expect": {"empty": True}},  # 「至少要...以上」保留語氣，prompt 規則 2 明令不下單
    {"id": "position_cap_only", "positions": {},
     "text": "仓位控制在2%以内",
     "expect": {"empty": True}},  # 只講風控，沒有操作
    {"id": "ma_commentary", "positions": {},
     "text": "1小时ma80均线，不破就没事，多头趋势继续",
     "expect": {"empty": True}},  # 盤面評論，無指令
    {"id": "chitchat", "positions": {},
     "text": "今天写了篇周报，晚点发给大家，记得看",
     "expect": {"empty": True}},
    {"id": "unsupported_symbol", "positions": {},
     "text": "DOGE现价直接开多，冲",
     "expect": {"empty": True}},  # 不在白名單的幣種，絕不可硬塞成 BTC/ETH/SOL
    {"id": "tp_without_position", "positions": {},
     "text": "BTC多单止盈挂64500",
     "expect": {"empty": True}},  # 沒有多單 → 止盈多不成立（Python 過濾保證，必過）
    {"id": "add_without_position", "positions": {},
     "text": "回踩58500就是加仓机会，大胆加多BTC",
     "expect": {"empty": True}},  # 沒有多單 → 加多不成立（Python 過濾保證，必過）
    {"id": "pure_hold", "positions": {},
     "text": "多单接着拿，不要瞎下车",
     "expect": {"empty": True}},  # 空泛喊話，無點位無動作

    # ---------- 開倉類 ----------
    {"id": "batch_range_long", "positions": {},
     "text": "这两天大家保持关注消息，插针我们就接多，BTC59200-60000分批接，合约和现货",
     "expect": {"count": (1, 3), "symbols": {"BTCUSDT"}, "actions": {"开多"},
                "mode": "到价触发", "triggers": {"BTCUSDT": (59200, 60000)},
                "stage4_all_ready": True}},
    {"id": "multi_symbol_split_pct", "positions": {},
     "text": "现价，大饼，eth，sol入多，总仓位1%",
     "expect": {"count": 3, "symbols": {"BTCUSDT", "ETHUSDT", "SOLUSDT"}, "actions": {"开多"},
                "mode": "现在执行",
                "triggers": {"BTCUSDT": "live", "ETHUSDT": "live", "SOLUSDT": "live"},
                "pct_range": (0.2, 0.5),  # 总仓位1% ÷ 3 標的 ≈ 0.33，不可每檔各 1%
                "grades": "全仓",  # 明確百分比不影響分級（分級只看語氣詞），仍是全仓
                "stage4_all_ready": True}},
    {"id": "dip_buy_level", "positions": {},
     "text": "新来的朋友，继续等待夜间或者明天的多机会，BTC60800附近",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"开多"},
                "mode": "到价触发", "triggers": {"BTCUSDT": (60600, 60950)},
                "stage4_all_ready": True}},
    {"id": "short_entry_level", "positions": {},
     "text": "ETH反弹到1820直接开空",
     "expect": {"count": 1, "symbols": {"ETHUSDT"}, "actions": {"开空"},
                "mode": "到价触发", "triggers": {"ETHUSDT": 1820},
                "stage4_all_ready": True}},
    {"id": "per_symbol_prices_no_mixing", "positions": {},
     "text": "BTC59500，ETH1780，到了就接多",
     "expect": {"count": 2, "symbols": {"BTCUSDT", "ETHUSDT"}, "actions": {"开多"},
                "mode": "到价触发",
                "triggers": {"BTCUSDT": 59500, "ETHUSDT": 1780},  # 價位絕不可張冠李戴
                "stage4_all_ready": True}},
    {"id": "alias_dabing_level", "positions": {},
     "text": "大饼回踩59800接多",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"开多"},
                "mode": "到价触发", "triggers": {"BTCUSDT": 59800},
                "stage4_all_ready": True}},
    # ---------- size_grade 倉位分級（2026-07-07 新增：一点点/轻仓=0.25、半仓=0.5、沒講=全仓） ----------
    {"id": "light_open_now", "positions": {},
     "text": "BTC现价一点点进多，试试水",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"开多"},
                "mode": "现在执行", "triggers": {"BTCUSDT": "live"},
                "grades": "轻仓", "stage4_all_ready": True}},
    {"id": "half_open_now", "positions": {},
     "text": "ETH现价买入，一半仓位就好",
     "expect": {"count": 1, "symbols": {"ETHUSDT"}, "actions": {"开多"},
                "mode": "现在执行", "grades": "半仓", "stage4_all_ready": True}},
    {"id": "plain_open_defaults_full", "positions": {},
     "text": "SOL现价直接开多，冲",
     "expect": {"count": 1, "symbols": {"SOLUSDT"}, "actions": {"开多"},
                "mode": "现在执行", "grades": "全仓", "stage4_all_ready": True}},
    {"id": "light_add_with_long", "positions": LONG_BTC,
     "text": "BTC多单现价小加一点",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"加多"},
                "mode": "现在执行", "grades": "轻仓", "stage4_all_ready": True}},

    {"id": "mixed_clarity_partial_output", "positions": {},
     "text": "行情很乱先观望，但ETH1780我会接多",
     "expect": {"count": 1, "symbols": {"ETHUSDT"}, "forbid_symbols": {"BTCUSDT", "SOLUSDT"},
                "actions": {"开多"}, "mode": "到价触发", "triggers": {"ETHUSDT": 1780},
                "stage4_all_ready": True}},  # 模糊的部分放棄、明確的部分保留

    # ---------- 持倉相關（止盈/止損/平/減/加） ----------
    {"id": "tp_with_long", "positions": LONG_BTC,
     "text": "BTC多单止盈挂64500",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"止盈多"},
                "mode": "到价触发", "triggers": {"BTCUSDT": 64500},
                "stage4_all_ready": True}},
    {"id": "sl_with_long", "positions": LONG_BTC,
     "text": "多单止损带好，BTC跌破57500就走人",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"止损多"},
                "mode": "到价触发", "triggers": {"BTCUSDT": 57500},
                "stage4_all_ready": True}},
    {"id": "close_long_now", "positions": LONG_BTC,
     "text": "BTC多单现价全部离场",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"平多"},
                "mode": "现在执行", "stage4_all_ready": True}},
    {"id": "reduce_half_long", "positions": LONG_BTC,
     "text": "BTC多单先减一半，落袋一部分",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"减多"},
                "mode": "现在执行", "stage4_all_ready": True}},
    {"id": "add_on_dip_with_long", "positions": LONG_BTC,
     "text": "回踩58500就是加仓机会，大胆加多BTC",
     "expect": {"count": 1, "symbols": {"BTCUSDT"}, "actions": {"加多"},
                "mode": "到价触发", "triggers": {"BTCUSDT": 58500},
                "stage4_all_ready": True}},
    {"id": "close_short_now", "positions": SHORT_ETH,
     "text": "ETH空单全部平掉，现价",
     "expect": {"count": 1, "symbols": {"ETHUSDT"}, "actions": {"平空"},
                "mode": "现在执行", "stage4_all_ready": True}},
    {"id": "tp_short", "positions": SHORT_ETH,
     "text": "ETH空单止盈1650挂好",
     "expect": {"count": 1, "symbols": {"ETHUSDT"}, "actions": {"止盈空"},
                "mode": "到价触发", "triggers": {"ETHUSDT": 1650},
                "stage4_all_ready": True}},

    # ---------- 進階/語意模糊觀察組（advisory：失敗只記錄，不算紅燈） ----------
    {"id": "chinese_numeral_target", "positions": LONG_BTC, "advisory": True,
     "text": "BTC多单目标6万7，到了就止盈",
     "expect": {"count": (0, 1), "symbols": {"BTCUSDT"}, "actions": {"止盈多"},
                "mode": "到价触发", "triggers": {"BTCUSDT": 67000}}},  # 中文數字換算
    {"id": "hold_with_target", "positions": LONG_ALL, "advisory": True,
     "text": "多单拿住，BTC7月20日之前，目标67500，ETH，SOL同步",
     "expect": {"count": (0, 3), "actions": {"止盈多"},
                "triggers": {"BTCUSDT": 67500}}},  # 「目標」是否算止盈指令，本身有模糊空間
]
