# -*- coding: utf-8 -*-
"""
SafeW 統一入口。所有功能都拆分成 app/ 底下的獨立元件，這裡只負責依子命令分派：

    python main.py monitor          持續監聽模式（每 2 秒截圖一次，即時結構化）
    python main.py scroll           深度回溯滾動擷取（往上滑到舊訊息，再慢慢往下滑擷取）
    python main.py pipeline         scroll -> stage3 -> stage4 一次跑完（取代舊 deep_scroll_pipeline.py）
    python main.py stage3           結構化.jsonl -> 可交易訊號.jsonl
    python main.py stage4           可交易訊號.jsonl -> 下單參數.jsonl
    python main.py stage5-validate  Binance Testnet 下單參數驗證與成交報告（其餘參數原樣轉給該腳本）
    python main.py stage5-execute   互動式逐筆下單執行（其餘參數原樣轉給該腳本）

詳細流程說明與圖示見 docs/PIPELINE.md。
"""
import sys


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    cmd = sys.argv[1]
    rest_args = sys.argv[2:]

    if cmd == "monitor":
        from app import monitor_loop
        monitor_loop.run()

    elif cmd == "scroll":
        from app import scroll_capture
        scroll_capture.run_scroll_capture()

    elif cmd == "pipeline":
        from app import scroll_capture
        n = scroll_capture.run_scroll_capture()
        if n < 0:
            print("[中止] 滾動擷取失敗（找不到視窗），不繼續執行 Stage3/Stage4。")
            return 1
        _run_stage3()
        _run_stage4()
        print("\n=== 全部階段執行完畢，請核對以下三個檔案 ===")
        print("  結構化：C:\\Users\\user\\Desktop\\SafeW\\阿佛禁言群B_結構化.jsonl")
        print("  可交易訊號：C:\\Users\\user\\Desktop\\SafeW\\阿佛禁言群B_可交易訊號.jsonl")
        print("  下單參數：C:\\Users\\user\\Desktop\\SafeW\\阿佛禁言群B_下單參數.jsonl")

    elif cmd == "stage3":
        _run_stage3()

    elif cmd == "stage4":
        _run_stage4()

    elif cmd == "stage5-validate":
        sys.argv = ["validate_and_execute_order_params.py"] + rest_args
        from app import validate_and_execute_order_params as stage5v
        stage5v.main()

    elif cmd == "stage5-execute":
        sys.argv = ["order_executor.py"] + rest_args
        from app import order_executor as stage5e
        stage5e.main()

    else:
        print(f"未知的子命令：{cmd}\n")
        print(__doc__)
        return 1

    return 0


def _run_stage3():
    print("\n>>> Stage 3：trading_signal_agent")
    from app import trading_signal_agent
    trading_signal_agent.main()


def _run_stage4():
    print("\n>>> Stage 4：signal_to_order_params")
    from app import signal_to_order_params
    signal_to_order_params.main()


if __name__ == "__main__":
    sys.exit(main())
