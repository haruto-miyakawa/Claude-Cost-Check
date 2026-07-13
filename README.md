# Claude-Cost-Check

Claude Codeの使用量（5時間枠 / 7日枠）をWindowsのタスクトレイに常駐表示するウィジェット。

**規約完全準拠**: データ源はClaude Code公式のstatusline機能が渡す `rate_limits` のみ。
OAuthトークン・セッションキー・APIポーリング・スクレイピングは一切使わない。
ネットワーク通信ゼロ、認証情報ゼロ。

## 仕組み

```
Claude Code (WSL)
  │ statusline JSON (stdin)          … rate_limits / cost / context_window
  ▼
statusline/statusline.py             … ① ターミナルにstatusline表示
  │                                    ② usage.json をアトミックに書き出し
  ▼
~/.local/share/claude-usage-widget/usage.json
  │ \\wsl.localhost\<distro>\… 経由でファイル読み取り（5秒間隔）
  ▼
widget/ClaudeUsageWidget.ps1 (Windows) … タスクトレイに5h使用率を数字で描画
```

- トレイアイコン: 5時間枠の使用率を数字＋色（緑 <50% / 黄 50–79% / 赤 ≥80% / 灰 =セッションなし）で表示
- ツールチップ: 5h/7d使用率とリセット時刻、最終更新
- ダブルクリック or 右クリック→「詳細を表示」: 直近48hのセッション別コストなど
- 5時間枠が80% / 95%を超えたらバルーン通知
- statuslineはセッション中しか更新されないため、リセット時刻を過ぎた値は「↺ 0%（推定）」として表示

## セットアップ

### 1. WSL側（収集）

`~/.claude/settings.json` に追記（設定済み）:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /home/harum1020/projects/costs-window/statusline/statusline.py",
    "refreshInterval": 30
  }
}
```

`refreshInterval: 30` により、セッションがアイドルでも30秒ごとに `updated_at` が更新され、
ウィジェット側が「Claude Code稼働中かどうか」を判定できる。

### 2. Windows側（表示）

エクスプローラーで `\\wsl.localhost\Ubuntu\home\harum1020\projects\costs-window\widget` を開き:

- **手動起動**: `ClaudeUsageWidget.ps1` を右クリック →「PowerShellで実行」
  （またはターミナルから `powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ClaudeUsageWidget.ps1`）
- **自動起動の登録**: `powershell -NoProfile -ExecutionPolicy Bypass -File install-autostart.ps1`
  （解除は `-Uninstall` を付ける）

追加インストール不要（Windows PowerShell 5.1 / .NET Framework標準機能のみ）。

## 動作確認

```bash
# WSL側: 収集スクリプトのテスト
tests/test.sh

# Windows側: GUIなしの疎通確認（WSLから実行可）
cd widget && powershell.exe -NoProfile -ExecutionPolicy Bypass -File ClaudeUsageWidget.ps1 -SelfTest
```

## usage.json のスキーマ（schema: 1）

```json
{
  "schema": 1,
  "updated_at": 1783940836,
  "updated_by": "<最後に書いたsession_id>",
  "rate_limits": {
    "five_hour":  { "used_percentage": 75, "resets_at": 1783942800, "observed_at": 1783940836 },
    "seven_day":  { "used_percentage": 17, "resets_at": 1784494800, "observed_at": 1783940836 }
  },
  "sessions": {
    "<session_id>": { "updated_at": 0, "model": "", "cost_usd": 0, "context_used_percentage": 0, "cwd": "" }
  }
}
```

- `rate_limits` はアカウント全体の値なので、複数セッション並行時は最後に書いた値が常に最新
- `five_hour` / `seven_day` はstatusline入力で独立に欠落しうるため、欠落時は前回値を保持し
  `observed_at` で観測時刻を区別する
- `sessions` は48時間より古いものを自動で間引く

## 制約・既知の挙動

- `rate_limits` はPro/Max加入者のみ・セッション初回API応答後に出現
- statuslineはClaude Codeセッション中しか動かない → セッションを閉じると値は止まる
  （ウィジェットは灰色アイコン＋「セッションなし」表示で区別）
- statuslineのJSON仕様が変わったら `statusline/statusline.py` を追従させる
  （仕様: https://code.claude.com/docs/en/statusline.md ）

## 将来の拡張（未着手）

- `~/.claude/projects/**/*.jsonl` のローカル解析による履歴・プロジェクト別コスト集計（ccusage方式・第2段階）
- ウィジェットの見た目強化が必要になったら .NET/WPF or Tauri へ移行（現状のPowerShell版はv0）
