"""
サイト構造の調査 2回目。autorace.jp の内部API（JSON）が
  - ブラウザ無し（requests）で呼べるか
  - 何年前まで遡れるか（出走表・試走・結果・確定オッズ）
を確かめる。GitHub Actions 上で実行。

  python tools/probe2.py OUTDIR
"""
import json
import sys
import time
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "samples2")
OUT.mkdir(parents=True, exist_ok=True)
B = "https://autorace.jp"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
log = []


def w(name, obj):
    (OUT / name).write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 1) ブラウザで結果ページを開き、裏のAPIとリクエストヘッダを記録 ----------
def browser_capture():
    caps = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=UA, locale="ja-JP")
        page = ctx.new_page()

        def on_resp(resp):
            req = resp.request
            if req.method == "POST" or "XML" in resp.url:
                try:
                    body = resp.text()
                except Exception:  # noqa: BLE001
                    body = ""
                fn = f"br_{len(caps):02d}.json"
                w(fn, body[:500000])
                caps.append({"page": page.url, "url": resp.url, "method": req.method, "post": req.post_data,
                             "req_headers": req.headers, "status": resp.status, "file": fn, "bytes": len(body)})

        page.on("response", on_resp)
        for u in ["/race_info/RaceResult/iizuka/2026-11-17_12", "/race_info/RaceResult/iizuka/2026-11-17_5?all=1",
                  "/race_info/Odds/iizuka/2026-11-17_12", "/race_info/Program/iizuka/2026-11-17_12",
                  "/race_info/SearchRace"]:
            try:
                page.goto(B + u, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                w("page_" + u.strip("/").replace("/", "_").replace("?", "_") + ".html", page.content())
            except Exception as e:  # noqa: BLE001
                caps.append({"page": u, "error": str(e)})
            time.sleep(1.5)
        b.close()
    w("browser_caps.json", caps)
    return caps


# ---------- 2) requests で直接呼ぶ ----------
s = requests.Session()
s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "ja",
                  "X-Requested-With": "XMLHttpRequest", "Origin": B, "Referer": B + "/race_info/"})


def call(method, path, payload=None, tag=""):
    try:
        if method == "GET":
            r = s.get(B + path, params=payload, timeout=30)
        else:
            r = s.post(B + path, json=payload, timeout=30)
        txt = r.text
    except Exception as e:  # noqa: BLE001
        log.append({"tag": tag, "path": path, "payload": payload, "error": str(e)})
        return None
    fn = f"rq_{len(log):03d}_{tag}.json"
    w(fn, txt[:500000])
    ok = None
    try:
        j = r.json()
        ok = j.get("result")
        summary = {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in (j.get("body") or {}).items()} \
            if isinstance(j.get("body"), dict) else (len(j["body"]) if isinstance(j.get("body"), list) else None)
    except Exception:  # noqa: BLE001
        j, summary = None, txt[:200]
    log.append({"tag": tag, "method": method, "path": path, "payload": payload, "status": r.status_code,
                "result": ok, "bytes": len(txt), "summary": summary, "file": fn})
    print(tag, r.status_code, ok, len(txt), flush=True)
    time.sleep(1.2)
    return j


def main():
    caps = browser_capture()
    # 結果ページで呼ばれた POST の宛先を、直接呼ぶ候補に加える
    post_paths = sorted({c["url"].replace(B, "") for c in caps if c.get("method") == "POST"})
    print("POST endpoints seen:", post_paths)

    # 年ごとに、カレンダーから開催日を1つ選んで各APIを試す
    for ym in ["2026-10", "2025-06", "2023-06", "2020-06", "2017-06", "2014-06", "2010-06", "2006-06"]:
        cal = call("GET", "/race_info/XML/Calendar/", {"date": ym}, f"cal_{ym}")
        pick = None
        for v in (cal or {}).get("body") or []:
            for dday in v.get("calendar") or []:
                race = dday.get("race")
                if race and isinstance(race, dict) and race.get("finalRaceNo"):
                    pick = (v["placeCode"], v["placeKey"], dday["date"], race["finalRaceNo"])
                    break
            if pick:
                break
        if not pick:
            continue
        pc, pk, d, last = pick
        body = {"placeCode": pc, "raceDate": d, "raceNo": last}
        for path in sorted(set(["/race_info/Program", "/race_info/Odds", "/race_info/OtherRaceInfo",
                                "/race_info/RaceResult"] + post_paths)):
            if path.startswith("/race_info/XML/Hold/Recent") or path.startswith("/race_info/Player"):
                continue
            call("POST", path, body, f"{ym}_{pk}_{path.split('/')[-1]}")
        # 1日分の結果一覧があるか（raceNo無し）
        call("POST", "/race_info/RaceResult", {"placeCode": pc, "raceDate": d}, f"{ym}_{pk}_RaceResult_day")
    call("POST", "/race_info/XML/Hold/Recent", {"placeCode": 2}, "recent_kawaguchi")
    call("POST", "/race_info/Player", {"placeCode": 2}, "player_kawaguchi")
    w("log.json", log)


if __name__ == "__main__":
    main()
