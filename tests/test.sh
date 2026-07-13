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

echo "--- 3. 不正入力: クラッシュせず1行出力する"
OUT=$(echo "not json" | python3 "$SCRIPT")
[ -n "$OUT" ] || fail "不正入力で出力が空"
echo "OK: $OUT"

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

echo ""
echo "全テスト通過"
