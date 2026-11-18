"""
「単勝と3連単のオッズの食い違い」が、実際に買える時点（締切前）でも残っているかの検証。

  python -m ai.odds_timing --data DIR [--th 1.2]

締切前の各時点のオッズで「3連単から出した1着確率 × その時点の単勝オッズ > 閾値」の単勝を買ったとする。
払戻は確定オッズで計算（実際の払戻は確定オッズで決まるため）。
締切までの残り時間ごとに 点数・的中・回収率 と、その時点から確定までの単勝オッズの変化を出す。
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
from pathlib import Path

BUCKETS = [(30, 60), (15, 30), (8, 15), (4, 8), (2, 4), (0, 2)]


def win_prob(t3: dict):
    q = {k: 1 / v for k, v in t3.items() if v}
    s = sum(q.values())
    w = collections.Counter()
    for k, x in q.items():
        w[k.split("-")[0]] += x / s
    return w


def load_results(data: Path):
    res = {}
    for fp in (data / "races").glob("*/*.jsonl.gz"):
        for line in gzip.open(fp, "rt", encoding="utf-8"):
            r = json.loads(line)
            if r.get("finished") and r["odds"].get("単勝"):
                w = [x["car"] for x in r["result"] if x["order"] == 1]
                if w:
                    res[r["race_id"]] = (str(w[0]), r["odds"]["単勝"])
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--th", type=float, default=1.2)
    a = ap.parse_args(argv)
    res = load_results(a.data)
    stat = {b: [0, 0, 0.0, []] for b in BUCKETS}   # 点数, 的中, 払戻, オッズ変化率
    nr = 0
    for fp in sorted((a.data / "odds_ts").glob("*/*/*.jsonl")):
        d = fp.parent.name
        place, rn = fp.stem.rsplit("_", 1)
        rid = f"{d}_{place}_{rn}"
        if rid not in res:
            continue
        nr += 1
        winner, final = res[rid]
        snaps = [json.loads(x) for x in fp.read_text().splitlines()]
        for b in BUCKETS:
            # その時間帯の中で一番締切に近い時点のオッズを使う
            ss = [s for s in snaps if b[0] <= s["m_left"] < b[1] and s["odds"].get("3連単") and s["odds"].get("単勝")]
            if not ss:
                continue
            s = min(ss, key=lambda x: x["m_left"])
            p = win_prob(s["odds"]["3連単"])
            for car, o in s["odds"]["単勝"].items():
                if p.get(car, 0) * o > a.th:
                    st = stat[b]
                    st[0] += 1
                    fo = final.get(car) or 0
                    if fo:
                        st[3].append(fo / o - 1)
                    if car == winner:
                        st[1] += 1
                        st[2] += fo
    print(f"締切前オッズのあるレース: {nr}R  （閾値 {a.th}）")
    print("締切まで   点数  的中  回収率(確定オッズで払戻)  買った時→確定の単勝オッズ変化(中央値)")
    for b in BUCKETS:
        n, h, ret, ch = stat[b]
        med = sorted(ch)[len(ch) // 2] if ch else 0
        print(f" {b[0]:>2}〜{b[1]:>2}分 {n:>6} {h:>5}  {ret / n * 100 if n else 0:7.1f}%        {med:+.0%}")


if __name__ == "__main__":
    main()
