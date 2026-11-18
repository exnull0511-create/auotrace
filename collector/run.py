"""
オートレースデータ収集 CLI（競輪版 keirinbusines/collector/run.py と同じ使い方）

  python -m collector.run collect   --data DIR [--date YYYY-MM-DD]
  python -m collector.run backfill  --data DIR --start 2018-01-01 [--end YYYY-MM-DD] [--max-minutes 30]
  python -m collector.run remaining --data DIR --start 2018-01-01

保存先:
  DIR/races/YYYY/YYYY-MM-DD.jsonl.gz   1行=1レース（出走表・試走・偏差・気象/走路・結果・周回順位・払戻・確定オッズ）
  DIR/state/done.txt                    取得が完了した日付
  DIR/state/calendar/YYYY-MM.json       月ごとの開催カレンダーのキャッシュ
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .autorace import Client, SiteDown, fetch_race, month_meets

JST = timezone(timedelta(hours=9))
SCHEMA = "1"


def today_jst() -> date:
    return datetime.now(JST).date()


class Store:
    def __init__(self, root: Path):
        self.root = root
        (root / "state" / "calendar").mkdir(parents=True, exist_ok=True)
        sp = root / "state" / "schema.txt"
        if not sp.exists() or sp.read_text().strip() != SCHEMA:
            (root / "state" / "done.txt").unlink(missing_ok=True)
            sp.write_text(SCHEMA + "\n")
        self.done_path = root / "state" / "done.txt"
        self.done = set(self.done_path.read_text().split()) if self.done_path.exists() else set()

    def mark_done(self, d: date):
        self.done.add(d.isoformat())
        self.done_path.write_text("\n".join(sorted(self.done)) + "\n")

    def race_file(self, d: date) -> Path:
        p = self.root / "races" / f"{d.year}" / f"{d.isoformat()}.jsonl.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def write_day(self, d: date, races: list[dict]):
        with gzip.open(self.race_file(d), "wt", encoding="utf-8") as fp:
            for r in races:
                fp.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

    def calendar(self, cli: Client, ym: str, refresh: bool) -> list[dict]:
        p = self.root / "state" / "calendar" / f"{ym}.json"
        if p.exists() and not refresh:
            return json.loads(p.read_text())
        meets = month_meets(cli, ym)
        if meets is None:
            if p.exists():
                return json.loads(p.read_text())
            raise SiteDown(f"{ym} のカレンダーが取得できない")
        p.write_text(json.dumps(meets, ensure_ascii=False))
        return meets


def races_on(cli: Client, st: Store, d: date) -> tuple[list[dict], bool]:
    """日付 d の全レース。戻り値: (レース一覧, 全レースの結果が出ているか)"""
    recent = (today_jst() - d).days <= 10
    meets = [m for m in st.calendar(cli, d.strftime("%Y-%m"), refresh=recent) if m["date"] == d.isoformat()]
    races, complete = [], True
    for m in sorted(meets, key=lambda x: x["place_code"]):
        got = []
        for rn in range(1, m["last_race"] + 1):
            r = fetch_race(cli, m, rn)
            if r is None:
                continue
            if not r["finished"]:
                complete = False
            got.append(r)
        odds_ok = sum(1 for r in got if r["odds"].get("3連単"))
        prog_ok = sum(1 for r in got if r["entries"] and r["entries"][0].get("rank"))
        print(f"  {d} {m['place_key']}: {len(got)}/{m['last_race']}R  確定オッズ {odds_ok}R  出走表 {prog_ok}R", flush=True)
        if len(got) < m["last_race"]:
            complete = False
        races += got
    return races, complete


def do_date(cli: Client, st: Store, d: date) -> int:
    races, complete = races_on(cli, st, d)
    if races:
        st.write_day(d, races)
    if complete:   # 開催の無い日も「完了」（カレンダーで確認済み）
        st.mark_done(d)
    return len(races)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["collect", "backfill", "remaining"])
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--date")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end")
    ap.add_argument("--max-minutes", type=float, default=30)
    ap.add_argument("--sleep", type=float, default=1.0)
    a = ap.parse_args(argv)
    st, cli = Store(a.data), Client(sleep=a.sleep)

    if a.cmd == "collect":
        days = [date.fromisoformat(a.date)] if a.date else [today_jst() - timedelta(days=k) for k in (3, 2, 1)]
        for d in days:
            if a.date or d.isoformat() not in st.done:
                try:
                    print(d, "races:", do_date(cli, st, d), f"requests={cli.n}", flush=True)
                except SiteDown as e:
                    print("サイト応答なし:", e)
        return

    end = date.fromisoformat(a.end) if a.end else today_jst() - timedelta(days=1)
    start = date.fromisoformat(a.start)
    if a.cmd == "remaining":
        d, n = start, 0
        while d <= end:
            n += d.isoformat() not in st.done
            d += timedelta(days=1)
        print(n)
        return

    t0 = time.time()
    d = end
    while d >= start:  # 新しい日付から順に遡る
        if d.isoformat() not in st.done:
            try:
                n = do_date(cli, st, d)
            except SiteDown as e:
                print("サイト応答なし。今回は中断して次回再開:", e, flush=True)
                break
            print(d, "races:", n, f"requests={cli.n}", f"elapsed={(time.time() - t0) / 60:.1f}m", flush=True)
            if (time.time() - t0) / 60 > a.max_minutes:
                break
        d -= timedelta(days=1)


if __name__ == "__main__":
    sys.exit(main())
