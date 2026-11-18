"""
サイト構造の調査。GitHub Actions 上で実行し、取得したページを probe-samples ブランチに保存する。

ブラウザ（Playwright）で実際にページを開き、
  - 描画後のHTML
  - ページが裏で呼んでいる JSON / XML（API）の中身
  - ページ内のリンク一覧
を保存する。そこから「出走表・試走・結果・オッズ」がどのURLで取れるかを見極める。

  python tools/probe.py OUTDIR
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
OUT.mkdir(parents=True, exist_ok=True)
JST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
VENUES = ["kawaguchi", "isesaki", "hamamatsu", "iizuka", "sanyo"]
KEY = re.compile(r"(race|Race|odds|Odds|result|Result|program|Program|syusso|shusso|yosou|kekka|hold|Hold|player|Player|"
                 r"schedule|Schedule|calendar|haraimodoshi|refund|shisou|trial|search)", re.I)

index = []
seen = set()
n_api = 0


def save(name: str, text: str) -> str:
    fn = f"{len(index):03d}_{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)[:80]}"
    (OUT / fn).write_text(text, encoding="utf-8")
    return fn


def visit(page, url: str, tag: str, wait=2500):
    """ページを開いて HTML・裏で呼ばれたAPI・リンクを保存。リンク一覧を返す。"""
    global n_api
    if url in seen:
        return []
    seen.add(url)
    apis = []

    def on_resp(resp):
        global n_api
        try:
            ct = resp.headers.get("content-type", "")
            u = resp.url
            if any(x in ct for x in ("json", "xml", "javascript")) or re.search(r"\.(json|xml)(\?|$)", u):
                if re.search(r"(google|gtag|doubleclick|facebook|twitter|yahoo|criteo|adobe|newrelic|\.js(\?|$))", u):
                    return
                body = resp.text()
                n_api += 1
                req = resp.request
                apis.append({"url": u, "method": req.method, "post": req.post_data, "status": resp.status,
                             "ct": ct, "file": save(f"api_{tag}_{n_api}.txt", body[:400000]), "bytes": len(body)})
        except Exception as e:  # noqa: BLE001
            apis.append({"url": resp.url, "error": str(e)})

    page.on("response", on_resp)
    rec = {"tag": tag, "url": url}
    try:
        r = page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(wait)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        html = page.content()
        rec.update(status=r.status if r else None, final_url=page.url, bytes=len(html), file=save(f"{tag}.html", html))
        links = page.eval_on_selector_all("a[href]", "els => els.map(e => [e.getAttribute('href'), (e.innerText||'').trim().slice(0,40)])")
    except Exception as e:  # noqa: BLE001
        rec["error"] = str(e)
        links = []
    page.remove_listener("response", on_resp)
    abs_links = []
    for h, t in links:
        if not h or h.startswith(("javascript:", "mailto:", "#")):
            continue
        abs_links.append([urljoin(page.url, h), t])
    rec["apis"] = apis
    rec["links"] = abs_links[:600]
    index.append(rec)
    (OUT / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[{tag}] {rec.get('status')} {url} html={rec.get('bytes')} api={len(apis)} links={len(abs_links)}", flush=True)
    time.sleep(1.5)
    return abs_links


def crawl(page, start: str, host: str, tag: str, limit: int, depth: int = 2, prefer=()):
    """start から KEY に合うリンクを幅優先で辿る（同一ホストのみ・上限 limit ページ）。"""
    q, done = [(start, 0)], 0
    patterns = {}
    while q and done < limit:
        url, d = q.pop(0)
        links = visit(page, url, f"{tag}{done:02d}")
        done += 1
        if d >= depth:
            continue
        cand = []
        for u, t in links:
            p = urlparse(u)
            if p.netloc != host or u in seen:
                continue
            if not (KEY.search(p.path + "?" + p.query) or any(k in t for k in ("出走", "結果", "オッズ", "払戻", "試走", "予想", "成績", "開催"))):
                continue
            # 同じ形のURL（数字だけ違う）は2つまで
            shape = re.sub(r"\d+", "N", p.path + "?" + re.sub(r"=[^&]*", "=", p.query))
            if patterns.get(shape, 0) >= 2:
                continue
            patterns[shape] = patterns.get(shape, 0) + 1
            score = 0 if any(k in u for k in prefer) else 1
            cand.append((score, u))
        for _, u in sorted(cand):
            q.append((u, d + 1))


def main():
    today = datetime.now(JST).date()
    past = today - timedelta(days=3)
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(user_agent=UA, locale="ja-JP", viewport={"width": 1280, "height": 2000})
        page = ctx.new_page()

        # 1) 公式 autorace.jp（試走タイム・偏差・走路状況が載る）
        crawl(page, "https://autorace.jp/", "autorace.jp", "aj", limit=45, depth=2,
              prefer=("Program", "RaceResult", "Odds", "Live", "race_info"))
        for v in VENUES[:2]:
            for path in (f"race_info/Live/{v}", f"race_info/Program/{v}", f"race_info/RaceResult/{v}",
                         f"race_info/Odds/{v}", f"race_info/Player/{v}"):
                visit(page, f"https://autorace.jp/{path}", f"aj_{path.split('/')[1]}_{v}")

        # 2) オッズパーク（予想情報は過去10年分が静的HTMLで残っている）
        ym = past.strftime("%Y/%m")
        visit(page, f"https://www.oddspark.com/autorace/yosou/{ym}/", "op_yosou_month")
        for v in VENUES:
            visit(page, f"https://www.oddspark.com/autorace/yosou/{v}/{past.year}/{past.strftime('%m%d')}.html", f"op_yosou_{v}")
        visit(page, "https://www.oddspark.com/autorace/yosou/kawaguchi/2020/0105.html", "op_yosou_old2020")
        crawl(page, "https://www.oddspark.com/autorace/", "www.oddspark.com", "op", limit=35, depth=2,
              prefer=("RaceResult", "Odds", "RaceList", "Kaisai", "result", "odds"))

        # 3) WINTICKET（オッズ・結果を JSON で埋め込んでいる可能性）
        crawl(page, "https://www.winticket.jp/autorace/", "www.winticket.jp", "wt", limit=20, depth=2,
              prefer=("racecard", "result", "odds"))
        b.close()

    # まとめ: 見つかったAPIのURL一覧
    apis = sorted({a["url"].split("?")[0] for r in index for a in r.get("apis", []) if "url" in a})
    (OUT / "api_urls.txt").write_text("\n".join(apis) + "\n", encoding="utf-8")
    links = sorted({re.sub(r"\d", "N", u) for r in index for u, _ in r.get("links", [])})
    (OUT / "link_shapes.txt").write_text("\n".join(links) + "\n", encoding="utf-8")
    print("pages:", len(index), "apis:", len(apis))


if __name__ == "__main__":
    main()
