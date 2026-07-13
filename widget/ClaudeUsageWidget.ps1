# ClaudeUsageWidget.ps1 — Claude Code使用量のタスクトレイ常駐ウィジェット（Windows側）
#
# WSL側の statusline スクリプトが書き出す usage.json を \\wsl.localhost\ 経由で読み、
# 5時間枠の使用率をトレイアイコンに数字で描画する。ネットワーク通信・認証情報なし。
# Windows PowerShell 5.1（Windows 11標準）で動作。追加インストール不要。
#
# 起動例:
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ClaudeUsageWidget.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File ClaudeUsageWidget.ps1 -SelfTest   # GUIなしの疎通確認

param(
    [string]$DataPath = "",   # usage.json のフルパス。省略時はWSLディストリビューションから自動検出
    [int]$PollSeconds = 5,    # ファイル再読込の間隔（秒）
    [switch]$SelfTest         # 検出とパースだけ行って終了（GUIなし）
)

$ErrorActionPreference = 'Stop'
$RelPath = '.local\share\claude-usage-widget\usage.json'  # WSLホーム配下の書き出し先（statusline.pyと対応）
$StaleAfterSec = 120   # updated_at がこれより古ければ「セッションなし」とみなす（refreshInterval=30の4倍）

function Find-UsageFile {
    # wsl.exe -l -q はUTF-16で出力されヌル文字が混ざるため除去する
    $distros = @()
    try {
        $distros = (wsl.exe -l -q 2>$null) | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ }
    } catch { }
    foreach ($d in $distros) {
        $base = "\\wsl.localhost\$d\home"
        if (-not (Test-Path $base)) { continue }
        foreach ($userDir in (Get-ChildItem $base -Directory -ErrorAction SilentlyContinue)) {
            $p = Join-Path $userDir.FullName $RelPath
            if (Test-Path $p) { return $p }
        }
    }
    return $null
}

function Read-Snapshot([string]$path) {
    if (-not $path -or -not (Test-Path $path)) { return $null }
    try {
        return (Get-Content -Raw -Path $path -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function From-Epoch([long]$sec) {
    return [DateTimeOffset]::FromUnixTimeSeconds($sec).ToLocalTime().DateTime
}

function Now-Epoch {
    return [DateTimeOffset]::Now.ToUnixTimeSeconds()
}

function Format-Ago([long]$sec) {
    if ($sec -lt 10) { return 'たった今' }
    if ($sec -lt 120) { return "$sec秒前" }
    if ($sec -lt 7200) { return "$([math]::Floor($sec / 60))分前" }
    return "$([math]::Floor($sec / 3600))時間前"
}

# ウィジェット表示用に snapshot を解釈する。リセット時刻を過ぎた観測値は 0%（推定）に読み替える
function Get-WindowState($window) {
    if (-not $window -or $null -eq $window.used_percentage) {
        return @{ Known = $false; Pct = $null; ResetText = ''; Estimated = $false }
    }
    $pct = [math]::Round([double]$window.used_percentage)
    $resetText = ''
    $estimated = $false
    if ($window.resets_at) {
        $reset = From-Epoch $window.resets_at
        if ((Now-Epoch) -ge [long]$window.resets_at) {
            $pct = 0; $estimated = $true
        } else {
            $resetText = if ($reset.Date -eq (Get-Date).Date) { $reset.ToString('HH:mm') } else { $reset.ToString('M/d HH:mm') }
        }
    }
    return @{ Known = $true; Pct = $pct; ResetText = $resetText; Estimated = $estimated }
}

# ---- 疎通確認モード -------------------------------------------------------
if ($SelfTest) {
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }  # WSLから呼んだときの文字化け防止
    $path = if ($DataPath) { $DataPath } else { Find-UsageFile }
    if (-not $path) {
        Write-Output "NG: usage.json が見つかりません。WSL側でClaude Codeセッションを一度開始してください。"
        exit 1
    }
    Write-Output "usage.json: $path"
    $snap = Read-Snapshot $path
    if (-not $snap) { Write-Output "NG: JSONのパースに失敗しました。"; exit 1 }
    $age = (Now-Epoch) - [long]$snap.updated_at
    Write-Output ("更新: {0} ({1})" -f (From-Epoch $snap.updated_at), (Format-Ago $age))
    foreach ($w in @('five_hour', 'seven_day')) {
        $st = Get-WindowState $snap.rate_limits.$w
        if ($st.Known) {
            $suffix = if ($st.Estimated) { '（リセット済・推定）' } elseif ($st.ResetText) { "（$($st.ResetText) リセット）" } else { '' }
            Write-Output ("{0}: {1}%{2}" -f $w, $st.Pct, $suffix)
        } else {
            Write-Output "${w}: データなし"
        }
    }
    Write-Output "OK"
    exit 0
}

# ---- GUI本体 ---------------------------------------------------------------
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool DestroyIcon(System.IntPtr handle);' -Name 'IconUtil' -Namespace 'Native'

# 二重起動防止（スタートアップ登録と手動起動が重なった場合など）
$script:mutex = New-Object System.Threading.Mutex($false, 'Global\ClaudeUsageWidget')
if (-not $script:mutex.WaitOne(0, $false)) { exit 0 }

$script:dataPath = if ($DataPath) { $DataPath } else { Find-UsageFile }
$script:readFailures = 0
$script:lastIconKey = ''
$script:prevHIcon = [IntPtr]::Zero
$script:lastFivePct = -1   # しきい値越え通知の判定用

function New-TrayIcon([string]$text, [string]$bgHex) {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.TextRenderingHint = 'AntiAliasGridFit'
    $brush = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($bgHex))
    $g.FillEllipse($brush, 0, 0, 31, 31)
    $fontSize = if ($text.Length -ge 2) { 13 } else { 16 }
    $font = New-Object System.Drawing.Font('Segoe UI', $fontSize, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $sf = New-Object System.Drawing.StringFormat
    $sf.Alignment = 'Center'; $sf.LineAlignment = 'Center'
    $rect = New-Object System.Drawing.RectangleF 0, 1, 32, 31
    $g.DrawString($text, $font, [System.Drawing.Brushes]::White, $rect, $sf)
    $g.Dispose(); $brush.Dispose(); $font.Dispose(); $sf.Dispose()
    $hIcon = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($hIcon)
    $bmp.Dispose()
    return @{ Icon = $icon; Handle = $hIcon }
}

function Set-TrayIcon([string]$text, [string]$bgHex) {
    $key = "$text|$bgHex"
    if ($key -eq $script:lastIconKey) { return }
    $script:lastIconKey = $key
    $new = New-TrayIcon $text $bgHex
    $script:notifyIcon.Icon = $new.Icon
    if ($script:prevHIcon -ne [IntPtr]::Zero) { [Native.IconUtil]::DestroyIcon($script:prevHIcon) | Out-Null }  # GDIハンドルのリーク防止
    $script:prevHIcon = $new.Handle
}

function Set-Tooltip([string]$text) {
    if ($text.Length -gt 63) { $text = $text.Substring(0, 63) }  # .NET FrameworkのNotifyIcon.Textは63文字上限
    $script:notifyIcon.Text = $text
}

function Get-PctColor([int]$pct, [bool]$stale) {
    if ($stale) { return '#6E6E6E' }
    if ($pct -ge 80) { return '#D0453A' }
    if ($pct -ge 50) { return '#C98A1B' }
    return '#2E9E4F'
}

function Update-Display {
    if (-not $script:dataPath -or -not (Test-Path $script:dataPath)) {
        $script:dataPath = Find-UsageFile
    }
    $snap = Read-Snapshot $script:dataPath
    if (-not $snap) {
        $script:readFailures++
        if ($script:readFailures -ge 5) { $script:dataPath = Find-UsageFile; $script:readFailures = 0 }
        Set-TrayIcon '?' '#6E6E6E'
        Set-Tooltip "Claude使用量: データ待ち`nWSLでセッションを開始してください"
        return
    }
    $script:readFailures = 0
    $script:lastSnap = $snap

    $age = (Now-Epoch) - [long]$snap.updated_at
    $stale = $age -gt $StaleAfterSec
    $five = Get-WindowState $snap.rate_limits.five_hour
    $seven = Get-WindowState $snap.rate_limits.seven_day

    if (-not $five.Known) {
        Set-TrayIcon '?' '#6E6E6E'
        Set-Tooltip "Claude使用量: レート情報待ち`n更新 $(Format-Ago $age)"
        return
    }

    $iconText = if ($five.Pct -ge 100) { '!!' } else { [string]$five.Pct }
    Set-TrayIcon $iconText (Get-PctColor $five.Pct $stale)

    $lines = @()
    $fiveReset = if ($five.Estimated) { '↺' } elseif ($five.ResetText) { "→$($five.ResetText)" } else { '' }
    $lines += "5h $($five.Pct)% $fiveReset".TrimEnd()
    if ($seven.Known) {
        $sevenReset = if ($seven.Estimated) { '↺' } elseif ($seven.ResetText) { "→$($seven.ResetText)" } else { '' }
        $lines += "7d $($seven.Pct)% $sevenReset".TrimEnd()
    }
    $lines += if ($stale) { "セッションなし ($(Format-Ago $age))" } else { "更新 $(Format-Ago $age)" }
    Set-Tooltip ($lines -join "`n")

    # 5時間枠のしきい値越え通知（新しい実測値のときだけ）
    if (-not $stale -and -not $five.Estimated -and $script:lastFivePct -ge 0) {
        foreach ($threshold in @(80, 95)) {
            if ($script:lastFivePct -lt $threshold -and $five.Pct -ge $threshold) {
                $script:notifyIcon.ShowBalloonTip(5000, 'Claude使用量',
                    "5時間枠が ${threshold}% を超えました（現在 $($five.Pct)%）", 'Warning')
                break
            }
        }
    }
    if (-not $five.Estimated) { $script:lastFivePct = $five.Pct }
}

function Show-Details {
    $snap = $script:lastSnap
    if (-not $snap) {
        [System.Windows.Forms.MessageBox]::Show('まだデータがありません。WSL側でClaude Codeセッションを開始してください。', 'Claude使用量') | Out-Null
        return
    }
    $age = (Now-Epoch) - [long]$snap.updated_at
    $lines = @('Claude Code 使用量', '')
    foreach ($def in @(@('5時間枠', 'five_hour'), @('7日枠  ', 'seven_day'))) {
        $st = Get-WindowState $snap.rate_limits.($def[1])
        if ($st.Known) {
            $suffix = if ($st.Estimated) { '（リセット済・推定）' } elseif ($st.ResetText) { "（$($st.ResetText) リセット）" } else { '' }
            $lines += "$($def[0]): $($st.Pct)%$suffix"
        } else {
            $lines += "$($def[0]): データなし"
        }
    }
    $lines += ''
    $lines += "最終更新: $(Format-Ago $age)" + $(if ($age -gt $StaleAfterSec) { '（セッションなし）' } else { '（セッション稼働中）' })
    if ($snap.sessions) {
        $lines += ''
        $lines += '直近48hのセッション:'
        foreach ($prop in $snap.sessions.PSObject.Properties) {
            $s = $prop.Value
            $name = if ($s.cwd) { Split-Path $s.cwd -Leaf } else { $prop.Name.Substring(0, 8) }
            $cost = if ($null -ne $s.cost_usd) { '${0:N2}' -f [double]$s.cost_usd } else { '-' }
            $lines += ("・{0}  {1}  ({2})" -f $name, $cost, (Format-Ago ((Now-Epoch) - [long]$s.updated_at)))
        }
    }
    $lines += ''
    $lines += "データ: $script:dataPath"
    [System.Windows.Forms.MessageBox]::Show(($lines -join "`n"), 'Claude使用量') | Out-Null
}

$script:notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$script:lastSnap = $null

$menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add('詳細を表示')
[void]$menu.Items.Add('usage.json を開く')
[void]$menu.Items.Add('パスを再検出')
[void]$menu.Items.Add('-')
[void]$menu.Items.Add('終了')
$menu.Items[0].Add_Click({ Show-Details })
$menu.Items[1].Add_Click({ if ($script:dataPath) { Start-Process notepad.exe $script:dataPath } })
$menu.Items[2].Add_Click({ $script:dataPath = Find-UsageFile; Update-Display })
$menu.Items[4].Add_Click({ [System.Windows.Forms.Application]::Exit() })
$script:notifyIcon.ContextMenuStrip = $menu
$script:notifyIcon.Add_DoubleClick({ Show-Details })

Set-TrayIcon '…' '#6E6E6E'
Set-Tooltip 'Claude使用量: 起動中'
$script:notifyIcon.Visible = $true

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [math]::Max(1, $PollSeconds) * 1000
$timer.Add_Tick({ try { Update-Display } catch { } })
$timer.Start()
Update-Display

try {
    [System.Windows.Forms.Application]::Run()
} finally {
    $timer.Stop()
    $script:notifyIcon.Visible = $false
    $script:notifyIcon.Dispose()
    if ($script:prevHIcon -ne [IntPtr]::Zero) { [Native.IconUtil]::DestroyIcon($script:prevHIcon) | Out-Null }
    $script:mutex.ReleaseMutex()
}
