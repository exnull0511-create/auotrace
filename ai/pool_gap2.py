"""
3連単のオッズと他の賭式（2連単・2連複・3連複・ワイド・単勝）の食い違いの検証。

  python -m ai.pool_gap2 --data DIR [--split 2025-01-01]

3連単の確定オッズから各組み合わせの確率を出し（控除分を除いて正規化）、他の賭式の組み合わせごとに足し上げる。
その確率 × その賭式のオッズ（=期待値）が閾値を超える目を買ったときの回収率を、賭式・閾値・オッズ帯ごとに出す。
ワイドはオッズに幅があるので下限で期待値を計算し、払戻は実際の払戻金で計算する。
split より前（学習）と後（検証）に分けて出す。
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
from itertools import combinations
from pathlib import Path

POOLS = ["単勝", "2連単", "2連複", "ワイド", "3連複"]
THS = [1.0, 1.1, 1.2, 1.5]
BANDS = [(0, 10), (10, 30), (30, 100), (100, 1e9)]


def implied(t3: dict) -> dict:
    """3連単の確定オッズ → 各賭式の組み合わせごとの確率"""
    q = {k: 1 / v for k, v in t3.items() if v}
    s = sum(q.values())
    out = {p: collections.Counter() for p in POOLS}
    for k, x in q.items():
        a, b, c = k.split("-")
        pr = x / s
        out["単勝"][a] += pr
        out["2連単"][f"{a}-{b}"] += pr
        out["2連複"]["-".join(sorted((a, b), key=int))] += pr
        out["3連複"]["-".join(sorted((a, b, c), key=int))] += pr
        for x1, x2 in combinations((a, b, c), 2):        # ワイド: 3着以内に2車とも
            out["ワイド"]["-".join(sorted((x1, x2), key=int))] += pr
    return out


def hits(r: dict) -> dict:
    """賭式ごとの的中の組み合わせ → 100円あたりの払戻倍率"""
    o = sorted([x for x in r["result"] if x.get("order")], key=lambda x: x["order"])
    if len(o) < 3:
        return {}
    a, b, c = (str(x["car"]) for x in o[:3])
    h = {"単勝": {a: None}, "2連単": {f"{a}-{b}": None}, "2連複": {"-".join(sorted((a, b), key=int)): None},
         "3連複": {"-".join(sorted((a, b, c), key=int)): None}}
    # ワイドは払戻金から（同着などで複数あり）
    w = {}
    for x in ((r["refund"].get("ワイド") or {}).get("list") or []):
        cars = [str(x.get(k)) for k in ("1thCarNo", "2thCarNo") if x.get(k)]
        if len(cars) == 2:
            w["-".join(sorted(cars, key=int))] = x["refund"] / 100
    h["ワイド"] = w
    for p in ("単勝", "2連単", "2連複", "3連複"):   # 確定オッズで払戻（払戻金と一致することは単勝・3連単で確認済み）
        h[p] = {k: (r["odds"].get(p) or {}).get(k) for k in h[p]}
    return h


def odds_of(r, pool, k):
    v = (r["odds"].get(pool) or {}).get(k)
    if isinstance(v, list):    # ワイド [下限, 上限]
        return v[0]
    return v


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--split", default="2025-01-01")
    a = ap.parse_args(argv)
    # stat[(期間, 賭式, 閾値, オッズ帯)] = [点数, 的中, 払戻]
    stat = collections.defaultdict(lambda: [0, 0, 0.0])
    sales = collections.defaultdict(list)
    nr = collections.Counter()
    for fp in sorted((a.data / "races").glob("*/*.jsonl.gz")):
        for line in gzip.open(fp, "rt", encoding="utf-8"):
            r = json.loads(line)
            if not r.get("finished") or not r["odds"].get("3連単") or not (r.get("odds_meta") or {}).get("final"):
                continue
            h = hits(r)
            if not h:
                continue
            per = "学習" if r["date"] < a.split else "検証"
            nr[per] += 1
            for p, v in ((r.get("odds_meta") or {}).get("sales") or {}).items():
                if v:
                    sales[p].append(int(v))
            imp = implied(r["odds"]["3連単"])
            for pool in POOLS:
                for k, pr in imp[pool].items():
                    o = odds_of(r, pool, k)
                    if not o:
                        continue
                    ev = pr * o
                    band = next(bb for bb in BANDS if bb[0] <= o < bb[1])
                    pay = h[pool].get(k) or 0
                    for th in THS:
                        if ev > th:
                            for bkey in (band, "全体"):
                                s = stat[(per, pool, th, bkey)]
                                s[0] += 1
                                if pay:
                                    s[1] += 1
                                    s[2] += pay
    print(f"レース数 学習{nr['学習']} / 検証{nr['検証']}（境目 {a.split}）")
    print("1レースの票数（中央値）:", {p: sorted(v)[len(v) // 2] for p, v in sales.items()})
    for pool in POOLS:
        print(f"\n■ {pool}  （期待値 = 3連単から出した確率 × {pool}のオッズ）")
        print("  閾値   学習: 点数 的中 回収率   |  検証: 点数 的中 回収率")
        for th in THS:
            tr, te = stat[("学習", pool, th, "全体")], stat[("検証", pool, th, "全体")]
            f = lambda s: f"{s[0]:>7} {s[1]:>5} {s[2] / s[0] * 100 if s[0] else 0:6.1f}%"  # noqa: E731
            print(f"  >{th:<4} {f(tr)}  |  {f(te)}")
        print("  （閾値1.2・オッズ帯別）")
        for bb in BANDS:
            tr, te = stat[("学習", pool, 1.2, bb)], stat[("検証", pool, 1.2, bb)]
            f = lambda s: f"{s[0]:>7} {s[1]:>5} {s[2] / s[0] * 100 if s[0] else 0:6.1f}%"  # noqa: E731
            print(f"   {bb[0]:>3}〜{'' if bb[1] > 1e8 else bb[1]:<4}倍 {f(tr)}  |  {f(te)}")


if __name__ == "__main__":
    main()
