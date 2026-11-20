"""
締切前オッズの時系列を記録する（GitHub Actions で数時間動かし続ける）。

  python -m collector.odds_watch --data DIR --minutes 295 [--push-every 10]

締切（電話投票締切 telvoteTime）までの残り時間に応じて取得間隔を変える:
  締切60〜30分前: 10分ごと / 30〜10分前: 5分ごと / 10〜3分前: 2分ごと / 3分前〜締切: 1分ごと / 締切3分後に最後の1回
1回分の記録: 時刻・締切まで何分・オッズ更新時刻・各賭式の票数・各車の試走タイム（発表済みなら）・全賭式のオッズ

保存先: DIR/odds_ts/YYYY/YYYY-MM-DD/<場>_<R>.jsonl（1行=1回の取得）

直前判定（ペーパートレード）: 締切3分前〜締切の取得のたびに、
  3連単オッズから出した1着確率 × その時点の単勝オッズ（=期待値）を全車について計算し
  DIR/paper/YYYY-MM-DD.jsonl に追記する。期待値1.2以上が推奨（実際には買わない）。
LINE通知: 各レースで最初の直前判定（締切3分前ごろ）に推奨があれば、1レース1通だけ送る（collector/notify_line.py）。
状態: DIR/health/watch_status.json（ダッシュボード用。見ているレース・次の締切）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notify_line
from .autorace import PLACES, Client, SiteDown, f, flat_odds, month_meets

JST = timezone(timedelta(hours=9))
POOLS = {"rt3OddsList": "3連単", "rf3OddsList": "3連複", "rtwOddsList": "2連単", "rfwOddsList": "2連複",
         "widOddsList": "ワイド", "tnsOddsList": "単勝", "fnsOddsList": "複勝"}
RULE = 1.2   # 推奨にする期待値の下限
SALES = {"rt3": "3連単", "rf3": "3連複", "rtw": "2連単", "rfw": "2連複", "wid": "ワイド", "tns": "単勝", "fns": "複勝"}


def now():
    return datetime.now(JST)


def interval(m: float) -> float:
    if m > 30:
        return 10
    if m > 10:
        return 5
    if m > 3:
        return 2
    return 1


class Watch:
    def __init__(self, root: Path, cli: Client):
        self.root, self.cli = root, cli
        self.races = {}      # race_id -> dict(meet, rn, close, start, last, done)
        self.sched_at = None
        self.self_stat = {}

    def refresh_schedule(self):
        """今日と昨日（深夜開催）の開催を調べ、各レースの締切時刻を得る"""
        t = now()
        days = {t.date(), (t - timedelta(hours=6)).date()}
        meets = []
        for ym in sorted({d.strftime("%Y-%m") for d in days}):
            meets += [m for m in (month_meets(self.cli, ym) or []) if m["date"] in {d.isoformat() for d in days}]
        for m in meets:
            for rn in range(1, m["last_race"] + 1):
                rid = f"{m['date']}_{m['place_key']}_{rn:02d}"
                r = self.races.get(rid)
                if r:
                    if r["done"]:
                        continue
                    far = (r["close"] - t).total_seconds() > 90 * 60
                    if (t - r["checked"]).total_seconds() < (3600 if far else 15 * 60):
                        continue    # 締切時刻の再確認は 先のレース1時間に1回・近いレース15分に1回
                info = self.cli.call("POST", "/race_info/OtherRaceInfo",
                                     {"placeCode": m["place_code"], "raceDate": m["date"], "raceNo": rn})
                b = (info or {}).get("body") or {}
                tv = b.get("telvoteTime") or b.get("raceStartTime")
                if not tv:
                    continue
                d0 = datetime.fromisoformat(m["date"]).replace(tzinfo=JST)
                hh, mm = map(int, tv.split(":"))
                close = d0 + timedelta(hours=hh, minutes=mm)
                if hh < 5:   # 深夜0時以降の表記
                    close += timedelta(days=1)
                self.races[rid] = {"meet": m, "rn": rn, "close": close, "start": b.get("raceStartTime"),
                                   "last": r["last"] if r else None, "done": close < t - timedelta(minutes=3),
                                   "checked": t}
        self.sched_at = t
        todo = [k for k, v in self.races.items() if not v["done"]]
        print(f"{t:%H:%M} 開催{len(meets)}場  未締切 {len(todo)}R  次: "
              + ", ".join(f"{k}({v['close']:%H:%M})" for k, v in sorted(self.races.items(), key=lambda x: x[1]["close"])
                          if not v["done"])[:300], flush=True)

    def snap(self, rid: str, r: dict, m_left: float):
        body = {"placeCode": r["meet"]["place_code"], "raceDate": r["meet"]["date"], "raceNo": r["rn"]}
        j = self.cli.call("POST", "/race_info/Odds", body)
        if not j:
            return False
        o = j.get("body") or {}
        si = o.get("salesInfo") or {}
        rec = {"t": now().strftime("%Y-%m-%dT%H:%M:%S"), "close": r["close"].strftime("%H:%M"),
               "m_left": round(m_left, 1), "updated": si.get("updateDate"), "status": o.get("statusCode"),
               "sales": {SALES[k]: v.get("salesCount") for k, v in si.items() if k in SALES and isinstance(v, dict)},
               "trial": {str(p.get("carNo")): f(p.get("trialTime")) for p in o.get("playerList") or []},
               "retry": {str(p.get("carNo")): p.get("trialRetryCode") for p in o.get("playerList") or []
                         if p.get("trialRetryCode") not in (None, "", 0, "0")},
               "absent": [p.get("carNo") for p in o.get("playerList") or [] if p.get("absent") not in (None, "", 0, "0")],
               "odds": {v: flat_odds(o.get(k)) for k, v in POOLS.items() if o.get(k)}}
        d = r["meet"]["date"]
        p = self.root / "odds_ts" / d[:4] / d / f"{r['meet']['place_key']}_{r['rn']:02d}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.self_stat["ok"] = self.self_stat.get("ok", 0) + 1
        if 0 <= m_left <= 3.2:
            self.judge(rid, r, rec)
        return True

    def judge(self, rid, r, rec):
        """締切直前のオッズで推奨を判定して paper/ に記録"""
        t3, tns = rec["odds"].get("3連単") or {}, rec["odds"].get("単勝") or {}
        if not t3 or not tns:
            return
        q = {k: 1 / v for k, v in t3.items() if v}
        s = sum(q.values())
        win = {}
        for k, x in q.items():
            c = k.split("-")[0]
            win[c] = win.get(c, 0) + x / s
        cars = [{"car": c, "odds": o, "p": round(win.get(c, 0), 4), "ev": round(win.get(c, 0) * o, 3)}
                for c, o in sorted(tns.items(), key=lambda x: int(x[0]))]
        picks = [x for x in cars if x["ev"] >= RULE]
        m = r["meet"]
        out = {"race_id": rid, "race": f"{PLACES.get(m['place_code'], m['place_key'])}{r['rn']}R", "close": rec["close"],
               "t": rec["t"], "m_left": rec["m_left"], "tns_sales": rec["sales"].get("単勝"), "cars": cars, "picks": picks}
        if not r.get("notified"):          # 1レース1回だけ（締切3分前ごろの最初の判定）
            r["notified"] = True
            out["notified"] = True
            if picks:
                lines = [f"{out['race']} 締切{rec['close']}（あと{max(0, rec['m_left']):.0f}分）"]
                lines += [f"単勝 {x['car']}番  {x['odds']}倍（期待値{x['ev']:.2f}）" for x in sorted(picks, key=lambda x: -x["ev"])]
                lines.append("※検証中の推奨です（金額は1点数百円まで）")
                out["line_sent"] = notify_line.send("\n".join(lines))
        p = self.root / "paper" / f"{m['date']}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(out, ensure_ascii=False, separators=(",", ":")) + "\n")

    def write_status(self, state="watching"):
        t = now()
        rows = sorted(((k, v) for k, v in self.races.items() if not v["done"]), key=lambda x: x[1]["close"])
        name = lambda v: f"{PLACES.get(v['meet']['place_code'], v['meet']['place_key'])}{v['rn']}R"  # noqa: E731
        st = {"updated_at": t.isoformat(timespec="seconds"), "state": state,
              "watching": [{"race": name(v), "close": v["close"].strftime("%H:%M")} for k, v in rows
                           if -3 <= (v["close"] - t).total_seconds() / 60 <= 60],
              "next": [{"race": name(v), "close": v["close"].strftime("%H:%M")} for k, v in rows[:3]],
              "stat": self.self_stat}
        p = self.root / "health" / "watch_status.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def tick(self):
        t = now()
        n = 0
        for rid, r in sorted(self.races.items(), key=lambda x: x[1]["close"]):
            if r["done"]:
                continue
            m_left = (r["close"] - t).total_seconds() / 60
            if m_left > 60:
                continue
            if m_left < -3:      # 締切3分後: 最後の1回を取って終了
                self.snap(rid, r, m_left)
                r["done"] = True
                n += 1
                continue
            if m_left < 0:
                continue
            if r["last"] is None or (t - r["last"]).total_seconds() / 60 >= interval(m_left) - 0.2:
                if self.snap(rid, r, m_left):
                    r["last"] = t
                    n += 1
        return n


def gh_dispatch(workflow: str, only_if_idle_min: float | None = None):
    """GitHub Actions のワークフローを起動する（cron が遅れたり飛ばされたりするため、自分たちで起こし合う）"""
    import os
    import urllib.request
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        return
    h = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    try:
        if only_if_idle_min is not None:
            req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs?per_page=3", headers=h)
            runs = json.load(urllib.request.urlopen(req, timeout=20)).get("workflow_runs", [])
            for r in runs:
                if r["status"] != "completed":
                    return
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))).total_seconds() / 60
                if age < only_if_idle_min:
                    return
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
                                     data=json.dumps({"ref": "main"}).encode(), method="POST", headers=h)
        urllib.request.urlopen(req, timeout=20)
        print("起動:", workflow, flush=True)
    except Exception as e:  # noqa: BLE001
        print("起動に失敗:", workflow, e, flush=True)


def push(msg):
    subprocess.run(["bash", "tools/data_branch.sh", "push", msg], check=False)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--minutes", type=float, default=295)
    ap.add_argument("--push-every", type=float, default=10)
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args(argv)
    cli = Client(sleep=0.5)
    w = Watch(a.data, cli)
    t0 = last_push = time.time()
    last_health = 0.0
    snaps = 0
    while (time.time() - t0) / 60 < a.minutes:
        try:
            if w.sched_at is None or (now() - w.sched_at).total_seconds() > 20 * 60:
                w.refresh_schedule()
            snaps += w.tick()
            w.write_status()
        except SiteDown as e:
            print("サイト応答なし:", e, flush=True)
            cli.fail_streak = 0
            time.sleep(60)
        except Exception as e:  # noqa: BLE001  1回の失敗で止めない
            print("エラー:", repr(e), flush=True)
            time.sleep(30)
        if not a.no_push and (time.time() - last_push) / 60 >= a.push_every and snaps:
            push(f"odds_ts {snaps} snaps")
            last_push, snaps = time.time(), 0
        if not a.no_push and time.time() - last_health > 30 * 60:   # 見張り係を30分ごとに起こす（cron頼みにしない）
            gh_dispatch("health.yml", only_if_idle_min=25)
            last_health = time.time()
        if w.sched_at and not any(not v["done"] for v in w.races.values()) and (now().hour >= 22 or now().hour < 9):
            print("今日のレースは全て締切済み。終了", flush=True)
            w.write_status("finished")
            snaps += 1
            break
        time.sleep(20)
    if not a.no_push and snaps:
        push(f"odds_ts {snaps} snaps (final)")
    # まだ今日の締切前レースが残っていれば、次の監視を自分で起動（予約実行が遅れても途切れないように）
    if not a.no_push and any(not v["done"] for v in w.races.values()):
        gh_dispatch("odds-watch.yml")


if __name__ == "__main__":
    main()
