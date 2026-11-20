"""
オートレース版の健康チェック（30分おき）とダッシュボード用データの作成。競輪版 keirinbusines/tools/health.py と同じ形。
  python tools/health.py --data DIR [--no-act]
異常は GitHub の Issue で知らせ（内容が変わったときだけ追記）、直ったら閉じる。直せるものは自動で直す。
結果:
  DIR/health/status.json     最新の結果（problems / notes / actions）
  DIR/health/log.jsonl       履歴
  DIR/health/dashboard.json  ダッシュボード（docs/index.html）用
  DIR/paper/summary.json     直前判定（ペーパートレード）の成績

確認と自動対応
  1. オッズ監視が動いているか（夜も止めずに動く作り）→ 止まっていれば時間に関係なく起動
  2. オッズ監視が固まっていないか（状態ファイルが45分以上更新なし）→ 止めて起動し直す
  3. オッズの記録が60分以上止まっていないか（開催中のみ）
  4. 前日のオッズ記録の取りこぼし（締切3分以内の記録があるレースが9割未満）
  5. 毎朝の収集で前日のレース結果が保存されたか → 9:00を過ぎて無く、収集が動いていなければ起動
  6. 収集・オッズ監視・過去分の取り込みの直近の失敗
"""
import argparse
import glob
import gzip
import json
import os
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
REPO = os.environ.get("GITHUB_REPOSITORY", "exnull0511-create/auotrace")
TOKEN = os.environ.get("GITHUB_TOKEN")
TITLE = "【自動監視】データ収集の異常"
WATCH_START = "2026-11-20"   # 修正後のオッズ監視を1日通して動かし始めた日
ACTIVE = ("in_progress", "queued", "waiting", "pending", "requested")
PLACES = {"kawaguchi": "川口", "kawaguchi2": "川口", "isesaki": "伊勢崎", "hamamatsu": "浜松", "iizuka": "飯塚", "sanyou": "山陽", "funabashi": "船橋"}
RULE = 1.2
VERIFY_DATE = "12/09"


def api(path, method="GET", body=None):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read()
        return json.loads(t) if t else {}


def runs(wf, n=20):
    try:
        return api(f"/actions/workflows/{wf}/runs?per_page={n}").get("workflow_runs", [])
    except Exception as e:
        print("API失敗", wf, e)
        return []


def jst(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(JST)


def read_gz(p):
    try:
        return [json.loads(x) for x in gzip.open(p, "rt", encoding="utf-8")]
    except Exception:
        return []


def read_jsonl(p):
    out = []
    try:
        for x in open(p, encoding="utf-8"):
            try:
                out.append(json.loads(x))
            except Exception:
                pass
    except Exception:
        pass
    return out


def meets_on(data, d):
    """カレンダーのキャッシュからその日の開催 [(場key, 最終R)]"""
    try:
        return [(m["place_key"], m["last_race"]) for m in json.load(open(data / "state" / "calendar" / f"{d[:7]}.json"))
                if m["date"] == d]
    except Exception:
        return []


def ts_files(data, d):
    return {Path(f).stem: f for f in glob.glob(str(data / "odds_ts" / d[:4] / d / "*.jsonl"))}


def coverage(data, d, race_ids, upto=None):
    """レース（race_id の一覧）のうち、オッズ記録・締切3分以内の記録・直前判定があるレースの数"""
    fs = ts_files(data, d)
    judged = {x["race_id"] for x in read_jsonl(data / "paper" / f"{d}.jsonl")}
    rec = near = jd = n = 0
    for rid in race_ids:
        key = rid.split("_", 1)[1]
        xs = read_jsonl(fs[key]) if key in fs else []
        if upto and (not xs or xs[0].get("close", "99:99") > upto):
            continue   # 当日分は「記録があり締切を過ぎたレース」だけ（記録の無いレースは翌朝の結果保存後に数える）
        n += 1
        rec += bool(xs)
        near += any(0 <= x["m_left"] <= 3 for x in xs)
        jd += rid in judged
    return {"races": n, "recorded": rec, "near": near, "judged": jd}


def day_race_ids(data, d):
    """結果が保存済みならその race_id、まだなら カレンダーから作る。戻り値: (race_id一覧, 結果保存済みか)"""
    rf = data / "races" / d[:4] / f"{d}.jsonl.gz"
    if rf.exists():
        return [r["race_id"] for r in read_gz(rf)], True
    return [f"{d}_{k}_{n:02d}" for k, last in meets_on(data, d) for n in range(1, last + 1)], False


def success_rate(rs, n=14):
    done = [r for r in rs if r["status"] == "completed" and r["conclusion"] not in ("cancelled", "skipped")][:n]
    return (sum(r["conclusion"] == "success" for r in done) / len(done)) if done else None, done


# ---------------- ペーパートレードの成績 ----------------

def paper_summary(data):
    """各レースで締切に一番近い判定の推奨を、確定オッズで払い戻したとする"""
    tot = {"bets": 0, "hits": 0, "ret": 0.0}
    days = []
    for pf in sorted(glob.glob(str(data / "paper" / "*.jsonl"))):
        d = Path(pf).stem
        rf = data / "races" / d[:4] / f"{d}.jsonl.gz"
        if not rf.exists():
            continue
        res = {}
        for r in read_gz(rf):
            w = [x["car"] for x in r["result"] if x.get("order") == 1]
            res[r["race_id"]] = (str(w[0]) if w else None, r["odds"].get("単勝") or {})
        last = {}
        for x in read_jsonl(pf):
            if x["race_id"] not in last or x["m_left"] <= last[x["race_id"]]["m_left"]:
                last[x["race_id"]] = x
        day = {"date": d, "bets": 0, "hits": 0, "ret": 0.0}
        for rid, x in last.items():
            if rid not in res:
                continue
            win, final = res[rid]
            for p in x["picks"]:
                day["bets"] += 1
                if p["car"] == win:
                    day["hits"] += 1
                    day["ret"] += final.get(p["car"]) or 0
        days.append(day)
        for k in tot:
            tot[k] += day[k]
    roi = lambda t: round(t["ret"] / t["bets"] * 100, 1) if t["bets"] else None  # noqa: E731
    return {"rule": f"期待値{RULE}以上の単勝（締切3分前〜締切の最後の判定）", "total": {**tot, "roi": roi(tot)},
            "days": [{**x, "roi": roi(x)} for x in days]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--no-act", action="store_true", help="Issue作成・再起動をしない（試験用）")
    a = ap.parse_args()
    now = datetime.now(JST)
    hm = now.strftime("%H:%M")
    today, yday = now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()
    problems, notes, actions = [], [], []

    def act(desc, fn):
        if a.no_act or not TOKEN:
            actions.append(desc + "（試験のため実行せず）")
            return
        try:
            fn()
            actions.append(desc)
        except Exception as e:
            actions.append(f"{desc} に失敗: {e}")

    W = {wf: runs(wf) for wf in ("odds-watch.yml", "collect.yml", "backfill.yml", "health.yml")}
    ws = {}
    try:
        ws = json.load(open(a.data / "health" / "watch_status.json"))
    except Exception:
        pass
    today_meets = meets_on(a.data, today)
    race_day = bool(today_meets)

    # 3. 最新の記録の新しさ（その日に締切前のレースがあるときだけ）
    latest = None
    for f in ts_files(a.data, today).values():
        xs = read_jsonl(f)
        if xs:
            t = datetime.fromisoformat(xs[-1]["t"]).replace(tzinfo=JST)
            latest = max(latest or t, t)
    # 1, 2. オッズ監視
    active = [r for r in W["odds-watch.yml"] if r["status"] in ACTIVE]
    # オッズ監視は夜も止めずに動き続ける作りなので、動いていなければ時間に関係なく起動する
    if not active:
        (problems if race_day and "09:40" <= hm <= "23:50" else notes).append("オッズ監視が動いていなかった（自動で起動した）")
        act("オッズ監視を起動", lambda: api("/actions/workflows/odds-watch.yml/dispatches", "POST", {"ref": "main"}))
    elif race_day and "09:40" <= hm <= "23:50":
        run = next((r for r in active if r["status"] == "in_progress"), active[0])   # 待ち状態の回より、動いている回を見る
        started = jst(run.get("run_started_at") or run["created_at"])
        age = (now - datetime.fromisoformat(ws["updated_at"])).total_seconds() / 60 if ws.get("updated_at") else None
        rec_age = (now - latest).total_seconds() / 60 if latest else None
        if run["status"] == "in_progress" and (now - started).total_seconds() > 50 * 60 and (age is None or age > 45) \
                and (rec_age is None or rec_age > 30):
            problems.append(f"オッズ監視が固まっている（状態の更新が{'なし' if age is None else f'{age:.0f}分前'}）。止めて起動し直した")
            act("オッズ監視を再起動", lambda: (api(f"/actions/runs/{run['id']}/cancel", "POST"),
                                          api("/actions/workflows/odds-watch.yml/dispatches", "POST", {"ref": "main"})))

    pending = [x for x in ws.get("next") or [] if x.get("close", "") > hm]
    if latest:
        age = (now - latest).total_seconds() / 60
        notes.append(f"最新のオッズ記録: {latest.strftime('%H:%M')}（{age:.0f}分前）")
        if pending and age > 60 and any(x["close"] <= (now + timedelta(minutes=60)).strftime("%H:%M") for x in pending):
            problems.append(f"オッズの記録が{age:.0f}分間止まっている（最後 {latest.strftime('%H:%M')}）")

    # 4, 5. 前日の取りこぼし・毎朝の収集
    yids, ydone = day_race_ids(a.data, yday)
    if ydone and yids:
        c = coverage(a.data, yday, yids)
        notes.append(f"前日 {yday}: {c['races']}レース中 締切3分以内の記録{c['near']}・直前判定{c['judged']}")
        if yday >= WATCH_START and c["near"] < 0.9 * c["races"]:
            have = set(ts_files(a.data, yday))
            miss = [rid.split("_", 1)[1] for rid in yids if rid.split("_", 1)[1] not in have]
            problems.append(f"前日のオッズ記録の取りこぼし: 締切3分以内の記録があるのは{c['near']}/{c['races']}レース" +
                            (f"（記録なしの例 {'、'.join(miss[:6])}）" if miss else ""))
    elif yids and hm >= "09:00":
        col_active = [r for r in W["collect.yml"] if r["status"] in ACTIVE]
        problems.append(f"毎朝の収集で前日（{yday}）のレース結果が保存されていない" + ("（収集は実行中）" if col_active else "（自動で起動した）"))
        if not col_active:
            act("毎朝の収集を起動", lambda: api("/actions/workflows/collect.yml/dispatches", "POST", {"ref": "main"}))

    # 6. 直近の失敗
    for wf, name in [("collect.yml", "毎朝の収集"), ("odds-watch.yml", "オッズ監視"), ("backfill.yml", "過去分の取り込み")]:
        _, done = success_rate(W[wf], 1)
        newer_active = done and any(r["status"] in ACTIVE and r["created_at"] > done[0]["created_at"] for r in W[wf])
        if done and done[0]["conclusion"] != "success" and not newer_active:
            problems.append(f"{name}の直近の実行が失敗（{jst(done[0]['created_at']).strftime('%m/%d %H:%M')}） {done[0]['html_url']}")

    prev = {}
    try:
        prev = json.load(open(a.data / "health" / "status.json"))
    except Exception:
        pass
    status = {"checked_at": now.isoformat(timespec="seconds"), "ok": not problems, "problems": problems, "notes": notes, "actions": actions}
    out = a.data / "health"
    out.mkdir(parents=True, exist_ok=True)
    (out / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(out / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(status, ensure_ascii=False) + "\n")
    print(json.dumps(status, ensure_ascii=False, indent=1))

    try:
        paper = paper_summary(a.data)
        (a.data / "paper").mkdir(exist_ok=True)
        (a.data / "paper" / "summary.json").write_text(json.dumps(paper, ensure_ascii=False, indent=1), encoding="utf-8")
        dash = dashboard(a.data, now, W, ws, status, paper, today_meets)
        (out / "dashboard.json").write_text(json.dumps(dash, ensure_ascii=False), encoding="utf-8")
    except Exception:
        traceback.print_exc()

    if a.no_act or not TOKEN:
        return
    try:
        issues = api("/issues?state=open&per_page=50")
        mine = [i for i in issues if i.get("title") == TITLE]
        if problems:
            body = "\n".join(f"- {p}" for p in problems) + "\n\n" + "\n".join(f"- {n}" for n in notes) + \
                   ("\n\n自動で行ったこと:\n" + "\n".join(f"- {x}" for x in actions) if actions else "") + \
                   f"\n\n確認時刻: {now.strftime('%m/%d %H:%M')}\n\n@{REPO.split('/')[0]}"
            if mine:
                if problems != prev.get("problems"):
                    api(f"/issues/{mine[0]['number']}/comments", "POST", {"body": body})
            else:
                api("/issues", "POST", {"title": TITLE, "body": body})
        elif mine:
            api(f"/issues/{mine[0]['number']}/comments", "POST", {"body": f"直りました（{now.strftime('%m/%d %H:%M')}）\n\n" + "\n".join(f"- {n}" for n in notes)})
            api(f"/issues/{mine[0]['number']}", "PATCH", {"state": "closed"})
    except Exception as e:
        print("Issue の更新に失敗", e)


# ---------------- ダッシュボード ----------------

def dashboard(data, now, W, ws, status, paper, today_meets):
    today = now.date().isoformat()
    hm = now.strftime("%H:%M")
    days = [(now.date() - timedelta(days=k)).isoformat() for k in range(1, 8)]

    pdays = {x["date"]: x for x in paper["days"]}
    cov_days = []
    for d in days:
        if d < WATCH_START:
            continue
        ids, done = day_race_ids(data, d)
        if done and ids:
            c = coverage(data, d, ids)
            pd = pdays.get(d) or {}
            cov_days.append({"date": d, **c, "bets": pd.get("bets", 0), "hits": pd.get("hits", 0), "roi": pd.get("roi")})
    tids, _ = day_race_ids(data, today)
    tc = coverage(data, today, tids, upto=hm) if tids else {"races": 0, "recorded": 0, "near": 0, "judged": 0}
    allc = cov_days + ([{"date": today, **tc}] if tc["races"] else [])
    tot = {k: sum(c[k] for c in allc) for k in ("races", "recorded", "near", "judged")}
    watch_rate = tot["near"] / tot["races"] if tot["races"] else None
    judge_rate = tot["judged"] / tot["near"] if tot["near"] else None

    # 今日の推奨（各レースで締切に一番近い判定）
    last = {}
    for x in read_jsonl(data / "paper" / f"{today}.jsonl"):
        if x["race_id"] not in last or x["m_left"] <= last[x["race_id"]]["m_left"]:
            last[x["race_id"]] = x
    picks = [{"race": x["race"], "close": x["close"], "car": p["car"], "odds": p["odds"], "p": p["p"], "ev": p["ev"]}
             for x in last.values() for p in x["picks"]]
    picks.sort(key=lambda p: (p["close"], p["race"], -p["ev"]))

    # データの量
    n_races, years = 0, {}
    for f in glob.glob(str(data / "races" / "*" / "*.jsonl.gz")):
        try:
            with gzip.open(f, "rb") as g:
                k = sum(1 for _ in g)
            n_races += k
            y = Path(f).parent.name
            years[y] = years.get(y, 0) + 1
        except Exception:
            pass
    try:
        done_days = len(set(open(data / "state" / "done.txt").read().split()))
    except Exception:
        done_days = 0
    goal_days = (now.date() - datetime(2018, 1, 1).date()).days
    ts_days = sorted(Path(p).name for p in glob.glob(str(data / "odds_ts" / "*" / "*")) if Path(p).name >= WATCH_START)
    ts_races = sum(len(glob.glob(str(data / "odds_ts" / d[:4] / d / "*.jsonl"))) for d in ts_days)

    def last_run(rs):
        r = rs[0] if rs else None
        return None if not r else {"at": jst(r["created_at"]).isoformat(timespec="minutes"), "status": r["status"], "conclusion": r["conclusion"], "url": r["html_url"]}

    def rate(wf, n=14):
        return success_rate(W.get(wf, []), n)[0]

    def active(wf):
        return any(r["status"] in ACTIVE for r in W.get(wf, []))

    hc = [r for r in W.get("health.yml", []) if (now - jst(r["created_at"])).total_seconds() < 86400]
    health_runs = len(hc) + 1

    # オッズ監視係
    wstate = ws.get("state")
    w_age = (now - datetime.fromisoformat(ws["updated_at"])).total_seconds() / 60 if ws.get("updated_at") else None
    near_now = [x for x in ws.get("watching") or [] if x.get("close", "") >= hm]
    if active("odds-watch.yml") and wstate == "watching" and (w_age if w_age is not None else 99) < 30:
        if near_now:
            w_doing, w_mode = "見ている: " + "、".join(f"{x['race']}（締切{x['close']}）" for x in near_now[:3]), "busy"
        else:
            nx = (ws.get("next") or [{}])[0]
            w_doing, w_mode = f"次の締切を待っている: {nx.get('race', '-')}（{nx.get('close', '-')}）", "wait"
    elif active("odds-watch.yml") and wstate == "standby":
        w_doing, w_mode = "今日のレースは終了。次の開催まで待機中（止まらずに翌日も見る）", "off"
    elif active("odds-watch.yml"):
        w_doing, w_mode = "出勤中（当日のレース一覧を取得中）", "busy"
    elif today_meets and "09:40" <= hm <= "23:50" and wstate != "finished":
        w_doing, w_mode = "止まっている（見張り係が起こしに行く）", "down"
    elif not today_meets:
        w_doing, w_mode = "今日は開催なし。お休み", "off"
    else:
        w_doing, w_mode = "本日の監視は終了。明日9:55に出勤", "off"

    t = paper["total"]
    j_doing = (f"今日の推奨 {len(picks)}点" if picks else
               ("締切3分前から判定しています（今日はまだ推奨なし）" if w_mode in ("busy", "wait") else "待機中"))
    j_mode = w_mode if w_mode in ("busy", "wait", "off", "down") else "wait"

    col = last_run(W.get("collect.yml"))
    col_doing = ("前日分を収集中" if active("collect.yml") else
                 (f"前日分の収集を完了（{col['at'][11:16]}）" if col and col["conclusion"] == "success" and col["at"][:10] == today else "毎晩0:40に直近3日分を収集"))
    bf_active = active("backfill.yml")
    left = max(0, goal_days - done_days)
    bf_doing = ("2018年からの過去分を年ごとに並列で取得中" if bf_active and left > 5 else
                ("取りこぼしの点検・取り直し中" if bf_active else
                 ("過去分は取得完了。6時間ごとに取りこぼしを点検" if left <= 5 else f"残り{left}日。次の実行を待っている")))

    vd = datetime(now.year if now.month <= 12 else now.year, 12, 9, tzinfo=JST)
    v_left = (vd.date() - now.date()).days
    agents = [
        {"id": "watch", "name": "オッズ監視係", "role": "締切60分前から全賭式のオッズ・票数・試走タイムを記録", "mode": w_mode, "doing": w_doing,
         "rate": watch_rate, "rate_label": "締切3分以内の記録率（7日）",
         "stats": {"今日の記録": f"{tc['near']}/{tc['races']}レース", "今回の取得": (ws.get("stat") or {}).get("ok", 0)}},
        {"id": "judge", "name": "直前判定係", "role": f"締切3分前のオッズで「期待値{RULE}以上の単勝」を判定（買わずに記録）", "mode": j_mode, "doing": j_doing,
         "rate": judge_rate, "rate_label": "判定できた率（7日）",
         "stats": {"累計": {"bets": t["bets"], "hits": t["hits"], "roi": t["roi"]}}},
        {"id": "collect", "name": "収集係", "role": "毎晩、前日までの全レースの結果と確定オッズを保存", "mode": "busy" if active("collect.yml") else "wait",
         "doing": col_doing, "rate": rate("collect.yml"), "rate_label": "成功率（直近14回）", "stats": {"保存済み": f"{n_races:,}レース"}, "last": col},
        {"id": "backfill", "name": "過去分係", "role": "2018年からの過去レースを年ごとに5人で並列取得", "mode": "busy" if bf_active else "wait",
         "doing": bf_doing, "rate": min(1.0, done_days / goal_days) if goal_days else None, "rate_label": "取得済みの日数",
         "stats": {"取得済み": f"{done_days:,}/{goal_days:,}日"}, "last": last_run(W.get("backfill.yml"))},
        {"id": "health", "name": "見張り係", "role": "30分おきに全員の様子を確認し、止まっていたら起こす", "mode": "alert" if status["problems"] else "wait",
         "doing": ("異常あり: " + status["problems"][0]) if status["problems"] else f"全員異常なし（{now.strftime('%H:%M')}確認）",
         "rate": min(1.0, health_runs / 48), "rate_label": "直近24時間の確認回数 / 48", "stats": {"確認(24h)": health_runs, "自動対応": len(status["actions"])}},
        {"id": "report", "name": "検証係（Claude）", "role": f"{VERIFY_DATE} 20:13に締切前オッズで勝ち筋を検証し、スマホに報告",
         "mode": "busy" if (v_left == 0 and hm >= "20:13") else ("off" if v_left < 0 else "wait"),
         "doing": (f"検証まであと{v_left}日（締切前オッズ {len(ts_days)}日分たまった）" if v_left > 0 else
                   ("今夜20:13に検証" if v_left == 0 else "検証済み。結果はプロジェクトの資料に")), "rate": None, "rate_label": "-"},
    ]
    rates = [x["rate"] for x in agents if x["rate"] is not None]
    return {
        "updated_at": now.isoformat(timespec="minutes"),
        "ok": status["ok"], "problems": status["problems"], "actions": status["actions"],
        "overall_rate": sum(rates) / len(rates) if rates else None,
        "agents": agents,
        "today": {"date": today, "races": len(tids), "closed": tc["races"], "near": tc["near"], "judged": tc["judged"],
                  "meets": [PLACES.get(k, k) for k, _ in today_meets], "picks": picks[:60]},
        "progress": [
            {"label": "レースのデータベース", "value": n_races, "unit": "レース", "note": "2018年から。毎晩追加"},
            {"label": "締切前オッズの記録", "value": len(ts_days), "unit": "日", "goal": 21, "note": f"{ts_races:,}レース分。21日分たまったら（{VERIFY_DATE}）勝ち筋を検証"},
            {"label": "直前判定のペーパートレード", "value": t["bets"], "unit": "点", "goal": 1000,
             "note": f"的中{t['hits']}・回収率{t['roi'] if t['roi'] is not None else '-'}%。1,000点で本番を判断"},
        ],
        "backtest": {"label": "過去の確定オッズでの成績（2018〜）", "note": "約5万レース・28,250点・的中率13.3%・回収率167%（確定オッズで計算した参考値）"},
        "coverage_days": cov_days,
    }


if __name__ == "__main__":
    main()
