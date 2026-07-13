# ClaudeUsageWidget.ps1 — Claude Code使用量のタスクトレイ常駐ウィジェット（Windows側）
#
# WSL側の statusline スクリプトが書き出す usage.json / history.jsonl を \\wsl.localhost\ 経由で読み、
#   - タスクトレイ: 5時間枠の使用率をゲージ付きアイコンで表示
#   - 左クリック/ダブルクリック: WPFダッシュボード（メーター・カウントダウン・24h推移・セッション別コスト）
# ネットワーク通信・認証情報なし。Windows PowerShell 5.1（Windows 11標準）のみで動作、追加インストール不要。
#
# 起動例:
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ClaudeUsageWidget.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File ClaudeUsageWidget.ps1 -SelfTest        # GUIなし疎通確認
#   powershell -NoProfile -ExecutionPolicy Bypass -File ClaudeUsageWidget.ps1 -RenderShot x.png # ダッシュボードを画面に出さずPNG化（デザイン確認用）

param(
    [string]$DataPath = "",    # usage.json のフルパス。省略時はWSLディストリビューションから自動検出
    [int]$PollSeconds = 5,     # ファイル再読込の間隔（秒）
    [switch]$SelfTest,         # 検出とパースだけ行って終了（GUIなし）
    [string]$RenderShot = ""   # ダッシュボードをオフスクリーン描画してPNG保存し終了（GUI検証用）
)

$ErrorActionPreference = 'Stop'
$RelPath = '.local\share\claude-usage-widget\usage.json'  # WSLホーム配下の書き出し先（statusline.pyと対応）
$StaleAfterSec = 120   # updated_at がこれより古ければ「セッションなし」とみなす（refreshInterval=30の4倍）

# ---- 配色（dataviz検証済みダークパレット。面 #1a1a19 に対するコントラスト検証済み） ----
$Col = @{
    Surface   = '#1A1A19'; Ink = '#FFFFFF'; Ink2 = '#C3C2B7'; Muted = '#898781'
    Grid      = '#2C2C2A'; Hairline = '#383835'
    Accent    = '#3987E5'  # 通常域（アクセント青）
    Warning   = '#FAB219'  # 50%以上
    Critical  = '#D03B3B'  # 80%以上
    StaleGray = '#6E6E6E'
}

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

function Read-History([string]$usagePath, [long]$sinceEpoch) {
    # history.jsonl（1行1サンプル・5分間隔）から sinceEpoch 以降を読む
    if (-not $usagePath) { return @() }
    $histPath = Join-Path (Split-Path $usagePath) 'history.jsonl'
    if (-not (Test-Path $histPath)) { return @() }
    $out = @()
    try {
        foreach ($line in (Get-Content -Path $histPath -Encoding UTF8)) {
            if (-not $line.Trim()) { continue }
            try {
                $e = $line | ConvertFrom-Json
                if ([long]$e.t -ge $sinceEpoch -and $null -ne $e.five) { $out += $e }
            } catch { }
        }
    } catch { }
    return $out
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

function Format-Remaining([long]$sec) {
    if ($sec -le 0) { return '' }
    if ($sec -lt 300) { return ('あと{0}:{1:00}' -f [math]::Floor($sec / 60), ($sec % 60)) }
    if ($sec -lt 3600) { return "あと$([math]::Floor($sec / 60))分" }
    return "あと$([math]::Floor($sec / 3600))時間$([math]::Floor(($sec % 3600) / 60))分"
}

# ウィジェット表示用に snapshot を解釈する。リセット時刻を過ぎた観測値は 0%（推定）に読み替える
function Get-WindowState($window) {
    if (-not $window -or $null -eq $window.used_percentage) {
        return @{ Known = $false; Pct = $null; ResetText = ''; ResetEpoch = 0; Estimated = $false }
    }
    $pct = [math]::Round([double]$window.used_percentage)
    $resetText = ''
    $estimated = $false
    $resetEpoch = 0
    if ($window.resets_at) {
        $resetEpoch = [long]$window.resets_at
        $reset = From-Epoch $resetEpoch
        if ((Now-Epoch) -ge $resetEpoch) {
            $pct = 0; $estimated = $true; $resetEpoch = 0
        } else {
            $resetText = if ($reset.Date -eq (Get-Date).Date) { $reset.ToString('HH:mm') } else { $reset.ToString('M/d HH:mm') }
        }
    }
    return @{ Known = $true; Pct = $pct; ResetText = $resetText; ResetEpoch = $resetEpoch; Estimated = $estimated }
}

function Get-SeverityColor([int]$pct, [bool]$stale) {
    if ($stale) { return $Col.StaleGray }
    if ($pct -ge 80) { return $Col.Critical }
    if ($pct -ge 50) { return $Col.Warning }
    return $Col.Accent
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
    $hist = @(Read-History $path ((Now-Epoch) - 86400))
    Write-Output "履歴サンプル(24h): $($hist.Count)件"
    Write-Output "OK"
    exit 0
}

# ---- GUIアセンブリ ---------------------------------------------------------
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Xaml
Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern bool DestroyIcon(System.IntPtr handle);' -Name 'IconUtil' -Namespace 'Native'

$script:dataPath = if ($DataPath) { $DataPath } else { Find-UsageFile }
$script:lastSnap = $null

function New-WpfBrush([string]$hex) {
    try {
        return New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.ColorConverter]::ConvertFromString($hex))
    } catch {
        throw "New-WpfBrush: 不正な色 '$hex' / $((Get-PSCallStack)[1].Command) 行$((Get-PSCallStack)[1].ScriptLineNumber)"
    }
}

function New-WpfBrushAlpha([string]$hex, [byte]$alpha) {
    $c = [System.Windows.Media.ColorConverter]::ConvertFromString($hex)
    return New-Object System.Windows.Media.SolidColorBrush ([System.Windows.Media.Color]::FromArgb($alpha, $c.R, $c.G, $c.B))
}

# ---- ダッシュボード（WPF） -------------------------------------------------
$Xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Claude使用量" WindowStyle="None" AllowsTransparency="True" Background="Transparent"
        ShowInTaskbar="False" Topmost="True" ResizeMode="NoResize" SizeToContent="Height"
        Width="420" ShowActivated="True">
  <Border Name="RootBorder" Margin="14" CornerRadius="14" Background="#1A1A19"
          BorderBrush="#1AFFFFFF" BorderThickness="1" Padding="22,18,22,16">
    <Border.Effect>
      <DropShadowEffect BlurRadius="22" ShadowDepth="3" Opacity="0.55" Color="#000000"/>
    </Border.Effect>
    <StackPanel>
      <Grid>
        <TextBlock Text="Claude Code 使用量" FontFamily="Segoe UI" FontSize="13" FontWeight="SemiBold" Foreground="#FFFFFF"/>
        <TextBlock Name="TxtUpdated" FontFamily="Segoe UI" FontSize="11" Foreground="#898781" HorizontalAlignment="Right" VerticalAlignment="Center"/>
      </Grid>

      <!-- 5時間ウィンドウ（ヒーロー） -->
      <TextBlock Text="5時間ウィンドウ" FontFamily="Segoe UI" FontSize="11" Foreground="#C3C2B7" Margin="0,18,0,0"/>
      <Grid Margin="0,0,0,0">
        <TextBlock Name="Txt5Pct" Text="--" FontFamily="Segoe UI" FontSize="44" FontWeight="SemiBold" Foreground="#FFFFFF" Margin="0,-4,0,0"/>
        <StackPanel HorizontalAlignment="Right" VerticalAlignment="Bottom" Margin="0,0,0,8">
          <TextBlock Name="Txt5Remain" FontFamily="Segoe UI" FontSize="13" Foreground="#C3C2B7" HorizontalAlignment="Right"/>
          <TextBlock Name="Txt5Reset" FontFamily="Segoe UI" FontSize="11" Foreground="#898781" HorizontalAlignment="Right" Margin="0,2,0,0"/>
        </StackPanel>
      </Grid>
      <Grid Height="8" Margin="0,6,0,0">
        <Border Name="Meter5Track" CornerRadius="4"/>
        <Border Name="Meter5Fill" CornerRadius="4" HorizontalAlignment="Left" Width="0"/>
      </Grid>

      <!-- 7日ウィンドウ -->
      <TextBlock Text="7日間ウィンドウ" FontFamily="Segoe UI" FontSize="11" Foreground="#C3C2B7" Margin="0,20,0,0"/>
      <Grid>
        <TextBlock Name="Txt7Pct" Text="--" FontFamily="Segoe UI" FontSize="22" FontWeight="SemiBold" Foreground="#FFFFFF"/>
        <TextBlock Name="Txt7Reset" FontFamily="Segoe UI" FontSize="11" Foreground="#898781" HorizontalAlignment="Right" VerticalAlignment="Bottom" Margin="0,0,0,4"/>
      </Grid>
      <Grid Height="6" Margin="0,5,0,0">
        <Border Name="Meter7Track" CornerRadius="3"/>
        <Border Name="Meter7Fill" CornerRadius="3" HorizontalAlignment="Left" Width="0"/>
      </Grid>

      <Rectangle Height="1" Fill="#2C2C2A" Margin="0,20,0,0"/>

      <!-- 推移チャート（単系列: 5時間ウィンドウ使用率） -->
      <TextBlock Text="5時間ウィンドウ使用率 · 直近24時間" FontFamily="Segoe UI" FontSize="11" Foreground="#C3C2B7" Margin="0,14,0,0"/>
      <Grid Height="96" Margin="0,8,0,0">
        <Canvas Name="ChartCanvas" Width="346" Height="96" HorizontalAlignment="Left" ClipToBounds="True"/>
        <TextBlock Name="ChartEmpty" Text="履歴を収集中です（5分ごとに記録されます）" FontFamily="Segoe UI" FontSize="11"
                   Foreground="#898781" HorizontalAlignment="Center" VerticalAlignment="Center" Visibility="Collapsed"/>
      </Grid>

      <Rectangle Height="1" Fill="#2C2C2A" Margin="0,14,0,0"/>

      <!-- セッション -->
      <TextBlock Text="セッション（直近48時間）" FontFamily="Segoe UI" FontSize="11" Foreground="#C3C2B7" Margin="0,14,0,0"/>
      <StackPanel Name="SessionsPanel" Margin="0,4,0,0"/>
      <TextBlock Name="CostNote" Text="コストはAPI従量課金に換算した推定値（サブスクの実請求額ではありません）"
                 FontFamily="Segoe UI" FontSize="10" Foreground="#898781" TextWrapping="Wrap" Margin="0,10,0,0"/>
    </StackPanel>
  </Border>
</Window>
'@

function New-Dashboard {
    $window = [System.Windows.Markup.XamlReader]::Parse($Xaml)
    $script:ui = @{}
    foreach ($name in @('RootBorder','TxtUpdated','Txt5Pct','Txt5Remain','Txt5Reset','Meter5Track','Meter5Fill',
                        'Txt7Pct','Txt7Reset','Meter7Track','Meter7Fill','ChartCanvas','ChartEmpty','SessionsPanel','CostNote')) {
        $script:ui[$name] = $window.FindName($name)
    }
    return $window
}

function Set-Meter($track, $fill, $state, [bool]$stale, [double]$fullWidth) {
    if (-not $state.Known) {
        $track.Background = New-WpfBrushAlpha $Col.StaleGray 56
        $fill.Width = 0
        return
    }
    $color = Get-SeverityColor $state.Pct $stale
    $track.Background = New-WpfBrushAlpha $color 56   # 未充填トラックは同系色の薄いステップ
    $fill.Background = New-WpfBrush $color
    $w = [math]::Round($fullWidth * [math]::Min(100, $state.Pct) / 100.0)
    if ($state.Pct -gt 0 -and $w -lt 8) { $w = 8 }    # 角丸(4px)がつぶれない最小幅
    $fill.Width = $w
}

function Update-ChartCanvas($history) {
    $canvas = $script:ui.ChartCanvas
    $canvas.Children.Clear()
    $W = 320.0; $plotTop = 4.0; $plotBottom = 72.0; $plotH = $plotBottom - $plotTop  # 右側26pxは目盛りラベル用マージン
    $now = Now-Epoch; $t0 = $now - 86400

    # ヘアライングリッド（0/50/100%）と目盛りラベル（プロット外の右マージンに置く）
    foreach ($tick in @(0, 50, 100)) {
        $y = $plotBottom - ($tick / 100.0) * $plotH
        $line = New-Object System.Windows.Shapes.Line
        $line.X1 = 0; $line.X2 = $W; $line.Y1 = $y; $line.Y2 = $y
        $line.Stroke = New-WpfBrush $Col.Grid; $line.StrokeThickness = 1
        [void]$canvas.Children.Add($line)
        $lbl = New-Object System.Windows.Controls.TextBlock
        $lbl.Text = [string]$tick; $lbl.FontFamily = 'Segoe UI'; $lbl.FontSize = 9
        $lbl.Foreground = New-WpfBrush $Col.Muted
        [System.Windows.Controls.Canvas]::SetLeft($lbl, $W + 6)
        [System.Windows.Controls.Canvas]::SetTop($lbl, [math]::Max(0.0, $y - 7))
        [void]$canvas.Children.Add($lbl)
    }
    # 時間軸ラベル
    foreach ($def in @(@('24時間前', 2.0), @('現在', ($W - 34)))) {
        $lbl = New-Object System.Windows.Controls.TextBlock
        $lbl.Text = $def[0]; $lbl.FontFamily = 'Segoe UI'; $lbl.FontSize = 10
        $lbl.Foreground = New-WpfBrush $Col.Muted
        [System.Windows.Controls.Canvas]::SetLeft($lbl, [double]$def[1])
        [System.Windows.Controls.Canvas]::SetTop($lbl, $plotBottom + 6)
        [void]$canvas.Children.Add($lbl)
    }

    $pts = @()
    foreach ($e in $history) {
        $x = ([double]([long]$e.t - $t0) / 86400.0) * $W
        $y = $plotBottom - ([math]::Min(100.0, [double]$e.five) / 100.0) * $plotH
        $pts += (New-Object System.Windows.Point ([math]::Max(0.0, [math]::Min($W, $x))), $y)
    }
    if ($pts.Count -lt 2) {
        $script:ui.ChartEmpty.Visibility = 'Visible'
        return
    }
    $script:ui.ChartEmpty.Visibility = 'Collapsed'

    # 面（系列色の10%ウォッシュ）
    $poly = New-Object System.Windows.Shapes.Polygon
    $poly.Fill = New-WpfBrushAlpha $Col.Accent 26
    $pc = New-Object System.Windows.Media.PointCollection
    foreach ($p in $pts) { $pc.Add($p) }
    $pc.Add((New-Object System.Windows.Point $pts[-1].X, $plotBottom))
    $pc.Add((New-Object System.Windows.Point $pts[0].X, $plotBottom))
    $poly.Points = $pc
    [void]$canvas.Children.Add($poly)

    # 線（2px・丸キャップ）
    $polyline = New-Object System.Windows.Shapes.Polyline
    $polyline.Stroke = New-WpfBrush $Col.Accent; $polyline.StrokeThickness = 2
    $polyline.StrokeLineJoin = 'Round'; $polyline.StrokeStartLineCap = 'Round'; $polyline.StrokeEndLineCap = 'Round'
    $pc2 = New-Object System.Windows.Media.PointCollection
    foreach ($p in $pts) { $pc2.Add($p) }
    $polyline.Points = $pc2
    [void]$canvas.Children.Add($polyline)

    # 終端マーカー（8px・面色2pxリング）
    $dot = New-Object System.Windows.Shapes.Ellipse
    $dot.Width = 8; $dot.Height = 8
    $dot.Fill = New-WpfBrush $Col.Accent
    $dot.Stroke = New-WpfBrush $Col.Surface; $dot.StrokeThickness = 2
    [System.Windows.Controls.Canvas]::SetLeft($dot, $pts[-1].X - 4)
    [System.Windows.Controls.Canvas]::SetTop($dot, $pts[-1].Y - 4)
    [void]$canvas.Children.Add($dot)
}

function Update-SessionsPanel($snap) {
    $panel = $script:ui.SessionsPanel
    $panel.Children.Clear()
    $now = Now-Epoch
    $sessions = @()
    if ($snap.sessions) {
        foreach ($prop in $snap.sessions.PSObject.Properties) { $sessions += ,@($prop.Name, $prop.Value) }
    }
    if (-not $sessions) {
        $none = New-Object System.Windows.Controls.TextBlock
        $none.Text = '（なし）'; $none.FontFamily = 'Segoe UI'; $none.FontSize = 12
        $none.Foreground = New-WpfBrush $Col.Muted; $none.Margin = '0,4,0,0'
        [void]$panel.Children.Add($none)
        return
    }
    $sessions = $sessions | Sort-Object { [long]$_[1].updated_at } -Descending | Select-Object -First 4
    foreach ($pair in $sessions) {
        $s = $pair[1]
        $age = $now - [long]$s.updated_at
        $active = $age -le $StaleAfterSec
        $grid = New-Object System.Windows.Controls.Grid
        $grid.Margin = '0,7,0,0'
        foreach ($wdef in @('Auto', '*', 'Auto', 'Auto')) {
            # 注意: 変数名を$colにするとパレットの$Colを上書きする（PSの変数名は大文字小文字を区別しない）
            $colDef = New-Object System.Windows.Controls.ColumnDefinition
            if ($wdef -eq '*') {
                $colDef.Width = New-Object System.Windows.GridLength 1, ([System.Windows.GridUnitType]::Star)
            } else {
                $colDef.Width = [System.Windows.GridLength]::Auto
            }
            [void]$grid.ColumnDefinitions.Add($colDef)
        }
        $dot = New-Object System.Windows.Shapes.Ellipse
        $dot.Width = 7; $dot.Height = 7; $dot.VerticalAlignment = 'Center'
        $dot.Fill = New-WpfBrush $(if ($active) { $Col.Accent } else { $Col.Hairline })
        [System.Windows.Controls.Grid]::SetColumn($dot, 0)
        [void]$grid.Children.Add($dot)

        $name = New-Object System.Windows.Controls.TextBlock
        $name.Text = if ($s.cwd) { Split-Path $s.cwd -Leaf } else { $pair[0].Substring(0, 8) }
        $name.FontFamily = 'Segoe UI'; $name.FontSize = 12; $name.FontWeight = 'SemiBold'
        $name.Foreground = New-WpfBrush $Col.Ink; $name.Margin = '9,0,0,0'
        $name.TextTrimming = 'CharacterEllipsis'; $name.VerticalAlignment = 'Center'
        [System.Windows.Controls.Grid]::SetColumn($name, 1)
        [void]$grid.Children.Add($name)

        $ago = New-Object System.Windows.Controls.TextBlock
        $ago.Text = if ($active) { 'アクティブ' } else { Format-Ago $age }
        $ago.FontFamily = 'Segoe UI'; $ago.FontSize = 11
        $ago.Foreground = New-WpfBrush $Col.Muted; $ago.Margin = '8,0,0,0'; $ago.VerticalAlignment = 'Center'
        [System.Windows.Controls.Grid]::SetColumn($ago, 2)
        [void]$grid.Children.Add($ago)

        $cost = New-Object System.Windows.Controls.TextBlock
        $cost.Text = if ($null -ne $s.cost_usd) { '${0:N2}' -f [double]$s.cost_usd } else { '-' }
        $cost.FontFamily = 'Segoe UI'; $cost.FontSize = 12
        $cost.Foreground = New-WpfBrush $Col.Ink2; $cost.Margin = '10,0,0,0'
        $cost.VerticalAlignment = 'Center'; $cost.TextAlignment = 'Right'; $cost.MinWidth = 52
        [System.Windows.Controls.Grid]::SetColumn($cost, 3)
        [void]$grid.Children.Add($cost)
        [void]$panel.Children.Add($grid)
    }
}

function Update-DashboardCountdown {
    # 1秒ごとの軽量更新（カウントダウンと経過表示のみ）
    if (-not $script:lastSnap) { return }
    $now = Now-Epoch
    $five = Get-WindowState $script:lastSnap.rate_limits.five_hour
    if ($five.ResetEpoch -gt 0) {
        $script:ui.Txt5Remain.Text = Format-Remaining ($five.ResetEpoch - $now)
    }
    $age = $now - [long]$script:lastSnap.updated_at
    $state = if ($age -gt $StaleAfterSec) { 'セッションなし' } else { '稼働中' }
    $script:ui.TxtUpdated.Text = "$state · 更新 $(Format-Ago $age)"
}

function Update-Dashboard {
    $snap = $script:lastSnap
    if (-not $snap) {
        $script:ui.TxtUpdated.Text = 'データ待ち — WSLでセッションを開始してください'
        return
    }
    $now = Now-Epoch
    $age = $now - [long]$snap.updated_at
    $stale = $age -gt $StaleAfterSec
    $meterWidth = 346.0

    $five = Get-WindowState $snap.rate_limits.five_hour
    if ($five.Known) {
        $script:ui.Txt5Pct.Text = "$($five.Pct)%"
        $script:ui.Txt5Reset.Text = if ($five.Estimated) { 'リセット済（次のセッションで実測更新）' } elseif ($five.ResetText) { "$($five.ResetText) リセット" } else { '' }
        $script:ui.Txt5Remain.Text = if ($five.ResetEpoch -gt 0) { Format-Remaining ($five.ResetEpoch - $now) } else { '' }
    } else {
        $script:ui.Txt5Pct.Text = '--'; $script:ui.Txt5Reset.Text = 'データ待ち'; $script:ui.Txt5Remain.Text = ''
    }
    Set-Meter $script:ui.Meter5Track $script:ui.Meter5Fill $five $stale $meterWidth

    $seven = Get-WindowState $snap.rate_limits.seven_day
    if ($seven.Known) {
        $script:ui.Txt7Pct.Text = "$($seven.Pct)%"
        $script:ui.Txt7Reset.Text = if ($seven.Estimated) { 'リセット済' } elseif ($seven.ResetText) { "$($seven.ResetText) リセット" } else { '' }
    } else {
        $script:ui.Txt7Pct.Text = '--'; $script:ui.Txt7Reset.Text = 'データ待ち'
    }
    Set-Meter $script:ui.Meter7Track $script:ui.Meter7Fill $seven $stale $meterWidth

    Update-ChartCanvas (@(Read-History $script:dataPath ($now - 86400)))
    Update-SessionsPanel $snap
    Update-DashboardCountdown
}

# ---- レンダリング検証モード（画面に出さずPNG保存） --------------------------
if ($RenderShot) {
    $script:lastSnap = Read-Snapshot $script:dataPath
    $window = New-Dashboard
    $window.Left = -9000; $window.Top = -9000; $window.ShowActivated = $false; $window.Topmost = $false
    $window.Show()
    Update-Dashboard
    $window.UpdateLayout()
    $root = $script:ui.RootBorder
    $w = [int][math]::Ceiling($window.ActualWidth); $h = [int][math]::Ceiling($window.ActualHeight)
    $rtb = New-Object System.Windows.Media.Imaging.RenderTargetBitmap($w, $h, 96, 96, [System.Windows.Media.PixelFormats]::Pbgra32)
    $rtb.Render($window.Content)
    $enc = New-Object System.Windows.Media.Imaging.PngBitmapEncoder
    $enc.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($rtb))
    $fs = [System.IO.File]::Create($RenderShot)
    $enc.Save($fs); $fs.Close()
    $window.Close()
    Write-Output "saved: $RenderShot ($w x $h)"
    exit 0
}

# ---- トレイ常駐本体 ---------------------------------------------------------
# 二重起動防止（スタートアップ登録と手動起動が重なった場合など）
$script:mutex = New-Object System.Threading.Mutex($false, 'Global\ClaudeUsageWidget')
try {
    if (-not $script:mutex.WaitOne(0, $false)) { exit 0 }
} catch [System.Threading.AbandonedMutexException] {
    # 前回インスタンスが強制終了された場合でも所有権はこちらに移っているので続行してよい
}

$script:readFailures = 0
$script:lastIconKey = ''
$script:prevHIcon = [IntPtr]::Zero
$script:lastFivePct = -1   # しきい値越え通知の判定用
$script:tick = 0
$script:lastHideTick = 0

function New-TrayIcon([string]$text, [string]$bgHex, [int]$pct) {
    # 塗り円（深刻度色）＋縁の白ゲージ弧＋中央に数字
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.TextRenderingHint = 'AntiAliasGridFit'
    $bgColor = [System.Drawing.ColorTranslator]::FromHtml($bgHex)
    $brush = New-Object System.Drawing.SolidBrush $bgColor
    $g.FillEllipse($brush, 0, 0, 31, 31)
    if ($pct -ge 0) {
        $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(230, 255, 255, 255)), 3
        $pen.StartCap = 'Round'; $pen.EndCap = 'Round'
        $sweep = [math]::Max(4, [math]::Min(360, $pct * 3.6))
        $g.DrawArc($pen, 2, 2, 27, 27, -90, $sweep)
        $pen.Dispose()
    }
    # 黄色地は白文字が沈むので暗色文字にする
    $inkColor = if ($bgHex -eq $Col.Warning) { [System.Drawing.ColorTranslator]::FromHtml($Col.Surface) } else { [System.Drawing.Color]::White }
    $ink = New-Object System.Drawing.SolidBrush $inkColor
    $fontSize = if ($text.Length -ge 2) { 12 } else { 15 }
    $font = New-Object System.Drawing.Font('Segoe UI', $fontSize, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $sf = New-Object System.Drawing.StringFormat
    $sf.Alignment = 'Center'; $sf.LineAlignment = 'Center'
    $rect = New-Object System.Drawing.RectangleF 0, 1, 32, 31
    $g.DrawString($text, $font, $ink, $rect, $sf)
    $g.Dispose(); $brush.Dispose(); $ink.Dispose(); $font.Dispose(); $sf.Dispose()
    $hIcon = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($hIcon)
    $bmp.Dispose()
    return @{ Icon = $icon; Handle = $hIcon }
}

function Set-TrayIcon([string]$text, [string]$bgHex, [int]$pct) {
    $key = "$text|$bgHex|$pct"
    if ($key -eq $script:lastIconKey) { return }
    $script:lastIconKey = $key
    $new = New-TrayIcon $text $bgHex $pct
    $script:notifyIcon.Icon = $new.Icon
    if ($script:prevHIcon -ne [IntPtr]::Zero) { [Native.IconUtil]::DestroyIcon($script:prevHIcon) | Out-Null }  # GDIハンドルのリーク防止
    $script:prevHIcon = $new.Handle
}

function Set-Tooltip([string]$text) {
    if ($text.Length -gt 63) { $text = $text.Substring(0, 63) }  # .NET FrameworkのNotifyIcon.Textは63文字上限
    $script:notifyIcon.Text = $text
}

function Update-Tray {
    if (-not $script:dataPath -or -not (Test-Path $script:dataPath)) {
        $script:dataPath = Find-UsageFile
    }
    $snap = Read-Snapshot $script:dataPath
    if (-not $snap) {
        $script:readFailures++
        if ($script:readFailures -ge 5) { $script:dataPath = Find-UsageFile; $script:readFailures = 0 }
        Set-TrayIcon '?' $Col.StaleGray -1
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
        Set-TrayIcon '?' $Col.StaleGray -1
        Set-Tooltip "Claude使用量: レート情報待ち`n更新 $(Format-Ago $age)"
        return
    }

    $iconText = if ($five.Pct -ge 100) { '!!' } else { [string]$five.Pct }
    Set-TrayIcon $iconText (Get-SeverityColor $five.Pct $stale) $five.Pct

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

function Show-Dashboard {
    Update-Dashboard
    $script:dashboard.UpdateLayout()
    $wa = [System.Windows.SystemParameters]::WorkArea
    $script:dashboard.Left = $wa.Right - $script:dashboard.Width - 4
    $script:dashboard.Top = $wa.Bottom - $script:dashboard.ActualHeight - 4
    $script:dashboard.Show()
    $script:dashboard.Activate()
    # SizeToContent確定後に位置を合わせ直す
    $script:dashboard.Top = $wa.Bottom - $script:dashboard.ActualHeight - 4
}

function Toggle-Dashboard {
    if ($script:dashboard.IsVisible) {
        $script:dashboard.Hide()
    } elseif (([Environment]::TickCount - $script:lastHideTick) -gt 400) {
        # フライアウトの定番バグ対策: 「外側クリックで閉じた直後の同じクリック」で再表示しない
        Show-Dashboard
    }
}

try {

$script:notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$script:dashboard = New-Dashboard
$script:dashboard.Add_Deactivated({ $script:dashboard.Hide(); $script:lastHideTick = [Environment]::TickCount })

$menu = New-Object System.Windows.Forms.ContextMenuStrip
[void]$menu.Items.Add('ダッシュボードを開く')
[void]$menu.Items.Add('usage.json を開く')
[void]$menu.Items.Add('パスを再検出')
[void]$menu.Items.Add('-')
[void]$menu.Items.Add('終了')
$menu.Items[0].Add_Click({ Show-Dashboard })
$menu.Items[1].Add_Click({ if ($script:dataPath) { Start-Process notepad.exe $script:dataPath } })
$menu.Items[2].Add_Click({ $script:dataPath = Find-UsageFile; Update-Tray })
$menu.Items[4].Add_Click({ [System.Windows.Forms.Application]::Exit() })
$script:notifyIcon.ContextMenuStrip = $menu
$script:notifyIcon.Add_MouseUp({ if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Left) { Toggle-Dashboard } })

Set-TrayIcon '…' $Col.StaleGray -1
Set-Tooltip 'Claude使用量: 起動中'
$script:notifyIcon.Visible = $true

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    try {
        $script:tick++
        if ($script:tick % [math]::Max(1, $PollSeconds) -eq 0) {
            Update-Tray
            if ($script:dashboard.IsVisible) { Update-Dashboard }
        } elseif ($script:dashboard.IsVisible) {
            Update-DashboardCountdown
        }
    } catch { }
})
$timer.Start()
Update-Tray

[System.Windows.Forms.Application]::Run()

} catch {
    # 隠しウィンドウ起動だとエラーが見えないため、原因調査用にログへ残す
    try { ($_ | Out-String) + ($_.ScriptStackTrace | Out-String) | Set-Content -Path (Join-Path $env:TEMP 'ClaudeUsageWidget.error.txt') } catch { }
} finally {
    if ($timer) { $timer.Stop() }
    if ($script:notifyIcon) { $script:notifyIcon.Visible = $false; $script:notifyIcon.Dispose() }
    if ($script:prevHIcon -ne [IntPtr]::Zero) { [Native.IconUtil]::DestroyIcon($script:prevHIcon) | Out-Null }
    try { $script:mutex.ReleaseMutex() } catch { }
}
