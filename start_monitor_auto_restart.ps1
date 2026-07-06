# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    以「自動重啟」的方式執行 ocr_monitor.py：
    若程式因例外/崩潰而意外結束，會自動重新啟動，不需要人工介入。
    正常關閉（手動關閉此視窗 / Ctrl+C）不會被視為「崩潰」，只有「非 0 結束碼」才會觸發重啟。

.NOTES
    - 這是「手動雙擊啟動」的腳本，不會隨開機自動執行。
    - 不會自動開啟或搜尋 SafeW 視窗；找不到視窗時 ocr_monitor.py 本身就會等待重試（見其 main() 迴圈），
      本腳本只負責在 python 進程「整個意外終止」時重新拉起一個新的進程。
    - 若短時間內連續崩潰次數過多，會拉長重試間隔並記錄，避免無限快速重啟造成資源浪費（例如環境設定壞掉、
      找不到 tesseract.exe 等持續性錯誤）。
#>

$ErrorActionPreference = "Stop"

# 腳本所在目錄即為 ocr_monitor.py 所在目錄，用絕對路徑避免「雙擊啟動時工作目錄不是此資料夾」的常見問題
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# 把主控台切到 UTF-8 內碼（65001），並讓 PowerShell 用 UTF-8 解讀外部程序（python）的 stdout/stderr，
# 否則 python 印出的 UTF-8 位元組會被主控台預設的系統內碼（例如 cp950）誤讀成亂碼，
# 這與 PowerShell 腳本自身 Write-Host 的中文顯示是兩條獨立的編碼路徑，必須分別處理。
try {
    chcp 65001 | Out-Null
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    Write-Host "警告：設定主控台編碼失敗，python 輸出的中文可能會顯示亂碼（不影響實際監聽功能）。"
}

$PythonExe = "python"          # 沿用系統 PATH 中的 python（與目前手動執行方式一致）
$TargetScript = Join-Path $ScriptDir "main.py"
$TargetArgs = @("monitor")      # 重構後統一入口：python main.py monitor
$LogDir = Join-Path $ScriptDir "monitor_logs"
$MaxRestartDelaySec = 60        # 連續崩潰時，重試間隔的上限（秒），避免無限快速重啟
$BaseRestartDelaySec = 3         # 初始重試間隔（秒）

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$env:PYTHONIOENCODING = "utf-8"  # 確保中文輸出/寫檔不會因主控台編碼造成例外

$consecutiveCrashes = 0

Write-Host "===== SafeW OCR 監聽器（帶自動重啟）啟動 ====="
Write-Host "腳本目錄: $ScriptDir"
Write-Host "日誌目錄: $LogDir"
Write-Host "按 Ctrl+C 可正常結束（不會觸發自動重啟）。"
Write-Host ""

while ($true) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $logFile = Join-Path $LogDir "monitor_$timestamp.log"

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] 啟動 ocr_monitor.py（日誌: $logFile）"

    # -u：強制 Python 使用無緩衝（unbuffered）stdout/stderr。
    # 若不加此參數，Python 偵測到輸出被導向管線（而非真正的終端機）時，
    # 會自動切換成「整塊緩衝」（block buffering，通常 4KB/8KB 才 flush 一次），
    # 導致在畫面沒有新交易訊號的期間，即使程式其實正常運作中，Tee-Object/日誌檔案也會長時間看不到任何輸出，
    # 容易誤以為程式「卡住」或「沒有真的在跑」。加上 -u 後，每次 print() 都會立即寫出，即時監控才準確。
    & $PythonExe -u $TargetScript @TargetArgs 2>&1 | Tee-Object -FilePath $logFile
    $exitCode = $LASTEXITCODE

    if ($exitCode -eq 0) {
        Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ocr_monitor.py 正常結束（結束碼 0），不會自動重啟。"
        break
    }

    $consecutiveCrashes++
    # 指數退避（但設上限），避免持續性錯誤（如環境設定壞掉）造成無限快速重啟、洗版日誌
    $delaySec = [Math]::Min($BaseRestartDelaySec * [Math]::Pow(2, $consecutiveCrashes - 1), $MaxRestartDelaySec)

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ocr_monitor.py 意外結束（結束碼 $exitCode，第 $consecutiveCrashes 次連續崩潰）。"
    Write-Host "  將於 $delaySec 秒後自動重啟..."
    Start-Sleep -Seconds $delaySec
}

Write-Host "===== 監聽器已停止 ====="
