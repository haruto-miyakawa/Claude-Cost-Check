#!/usr/bin/env python3
"""Claude使用量ダッシュボードの生成（claude-usage-widget）。

statusline が書き出すスナップショット（usage.json / history.jsonl / cost-ledger.json）と
Claude Code のトランスクリプト（~/.claude/projects/**/*.jsonl）を集計し、
自己完結の1枚HTML（dashboard.html）を書き出す。

  python3 dashboard/build.py [-o 出力先.html]

stdlibのみ・ネットワーク通信なし。生成物はローカル完結で、外部へ送らない
（プロジェクト名やパスが含まれるため）。
"""

import argparse
import collections
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CLAUDE_USAGE_WIDGET_DIR") or os.path.expanduser(
    "~/.local/share/claude-usage-widget"
)
DEFAULT_OUT = os.path.join(os.path.dirname(HERE), "dashboard.html")
# ファイル単位の集計キャッシュ。Claude Code が古いトランスクリプトを消しても
# 過去ぶんの集計はここに残る（statusline のデータには触らない別ファイル）
CACHE_FILE = os.path.join(DATA_DIR, "transcript-cache.json")

# トランスクリプトの探索先（WSL側とWindows側の両方。存在するものだけ使う）。
# テストから差し替えられるよう、環境変数で上書きできる（`:` 区切り）
TRANSCRIPT_GLOBS = (
    os.environ.get("CLAUDE_USAGE_TRANSCRIPT_GLOBS", "").split(":")
    if os.environ.get("CLAUDE_USAGE_TRANSCRIPT_GLOBS")
    else [
        os.path.expanduser("~/.claude/projects/*/*.jsonl"),
        "/mnt/c/Users/*/.claude/projects/*/*.jsonl",
    ]
)

# statusline が失敗を書き残す先（「計上の健全性」カードで表示する）
ERROR_FILE = os.path.join(DATA_DIR, "statusline.err")
ERROR_TAIL_LINES = 12

HOST_LABELS = {"windows": "Windows側", "wsl": "WSL側", "unknown": "(不明)"}

# 公式の従量課金レート（$ / 100万トークン）。サブスク利用でも「API換算いくらぶんか」を出すために使う。
# 出典: claude-api skill のモデル表（2026-06-24 時点のキャッシュ）
PRICING = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CACHE_WRITE_5M = 1.25  # 入力単価に対する倍率
CACHE_WRITE_1H = 2.0
CACHE_READ = 0.1

MODEL_LABELS = {
    "claude-fable-5": "Fable 5",
    "claude-mythos-5": "Mythos 5",
    "claude-opus-5": "Opus 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-sonnet-5": "Sonnet 5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-4-5": "Haiku 4.5",
}

TOP_PROJECTS = 8  # これを超えた分は「その他」に畳む
TOP_TOOLS = 10


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def write_json_atomic(path, obj):
    """読み手が壊れたJSONを見ないよう、一時ファイル経由で置換する。"""
    import tempfile

    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_ts(iso):
    """ISO8601（末尾Z）をエポック秒に。失敗したら None。"""
    if not isinstance(iso, str):
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", iso)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    # 末尾Zや+00:00はUTC扱い。それ以外の表記は来ない想定
    return int(
        time.mktime((y, mo, d, h, mi, s, 0, 0, 0)) - time.timezone
        if iso.endswith("Z") or "+00:00" in iso
        else time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
    )


def project_name(cwd):
    """cwd からプロジェクト名を取り出す（Windowsパスにも対応）。"""
    if not cwd:
        return "(不明)"
    parts = [p for p in re.split(r"[\\/]+", str(cwd)) if p and not p.endswith(":")]
    return parts[-1] if parts else "(不明)"


def host_of(cwd):
    """cwd がWindowsネイティブ側かWSL側か。statusline.py の同名関数と同じ判定。"""
    if not cwd:
        return "unknown"
    s = str(cwd)
    if re.match(r"^[A-Za-z]:[\\/]", s) or s.startswith("\\\\"):
        return "windows"
    return "wsl"


def cost_of(model, usage):
    """1レスポンスぶんのAPI換算コスト（$）。キャッシュのTTL別倍率まで見る。"""
    price = PRICING.get(model)
    if not price:
        return 0.0
    inp, outp = price
    cc = usage.get("cache_creation") or {}
    w1h = cc.get("ephemeral_1h_input_tokens") or 0
    w5m = cc.get("ephemeral_5m_input_tokens") or 0
    if not (w1h or w5m):  # 内訳が無ければ合計を5分TTL扱いにする
        w5m = usage.get("cache_creation_input_tokens") or 0
    return (
        (usage.get("input_tokens") or 0) * inp
        + (usage.get("output_tokens") or 0) * outp
        + (usage.get("cache_read_input_tokens") or 0) * inp * CACHE_READ
        + w5m * inp * CACHE_WRITE_5M
        + w1h * inp * CACHE_WRITE_1H
    ) / 1_000_000


NUMERIC_KEYS = (
    "input", "output", "cache_read", "cache_write", "thinking", "messages", "cost",
)


def blank_bucket():
    return {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "thinking": 0,
        "messages": 0,
        "cost": 0.0,
        "sessions": set(),
    }


def bucket_to_json(b):
    out = {k: b[k] for k in NUMERIC_KEYS}
    out["sessions"] = sorted(b["sessions"])
    return out


def merge_bucket(dst, src):
    for k in NUMERIC_KEYS:
        dst[k] += src.get(k, 0)
    dst["sessions"] |= set(src.get("sessions") or [])


def scan_file(path):
    """1ファイルを読んで、JSONにできる形の集計を返す。

    1回のAPI応答が複数のassistantレコードに分かれ、それぞれが**同じ usage を持つ**ため、
    usage は requestId 単位で1度だけ数える（レコード単位で足すと数倍に膨らむ）。
    ツール呼び出しはレコードごとに中身が違うので全レコードから拾う。
    ファイルをまたぐ requestId の重複は実測ゼロなので、ファイル単位で閉じて数えてよい。
    """
    agg = {
        "project": {}, "model": {}, "day": {},
        # host: windows/wsl別の内訳。day_host は "YYYY-MM-DD|host" をキーにした日×ホスト。
        # 「トランスクリプトには活動があるのに台帳に記録が無い日」を割り出すために使う
        "host": {}, "day_host": {},
        "heat": {}, "tools": {},
        "totals": {k: 0 for k in NUMERIC_KEYS},
        "sessions": set(),
    }
    for extra in ("records", "sidechain", "errors", "web_search", "web_fetch"):
        agg["totals"][extra] = 0
    seen_requests = set()

    def bucket(kind, key):
        return agg[kind].setdefault(key, blank_bucket())

    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            if '"assistant"' not in line:  # 安いプレフィルタ
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            model = msg.get("model") or "(不明)"
            if model.startswith("<"):  # <synthetic> 等はAPI利用ではない
                continue
            agg["totals"]["records"] += 1

            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name") or "(不明)"
                    agg["tools"][name] = agg["tools"].get(name, 0) + 1

            req = rec.get("requestId") or msg.get("id") or rec.get("uuid")
            if req in seen_requests:
                continue
            seen_requests.add(req)

            usage = msg.get("usage") or {}
            inp = usage.get("input_tokens") or 0
            out = usage.get("output_tokens") or 0
            cread = usage.get("cache_read_input_tokens") or 0
            cwrite = usage.get("cache_creation_input_tokens") or 0
            think = (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            cost = cost_of(model, usage)
            sid = rec.get("sessionId") or rec.get("session_id") or ""
            ts = parse_ts(rec.get("timestamp"))

            host = host_of(rec.get("cwd"))
            targets = [
                bucket("project", project_name(rec.get("cwd"))),
                bucket("model", model),
                bucket("host", host),
            ]
            if ts:
                day = time.strftime("%Y-%m-%d", time.localtime(ts))
                targets.append(bucket("day", day))
                targets.append(bucket("day_host", f"{day}|{host}"))
                lt = time.localtime(ts)
                hk = f"{lt.tm_wday},{lt.tm_hour}"
                agg["heat"][hk] = agg["heat"].get(hk, 0) + 1
            for b in targets:
                b["input"] += inp
                b["output"] += out
                b["cache_read"] += cread
                b["cache_write"] += cwrite
                b["thinking"] += think
                b["messages"] += 1
                b["cost"] += cost
                if sid:
                    b["sessions"].add(sid)

            t = agg["totals"]
            t["input"] += inp
            t["output"] += out
            t["cache_read"] += cread
            t["cache_write"] += cwrite
            t["thinking"] += think
            t["messages"] += 1
            t["cost"] += cost
            if sid:
                agg["sessions"].add(sid)
            if rec.get("isSidechain"):
                t["sidechain"] += 1
            if rec.get("isApiErrorMessage"):
                t["errors"] += 1
            stu = usage.get("server_tool_use") or {}
            t["web_search"] += stu.get("web_search_requests") or 0
            t["web_fetch"] += stu.get("web_fetch_requests") or 0

    for kind in ("project", "model", "day", "host", "day_host"):
        agg[kind] = {k: bucket_to_json(v) for k, v in agg[kind].items()}
    agg["sessions"] = sorted(agg["sessions"])
    return agg


def host_of_path(path):
    """トランスクリプトの置き場所からホストを決める。

    探索先は「WSLのホーム」と「/mnt/<ドライブ>/Users/…（Windows側のホーム）」の2種類だけなので、
    パスだけで確実に分かる。cwd から引く host_of と結果は一致する。
    """
    return "windows" if re.match(r"^/mnt/[a-zA-Z]/Users/", path) else "wsl"


def migrate_agg_v1(path, agg):
    """schema 1 のキャッシュ項目に host / day_host を補う（その場で書き換える）。

    1ファイルは1プロジェクト＝1ホストぶんなので、パスから決めたホストに全量を寄せてよい。
    再スキャンで作り直せない（Claude Codeに消されたファイルの）集計を守るための移行。
    """
    if "host" in agg and "day_host" in agg:
        return
    host = host_of_path(path)
    totals = agg.get("totals") or {}
    whole = {k: totals.get(k, 0) for k in NUMERIC_KEYS}
    whole["sessions"] = sorted(agg.get("sessions") or [])
    agg["host"] = {host: whole}
    agg["day_host"] = {f"{day}|{host}": dict(b) for day, b in (agg.get("day") or {}).items()}


def scan_transcripts():
    """全トランスクリプトを集計する。結果はファイル単位でキャッシュする。

    Claude Code は古いトランスクリプトを定期的に削除する（実測: 2026-08-14 の
    cleanup で19→15ファイル）。ファイル単位の集計をキャッシュに残しておけば、
    元ファイルが消えても過去ぶんの集計は残る。mtime と size が変わらなければ
    再読み込みもしないので、2回目以降は速い。
    """
    # schema 2 (2026-08-17): host / day_host の内訳を追加。
    # 旧キャッシュは捨てずに移行する（消えたファイルの集計はもう読み直せないため）
    cache = load_json(CACHE_FILE, {})
    if not isinstance(cache.get("files"), dict) or cache.get("schema") not in (1, 2):
        cache = {"schema": 2, "files": {}}
    if cache.get("schema") == 1:
        for path, entry in cache["files"].items():
            migrate_agg_v1(path, entry.get("agg") or {})
        cache["schema"] = 2
    files = cache["files"]

    present, rescanned = set(), 0
    for pattern in TRANSCRIPT_GLOBS:
        for path in glob.glob(pattern):
            real = os.path.realpath(path)
            if real in present:
                continue
            present.add(real)
            try:
                st = os.stat(real)
            except OSError:
                continue
            entry = files.get(real)
            if entry and entry.get("size") == st.st_size and entry.get("mtime") == int(st.st_mtime):
                continue  # 変わっていない: キャッシュをそのまま使う
            agg = scan_file(real)
            if agg is None:
                continue
            files[real] = {"size": st.st_size, "mtime": int(st.st_mtime), "agg": agg}
            rescanned += 1

    # 集計を合算（消えたファイルのぶんもキャッシュに残っているので一緒に足す）
    by_project = collections.defaultdict(blank_bucket)
    by_model = collections.defaultdict(blank_bucket)
    by_day = collections.defaultdict(blank_bucket)
    by_host = collections.defaultdict(blank_bucket)
    by_day_host = collections.defaultdict(blank_bucket)
    heat = collections.Counter()
    tools = collections.Counter()
    totals = blank_bucket()
    for extra in ("records", "sidechain", "errors", "web_search", "web_fetch"):
        totals[extra] = 0

    for real, entry in files.items():
        agg = entry.get("agg") or {}
        for kind, dst in (
            ("project", by_project), ("model", by_model), ("day", by_day),
            ("host", by_host), ("day_host", by_day_host),
        ):
            for name, b in (agg.get(kind) or {}).items():
                merge_bucket(dst[name], b)
        for k, n in (agg.get("heat") or {}).items():
            w, h = k.split(",")
            heat[(int(w), int(h))] += n
        for name, n in (agg.get("tools") or {}).items():
            tools[name] += n
        t = agg.get("totals") or {}
        for k in NUMERIC_KEYS:
            totals[k] += t.get(k, 0)
        for extra in ("records", "sidechain", "errors", "web_search", "web_fetch"):
            totals[extra] += t.get(extra, 0)
        totals["sessions"] |= set(agg.get("sessions") or [])

    try:
        write_json_atomic(CACHE_FILE, cache)
    except OSError:
        pass  # キャッシュに書けなくても集計自体は成立する

    return {
        "by_project": by_project,
        "by_model": by_model,
        "by_day": by_day,
        "by_host": by_host,
        "by_day_host": by_day_host,
        "heat": heat,
        "tools": tools,
        "totals": totals,
        "files": len(files),
        "files_present": len(present),
        "files_gone": len(files) - len(present),
        "rescanned": rescanned,
    }
def finalize(bucket):
    out = {k: v for k, v in bucket.items() if k != "sessions"}
    out["sessions"] = len(bucket["sessions"]) if "sessions" in bucket else 0
    out["cost"] = round(bucket.get("cost", 0.0), 4)
    return out


def rank(mapping, limit, label_fn=lambda k: k):
    """合計トークンの多い順に並べ、あふれた分を「その他」に畳む。"""
    items = sorted(
        mapping.items(),
        key=lambda kv: kv[1]["input"] + kv[1]["output"] + kv[1]["cache_write"],
        reverse=True,
    )
    head = [dict(finalize(v), name=label_fn(k)) for k, v in items[:limit]]
    tail = items[limit:]
    if tail:
        merged = blank_bucket()
        for _, v in tail:
            for key in ("input", "output", "cache_read", "cache_write", "thinking", "messages"):
                merged[key] += v[key]
            merged["cost"] += v["cost"]
            merged["sessions"] |= v["sessions"]
        head.append(dict(finalize(merged), name=f"その他（{len(tail)}件）"))
    return head


def read_error_tail():
    """statusline.err の末尾を読む。statusline が黙って失敗していないかの確認用。"""
    try:
        st = os.stat(ERROR_FILE)
        with open(ERROR_FILE, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
    except OSError:
        return {"exists": False, "lines": [], "count": 0, "mtime": 0}
    return {
        "exists": True,
        "lines": lines[-ERROR_TAIL_LINES:],
        "count": len(lines),
        "mtime": int(st.st_mtime),
    }


def build_health(scan, ledger):
    """「記録できているつもりで記録できていない」を検出する。

    ホスト（Windows側 / WSL側）ごとに、トランスクリプト実測とコスト台帳を突き合わせる。
    トランスクリプトに応答があるのに台帳にその日・そのホストの記録が無ければ、
    statusline がそのホストで動いていない＝計上漏れとして並べる。2026-08-14に
    Windows側へstatuslineを登録したのに実際は起動すらしていなかった件を、
    次からは翌日に気づけるようにするためのカード。
    """
    days = ledger.get("days") or {}

    last_day, per_host = {}, {}
    for key, bucket in scan["by_day_host"].items():
        day, _, host = key.partition("|")
        per_host.setdefault(host, []).append((day, bucket))
        if day > last_day.get(host, ""):
            last_day[host] = day

    hosts = []
    for host, bucket in sorted(
        scan["by_host"].items(), key=lambda kv: -kv[1]["messages"]
    ):
        ledger_cost = sum(
            (((v or {}).get("by_host") or {}).get(host) or {}).get(kind, 0)
            for v in days.values()
            for kind in ("subscription", "api")
        )
        hosts.append({
            "host": host,
            "label": HOST_LABELS.get(host, host),
            "messages": bucket["messages"],
            "tokens": bucket["input"] + bucket["output"] + bucket["cache_write"],
            "cost": round(bucket["cost"], 4),
            "sessions": len(bucket["sessions"]),
            "last_day": last_day.get(host, ""),
            "ledger_cost": round(ledger_cost, 4),
        })

    gaps = []
    for host, rows in per_host.items():
        for day, bucket in rows:
            if not bucket["messages"]:
                continue
            entry = days.get(day)
            if entry is None:
                reason = "台帳にこの日の記録が無い"
            elif "by_host" not in entry:
                continue  # by_host以前(〜2026-08-16)の日はホスト別に判定できない
            elif host not in (entry.get("by_host") or {}):
                reason = "台帳のこの日にこのホストぶんが無い"
            else:
                continue
            gaps.append({
                "day": day,
                "host": host,
                "label": HOST_LABELS.get(host, host),
                "messages": bucket["messages"],
                "est_cost": round(bucket["cost"], 4),
                "reason": reason,
            })
    gaps.sort(key=lambda g: (g["day"], g["host"]), reverse=True)

    return {
        "hosts": hosts,
        "gaps": gaps,
        "gap_cost": round(sum(g["est_cost"] for g in gaps), 4),
        "errors": read_error_tail(),
    }


def build_payload():
    usage = load_json(os.path.join(DATA_DIR, "usage.json"), {})
    ledger = load_json(os.path.join(DATA_DIR, "cost-ledger.json"), {})

    history = []
    try:
        with open(os.path.join(DATA_DIR, "history.jsonl"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("t"):
                    history.append(
                        {"t": e["t"], "five": e.get("five"), "seven": e.get("seven")}
                    )
    except OSError:
        pass
    history.sort(key=lambda e: e["t"])

    scan = scan_transcripts()
    now = int(time.time())

    days = ledger.get("days") or {}
    est_by_day = {d: b["cost"] for d, b in scan["by_day"].items()}
    # 日の軸は台帳とトランスクリプトの和集合。台帳に無い日を落とすと、まさに
    # 「計上漏れした日」がグラフから消えて気づけなくなる
    ledger_days = [
        {
            "day": d,
            "subscription": round((days.get(d) or {}).get("subscription", 0), 4),
            "api": round((days.get(d) or {}).get("api", 0), 4),
            # 同じ日をトランスクリプトから推定した額。台帳と大きく食い違えば計上漏れの疑い
            "estimated": round(est_by_day.get(d, 0), 4),
        }
        for d in sorted(set(days) | set(est_by_day))
    ]

    sessions = []
    for sid, s in (usage.get("sessions") or {}).items():
        if not isinstance(s, dict):
            continue
        sessions.append(
            {
                "id": sid[:8],
                "model": s.get("model"),
                "cost": s.get("cost_usd"),
                "ctx": s.get("context_used_percentage"),
                "cwd": project_name(s.get("cwd")),
                "host": s.get("host") or host_of(s.get("cwd")),
                "updated_at": s.get("updated_at"),
                "subscription": bool(s.get("subscription")),
            }
        )
    sessions.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)

    totals = finalize(scan["totals"])
    totals["web_search"] = scan["totals"]["web_search"]
    totals["web_fetch"] = scan["totals"]["web_fetch"]
    totals["sidechain"] = scan["totals"]["sidechain"]
    totals["errors"] = scan["totals"]["errors"]
    totals["records"] = scan["totals"]["records"]
    totals["files"] = scan["files"]
    totals["files_present"] = scan["files_present"]
    totals["files_gone"] = scan["files_gone"]
    totals["tool_calls"] = sum(scan["tools"].values())

    return {
        "generated_at": now,
        "rate_limits": usage.get("rate_limits") or {},
        "sessions": sessions,
        "history": history,
        "ledger": ledger_days,
        "projects": rank(scan["by_project"], TOP_PROJECTS),
        "models": rank(scan["by_model"], 6, lambda m: MODEL_LABELS.get(m, m)),
        "days": [dict(finalize(v), day=d) for d, v in sorted(scan["by_day"].items())],
        "heat": [
            {"wday": w, "hour": h, "n": n} for (w, h), n in sorted(scan["heat"].items())
        ],
        "tools": [
            {"name": n, "n": c} for n, c in scan["tools"].most_common(TOP_TOOLS)
        ],
        "totals": totals,
        "health": build_health(scan, ledger),
    }


# ---------------------------------------------------------------- HTML

TEMPLATE = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude 使用量ダッシュボード</title>
<style>
:root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --plane: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --s1: #2a78d6;  /* blue   */
  --s2: #eb6834;  /* orange */
  --s3: #1baf7a;  /* aqua   */
  --s4: #eda100;  /* yellow */
  --good: #0ca30c;
  --warning: #fab219;
  --critical: #d03b3b;
  --seq-100: #cde2fb; --seq-250: #86b6ef; --seq-400: #3987e5;
  --seq-550: #1c5cab; --seq-700: #0d366b;
  --font: system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --seq-100: #184f95; --seq-250: #256abf; --seq-400: #3987e5;
    --seq-550: #86b6ef; --seq-700: #cde2fb;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --plane: #0d0d0d;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --seq-100: #184f95; --seq-250: #256abf; --seq-400: #3987e5;
  --seq-550: #86b6ef; --seq-700: #cde2fb;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  background: var(--plane);
  color: var(--text-primary);
  font-family: var(--font);
  font-size: 15px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 72px; }

header.top { display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
header.top h1 { font-size: 22px; font-weight: 650; margin: 0; letter-spacing: .01em; }
header.top .sub { color: var(--muted); font-size: 13px; }
.spacer { flex: 1 1 auto; }
button.theme {
  font: inherit; font-size: 13px; color: var(--text-secondary);
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 999px; padding: 5px 14px; cursor: pointer;
}
button.theme:hover { color: var(--text-primary); }

.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 20px 22px;
  margin-bottom: 18px;
}
.card > h2 {
  font-size: 14px; font-weight: 650; margin: 0 0 2px;
  letter-spacing: .02em;
}
.card > .note { color: var(--muted); font-size: 12.5px; margin: 0 0 18px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
@media (max-width: 820px) { .grid2 { grid-template-columns: 1fr; } }

/* ---- hero ---- */
.hero { display: grid; grid-template-columns: auto auto 1fr; gap: 28px; align-items: center; }
@media (max-width: 820px) { .hero { grid-template-columns: 1fr 1fr; } }
@media (max-width: 520px) { .hero { grid-template-columns: 1fr; } }
.gauge { text-align: center; }
.gauge svg { display: block; margin: 0 auto; }
.gauge .glabel { font-size: 12px; color: var(--muted); letter-spacing: .08em; }
.gauge .greset { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
.gauge-val { font-size: 30px; font-weight: 650; fill: var(--text-primary); }
.gauge-track { stroke: var(--grid); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); gap: 14px 22px; }
.tile .label { font-size: 12px; color: var(--muted); }
.tile .value { font-size: 26px; font-weight: 650; line-height: 1.15; }
.tile .value small { font-size: 14px; font-weight: 500; color: var(--text-secondary); margin-left: 2px; }
.tile .delta { font-size: 12px; color: var(--text-secondary); }

/* ---- charts ---- */
.chart { width: 100%; overflow-x: auto; }
.chart svg { display: block; }
.axis text { fill: var(--muted); font-size: 11px; }
.axis line, .axis path { stroke: var(--axis); stroke-width: 1; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.legend { display: flex; gap: 18px; flex-wrap: wrap; margin: 0 0 12px; font-size: 12.5px; color: var(--text-secondary); }
.legend span.key { display: inline-flex; align-items: center; gap: 7px; }
.legend i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }

.line-path { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.area-fill { opacity: .10; }
.bar { transition: none; }
.rowbar { display: grid; grid-template-columns: minmax(96px, 150px) 1fr auto; gap: 12px; align-items: center; margin-bottom: 9px; font-size: 13px; }
.rowbar .nm { color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rowbar .track { background: var(--grid); border-radius: 4px; height: 14px; position: relative; overflow: hidden; }
.rowbar .fill { height: 100%; border-radius: 0 4px 4px 0; width: 0; }
.rowbar .val { color: var(--text-secondary); font-variant-numeric: tabular-nums; font-size: 12.5px; white-space: nowrap; }

.meter { height: 16px; border-radius: 5px; background: var(--seq-100); overflow: hidden; }
.meter > div { height: 100%; width: 0; background: var(--s1); border-radius: 0 5px 5px 0; }

/* ---- heatmap ---- */
.heat { display: grid; grid-template-columns: 34px repeat(24, 1fr); gap: 2px; min-width: 620px; }
.heat .cell { aspect-ratio: 1 / 1; border-radius: 3px; background: var(--grid); opacity: 0; }
.heat .rowlab, .heat .collab { font-size: 10.5px; color: var(--muted); text-align: center; line-height: 1.9; }
.heat .rowlab { text-align: right; padding-right: 5px; }

/* ---- tooltip ---- */
#tip {
  position: fixed; z-index: 50; pointer-events: none; opacity: 0;
  background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 9px;
  padding: 8px 11px; font-size: 12.5px; line-height: 1.5;
  box-shadow: 0 6px 22px rgba(0,0,0,.14);
  transition: opacity .12s ease;
  max-width: 260px;
}
#tip b { font-weight: 650; }
#tip .k { color: var(--muted); }

/* ---- table view ---- */
details.tv { margin-top: 14px; }
details.tv summary { cursor: pointer; font-size: 12.5px; color: var(--text-secondary); }
details.tv table { border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 12.5px; }
details.tv th, details.tv td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }
details.tv th:first-child, details.tv td:first-child { text-align: left; font-variant-numeric: normal; }
details.tv th { color: var(--muted); font-weight: 500; }

footer.notes { color: var(--muted); font-size: 12px; margin-top: 26px; line-height: 1.8; }
footer.notes code { font-size: 11.5px; }

/* ---- health ---- */
.card.health { border-left: 3px solid var(--good); }
.card.health.warn { border-left-color: var(--warning); }
.card.health.bad { border-left-color: var(--critical); }
.verdict { display: flex; align-items: baseline; gap: 10px; font-size: 13.5px; margin: 0 0 16px; }
.verdict .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--good); flex: none; align-self: center; }
.card.health.warn .verdict .dot { background: var(--warning); }
.card.health.bad .verdict .dot { background: var(--critical); }
.hosts { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px 22px; margin-bottom: 6px; }
.hosts .hcard { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.hosts .hname { font-size: 13px; font-weight: 650; margin-bottom: 6px; }
.hosts dl { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; margin: 0; font-size: 12.5px; }
.hosts dt { color: var(--muted); }
.hosts dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; color: var(--text-secondary); }
.gaps { margin-top: 16px; }
.gaps table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
.gaps th, .gaps td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--grid); font-variant-numeric: tabular-nums; }
.gaps th:first-child, .gaps td:first-child,
.gaps th:nth-child(2), .gaps td:nth-child(2),
.gaps th:last-child, .gaps td:last-child { text-align: left; font-variant-numeric: normal; }
.gaps th { color: var(--muted); font-weight: 500; }
pre.errlog {
  margin: 10px 0 0; padding: 10px 12px; border-radius: 8px;
  background: var(--plane); border: 1px solid var(--border);
  font-size: 11.5px; line-height: 1.65; overflow-x: auto; white-space: pre;
  color: var(--text-secondary);
}

@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
}
</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <h1>Claude 使用量ダッシュボード</h1>
  <span class="sub" id="genat"></span>
  <span class="spacer"></span>
  <button class="theme" id="themebtn" type="button">テーマ切替</button>
</header>

<section class="card hero" id="hero">
  <div class="gauge">
    <svg width="132" height="132" viewBox="0 0 132 132" role="img" aria-label="5時間枠の使用率">
      <circle class="gauge-track" cx="66" cy="66" r="54" fill="none" stroke-width="11"/>
      <circle id="g5" cx="66" cy="66" r="54" fill="none" stroke-width="11"
              stroke-linecap="round" transform="rotate(-90 66 66)"/>
      <text id="g5t" class="gauge-val" x="66" y="72" text-anchor="middle">–</text>
    </svg>
    <div class="glabel">5時間枠</div>
    <div class="greset" id="g5r"></div>
  </div>
  <div class="gauge">
    <svg width="132" height="132" viewBox="0 0 132 132" role="img" aria-label="7日枠の使用率">
      <circle class="gauge-track" cx="66" cy="66" r="54" fill="none" stroke-width="11"/>
      <circle id="g7" cx="66" cy="66" r="54" fill="none" stroke-width="11"
              stroke-linecap="round" transform="rotate(-90 66 66)"/>
      <text id="g7t" class="gauge-val" x="66" y="72" text-anchor="middle">–</text>
    </svg>
    <div class="glabel">7日枠</div>
    <div class="greset" id="g7r"></div>
  </div>
  <div class="tiles" id="tiles"></div>
</section>

<section class="card health" id="health">
  <h2>計上の健全性</h2>
  <p class="note">statusline（コスト台帳）とトランスクリプト実測を突き合わせ、記録できていないホスト・日を洗い出す。</p>
  <div class="verdict" id="verdict"></div>
  <div class="hosts" id="hosts"></div>
  <div class="gaps" id="gaps"></div>
  <div id="errbox"></div>
</section>

<section class="card">
  <h2>使用率の推移</h2>
  <p class="note" id="histnote"></p>
  <div class="legend">
    <span class="key"><i style="background:var(--s1)"></i>5時間枠</span>
    <span class="key"><i style="background:var(--s2)"></i>7日枠</span>
  </div>
  <div class="chart" id="histchart"></div>
  <details class="tv"><summary>表で見る（直近30点）</summary><div id="histtable"></div></details>
</section>

<section class="card">
  <h2>日別コスト（API換算）</h2>
  <p class="note">Claude Code が報告する実コスト。サブスク利用分は実際には課金されず、「従量課金なら いくらぶんか」を表す。</p>
  <div class="legend">
    <span class="key"><i style="background:var(--s1)"></i>サブスク枠（実支払いなし）</span>
    <span class="key"><i style="background:var(--s2)"></i>API従量課金</span>
    <span class="key"><i style="background:var(--s3)"></i>トランスクリプト実測からの推定</span>
  </div>
  <div class="chart" id="costchart"></div>
  <details class="tv"><summary>表で見る</summary><div id="costtable"></div></details>
</section>

<div class="grid2">
  <section class="card">
    <h2>プロジェクト別</h2>
    <p class="note">トランスクリプトの実測トークン（入力＋出力＋キャッシュ書き込み）。</p>
    <div id="projbars"></div>
    <details class="tv"><summary>表で見る</summary><div id="projtable"></div></details>
  </section>
  <section class="card">
    <h2>モデル別</h2>
    <p class="note">公式レートでのコスト推定（棒の長さもコスト）。トークンの内訳はホバーで。</p>
    <div id="modelbars"></div>
    <details class="tv"><summary>表で見る</summary><div id="modeltable"></div></details>
  </section>
</div>

<div class="grid2">
  <section class="card">
    <h2>キャッシュ効率</h2>
    <p class="note">入力トークンのうち、キャッシュから読めた割合。高いほど安く済んでいる（読み込みは入力単価の約1/10）。</p>
    <div id="cachebox"></div>
  </section>
  <section class="card">
    <h2>ツール使用</h2>
    <p class="note">アシスタントが呼び出したツールの回数（上位）。</p>
    <div id="toolbars"></div>
  </section>
</div>

<section class="card">
  <h2>作業時間帯</h2>
  <p class="note">曜日 × 時刻ごとの応答数。色が濃いほど活動が多い。</p>
  <div class="chart"><div class="heat" id="heat"></div></div>
</section>

<footer class="notes" id="footnotes"></footer>
</div>

<div id="tip" role="status" aria-live="polite"></div>

<script id="payload" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("payload").textContent);
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- helpers ---------- */
const fmtInt = n => Math.round(n || 0).toLocaleString("ja-JP");
const fmtTok = n => {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));  // カウントアップ中の小数をそのまま出さない
};
const fmtUsd = n => "$" + (n || 0).toLocaleString("ja-JP", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const pad = n => String(n).padStart(2, "0");
const dt = t => new Date(t * 1000);
const fmtDateTime = t => { const d = dt(t); return `${d.getMonth()+1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const fmtDate = t => { const d = dt(t); return `${d.getMonth()+1}/${d.getDate()}`; };
const el = (tag, attrs, kids) => {
  const n = document.createElementNS(attrs && attrs.__svg ? "http://www.w3.org/2000/svg" : "http://www.w3.org/1999/xhtml", tag);
  for (const k in (attrs || {})) { if (k !== "__svg") n.setAttribute(k, attrs[k]); }
  for (const c of (kids || [])) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  return n;
};
const svgEl = (tag, attrs, kids) => el(tag, Object.assign({__svg: 1}, attrs || {}), kids);

/* ---------- theme ---------- */
const btn = document.getElementById("themebtn");
btn.addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  const sysDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const next = cur ? (cur === "dark" ? "light" : "dark") : (sysDark ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", next);
});

/* ---------- tooltip ---------- */
const tip = document.getElementById("tip");
let tipOn = false;
function showTip(html, x, y) {
  tip.innerHTML = html;
  tip.style.opacity = "1";
  tipOn = true;
  const r = tip.getBoundingClientRect();
  let left = x + 14, top = y - r.height - 12;
  if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
  if (top < 8) top = y + 18;
  tip.style.left = left + "px";
  tip.style.top = top + "px";
}
function hideTip() { if (tipOn) { tip.style.opacity = "0"; tipOn = false; } }
window.addEventListener("scroll", hideTip, {passive: true});

/* ---------- reveal on scroll ----------
   1ノードに複数のアニメーションがぶら下がるので Map でまとめて持つ。
   IntersectionObserver が使えない環境や、発火しないまま印刷/スクショされる場合に
   備えて、一定時間後に未実行ぶんを必ず流す（グラフが空のまま残らないように）。 */
const revealers = new Map();
function onReveal(node, fn) {
  if (REDUCED) { fn(); return; }
  if (!revealers.has(node)) revealers.set(node, []);
  revealers.get(node).push(fn);
}
function flush(node) {
  const fns = revealers.get(node);
  if (!fns) return;
  revealers.delete(node);
  for (const fn of fns) { try { fn(); } catch (e) { console.error(e); } }
}
function flushAll() { for (const node of [...revealers.keys()]) flush(node); }
function startObserver() {
  if (!("IntersectionObserver" in window)) { flushAll(); return; }
  const io = new IntersectionObserver(entries => {
    for (const e of entries) {
      if (e.isIntersecting) { flush(e.target); io.unobserve(e.target); }
    }
  }, {threshold: 0.12});
  for (const node of revealers.keys()) io.observe(node);
  setTimeout(flushAll, 2500);  // 保険: 何があっても2.5秒で全部出す
}

/* count-up */
function countUp(node, to, fmt, ms) {
  if (REDUCED) { node.textContent = fmt(to); return; }
  const t0 = performance.now(), dur = ms || 900;
  (function step(now) {
    const p = Math.min(1, (now - t0) / dur);
    const e = 1 - Math.pow(1 - p, 3);
    node.textContent = fmt(to * e);
    if (p < 1) requestAnimationFrame(step);
  })(performance.now());
}

/* ---------- header ---------- */
document.getElementById("genat").textContent = "生成 " + fmtDateTime(D.generated_at);

/* ---------- gauges ---------- */
const R = 54, CIRC = 2 * Math.PI * R;
function gauge(idArc, idText, idReset, win, label) {
  const arc = document.getElementById(idArc);
  const txt = document.getElementById(idText);
  const rst = document.getElementById(idReset);
  arc.style.strokeDasharray = CIRC;
  arc.style.strokeDashoffset = CIRC;
  if (!win || win.used_percentage == null) {
    txt.textContent = "–";
    rst.textContent = "データなし";
    arc.setAttribute("stroke", "var(--grid)");
    return;
  }
  const pct = Math.max(0, Math.min(100, win.used_percentage));
  const color = pct >= 80 ? "var(--critical)" : pct >= 50 ? "var(--warning)" : "var(--s1)";
  arc.setAttribute("stroke", color);
  const resets = win.resets_at;
  if (resets) {
    const past = resets <= D.generated_at;
    rst.textContent = past ? "リセット済み" : "→ " + fmtDateTime(resets);
  }
  onReveal(document.getElementById("hero"), () => {
    if (!REDUCED) arc.style.transition = "stroke-dashoffset 1100ms cubic-bezier(.2,.7,.3,1)";
    arc.style.strokeDashoffset = CIRC * (1 - pct / 100);
    countUp(txt, pct, v => Math.round(v) + "%", 1100);
  });
}
gauge("g5", "g5t", "g5r", D.rate_limits.five_hour, "5h");
gauge("g7", "g7t", "g7r", D.rate_limits.seven_day, "7d");

/* ---------- hero tiles ---------- */
(function tiles() {
  const t = D.totals;
  const today = new Date(D.generated_at * 1000);
  const key = `${today.getFullYear()}-${pad(today.getMonth()+1)}-${pad(today.getDate())}`;
  const todayRow = D.ledger.find(r => r.day === key);
  const todayCost = todayRow ? todayRow.subscription + todayRow.api : 0;
  const ledgerTotal = D.ledger.reduce((s, r) => s + r.subscription + r.api, 0);
  const activeSessions = D.sessions.length;
  const rows = [
    ["今日のAPI換算", fmtUsd, todayCost, D.ledger.length ? D.ledger[D.ledger.length-1].day + " まで記録" : ""],
    ["記録期間の合計", fmtUsd, ledgerTotal, D.ledger.length ? D.ledger.length + "日ぶん" : ""],
    ["総トークン", fmtTok, t.input + t.output + t.cache_write + t.cache_read, "キャッシュ読込を含む"],
    ["応答数", fmtInt, t.messages, t.sessions + " セッション"],
    ["直近のセッション", fmtInt, activeSessions, "48時間以内"],
  ];
  const box = document.getElementById("tiles");
  for (const [label, fmt, val, sub] of rows) {
    const v = el("div", {class: "value"});
    box.appendChild(el("div", {class: "tile"}, [
      el("div", {class: "label"}, [label]), v, el("div", {class: "delta"}, [sub || ""])
    ]));
    onReveal(document.getElementById("hero"), () => countUp(v, val, fmt));
  }
})();

/* ---------- health: are we recording everything? ---------- */
(function health() {
  const H = D.health;
  const card = document.getElementById("health");
  const hostBox = document.getElementById("hosts");
  const fmtDay = d => d ? d.slice(5).replace("-", "/") : "–";

  for (const h of H.hosts) {
    const dl = el("dl", {}, [
      el("dt", {}, ["応答"]),        el("dd", {}, [fmtInt(h.messages)]),
      el("dt", {}, ["トークン"]),    el("dd", {}, [fmtTok(h.tokens)]),
      el("dt", {}, ["推定コスト"]),  el("dd", {}, [fmtUsd(h.cost)]),
      el("dt", {}, ["台帳の記録"]),  el("dd", {}, [h.ledger_cost > 0 ? fmtUsd(h.ledger_cost) : "なし"]),
      el("dt", {}, ["最終活動"]),    el("dd", {}, [fmtDay(h.last_day)]),
    ]);
    hostBox.appendChild(el("div", {class: "hcard"}, [
      el("div", {class: "hname"}, [h.label]), dl,
    ]));
  }

  // 判定: 未計上の日があれば警告、statusline.err があれば異常
  const nErr = H.errors.exists ? H.errors.count : 0;
  const nGap = H.gaps.length;
  const state = nErr ? "bad" : (nGap ? "warn" : "ok");
  if (state !== "ok") card.classList.add(state);
  const msg = state === "bad"
    ? `statusline がエラーを記録している（${fmtInt(nErr)}件）。下のログを確認すること。`
    : state === "warn"
      ? `トランスクリプトに活動があるのに台帳に記録が無い日が ${nGap} 件（推定 ${fmtUsd(H.gap_cost)} ぶん）。`
      : "台帳とトランスクリプトの対象ホスト・日は一致している。計上漏れは検出されていない。";
  document.getElementById("verdict").appendChild(el("span", {class: "dot"}));
  document.getElementById("verdict").appendChild(el("span", {}, [msg]));

  if (nGap) {
    const box = document.getElementById("gaps");
    const rows = H.gaps.slice(0, 20).map(g =>
      [g.day, g.label, fmtInt(g.messages), fmtUsd(g.est_cost), g.reason]);
    box.appendChild(el("div", {class: "note", style: "color:var(--muted);font-size:12.5px;margin-bottom:6px"},
      ["未計上の疑いがある日（推定コストは公式レートからの目安で、台帳へは遡って足さない）"]));
    box.appendChild(table(["日付", "ホスト", "応答", "推定$", "理由"], rows));
    if (H.gaps.length > 20) {
      box.appendChild(el("div", {class: "note"}, [`ほか ${H.gaps.length - 20} 件`]));
    }
  }

  if (H.errors.exists && H.errors.lines.length) {
    const box = document.getElementById("errbox");
    box.appendChild(el("div", {class: "note", style: "margin-top:16px"},
      [`statusline.err（末尾 ${H.errors.lines.length} 行 / 全 ${fmtInt(H.errors.count)} 行・最終 ${fmtDateTime(H.errors.mtime)}）`]));
    box.appendChild(el("pre", {class: "errlog"}, [H.errors.lines.join("\n")]));
  }
})();

/* ---------- line chart: rate-limit history ---------- */
(function history() {
  const host = document.getElementById("histchart");
  const data = D.history;
  if (!data.length) { host.textContent = "履歴がまだありません。"; return; }
  document.getElementById("histnote").textContent =
    `${fmtDate(data[0].t)} 〜 ${fmtDate(data[data.length-1].t)}・${fmtInt(data.length)}点`;

  const W = Math.max(680, Math.min(1120, host.clientWidth || 900)), H = 260;
  const M = {t: 14, r: 16, b: 26, l: 34};
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const t0 = data[0].t, t1 = data[data.length-1].t || t0 + 1;
  const X = t => M.l + ((t - t0) / Math.max(1, t1 - t0)) * iw;
  const Y = v => M.t + ih - (Math.max(0, Math.min(100, v)) / 100) * ih;

  const svg = svgEl("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
                            "aria-label": "5時間枠と7日枠の使用率の推移"});
  for (const v of [0, 25, 50, 75, 100]) {
    svg.appendChild(svgEl("line", {class: "gridline", x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v)}));
    svg.appendChild(svgEl("text", {class: "t", x: M.l - 7, y: Y(v) + 4, "text-anchor": "end",
                                   fill: "var(--muted)", "font-size": 11}, [v + "%"]));
  }
  // x軸: 日付ラベルを5つ
  for (let i = 0; i <= 4; i++) {
    const t = t0 + (t1 - t0) * (i / 4);
    svg.appendChild(svgEl("text", {x: X(t), y: H - 8, "text-anchor": "middle",
                                   fill: "var(--muted)", "font-size": 11}, [fmtDate(t)]));
  }
  const paths = [];
  for (const [key, color] of [["five", "var(--s1)"], ["seven", "var(--s2)"]]) {
    let d = "";
    for (const p of data) {
      if (p[key] == null) continue;
      d += (d ? "L" : "M") + X(p.t).toFixed(1) + " " + Y(p[key]).toFixed(1);
    }
    if (!d) continue;
    const path = svgEl("path", {class: "line-path", d: d, stroke: color});
    svg.appendChild(path);
    paths.push(path);
  }
  // クロスヘア
  const cross = svgEl("line", {y1: M.t, y2: M.t + ih, stroke: "var(--axis)", "stroke-width": 1, opacity: 0});
  svg.appendChild(cross);
  const hit = svgEl("rect", {x: M.l, y: M.t, width: iw, height: ih, fill: "transparent"});
  svg.appendChild(hit);
  hit.addEventListener("mousemove", ev => {
    const box = svg.getBoundingClientRect();
    const px = ev.clientX - box.left;
    const t = t0 + ((px - M.l) / iw) * (t1 - t0);
    let best = data[0], bd = Infinity;
    for (const p of data) { const d2 = Math.abs(p.t - t); if (d2 < bd) { bd = d2; best = p; } }
    cross.setAttribute("x1", X(best.t)); cross.setAttribute("x2", X(best.t));
    cross.setAttribute("opacity", 1);
    showTip(`<b>${fmtDateTime(best.t)}</b><br><span class="k">5時間枠</span> ${best.five ?? "–"}%<br><span class="k">7日枠</span> ${best.seven ?? "–"}%`,
            ev.clientX, ev.clientY);
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  host.appendChild(svg);

  onReveal(host, () => {
    paths.forEach((p, i) => {
      const len = p.getTotalLength();
      p.style.strokeDasharray = len;
      p.style.strokeDashoffset = len;
      requestAnimationFrame(() => {
        p.style.transition = `stroke-dashoffset 1500ms ${i * 220}ms cubic-bezier(.25,.6,.3,1)`;
        p.style.strokeDashoffset = 0;
      });
    });
  });

  // 表ビュー（直近30点）
  const tail = data.slice(-30).reverse();
  document.getElementById("histtable").appendChild(table(
    ["時刻", "5時間枠", "7日枠"],
    tail.map(p => [fmtDateTime(p.t), (p.five ?? "–") + "%", (p.seven ?? "–") + "%"])
  ));
})();

/* ---------- stacked columns: daily cost ---------- */
(function cost() {
  const host = document.getElementById("costchart");
  const rows = D.ledger;
  if (!rows.length) { host.textContent = "台帳がまだありません。"; return; }
  const W = Math.max(680, Math.min(1120, host.clientWidth || 900)), H = 250;
  const M = {t: 12, r: 14, b: 30, l: 46};
  const iw = W - M.l - M.r, ih = H - M.t - M.b;
  const max = Math.max(...rows.map(r => Math.max(r.subscription + r.api, r.estimated)), 1);
  const nice = Math.ceil(max / 25) * 25 || 25;
  const band = iw / rows.length;
  const bw = Math.min(24, band - 4);
  const Y = v => M.t + ih - (v / nice) * ih;

  const svg = svgEl("svg", {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
                            "aria-label": "日別のAPI換算コスト"});
  for (let i = 0; i <= 4; i++) {
    const v = nice * i / 4;
    svg.appendChild(svgEl("line", {class: "gridline", x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v)}));
    svg.appendChild(svgEl("text", {x: M.l - 7, y: Y(v) + 4, "text-anchor": "end",
                                   fill: "var(--muted)", "font-size": 11}, ["$" + Math.round(v)]));
  }
  const bars = [];
  rows.forEach((r, i) => {
    const x = M.l + band * i + (band - bw) / 2;
    const total = r.subscription + r.api;
    // 積み上げは下からサブスク→API。2pxのサーフェスギャップで区切る
    const segs = [
      ["subscription", r.subscription, "var(--s1)"],
      ["api", r.api, "var(--s2)"],
    ];
    let acc = 0;
    for (const [k, v, color] of segs) {
      if (v <= 0) continue;
      const y0 = Y(acc + v), y1 = Y(acc);
      const h = Math.max(1, y1 - y0 - (acc > 0 ? 2 : 0));
      const rect = svgEl("rect", {x: x, y: y1 - h, width: bw, height: 0, rx: 3, fill: color,
                                  "data-h": h, "data-y": y1 - h});
      svg.appendChild(rect);
      bars.push(rect);
      acc += v;
    }
    const hit = svgEl("rect", {x: M.l + band * i, y: M.t, width: band, height: ih, fill: "transparent"});
    const missing = r.estimated > 0 && total === 0;
    hit.addEventListener("mousemove", ev => showTip(
      `<b>${r.day}</b><br><span class="k">サブスク</span> ${fmtUsd(r.subscription)}<br>` +
      `<span class="k">API</span> ${fmtUsd(r.api)}<br><span class="k">合計</span> ${fmtUsd(total)}<br>` +
      `<span class="k">実測からの推定</span> ${fmtUsd(r.estimated)}` +
      (missing ? `<br><b>台帳に記録なし（計上漏れ）</b>` : ""),
      ev.clientX, ev.clientY));
    hit.addEventListener("mouseleave", hideTip);
    svg.appendChild(hit);
    if (i % Math.ceil(rows.length / 10) === 0) {
      const d = r.day.slice(5).replace("-", "/");
      svg.appendChild(svgEl("text", {x: M.l + band * i + band / 2, y: H - 9, "text-anchor": "middle",
                                     fill: "var(--muted)", "font-size": 11}, [d]));
    }
  });
  // トランスクリプト実測からの推定を重ねる。棒（台帳）だけが低い日＝statuslineが動いていない日
  let dEst = "";
  rows.forEach((r, i) => {
    const x = M.l + band * i + band / 2;
    dEst += (dEst ? "L" : "M") + x.toFixed(1) + " " + Y(r.estimated).toFixed(1);
  });
  if (dEst) {
    svg.appendChild(svgEl("path", {class: "line-path", d: dEst, stroke: "var(--s3)",
                                   "stroke-dasharray": "4 3", "stroke-width": 1.5, opacity: .9}));
  }
  host.appendChild(svg);
  onReveal(host, () => {
    bars.forEach((b, i) => {
      const h = +b.getAttribute("data-h"), y = +b.getAttribute("data-y");
      b.setAttribute("y", y + h);
      requestAnimationFrame(() => {
        b.style.transition = `height 700ms ${Math.min(600, i * 14)}ms cubic-bezier(.2,.7,.3,1), y 700ms ${Math.min(600, i * 14)}ms cubic-bezier(.2,.7,.3,1)`;
        b.setAttribute("height", h);
        b.setAttribute("y", y);
      });
    });
  });
  document.getElementById("costtable").appendChild(table(
    ["日付", "サブスク", "API", "台帳合計", "実測推定"],
    rows.slice().reverse().map(r => [r.day, fmtUsd(r.subscription), fmtUsd(r.api),
                                     fmtUsd(r.subscription + r.api), fmtUsd(r.estimated)])
  ));
})();

/* ---------- horizontal bars ---------- */
function barList(host, items, valueOf, labelOf, color) {
  const max = Math.max(...items.map(valueOf), 1);
  const fills = [];
  for (const it of items) {
    const fill = el("div", {class: "fill", style: `background:${color}`});
    const row = el("div", {class: "rowbar"}, [
      el("div", {class: "nm", title: it.name}, [it.name]),
      el("div", {class: "track"}, [fill]),
      el("div", {class: "val"}, [labelOf(it)]),
    ]);
    row.addEventListener("mousemove", ev => showTip(tipOf(it), ev.clientX, ev.clientY));
    row.addEventListener("mouseleave", hideTip);
    host.appendChild(row);
    fills.push([fill, (valueOf(it) / max) * 100]);
  }
  onReveal(host, () => {
    fills.forEach(([f, pct], i) => requestAnimationFrame(() => {
      f.style.transition = `width 800ms ${i * 60}ms cubic-bezier(.2,.7,.3,1)`;
      f.style.width = pct + "%";
    }));
  });
}
function tipOf(it) {
  if (it.n != null && it.input == null) return `<b>${it.name}</b><br>${fmtInt(it.n)} 回`;
  const tok = it.input + it.output + it.cache_write + it.cache_read;
  return `<b>${it.name}</b><br>` +
    `<span class="k">入力</span> ${fmtTok(it.input)} / <span class="k">出力</span> ${fmtTok(it.output)}<br>` +
    `<span class="k">キャッシュ</span> 書 ${fmtTok(it.cache_write)} / 読 ${fmtTok(it.cache_read)}<br>` +
    `<span class="k">合計</span> ${fmtTok(tok)}・<span class="k">応答</span> ${fmtInt(it.messages)}<br>` +
    `<span class="k">推定コスト</span> ${fmtUsd(it.cost)}`;
}

barList(document.getElementById("projbars"), D.projects,
        it => it.input + it.output + it.cache_write,
        it => fmtTok(it.input + it.output + it.cache_write), "var(--s1)");
barList(document.getElementById("modelbars"), D.models,
        it => it.cost, it => fmtUsd(it.cost), "var(--s1)");
barList(document.getElementById("toolbars"), D.tools,
        it => it.n, it => fmtInt(it.n) + " 回", "var(--s1)");

function table(head, rows) {
  const thead = el("tr", {}, head.map(h => el("th", {}, [h])));
  const body = rows.map(r => el("tr", {}, r.map(c => el("td", {}, [String(c)]))));
  return el("table", {}, [el("thead", {}, [thead]), el("tbody", {}, body)]);
}
document.getElementById("projtable").appendChild(table(
  ["プロジェクト", "入力", "出力", "キャッシュ読", "応答", "推定$"],
  D.projects.map(p => [p.name, fmtTok(p.input), fmtTok(p.output), fmtTok(p.cache_read), fmtInt(p.messages), fmtUsd(p.cost)])
));
document.getElementById("modeltable").appendChild(table(
  ["モデル", "入力", "出力", "思考", "応答", "推定$"],
  D.models.map(m => [m.name, fmtTok(m.input), fmtTok(m.output), fmtTok(m.thinking), fmtInt(m.messages), fmtUsd(m.cost)])
));

/* ---------- cache efficiency ---------- */
(function cache() {
  const t = D.totals;
  const totalIn = t.input + t.cache_read + t.cache_write;
  const hit = totalIn ? (t.cache_read / totalIn) * 100 : 0;
  // キャッシュ無しなら全部が新規入力だったと仮定した場合との差額（読込 0.1x / 書込 1.25x を戻す）
  const host = document.getElementById("cachebox");
  const meter = el("div", {class: "meter"}, [el("div", {})]);
  const v = el("div", {class: "value"});
  host.appendChild(el("div", {class: "tile"}, [
    el("div", {class: "label"}, ["キャッシュ読込の割合"]), v,
    el("div", {class: "delta"}, [`入力系トークン ${fmtTok(totalIn)} のうち ${fmtTok(t.cache_read)}`]),
  ]));
  host.appendChild(meter);
  const grid = el("div", {class: "tiles", style: "margin-top:18px"});
  const cells = [
    ["キャッシュ読込", fmtTok, t.cache_read, "入力単価の約1/10"],
    ["キャッシュ書込", fmtTok, t.cache_write, "入力単価の1.25〜2倍"],
    ["新規入力", fmtTok, t.input, "全額"],
    ["思考トークン", fmtTok, t.thinking, "出力に含まれる"],
    ["ツール呼び出し", fmtInt, t.tool_calls, "Edit / Bash / Read など"],
    ["API応答", fmtInt, t.messages, `記録 ${fmtInt(t.records)} 行を集約`],
  ];
  for (const [label, fmt, val, sub] of cells) {
    const node = el("div", {class: "value"});
    grid.appendChild(el("div", {class: "tile"}, [
      el("div", {class: "label"}, [label]), node, el("div", {class: "delta"}, [sub]),
    ]));
    onReveal(host, () => countUp(node, val, fmt));
  }
  host.appendChild(grid);
  onReveal(host, () => {
    countUp(v, hit, x => x.toFixed(1) + "%");
    requestAnimationFrame(() => {
      meter.firstChild.style.transition = "width 900ms cubic-bezier(.2,.7,.3,1)";
      meter.firstChild.style.width = hit + "%";
    });
  });
})();

/* ---------- heatmap ---------- */
(function heatmap() {
  const host = document.getElementById("heat");
  const WD = ["月", "火", "水", "木", "金", "土", "日"];
  const map = new Map();
  let max = 0;
  for (const c of D.heat) { map.set(c.wday * 24 + c.hour, c.n); if (c.n > max) max = c.n; }
  const steps = ["var(--seq-100)", "var(--seq-250)", "var(--seq-400)", "var(--seq-550)", "var(--seq-700)"];

  host.appendChild(el("div", {}));
  for (let h = 0; h < 24; h++) {
    host.appendChild(el("div", {class: "collab"}, [h % 3 === 0 ? String(h) : ""]));
  }
  const cells = [];
  for (let w = 0; w < 7; w++) {
    host.appendChild(el("div", {class: "rowlab"}, [WD[w]]));
    for (let h = 0; h < 24; h++) {
      const n = map.get(w * 24 + h) || 0;
      const cell = el("div", {class: "cell"});
      if (n > 0) {
        const idx = Math.min(steps.length - 1, Math.floor((n / max) * steps.length));
        cell.style.background = steps[idx];
      }
      cell.addEventListener("mousemove", ev => showTip(
        `<b>${WD[w]}曜 ${pad(h)}:00</b><br>${fmtInt(n)} 応答`, ev.clientX, ev.clientY));
      cell.addEventListener("mouseleave", hideTip);
      host.appendChild(cell);
      cells.push(cell);
    }
  }
  onReveal(host, () => {
    cells.forEach((c, i) => {
      if (REDUCED) { c.style.opacity = 1; return; }
      c.style.transition = `opacity 420ms ${Math.min(900, i * 4)}ms ease`;
      requestAnimationFrame(() => { c.style.opacity = 1; });
    });
  });
})();

/* ---------- footnotes ---------- */
(function notes() {
  const t = D.totals;
  const gone = t.files_gone
    ? `（うち ${fmtInt(t.files_gone)} ファイルは Claude Code の自動削除で消滅済み・集計はキャッシュに保持）`
    : "";
  document.getElementById("footnotes").innerHTML =
    `トランスクリプト ${fmtInt(t.files)} ファイルを集計${gone}。応答 ${fmtInt(t.messages)} 件／` +
    `セッション ${fmtInt(t.sessions)} 件／APIエラー応答 ${fmtInt(t.errors)} 件。<br>` +
    `「日別コスト」は Claude Code が報告する実測値。「推定コスト」は公式の従量課金レート` +
    `（キャッシュ読込 0.1倍・書込 1.25〜2倍）でトークンから計算した目安で、実際の請求額ではない。<br>` +
    `このページはローカル生成・ローカル完結。プロジェクト名やパスを含むため外部に公開しないこと。`;
})();

startObserver();
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="Claude使用量ダッシュボードを生成する")
    ap.add_argument("-o", "--out", default=DEFAULT_OUT, help="出力先HTML（既定: リポジトリ直下の dashboard.html）")
    ap.add_argument("-q", "--quiet", action="store_true", help="何も出力しない（自動生成用）")
    args = ap.parse_args()

    t0 = time.time()
    payload = build_payload()
    html = TEMPLATE.replace(
        "__DATA__",
        json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
    )
    # ブラウザが書きかけのHTMLを読まないよう、一時ファイル経由で置換する
    import tempfile

    out_dir = os.path.dirname(os.path.abspath(args.out)) or "."
    fd, tmp = tempfile.mkstemp(dir=out_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, args.out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    if args.quiet:
        return

    t = payload["totals"]
    print(f"書き出し: {args.out}")
    print(
        f"  トランスクリプト {t['files']}ファイル"
        f"（現存 {t['files_present']} / 消滅済み {t['files_gone']}）"
        f" / 応答 {t['messages']:,}件 / セッション {t['sessions']}件 / {time.time() - t0:.1f}秒"
    )
    print(f"  プロジェクト {len(payload['projects'])}件 / モデル {len(payload['models'])}件 / "
          f"履歴 {len(payload['history'])}点 / 台帳 {len(payload['ledger'])}日")


if __name__ == "__main__":
    main()
