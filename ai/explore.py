"""
仮説の検証（初版）: 「市場が見誤っている選手」がいるか。

  python -m ai.explore --data DIR [--since 2024-01-01] [--place 川口] [--track 良]

各選手について
  - 試走順位（試走タイムが速い順）
  - 予測順位（(距離+ハンデ) × (試走タイム+偏差) が短い順 = 試走と偏差から見た本番の予想ゴール順）
  - 人気（3連単オッズから出した1着確率の順）
を出し、グループごとに
  実際の1着率 / 市場の1着確率 / 単勝の回収率 / 3着内率
を並べる。「実際 > 市場」かつ回収率が高いグループが割安候補。
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
from pathlib import Path


def load(data: Path, since=None, place=None, track=None):
    for fp in sorted((data / "races").glob("*/*.jsonl.gz")):
        if since and fp.name[:10] < since:
            continue
        for line in gzip.open(fp, "rt", encoding="utf-8"):
            r = json.loads(line)
            if not r.get("finished") or not r["odds"].get("3連単") or not r.get("odds_meta", {}).get("final"):
                continue
            if place and r["place"] != place:
                continue
            if track and (r["cond"].get("race_track") or r["cond"].get("track")) != track:
                continue
            yield r


def market_win(r):
    """3連単の確定オッズ → 各車の1着確率（控除分を除いて正規化）"""
    q = {k: 1 / v for k, v in r["odds"]["3連単"].items() if v}
    s = sum(q.values())
    w = collections.Counter()
    for k, x in q.items():
        w[int(k.split("-")[0])] += x / s
    return w


def rows(r):
    res = {x["car"]: x for x in r["result"]}
    win = market_win(r)
    tns = r["odds"].get("単勝") or {}
    es = [e for e in r["entries"] if e.get("trial") and not e.get("absent") and e["car"] in res]
    if len(es) < 5:
        return []
    dist = r.get("distance") or 3100
    for e in es:
        dev = e.get("race_dev") if e.get("race_dev") is not None else 0.08
        e["_pred"] = (dist + (e.get("handicap") or 0)) / 100 * (e["trial"] + dev)
    by_trial = {e["car"]: i + 1 for i, e in enumerate(sorted(es, key=lambda e: e["trial"]))}
    by_pred = {e["car"]: i + 1 for i, e in enumerate(sorted(es, key=lambda e: e["_pred"]))}
    by_pop = {c: i + 1 for i, (c, _) in enumerate(sorted(win.items(), key=lambda x: -x[1]))}
    out = []
    for e in es:
        o = res[e["car"]].get("order") or 9
        out.append({"trial_rank": by_trial[e["car"]], "pred_rank": by_pred[e["car"]], "pop": by_pop.get(e["car"], 9),
                    "hc": e.get("handicap") or 0, "mkt": win.get(e["car"], 0), "won": o == 1, "top3": 1 <= o <= 3,
                    "tns": tns.get(str(e["car"])) or 0})
    return out


def table(rs, key, label):
    g = collections.defaultdict(list)
    for x in rs:
        g[key(x)].append(x)
    print(f"\n■ {label}")
    print(f"{'':>8} {'件数':>6} {'1着率':>7} {'市場':>7} {'差':>7} {'単勝回収':>8} {'3着内':>7}")
    for k in sorted(g):
        v = g[k]
        n = len(v)
        w = sum(x["won"] for x in v) / n
        m = sum(x["mkt"] for x in v) / n
        roi = sum(x["tns"] for x in v if x["won"]) / n * 100   # 単勝オッズは100円あたりの倍率
        t3 = sum(x["top3"] for x in v) / n
        print(f"{str(k):>8} {n:>6} {w:>7.1%} {m:>7.1%} {w - m:>+7.1%} {roi:>7.0f}% {t3:>7.1%}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--since")
    ap.add_argument("--place")
    ap.add_argument("--track", help="良 / 湿 / 斑")
    a = ap.parse_args(argv)
    rs, n = [], 0
    for r in load(a.data, a.since, a.place, a.track):
        rs += rows(r)
        n += 1
    print(f"対象 {n} レース / {len(rs)} 選手")
    if not rs:
        return
    table(rs, lambda x: x["pop"], "人気順（3連単から出した1着確率の順）")
    table(rs, lambda x: x["trial_rank"], "試走タイム順位")
    table(rs, lambda x: x["pred_rank"], "予測順位（試走+偏差とハンデから）")
    table(rs, lambda x: x["hc"], "ハンデ")
    table(rs, lambda x: (x["pred_rank"] <= 2, x["pop"] >= 4), "予測2位以内 × 4番人気以下（割安候補）")


if __name__ == "__main__":
    main()
