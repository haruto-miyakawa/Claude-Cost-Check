#!/usr/bin/env python3
"""Claude Code statusline + 使用量スナップショット書き出し（claude-usage-widget WSL側）。

Claude Codeがstdinで渡すJSON（rate_limits / cost / context_window など）を
  1) ~/.local/share/claude-usage-widget/usage.json にアトミックに書き出し（Windows側ウィジェットが読む）
  2) ターミナル用のstatusline 1行として整形してstdoutへ出力
する。stdlibのみ・ネットワーク通信なし・認証情報なし。

仕様メモ:
- rate_limits はPro/Max加入者のみ・セッション初回API応答後に出現。five_hour/seven_day は
  独立に欠落しうるので、欠落時は前回スナップショットの値を保持する（observed_at で鮮度を区別）。
- statuslineは失敗してもClaude CodeのUIを壊さないよう、例外時は最低限の行を出して正常終了する。
"""

import json
import os
import sys
import tempfile
import time

DATA_DIR = os.environ.get("CLAUDE_USAGE_WIDGET_DIR") or os.path.expanduser(
    "~/.local/share/claude-usage-widget"
)
DATA_FILE = os.path.join(DATA_DIR, "usage.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
LEDGER_FILE = os.path.join(DATA_DIR, "cost-ledger.json")
SESSION_TTL_SEC = 48 * 3600  # これより古いセッション記録はスナップショットから間引く
LEDGER_KEEP_DAYS = 400
HISTORY_INTERVAL_SEC = 300  # 履歴サンプリング間隔（ダッシュボードの推移グラフ用）
HISTORY_KEEP_SEC = 7 * 24 * 3600
HISTORY_PRUNE_SIZE = 256 * 1024  # このサイズを超えたら古い行を間引く

RESET = "\033[0m"
DIM = "\033[2m"
BOLD_CYAN = "\033[1;36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def load_previous():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            prev = json.load(f)
        return prev if isinstance(prev, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path, obj):
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp_path, path)  # 読み手が中途半端なJSONを見ないようアトミックに置換
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def build_snapshot(data, prev, now):
    sessions = {
        sid: s
        for sid, s in (prev.get("sessions") or {}).items()
        if isinstance(s, dict) and now - s.get("updated_at", 0) < SESSION_TTL_SEC
    }
    snapshot = {
        "schema": 1,
        "updated_at": now,
        "updated_by": data.get("session_id"),
        "rate_limits": dict(prev.get("rate_limits") or {}),
        "sessions": sessions,
    }

    incoming = data.get("rate_limits") or {}
    for window in ("five_hour", "seven_day"):
        value = incoming.get(window)
        if isinstance(value, dict) and value.get("used_percentage") is not None:
            snapshot["rate_limits"][window] = {
                "used_percentage": value.get("used_percentage"),
                "resets_at": value.get("resets_at"),
                "observed_at": now,
            }

    sid = data.get("session_id")
    if sid:
        prev_session = (prev.get("sessions") or {}).get(sid) or {}
        snapshot["sessions"][sid] = {
            "updated_at": now,
            "model": (data.get("model") or {}).get("display_name"),
            "cost_usd": (data.get("cost") or {}).get("total_cost_usd"),
            "context_used_percentage": (data.get("context_window") or {}).get(
                "used_percentage"
            ),
            "cwd": data.get("cwd"),
            # rate_limitsはサブスク(Pro/Max)にしか来ない → 一度でも見えたらサブスクセッション確定
            "subscription": bool(incoming) or bool(prev_session.get("subscription")),
        }
    return snapshot


def last_history_time():
    try:
        with open(HISTORY_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256))
            lines = f.read().decode("utf-8", "ignore").strip().splitlines()
        return json.loads(lines[-1]).get("t", 0) if lines else 0
    except (OSError, ValueError, IndexError):
        return 0


def append_history(snapshot, now):
    """推移グラフ用に5分間隔で使用率を記録する（セッション並行時も間隔ゲートで重複しない）"""
    limits = snapshot.get("rate_limits") or {}
    five = (limits.get("five_hour") or {}).get("used_percentage")
    seven = (limits.get("seven_day") or {}).get("used_percentage")
    if five is None and seven is None:
        return
    if now - last_history_time() < HISTORY_INTERVAL_SEC:
        return
    entry = json.dumps({"t": now, "five": five, "seven": seven}, ensure_ascii=False)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    if os.path.getsize(HISTORY_FILE) > HISTORY_PRUNE_SIZE:
        cutoff = now - HISTORY_KEEP_SEC
        with open(HISTORY_FILE, encoding="utf-8") as f:
            kept = [
                line
                for line in f
                if line.strip()
                and json.loads(line).get("t", 0) >= cutoff
            ]
        fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp_path, HISTORY_FILE)


def update_ledger(data, now):
    """日別のコスト台帳（API換算）。サブスク分と従量課金(API)分を別勘定で積算する。

    セッションの total_cost_usd は累積値なので、前回値との差分だけをその日の合計に足す。
    区分は「同じ入力に rate_limits が入っているか」で判定する（サブスクにしか来ないため、
    コストが増えるAPI応答後の入力には必ず同時に含まれる）。
    """
    sid = data.get("session_id")
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if not sid or cost is None:
        return
    try:
        with open(LEDGER_FILE, encoding="utf-8") as f:
            ledger = json.load(f)
        if not isinstance(ledger, dict):
            ledger = {}
    except (OSError, ValueError):
        ledger = {}
    sessions = ledger.get("sessions") or {}
    days = ledger.get("days") or {}

    last = (sessions.get(sid) or {}).get("last_cost", 0)
    delta = cost - last
    if delta < 0:  # 累積値が巻き戻ることは通常ないが、あれば新規開始として扱う
        delta = cost
    if delta > 0:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        kind = "subscription" if data.get("rate_limits") else "api"
        totals = days.setdefault(day, {})
        totals[kind] = round(totals.get(kind, 0) + delta, 6)
    sessions[sid] = {"last_cost": cost, "updated_at": now}

    sessions = {
        k: v
        for k, v in sessions.items()
        if now - v.get("updated_at", 0) < SESSION_TTL_SEC
    }
    if len(days) > LEDGER_KEEP_DAYS:
        days = dict(sorted(days.items())[-LEDGER_KEEP_DAYS:])
    write_json_atomic(LEDGER_FILE, {"schema": 1, "sessions": sessions, "days": days})


def pct_color(pct, warn, crit):
    if pct is None:
        return DIM
    if pct >= crit:
        return RED
    if pct >= warn:
        return YELLOW
    return GREEN


def fmt_pct(pct):
    return "--" if pct is None else f"{round(pct)}%"


def fmt_reset(resets_at, now, with_date):
    if not resets_at:
        return ""
    local = time.localtime(resets_at)
    today = time.localtime(now)
    if with_date or (local.tm_yday, local.tm_year) != (today.tm_yday, today.tm_year):
        return f"→{local.tm_mon}/{local.tm_mday} {local.tm_hour:02d}:{local.tm_min:02d}"
    return f"→{local.tm_hour:02d}:{local.tm_min:02d}"


def bar(pct, width=5):
    filled = 0 if pct is None else max(0, min(width, round(pct / 100 * width)))
    return "▰" * filled + "▱" * (width - filled)


def rate_segment(label, window, now):
    if not window:
        return f"{DIM}{label} {bar(None)} --{RESET}"
    pct = window.get("used_percentage")
    resets_at = window.get("resets_at")
    if resets_at and now >= resets_at:
        # リセット時刻を過ぎた古い観測値: 実際は0%に戻っているはず（次のAPI応答で更新される）
        return f"{DIM}{label} {bar(0)} ↺0%{RESET}"
    color = pct_color(pct, 50, 80)
    reset_txt = fmt_reset(resets_at, now, with_date=(label == "7d"))
    reset_part = f" {DIM}{reset_txt}{RESET}" if reset_txt else ""
    return f"{color}{label} {bar(pct)} \033[1m{fmt_pct(pct)}{RESET}{reset_part}"


def render_statusline(data, snapshot, now):
    segments = []

    model = (data.get("model") or {}).get("display_name")
    if model:
        segments.append(f"{BOLD_CYAN}{model}{RESET}")

    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        segments.append(f"{pct_color(ctx, 60, 85)}ctx {fmt_pct(ctx)}{RESET}")

    limits = snapshot.get("rate_limits") or {}
    segments.append(rate_segment("5h", limits.get("five_hour"), now))
    segments.append(rate_segment("7d", limits.get("seven_day"), now))
    # コスト(API換算)は誤解を招きやすいのでstatuslineには出さない。ダッシュボード側で注記付きで表示する

    return f" {DIM}│{RESET} ".join(segments)


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}

    now = int(time.time())
    snapshot = build_snapshot(data, load_previous(), now)

    try:
        write_json_atomic(DATA_FILE, snapshot)
        append_history(snapshot, now)
        update_ledger(data, now)
    except (OSError, ValueError):
        pass  # 書き出せなくてもstatusline表示は続行する

    print(render_statusline(data, snapshot, now))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("claude-usage-widget: error")  # UIを壊さないため必ず1行出して正常終了
