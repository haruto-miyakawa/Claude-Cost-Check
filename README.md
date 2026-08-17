# Claude-Cost-Check

Claude Codeの使用量（5時間枠 / 7日枠）をWindowsのタスクトレイに常駐表示するウィジェット。
トレイアイコンをクリックするとダッシュボードが開く。

![ダッシュボード](docs/dashboard.png)

**規約完全準拠**: データ源はClaude Code公式のstatusline機能が渡す `rate_limits` のみ。
OAuthトークン・セッションキー・APIポーリング・スクレイピングは一切使わない。
ネットワーク通信ゼロ、認証情報ゼロ。

## 仕組み

```
Claude Code (WSL)
  │ statusline JSON (stdin)          … rate_limits / cost / context_window
  ▼
statusline/statusline.py             … ① ターミナルにstatusline表示（バー付き）
  │                                    ② usage.json をアトミックに書き出し
  │                                    ③ history.jsonl に5分間隔で使用率を記録
  ▼
~/.local/share/claude-usage-widget/{usage.json, history.jsonl}
  │ \\wsl.localhost\<distro>\… 経由でファイル読み取り（5秒間隔）
  ▼
widget/ClaudeUsageWidget.ps1 (Windows) … トレイアイコン＋WPFダッシュボード
```

### トレイアイコン

- 5時間枠の使用率を数字＋深刻度色（青 <50% / 黄 50–79% / 赤 ≥80% / 灰 =セッションなし）＋縁のゲージ弧で表示
- ホバーで5h/7d使用率とリセット時刻のツールチップ
- 5時間枠が80% / 95%を超えたらバルーン通知

### ダッシュボード（トレイアイコン左クリック / ダブルクリック）

- **5時間枠**: 大きな使用率表示＋リセットまでのライブカウントダウン＋深刻度色メーター
- **7日間枠**: 使用率＋リセット日時＋メーター
- **直近24時間の使用率推移**: statusline側が5分間隔で記録した履歴のエリアチャート
- **サブスクのお得分**: 今月/今日の使用量をAPI従量課金の価格に換算した「お得した額」。サブスク利用分と実際のAPI従量課金の実費は**最初から別勘定**で集計され（判定: statusline入力に `rate_limits` が入るのはサブスクのみ）、実費が発生した月だけ「API実費」行が別に表示される
- **セッション一覧（直近48h)**: プロジェクト名・稼働状態・コスト。API従量課金のセッションには「API実費」マーカー
- フォーカスが外れると自動で閉じる（Windowsの音量フライアウトと同じ挙動）
- statuslineはセッション中しか更新されないため、リセット時刻を過ぎた値は「0%（推定）」として表示

### ターミナルのstatusline

```
Fable 5 │ ctx 8% │ 5h ▰▰▰▱▱ 62% →20:40 │ 7d ▰▱▱▱▱ 17% →7/18 21:26
```

視認性のためブロックバー＋太字%表示。コスト（API換算$）は誤解を招きやすいのでstatuslineには出さず、ダッシュボード側に注記付きで表示する。

### 詳細ダッシュボード（ローカルHTML・`dashboard/build.py`）

トレイのダッシュボードが「今の状態」を見るものなのに対し、こちらは**溜まったデータを掘る**ための1枚もの。

開き方は3通り（中身はどれも自動で最新）:

1. **トレイアイコンを右クリック → 「詳細ダッシュボード（ブラウザ）」**（通常はこれ）
2. Windowsから直接 `\\wsl.localhost\Ubuntu\home\<user>\projects\costs-window\dashboard.html` — ブックマークやショートカットにできる
3. WSLから `explorer.exe dashboard.html`

```bash
python3 dashboard/build.py          # 手動で作り直したいとき（2回目以降は0.1秒程度）
```

**生成は自動**。cronもタスクスケジューラも登録しない。集計元が増えるのはClaude Codeが動いている間だけで、statuslineはまさにその間だけ走るので、statuslineに相乗りして5分に1回だけ裏で作り直す（`maybe_rebuild_dashboard`）。セッションが無い間はデータが変わらないので、作り直す必要もない。statuslineの描画は待たせない（切り離した子プロセスに投げっぱなし・実測32ms）。止めたいときは環境変数 `CLAUDE_USAGE_DASHBOARD_AUTOBUILD=0`。

スナップショット3種に加えて **Claude Code のトランスクリプト**（`~/.claude/projects/**/*.jsonl`、WSL側とWindows側の両方）を集計し、次を表示する:

- **計上の健全性**（後述）、5h/7d の円ゲージ、使用率の推移（全履歴）、日別コストの積み上げ棒
- プロジェクト別・モデル別のトークンとコスト推定、ツール使用回数
- キャッシュ効率（入力系トークンのうちキャッシュ読込の割合）、曜日×時刻のヒートマップ

実装上の要点:

- **1回のAPI応答は複数のassistantレコードに分かれ、各レコードが同じ `usage` を持つ**。レコード単位で足すとトークンもコストも数倍になるため、`requestId` 単位で1度だけ計上する（ツール呼び出しだけはレコードごとに中身が違うので全件から拾う）。この処理を入れた状態で、推定コストは Claude Code 自身が報告する実コストと**24日間で誤差2.6%**に収まる
- **Claude Code は古いトランスクリプトを定期的に削除する**（実測あり）。`~/.local/share/claude-usage-widget/transcript-cache.json` にファイル単位の集計を残すことで、元ファイルが消えても過去の集計は保持される。mtime/sizeが変わらないファイルは再読み込みしない
- コスト推定は公式の従量課金レート（キャッシュ読込 0.1倍・書込 1.25〜2倍、TTL別に判定）による目安で、実際の請求額ではない
- 生成物は**プロジェクト名や作業ディレクトリのパスを含む**ため `.gitignore` 済み。外部に公開しないこと

#### 計上の健全性カード

データ源が2系統（statusline由来の台帳 / トランスクリプト実測）ある以上、**片方だけ壊れても数字は出続ける**。
実際に2026-08-14〜08-17、Windows側のstatuslineが起動すらしていないのに、トランスクリプト側は
正常に集計され続けていたため3日間気づけなかった。同じことを繰り返さないためのカード。

- ホスト（Windows側 / WSL側）ごとに、トランスクリプト実測と台帳の記録額を並べる。片方の「台帳の記録」が
  0のままなら、そのホストのstatuslineが動いていない
- **トランスクリプトに応答があるのに台帳に記録が無い日**を列挙する（`cost-ledger.json` の
  `days.<日>.by_host` と突き合わせ）。`by_host` を持たない2026-08-16以前の日はホスト別に判定できないので対象外
- `statusline.err`（後述）に記録があれば、末尾を直接表示する
- 日別コストのグラフには、同じ日をトランスクリプトから推定した額を破線で重ねる。台帳の棒だけが低い日＝計上漏れ

台帳（Claude Codeの報告値）と推定（トークンからの計算）は**別種の数字**なので、欠測日を推定値で埋める
遡及補正はしない。欠けたことが分かる形で残すほうが誤解が少ない。

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

### 2. Windowsネイティブ側（収集）

Windowsで直接動かすClaude Code（`E:\...` などで作業するセッション）も同じWSL側スクリプトに集約する。
`C:\Users\<user>\.claude\settings.json` に追記:

```json
{
  "statusLine": {
    "type": "command",
    "command": "wsl.exe -e python3 //home/harum1020/projects/costs-window/statusline/statusline.py",
    "refreshInterval": 30
  }
}
```

**パス先頭のスラッシュは2本**。ここが1本だと動かない:

Windows版Claude Codeはstatuslineコマンドを**Git Bash経由**で実行する。Git Bash(MSYS2)は、
ネイティブexe（`wsl.exe`）へ渡す引数のうち `/` で始まるものを勝手にWindowsパスへ変換するため、
`/home/harum1020/...` は `C:/Program Files/Git/home/harum1020/...` に化ける。結果、
`python3: can't open file` で終了コード2になるが、**Claude Codeはstatuslineの失敗を画面に出さない**ので、
設定は入っているのに一切収集されない状態が静かに続く（2026-08-14〜08-17に実際に発生）。

先頭を `//` にするとMSYS2は変換せずそのまま渡し、Linux側では `//home/...` が `/home/...` と同じものを指す。
cmd.exe / PowerShell / Git Bash のどれで起動されても同じように動く形（3シェルで実測確認済み）。

うまく動いているかは、詳細ダッシュボードの**「計上の健全性」カード**で確認できる（後述）。

### 3. Windows側（表示）

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

# Windows側: ダッシュボードを画面に出さずPNGにレンダリング（デザイン確認用）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ClaudeUsageWidget.ps1 -RenderShot 'C:\path\to\out.png'
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
    "<session_id>": {
      "updated_at": 0, "model": "", "cost_usd": 0, "context_used_percentage": 0,
      "cwd": "", "host": "windows | wsl", "subscription": true
    }
  }
}
```

- `rate_limits` はアカウント全体の値なので、複数セッション並行時は最後に書いた値が常に最新
- `five_hour` / `seven_day` はstatusline入力で独立に欠落しうるため、欠落時は前回値を保持し
  `observed_at` で観測時刻を区別する
- `sessions` は48時間より古いものを自動で間引く。各セッションの `subscription` は
  rate_limitsを一度でも観測したらtrue（サブスクセッション確定）
- `host` は `cwd` から判定（`C:\...` / `\\...` ならwindows、それ以外はwsl）。2026-08-17からの追加

### cost-ledger.json（日別コスト台帳）

```json
{
  "schema": 1,
  "sessions": { "<session_id>": { "last_cost": 4.12, "updated_at": 0 } },
  "days": {
    "2026-07-13": {
      "subscription": 12.40, "api": 0.50,
      "by_host": { "wsl": { "subscription": 10.00 }, "windows": { "subscription": 2.40, "api": 0.50 } }
    }
  }
}
```

- セッションのコストは累積値なので、前回値との**差分**だけをその日の合計に加算（日またぎでも二重計上しない）
- 区分は入力に `rate_limits` が含まれるかで判定（コストが増えるAPI応答後の入力には、サブスクなら必ず同時に含まれる）
- `by_host` は2026-08-17からの追加。合計だけだと、片方のホストが丸ごと計上漏れしていても
  「その日は作業が少なかった」と区別がつかないため
- 集計は導入日以降のみ。`days` は400日分保持

### statusline.err（失敗の記録）

statuslineが想定外に遭遇したら、`~/.local/share/claude-usage-widget/statusline.err` に追記する
（statuslineの表示自体は壊さない）。Claude Codeは statusline の stderr も終了コードも画面に出さないため、
**ここが唯一の痕跡**になる。64KBを超えたら古い方から捨てる。

```
2026-08-17T19:44:02 [stdin] stdinが空（Claude CodeからのJSONが届いていない） (pid=1234 cwd=/... python=/usr/bin/python3)
```

記録するのは「stdinが読めない/空/JSONでない/session_idが無い」「usage.json・history.jsonl・cost-ledger.json の
書き出し失敗」「ダッシュボード再生成の起動失敗」「main内の未捕捉例外」。
中身は詳細ダッシュボードの「計上の健全性」カードにも出るので、普段は開く必要はない。

なお**スクリプトが起動すらしない場合はここにも残らない**（起動経路の設定ミスがこれ）。その場合は
健全性カードの「台帳の記録なし」で気づく形になる。

## 制約・既知の挙動

- `rate_limits` はPro/Max加入者のみ・セッション初回API応答後に出現
- statuslineはClaude Codeセッション中しか動かない → セッションを閉じると値は止まる
  （ウィジェットは灰色アイコン＋「セッションなし」表示で区別）
- statuslineのJSON仕様が変わったら `statusline/statusline.py` を追従させる
  （仕様: https://code.claude.com/docs/en/statusline.md ）
- **Claude Codeはstatuslineの失敗を一切表示しない**（非ゼロ終了もstderrも黙殺）。設定を変えたら
  「計上の健全性」カードで実際に記録されているかを必ず確認すること
- 台帳（`cost-ledger.json`）は導入日以降の実測のみで、遡って埋めることはできない。
  Windows側は2026-08-17から計上開始（08-14〜08-16ぶんはトランスクリプト推定でしか見られない）

## 将来の拡張（未着手）

- `~/.claude/projects/**/*.jsonl` のローカル解析による履歴・プロジェクト別コスト集計（ccusage方式・第2段階）
- ウィジェットの見た目強化が必要になったら .NET/WPF or Tauri へ移行（現状のPowerShell版はv0）

## ライセンス

MIT License（[LICENSE](LICENSE) 参照）
