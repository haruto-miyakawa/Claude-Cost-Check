# install-autostart.ps1 — サインイン時にウィジェットを自動起動するショートカットを登録する
# 実行: powershell -NoProfile -ExecutionPolicy Bypass -File install-autostart.ps1
#       解除は -Uninstall を付けて実行
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$linkPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'ClaudeUsageWidget.lnk'

if ($Uninstall) {
    if (Test-Path $linkPath) { Remove-Item $linkPath; Write-Output "削除しました: $linkPath" }
    else { Write-Output '登録されていません。' }
    exit 0
}

$widgetPath = Join-Path $PSScriptRoot 'ClaudeUsageWidget.ps1'
if (-not (Test-Path $widgetPath)) { throw "ClaudeUsageWidget.ps1 が見つかりません: $widgetPath" }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = 'powershell.exe'
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$widgetPath`""
$shortcut.Description = 'Claude Code使用量ウィジェット'
$shortcut.Save()
Write-Output "登録しました: $linkPath"
Write-Output '次回サインインから自動起動します。今すぐ起動する場合はショートカットをダブルクリックしてください。'
