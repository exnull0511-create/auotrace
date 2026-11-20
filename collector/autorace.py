"""
autorace.jp（公式）の内部API からの取得と整形。

調査（tools/probe*.py）で分かったこと:
  GET  /race_info/XML/Calendar/?date=YYYY-MM          月の開催一覧（場・日付・最終レース番号）2012年頃〜
  POST /race_info/RaceResult    {placeCode, raceDate, raceNo}  着順・試走・競走タイム・ST・周回ごとの順位・払戻
  POST /race_info/OtherRaceInfo {placeCode, raceDate, raceNo}  天候・気温・湿度・走路温度・走路状況・レース名・距離
  POST /race_info/Odds          {placeCode, raceDate, raceNo}  全賭式のオッズ（発走後は確定オッズ）・票数・公式/AI予想
  POST /race_info/Program       {placeCode, raceDate, raceNo}  出走表（ランク・ハンデ・試走・偏差・90日/180日成績）
  Odds と Program は CSRF トークン（Laravel の XSRF-TOKEN クッキー）が必要。
場コード: 1 船橋(2016廃止) 2 川口 3 伊勢崎 4 浜松 5 飯塚 6 山陽
走路状況コード: 0 良 / 1 湿 / 5 斑 （他の値は生のまま保存）
"""
from __future__ import annotations

import time
from urllib.parse import unquote

import requests

BASE = "https://autorace.jp"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
PLACES = {1: "船橋", 2: "川口", 3: "伊勢崎", 4: "浜松", 5: "飯塚", 6: "山陽"}
TRACK = {0: "良", 1: "湿", 5: "斑"}
BETS = {"rt3": "3連単", "rf3": "3連複", "rtw": "2連単", "rfw": "2連複", "wid": "ワイド", "tns": "単勝", "fns": "複勝"}


class SiteDown(Exception):
    """サイトが応答しない（制限・障害）。この回の取得を中断し、後で再開する。"""


class Client:
    def __init__(self, sleep: float = 1.0, retries: int = 3):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                               "Accept-Language": "ja", "X-Requested-With": "XMLHttpRequest",
                               "Origin": BASE, "Referer": BASE + "/race_info/"})
        self.sleep, self.retries = sleep, retries
        self.n = 0
        self.fail_streak = 0
        self._token_at = 0.0

    def _token(self, force=False):
        """ページを1回開いて XSRF-TOKEN クッキーを受け取り、ヘッダに載せる（1時間ごとに更新）"""
        if not force and self.s.headers.get("X-XSRF-TOKEN") and time.time() - self._token_at < 3600:
            return
        self.s.get(BASE + "/race_info/", timeout=30)
        self.n += 1
        tok = self.s.cookies.get("XSRF-TOKEN")
        if tok:
            self.s.headers["X-XSRF-TOKEN"] = unquote(tok)
        self._token_at = time.time()
        time.sleep(self.sleep)

    def call(self, method: str, path: str, payload: dict | None = None):
        """JSONを返す。result が Success でなければ None。"""
        for i in range(self.retries):
            try:
                if method == "POST":
                    self._token()
                    r = self.s.post(BASE + path, json=payload, timeout=30)
                else:
                    r = self.s.get(BASE + path, params=payload, timeout=30)
                self.n += 1
                time.sleep(self.sleep)
                if r.status_code == 419:          # トークン切れ
                    self._token(force=True)
                    continue
                if r.status_code == 200:
                    self.fail_streak = 0
                    j = r.json()
                    return j if j.get("result") == "Success" else None
                if r.status_code == 404:
                    self.fail_streak = 0
                    return None
            except (requests.RequestException, ValueError):
                pass
            time.sleep(self.sleep * (i + 2) * 3)
        self.fail_streak += 1
        if self.fail_streak >= 5:
            raise SiteDown(f"連続{self.fail_streak}回の取得失敗: {path} {payload}")
        return None


# ---------- 開催カレンダー ----------

def month_meets(cli: Client, ym: str) -> list[dict] | None:
    """月の開催日一覧: [{place_code, place_key, date, last_race, grade, title, nighter, day, days}]"""
    j = cli.call("GET", "/race_info/XML/Calendar/", {"date": ym})
    if j is None:
        return None
    out = []
    for v in j.get("body") or []:
        for dd in v.get("calendar") or []:
            race = dd.get("race")
            if not isinstance(race, dict) or not race:
                continue
            last = race.get("finalRaceNo")
            if not last:
                # 開催が始まったばかりの日程は最終レース番号が空のことがある → 1Rの情報から取る（取れなければ12R）
                j = cli.call("POST", "/race_info/OtherRaceInfo", {"placeCode": v["placeCode"], "raceDate": dd["date"], "raceNo": 1})
                last = ((j or {}).get("body") or {}).get("finalRaceNo") or 12
            out.append({"place_code": v["placeCode"], "place_key": v["placeKey"], "date": dd["date"],
                        "last_race": int(last), "grade": race.get("gradeName"),
                        "title": race.get("title"), "nighter": race.get("nighterName") or None,
                        "day": race.get("paragraphDay"), "days": race.get("days")})
    return out


# ---------- 1レース分の取得と整形 ----------

def f(x):
    """'3.45' -> 3.45、'None'/''/None -> None"""
    if x in (None, "", "None", "null"):
        return None
    try:
        v = float(x)
        return int(v) if isinstance(x, int) else v
    except (TypeError, ValueError):
        return x


def i(x):
    v = f(x)
    return int(v) if isinstance(v, float) else v


def name(s):
    return " ".join((s or "").replace("　", " ").split())


def flat_odds(d: dict, prefix=()) -> dict:
    """{'1': {'2': {'3': '22.9'}}} -> {'1-2-3': 22.9}。0.0（発売なし）は除く。ワイド・複勝は [min, max]"""
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict) and set(v) >= {"min", "max"}:
            lo, hi = f(v["min"]), f(v["max"])
            if lo:
                out["-".join(prefix + (k,))] = [lo, hi]
        elif isinstance(v, dict):
            out.update(flat_odds(v, prefix + (k,)))
        else:
            x = f(v)
            if x:
                out["-".join(prefix + (k,))] = x
    return out


def build_race(meet: dict, rn: int, res: dict | None, info: dict | None, odds: dict | None, prog: dict | None) -> dict:
    res, info, odds, prog = [(x or {}).get("body") or {} for x in (res, info, odds, prog)]
    pc = meet["place_code"]
    r = {
        "race_id": f"{meet['date']}_{meet['place_key']}_{rn:02d}",
        "date": meet["date"], "place_code": pc, "place": PLACES.get(pc, meet["place_key"]), "race_no": rn,
        "grade": info.get("gradeName") or meet.get("grade"), "title": info.get("title") or meet.get("title"),
        "race_name": info.get("raceName"), "class_code": info.get("classCode"), "distance": info.get("distance"),
        "day": info.get("paragraphday") or meet.get("day"), "days": meet.get("days"),
        "period": [info.get("periodStartDate"), info.get("periodEndDate")],
        "start_time": info.get("raceStartTime"), "nighter": meet.get("nighter"),
        # 発走前（試走時点）と発走時点の気象・走路
        "cond": {"weather": info.get("weather"), "temp": f(info.get("temp")), "humid": f(info.get("humid")),
                 "roadtemp": f(info.get("roadtemp")), "track_code": info.get("situationCode"),
                 "track": TRACK.get(info.get("situationCode")),
                 "race_weather": info.get("raceWeather"), "race_temp": f(info.get("raceTemp")),
                 "race_humid": f(info.get("raceHumid")), "race_roadtemp": f(info.get("raceRoadtemp")),
                 "race_track_code": info.get("raceSituationCode"), "race_track": TRACK.get(info.get("raceSituationCode"))},
    }

    # 出走表（Program）。無ければ結果・オッズの選手一覧で代用
    p90, p180, win = prog.get("latest90List") or {}, prog.get("latest180List") or {}, prog.get("winList") or {}
    oplayers = {str(p.get("carNo")): p for p in odds.get("playerList") or []}
    entries = []
    for p in prog.get("playerList") or odds.get("playerList") or []:
        code = str(p.get("playerCode"))
        a, b, w = p90.get(code) or {}, p180.get(code) or {}, win.get(code) or {}
        op = oplayers.get(str(p.get("carNo"))) or {}
        entries.append({
            "car": i(p.get("carNo")), "player_code": code, "name": name(p.get("playerName")),
            "lg": name(p.get("placeName")), "term": i(p.get("graduationCode")), "age": i(p.get("age")),
            "bike_class": i(p.get("bikeClass")), "bike": p.get("bikeName"), "rank": p.get("rank"),
            "handicap": i(p.get("handicap")), "trial": f(p.get("trialRunTime") or p.get("trialTime")),
            "trial_retry": f(p.get("trialRetryCode")), "absent": f(p.get("absent")),
            # 偏差: 良走路の平均試走タイムと平均競走タイムの差（'084' → 0.084秒）。試走タイム+偏差 ≒ 本番の予想タイム
            "race_dev": (int(p["raceDev"]) / 1000 if str(p.get("raceDev") or "").isdigit() else None),
            "rate2": f(p.get("rate2")), "rate3": f(p.get("rate3")),
            "expect_sunny": p.get("sunnyExpectCode") or None, "expect_rain": p.get("rainExpectCode") or None,
            "expect_st": op.get("stExpectCode") or None, "expect_ai": op.get("aiExpectCode") or None,
            "d90": {"runs": i(a.get("runCount")), "st_ave": f(a.get("stAve") or op.get("stAve")),
                    "good_trial_ave": f(a.get("goodTrackTraialAve") or op.get("goodTrackTraialAve")),
                    "good_race_ave": f(a.get("goodTrackRaceAve") or op.get("goodTrackRaceAve")),
                    "good_race_best": f(a.get("goodTrackRaceBest") or op.get("goodTrackRaceBest")),
                    "finals": i(a.get("advanceFinalCount")), "wins": i(a.get("winCount"))},
            "d180": {"good_rate2": f((b.get("goodTrackOdds") or {}).get("rate2")),
                     "good_runs": i((b.get("goodTrackOdds") or {}).get("runCount")),
                     "wet_rate2": f((b.get("wetTrackOdds") or {}).get("rate2")),
                     "wet_runs": i((b.get("wetTrackOdds") or {}).get("runCount"))},
            "career": {k: f(w.get(k)) for k in ("rate1", "rate2", "rate3", "totalWinCount", "thisYearWinCount")} if w else {},
        })
    if not entries:   # 出走表が取れない古いレースは結果の選手一覧で代用
        entries = [{"car": i(x.get("carNo")), "player_code": str(x.get("playerCode")), "name": name(x.get("playerName")),
                    "handicap": i(x.get("handicap")), "trial": f(x.get("traialTime")), "bike": x.get("motorcycleName")}
                   for x in sorted(res.get("raceResult") or [], key=lambda y: i(y.get("carNo")) or 0)]
    r["entries"] = entries
    r["n"] = len(entries)

    r["result"] = [{"order": i(x.get("order")), "car": i(x.get("carNo")), "player_code": str(x.get("playerCode")),
                    "name": name(x.get("playerName")), "handicap": i(x.get("handicap")),
                    "trial": f(x.get("traialTime")), "trial_retry": f(x.get("traialRetryCode")),
                    "race_time": f(x.get("raceTime")), "st": f(x.get("st")),
                    "accident": x.get("accidentName"), "accident_code": x.get("accidentCode"),
                    "foul": x.get("foulCode"), "bike": x.get("motorcycleName")}
                   for x in res.get("raceResult") or []]
    # 周回ごとの順位（lap 1 が1周目）
    r["laps"] = {str(g["lapNo"]): [i(x["carNo"]) for x in sorted(g["rankList"], key=lambda y: y["rank"])]
                 for g in res.get("grandNoteList") or []}
    r["warnings"] = {"race": res.get("warningPlayerList") or [], "trial": res.get("trialWarningPlayerList") or []}
    ref = res.get("refundInfo") or {}
    r["refund"] = {BETS.get(k, k): v for k, v in ref.items()} if ref else {}

    r["odds"] = {BETS[k[:3]]: flat_odds(odds.get(k)) for k in
                 ("rt3OddsList", "rf3OddsList", "rtwOddsList", "rfwOddsList", "widOddsList", "tnsOddsList", "fnsOddsList")
                 if odds.get(k)}
    r["odds_meta"] = {"status": odds.get("statusCode"), "final": odds.get("salesInfo", {}).get("oldFlg") == 1,
                      "updated": (odds.get("salesInfo") or {}).get("updateDate"),
                      "sales": {BETS.get(k, k): v.get("salesCount") for k, v in (odds.get("salesInfo") or {}).items()
                                if isinstance(v, dict)}} if odds else {}
    r["expect"] = odds.get("expectInfo") or {}
    r["finished"] = bool(r["result"]) and any(x["order"] for x in r["result"])
    # 確定オッズと払戻の照合（3連単。払戻100円あたり = オッズ×100）
    t3 = r["odds"].get("3連単") or {}
    hits = [x for x in (r["refund"].get("3連単") or {}).get("list") or []]
    if t3 and hits and r["odds_meta"].get("final"):
        h = hits[0]
        k = f"{h['1thCarNo']}-{h['2thCarNo']}-{h['3thCarNo']}"
        r["odds_check"] = abs((t3.get(k) or 0) * 100 - h["refund"]) <= 10
    return r


def fetch_race(cli: Client, meet: dict, rn: int, with_program=True) -> dict | None:
    body = {"placeCode": meet["place_code"], "raceDate": meet["date"], "raceNo": rn}
    res = cli.call("POST", "/race_info/RaceResult", body)
    info = cli.call("POST", "/race_info/OtherRaceInfo", body)
    if res is None and info is None:
        return None
    odds = cli.call("POST", "/race_info/Odds", body)
    prog = cli.call("POST", "/race_info/Program", body) if with_program else None
    return build_race(meet, rn, res, info, odds, prog)
