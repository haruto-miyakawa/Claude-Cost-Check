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
  ただし**黙って終わらない**: 失敗の理由は必ず statusline.err に残す（下記）。

失敗を黙殺しない方針（2026-08-17）:
  Claude Code は statusline の非ゼロ終了も stderr も画面に出さない。そのため「設定したのに
  効いていない」状態が何日も気づかれないことが実際に起きた（Windows側の計上漏れ・3日間）。
  対策として、想定外はすべて DATA_DIR/statusline.err に追記し、ダッシュボードの
  「計上の健全性」カードがその内容と計上漏れの疑いを表示する。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback

DATA_DIR = os.environ.get("CLAUDE_USAGE_WIDGET_DIR") or os.path.expanduser(
    "~/.local/share/claude-usage-widget"
)
DATA_FILE = os.path.join(DATA_DIR, "usage.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
LEDGER_FILE = os.path.join(DATA_DIR, "cost-ledger.json")
ERROR_FILE = os.path.join(DATA_DIR, "statusline.err")
ERROR_FILE_MAX = 64 * 1024  # これを超えたら古い方から捨てる（無限に太らせない）
SESSION_TTL_SEC = 48 * 3600  # これより古いセッション記録はスナップショットから間引く
LEDGER_KEEP_DAYS = 400
HISTORY_INTERVAL_SEC = 300  # 履歴サンプリング間隔（ダッシュボードの推移グラフ用）
HISTORY_KEEP_SEC = 7 * 24 * 3600
HISTORY_PRUNE_SIZE = 256 * 1024  # このサイズを超えたら古い行を間引く

# 詳細ダッシュボードの自動生成（statuslineに相乗り。詳細は maybe_rebuild_dashboard）
DASHBOARD_BUILD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard", "build.py"
)
DASHBOARD_STAMP = os.path.join(DATA_DIR, "dashboard-build.stamp")
DASHBOARD_INTERVAL_SEC = 300  # 5分に1回まで

RESET = "\033[0m"
DIM = "\033[2m"
BOLD_CYAN = "\033[1;36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"


def log_problem(stage, message, exc=None):
    """想定外を statusline.err に追記する。ここ自体は絶対に例外を投げない。

    Claude Code は statusline の stderr も終了コードも見せてくれないので、
    ここが唯一の「失敗した」という痕跡になる。あとから原因を追えるよう、
    どのホストのどのプロセスから呼ばれたかも一緒に残す。
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        head = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{stage}] {message} "
            f"(pid={os.getpid()} cwd={os.getcwd()} python={sys.executable})"
        )
        body = ""
        if exc is not None:
            body = "\n" + "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ).rstrip()
        with open(ERROR_FILE, "a", encoding="utf-8") as f:
            f.write(head + body + "\n")
        if os.path.getsize(ERROR_FILE) > ERROR_FILE_MAX:
            with open(ERROR_FILE, encoding="utf-8") as f:
                tail = f.read()[-(ERROR_FILE_MAX // 2):]
            fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("（古いログは切り詰め済み）\n" + tail)
            os.replace(tmp_path, ERROR_FILE)
    except Exception:
        pass  # ログすら書けない状況でstatuslineを巻き込まない


def host_of(cwd):
    """セッションがWindowsネイティブ側かWSL側かを cwd から判定する。

    Claude Code は Windows でも WSL でも同じ statusline を呼びうるが、
    書き出し先は WSL 側の1か所に集約される。どちら由来かを残しておかないと、
    片側だけ計上漏れしていても気づけない（実際に3日気づけなかった）。
    """
    if not cwd:
        return "unknown"
    s = str(cwd)
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\"):
        return "windows"
    return "wsl"


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
            "host": host_of(data.get("cwd")),
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

    日ごとに by_host（windows/wsl別の内訳）も持つ。合計だけだと、片方のホストが
    丸ごと計上漏れしていても「その日は作業が少なかった」と区別がつかないため。
    by_host は2026-08-17からの追加なので、それ以前の日には存在しない。
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
        per_host = totals.setdefault("by_host", {}).setdefault(host_of(data.get("cwd")), {})
        per_host[kind] = round(per_host.get(kind, 0) + delta, 6)
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


def maybe_rebuild_dashboard(now):
    """詳細ダッシュボード(dashboard.html)を裏で作り直す。

    集計元が増えるのはClaude Codeが動いている間だけで、statuslineはまさにその間だけ
    走る。ここに相乗りすれば cron もタスクスケジューラも要らない（セッションが無い間は
    データが変わらないので、作り直す必要もない）。

    statuslineの描画は絶対に待たせない: 間隔ゲートを通ったときだけ、切り離した子プロセスへ
    投げっぱなしにする。失敗してもstatuslineは壊さないが、理由は statusline.err に残す。
    無効化: 環境変数 CLAUDE_USAGE_DASHBOARD_AUTOBUILD=0
    """
    if os.environ.get("CLAUDE_USAGE_DASHBOARD_AUTOBUILD") == "0":
        return
    try:
        if not os.path.exists(DASHBOARD_BUILD):
            log_problem("dashboard", f"build.py が見つからない: {DASHBOARD_BUILD}")
            return
        try:
            last = os.path.getmtime(DASHBOARD_STAMP)
        except OSError:
            last = 0
        if now - last < DASHBOARD_INTERVAL_SEC:
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        # 先にスタンプを打つ: 並行セッションが同時に走っても起動は1本に絞られる
        with open(DASHBOARD_STAMP, "w", encoding="utf-8") as f:
            f.write(str(now))
        with open(os.devnull, "wb") as devnull:
            subprocess.Popen(
                [sys.executable, DASHBOARD_BUILD, "--quiet"],
                stdin=devnull, stdout=devnull, stderr=devnull,
                start_new_session=True,  # 親(statusline)が終了しても生き残らせる
            )
    except Exception as exc:
        log_problem("dashboard", "ダッシュボードの再生成を起動できなかった", exc)


def read_input():
    """stdinのJSONを読む。読めなかった理由は握りつぶさず statusline.err に残す。

    「Claude CodeがJSONを渡してくれていない」は起動経路の設定ミスで実際に起きる
    （例: Git Bash経由でwsl.exeを呼ぶとパスが化けてスクリプトごと起動しない）。
    空入力のまま黙って書き込むと updated_by=null のスナップショットが延々と上書きされ、
    「動いているように見えるのに何も記録されない」状態になるため、そこで止める。
    """
    try:
        raw = sys.stdin.read()
    except Exception as exc:
        log_problem("stdin", "stdinを読めなかった", exc)
        return None
    if not raw.strip():
        log_problem("stdin", "stdinが空（Claude CodeからのJSONが届いていない）")
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        log_problem("stdin", f"stdinがJSONとして読めない: 先頭120字={raw[:120]!r}", exc)
        return None
    if not isinstance(data, dict):
        log_problem("stdin", f"stdinのJSONがオブジェクトでない: {type(data).__name__}")
        return None
    if not data.get("session_id"):
        log_problem("stdin", f"session_idが無い: keys={sorted(data)}")
    return data


def main():
    data = read_input()
    now = int(time.time())
    usable = data is not None

    snapshot = build_snapshot(data or {}, load_previous(), now)

    if usable:
        # 入力が使えたときだけ書く。空入力で上書きすると計上漏れが見えなくなる
        for stage, fn in (
            ("usage.json", lambda: write_json_atomic(DATA_FILE, snapshot)),
            ("history.jsonl", lambda: append_history(snapshot, now)),
            ("cost-ledger.json", lambda: update_ledger(data, now)),
        ):
            try:
                fn()
            except Exception as exc:
                log_problem(stage, "書き出しに失敗した", exc)

    print(render_statusline(data or {}, snapshot, now))
    maybe_rebuild_dashboard(now)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # UIを壊さないため必ず1行出して正常終了する。ただし理由はログに必ず残す
        log_problem("main", "statuslineが例外で終了した", exc)
        print("claude-usage-widget: error（詳細は statusline.err）")
