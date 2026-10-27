#!/usr/bin/env bash
# statusline.py の動作確認。書き出し先を一時ディレクトリに向けて実行する。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_DIR/statusline/statusline.py"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
export CLAUDE_USAGE_WIDGET_DIR="$WORK_DIR"

NOW=$(date +%s)
fail() { echo "NG: $1" >&2; exit 1; }

# resets_at を未来時刻に差し替えたサンプル入力を作る
python3 - "$REPO_DIR/tests/sample-input.json" "$NOW" > "$WORK_DIR/input.json" <<'EOF'
import json, sys
data = json.load(open(sys.argv[1]))
now = int(sys.argv[2])
data["rate_limits"]["five_hour"]["resets_at"] = now + 3 * 3600
data["rate_limits"]["seven_day"]["resets_at"] = now + 5 * 24 * 3600
print(json.dumps(data))
EOF

echo "--- 1. 通常入力: statusline出力とスナップショット生成"
OUT=$(python3 "$SCRIPT" < "$WORK_DIR/input.json")
echo "$OUT"
echo "$OUT" | grep -q "Fable 5" || fail "モデル名が表示されていない"
echo "$OUT" | grep -q "5h ▰▰▱▱▱" || fail "5hバーが表示されていない (40%→2/5)"
echo "$OUT" | grep -q "40%" || fail "5h使用率が表示されていない (割合の丸め含む)"
echo "$OUT" | grep -q "7d ▰▱▱▱▱" || fail "7dバーが表示されていない (12%→1/5)"
echo "$OUT" | grep -q '\$' && fail "コストがstatuslineに表示されている（ダッシュボード専用のはず）"
[ -f "$WORK_DIR/usage.json" ] || fail "usage.json が生成されていない"
[ -f "$WORK_DIR/history.jsonl" ] || fail "history.jsonl が生成されていない"
[ "$(wc -l < "$WORK_DIR/history.jsonl")" = "1" ] || fail "履歴が1行でない"

python3 - "$WORK_DIR/usage.json" <<'EOF'
import json, sys
snap = json.load(open(sys.argv[1]))
assert snap["schema"] == 1
assert snap["rate_limits"]["five_hour"]["used_percentage"] == 40.5
assert snap["rate_limits"]["five_hour"]["observed_at"] > 0
assert snap["sessions"]["test-session-0001"]["cost_usd"] == 3.21
assert snap["sessions"]["test-session-0001"]["model"] == "Fable 5"
EOF
echo "OK"

echo "--- 2. rate_limits欠落入力: 前回値を保持する"
OUT=$(python3 "$SCRIPT" <<'EOF'
{"session_id": "test-session-0002", "model": {"display_name": "Fable 5"}, "context_window": {"used_percentage": null}}
EOF
)
echo "$OUT"
echo "$OUT" | grep -q "40%" || fail "欠落時に前回の5h値が保持されていない"
[ "$(wc -l < "$WORK_DIR/history.jsonl")" = "1" ] || fail "5分未満の再実行で履歴が増えた（間隔ゲートが効いていない）"
python3 - "$WORK_DIR/usage.json" <<'EOF'
import json, sys
snap = json.load(open(sys.argv[1]))
assert snap["rate_limits"]["five_hour"]["used_percentage"] == 40.5, "前回値が消えた"
assert len(snap["sessions"]) == 2, "セッションが統合されていない"
EOF
echo "OK"

echo "--- 2b. コスト台帳: 差分積算とサブスク/API別勘定"
python3 - "$WORK_DIR/usage.json" <<'EOF'
import json, sys
snap = json.load(open(sys.argv[1]))
assert snap["sessions"]["test-session-0001"]["subscription"] is True, "rate_limitsありのセッションがサブスク判定されていない"
EOF
# 同一セッションでコスト増(3.21→5.00) → 差分1.79が加算され日合計5.00になる
python3 - "$WORK_DIR/input.json" <<'EOF' | python3 "$SCRIPT" > /dev/null
import json, sys
d = json.load(open(sys.argv[1]))
d["cost"]["total_cost_usd"] = 5.00
print(json.dumps(d))
EOF
# rate_limitsなし＋コストあり（=API従量課金セッション相当）
echo '{"session_id": "api-session", "cwd": "/home/u/proj", "cost": {"total_cost_usd": 0.50}, "model": {"display_name": "Fable 5"}}' | python3 "$SCRIPT" > /dev/null
python3 - "$WORK_DIR/cost-ledger.json" <<'EOF'
import json, sys, time
ledger = json.load(open(sys.argv[1]))
day = time.strftime("%Y-%m-%d")
totals = ledger["days"][day]
assert abs(totals["subscription"] - 5.00) < 1e-6, f"サブスク日合計が5.00でない: {totals}"
assert abs(totals["api"] - 0.50) < 1e-6, f"API日合計が0.50でない: {totals}"
assert "test-session-0001" in ledger["sessions"] and "api-session" in ledger["sessions"]
EOF
python3 - "$WORK_DIR/usage.json" <<'EOF'
import json, sys
snap = json.load(open(sys.argv[1]))
assert snap["sessions"]["api-session"].get("subscription") is False, "rate_limitsなしセッションがサブスク判定されている"
EOF
echo "OK"

echo "--- 2c. ホスト判定: Windows側セッションを windows として記録し、台帳もホスト別に分ける"
echo '{"session_id": "win-session", "cwd": "E:\\sample-project", "cost": {"total_cost_usd": 2.00}, "model": {"display_name": "Opus 5"}, "rate_limits": {"five_hour": {"used_percentage": 9, "resets_at": 9999999999}}}' | python3 "$SCRIPT" > /dev/null
python3 - "$WORK_DIR/usage.json" "$WORK_DIR/cost-ledger.json" <<'EOF'
import json, sys, time
snap = json.load(open(sys.argv[1]))
assert snap["sessions"]["win-session"]["host"] == "windows", "Windowsパスのcwdがwindows判定されていない"
assert snap["sessions"]["test-session-0001"]["host"] == "wsl", "WSLパスのcwdがwsl判定されていない"
ledger = json.load(open(sys.argv[2]))
by_host = ledger["days"][time.strftime("%Y-%m-%d")]["by_host"]
assert abs(by_host["windows"]["subscription"] - 2.00) < 1e-6, f"Windows側ぶんが台帳に分離されていない: {by_host}"
assert abs(by_host["wsl"]["subscription"] - 5.00) < 1e-6, f"WSL側ぶんが台帳に分離されていない: {by_host}"
assert abs(by_host["wsl"]["api"] - 0.50) < 1e-6, f"WSL側のAPI勘定が分離されていない: {by_host}"
EOF
echo "OK"

echo "--- 3. 不正入力: クラッシュせず1行出力する"
OUT=$(echo "not json" | python3 "$SCRIPT")
[ -n "$OUT" ] || fail "不正入力で出力が空"
echo "OK: $OUT"

echo "--- 3b. 失敗を黙殺しない: statusline.err に理由が残り、空入力では上書きしない"
[ -f "$WORK_DIR/statusline.err" ] || fail "不正入力なのに statusline.err が作られていない"
grep -q "JSONとして読めない" "$WORK_DIR/statusline.err" || fail "不正入力の理由が記録されていない"
BEFORE=$(cat "$WORK_DIR/usage.json")
OUT=$(printf '' | python3 "$SCRIPT")
[ -n "$OUT" ] || fail "空入力で出力が空"
grep -q "stdinが空" "$WORK_DIR/statusline.err" || fail "空入力の理由が記録されていない"
[ "$BEFORE" = "$(cat "$WORK_DIR/usage.json")" ] || fail "空入力で usage.json が上書きされた（計上漏れを隠す）"
echo '{"cwd": "/tmp"}' | python3 "$SCRIPT" > /dev/null
grep -q "session_idが無い" "$WORK_DIR/statusline.err" || fail "session_id欠落が記録されていない"
echo "OK"

echo "--- 4. リセット時刻超過: ↺0% 表示になる"
python3 - "$WORK_DIR/usage.json" <<'EOF'
import json, sys
path = sys.argv[1]
snap = json.load(open(path))
snap["rate_limits"]["five_hour"]["resets_at"] = 1000  # 過去
json.dump(snap, open(path, "w"))
EOF
OUT=$(echo '{"session_id": "test-session-0001"}' | python3 "$SCRIPT")
echo "$OUT"
echo "$OUT" | grep -q "↺0%" || fail "リセット超過の表示がない"
echo "OK"

echo "--- 5. ダッシュボード: ホスト別集計と計上漏れの検出"
# Windows側のトランスクリプトを模したファイルを作る（台帳には存在しない日付にする）
FAKE="$WORK_DIR/fake/E--sample-project"
mkdir -p "$FAKE"
python3 - "$FAKE/win-sess.jsonl" <<'EOF'
import json, sys
rec = {
    "type": "assistant", "cwd": "E:\\sample-project", "sessionId": "win-sess",
    "timestamp": "2020-01-02T03:04:05Z", "requestId": "req-1",
    "message": {"model": "claude-opus-5", "usage": {
        "input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 300}},
}
with open(sys.argv[1], "w") as f:
    f.write(json.dumps(rec) + "\n")
    rec["requestId"] = "req-2"
    f.write(json.dumps(rec) + "\n")
EOF
CLAUDE_USAGE_TRANSCRIPT_GLOBS="$WORK_DIR/fake/*/*.jsonl" \
  python3 "$REPO_DIR/dashboard/build.py" -o "$WORK_DIR/dash.html" --quiet
python3 - "$WORK_DIR/dash.html" <<'EOF'
import json, re, sys
html = open(sys.argv[1], encoding="utf-8").read()
raw = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S).group(1)
D = json.loads(raw.replace("<\\/", "</"))
hosts = {h["host"]: h for h in D["health"]["hosts"]}
assert "windows" in hosts, f"Windows側がホスト別集計に出ていない: {list(hosts)}"
assert hosts["windows"]["messages"] == 2, f"応答数が合わない: {hosts['windows']}"
assert hosts["windows"]["cost"] > 0, "推定コストが0"
# 台帳側は 2c で積んだWindows側ぶん($2.00)だけ。ホストをまたいで混ざっていないことの確認
assert abs(hosts["windows"]["ledger_cost"] - 2.00) < 1e-6, \
    f"台帳のホスト別突き合わせが合わない: {hosts['windows']}"
gaps = [g for g in D["health"]["gaps"] if g["day"] == "2020-01-02" and g["host"] == "windows"]
assert gaps, f"計上漏れが検出されていない: {D['health']['gaps']}"
assert gaps[0]["messages"] == 2, f"計上漏れの応答数が合わない: {gaps[0]}"
day = [r for r in D["ledger"] if r["day"] == "2020-01-02"]
assert day and day[0]["estimated"] > 0 and day[0]["subscription"] == 0, \
    f"台帳に無い日がグラフから落ちている（計上漏れが見えなくなる）: {day}"
EOF
echo "OK"

echo "--- 6. ダッシュボード: モデル別の値段と、値段の表に無いモデルの検出"
PRICED="$WORK_DIR/priced/sample-project"
mkdir -p "$PRICED"
python3 - "$PRICED/sess.jsonl" <<'EOF'
import json, sys
def rec(req, model, **usage):
    return {
        "type": "assistant", "cwd": "/tmp/sample-project", "sessionId": "priced-sess",
        "timestamp": "2020-01-03T03:04:05Z", "requestId": req,
        "message": {"model": model, "usage": usage},
    }
rows = [
    # Opus 5.5: キャッシュ読込は入力単価の0.05倍 → 4.00 + 0.20
    rec("r1", "claude-opus-5-5", input_tokens=1_000_000, cache_read_input_tokens=1_000_000),
    # 日付つきのID は表のキー（claude-haiku-4-5）にそろえる → 1.00
    rec("r2", "claude-haiku-4-5-20251001", input_tokens=1_000_000),
    # Haiku 5.5: 入力の合計が10万以下は 0.10/MTok → 0.005
    rec("r3", "claude-haiku-5-5", input_tokens=50_000),
    # 10万を超えると 0.50/MTok（キャッシュ読込も長さに入る）→ 0.025 + 0.003
    rec("r4", "claude-haiku-5-5", input_tokens=50_000, cache_read_input_tokens=60_000),
    # 表に無いモデル → 0 のまま、健全性カードに出る
    rec("r5", "claude-new-model-9", input_tokens=1_000_000),
]
with open(sys.argv[1], "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
EOF
check_priced() {
python3 - "$WORK_DIR/dash6.html" <<'EOF'
import json, re, sys
html = open(sys.argv[1], encoding="utf-8").read()
raw = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S).group(1)
D = json.loads(raw.replace("<\\/", "</"))
models = {m["name"]: m for m in D["models"]}
def near(a, b):
    return abs(a - b) < 1e-9
assert near(models["Opus 5.5"]["cost"], 4.20), f"Opus 5.5 の値段が合わない: {models['Opus 5.5']}"
assert near(models["Haiku 4.5"]["cost"], 1.00), f"日付つきのIDが表に当たっていない: {list(models)}"
assert near(models["Haiku 5.5"]["cost"], 0.005 + 0.028), f"Haiku 5.5 の長さの値段が合わない: {models['Haiku 5.5']}"
assert models["claude-new-model-9"]["cost"] == 0, "表に無いモデルに値段が付いている"
unpriced = {u["model"]: u["messages"] for u in D["health"]["unpriced"]}
assert unpriced == {"claude-new-model-9": 1}, f"表に無いモデルが健全性カードに出ていない: {unpriced}"
EOF
}
CLAUDE_USAGE_TRANSCRIPT_GLOBS="$WORK_DIR/priced/*/*.jsonl" \
  python3 "$REPO_DIR/dashboard/build.py" -o "$WORK_DIR/dash6.html" --quiet
check_priced
# 古い値段の表で数えたキャッシュは、元ファイルがあれば読み直される（PRICING_VERSION）
python3 - "$WORK_DIR/transcript-cache.json" <<'EOF'
import json, sys
path = sys.argv[1]
cache = json.load(open(path))
for entry in cache["files"].values():
    if entry["agg"]["model"].get("claude-opus-5-5"):
        entry["pricing"] = 1
        entry["agg"]["model"]["claude-opus-5-5"]["cost"] = 0
json.dump(cache, open(path, "w"))
EOF
CLAUDE_USAGE_TRANSCRIPT_GLOBS="$WORK_DIR/priced/*/*.jsonl" \
  python3 "$REPO_DIR/dashboard/build.py" -o "$WORK_DIR/dash6.html" --quiet
check_priced
echo "OK"

echo "--- 6b. 元ファイルが消えた古い集計: 0円のまま残る分を知らせる"
GONE="$WORK_DIR/gone/sample-project"
mkdir -p "$GONE"
python3 - "$GONE/old.jsonl" <<'EOF'
import json, sys
rec = {
    "type": "assistant", "cwd": "/tmp/sample-project", "sessionId": "gone-sess",
    "timestamp": "2020-01-05T03:04:05Z", "requestId": "g1",
    "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 1_000_000}},
}
with open(sys.argv[1], "w") as f:
    f.write(json.dumps(rec) + "\n")
EOF
CLAUDE_USAGE_TRANSCRIPT_GLOBS="$WORK_DIR/gone/*/*.jsonl" \
  python3 "$REPO_DIR/dashboard/build.py" -o "$WORK_DIR/dash6b.html" --quiet
# 値段の表の版が無かったころのキャッシュを再現する（pricing キーなし・日付つきID・0円）
python3 - "$WORK_DIR/transcript-cache.json" "$GONE/old.jsonl" <<'EOF'
import json, os, sys
path, target = sys.argv[1], os.path.realpath(sys.argv[2])
cache = json.load(open(path))
entry = cache["files"][target]
del entry["pricing"]
bucket = entry["agg"]["model"].pop("claude-opus-5-5")
bucket["cost"] = 0
entry["agg"]["model"]["claude-opus-5-5-20260101"] = bucket
entry["agg"].pop("unpriced", None)
json.dump(cache, open(path, "w"))
EOF
rm "$GONE/old.jsonl"
CLAUDE_USAGE_TRANSCRIPT_GLOBS="$WORK_DIR/gone/*/*.jsonl" \
  python3 "$REPO_DIR/dashboard/build.py" -o "$WORK_DIR/dash6b.html" --quiet
python3 - "$WORK_DIR/dash6b.html" <<'EOF'
import json, re, sys
html = open(sys.argv[1], encoding="utf-8").read()
raw = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S).group(1)
D = json.loads(raw.replace("<\\/", "</"))
stale = {u["model"]: u["messages"] for u in D["health"]["stale_unpriced"]}
assert stale.get("claude-opus-5-5") == 1, f"消えたファイルの0円の分が知らされていない: {stale}"
names = [m["name"] for m in D["models"]]
assert "claude-opus-5-5-20260101" not in names, f"古いキャッシュの日付つきIDが別の行になった: {names}"
EOF
echo "OK"

echo "--- 7. ダッシュボード: サブエージェントの記録（下のフォルダ）も数える"
SUB="$WORK_DIR/nested/sample-project/parent-sess/subagents"
mkdir -p "$SUB"
python3 - "$WORK_DIR/nested/sample-project/parent-sess.jsonl" "$SUB/agent-1.jsonl" <<'EOF'
import json, sys
for path, req in ((sys.argv[1], "main-1"), (sys.argv[2], "sub-1")):
    rec = {
        "type": "assistant", "cwd": "/tmp/sample-project", "sessionId": "parent-sess",
        "timestamp": "2020-01-04T03:04:05Z", "requestId": req,
        "message": {"model": "claude-sonnet-5-5", "usage": {"input_tokens": 1_000_000}},
    }
    with open(path, "w") as f:
        f.write(json.dumps(rec) + "\n")
EOF
CLAUDE_USAGE_TRANSCRIPT_GLOBS="$WORK_DIR/nested/**/*.jsonl" \
  python3 "$REPO_DIR/dashboard/build.py" -o "$WORK_DIR/dash7.html" --quiet
python3 - "$WORK_DIR/dash7.html" <<'EOF'
import json, re, sys
html = open(sys.argv[1], encoding="utf-8").read()
raw = re.search(r'<script id="payload" type="application/json">(.*?)</script>', html, re.S).group(1)
D = json.loads(raw.replace("<\\/", "</"))
day = [d for d in D["days"] if d["day"] == "2020-01-04"]
assert day and day[0]["messages"] == 2, f"サブエージェントの記録が数えられていない: {day}"
assert abs(day[0]["cost"] - 4.00) < 1e-9, f"サブエージェント込みの値段が合わない: {day}"
EOF
echo "OK"

echo ""
echo "全テスト通過"
