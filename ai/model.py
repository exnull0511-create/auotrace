"""
市場（確定オッズ）＋オートレース特有の要素で1着確率を出し直し、市場より高く見える目を買ったら儲かるかを検証する。

  python -m ai.model --data DIR [--split 2025-01-01]

- 条件付きロジット（レース内ソフトマックス）。入力の1つが「市場の1着確率の対数」なので、
  他の要素の係数＝「市場が織り込み切れていない分」になる。
- split より前で学習、後で検証（学習に使っていない期間で回収率を見る）。
- 買い方: 単勝（モデル確率×単勝オッズ > 閾値）、3連単（ハーヴィル法でモデル確率を出し、期待値 > 閾値、オッズ上限つき）
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
import re
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

FEATS = ["lp", "trial_gap", "pred_gap", "dev", "hc", "st", "retry", "wet_trial", "wet_rate", "trial_up", "trial_up_n",
         "rank_n", "temp_x_trial", "bad_st"]


def load(data: Path):
    rs = []
    for fp in sorted((data / "races").glob("*/*.jsonl.gz")):
        for line in gzip.open(fp, "rt", encoding="utf-8"):
            r = json.loads(line)
            if r.get("finished") and r["odds"].get("3連単") and r.get("odds_meta", {}).get("final"):
                rs.append(r)
    rs.sort(key=lambda r: (r["date"], r["place_code"], r["race_no"]))
    return rs


def market_win(r):
    q = {k: 1 / v for k, v in r["odds"]["3連単"].items() if v}
    s = sum(q.values())
    w = collections.Counter()
    for k, x in q.items():
        w[int(k.split("-")[0])] += x / s
    return w


def rank_num(s):
    m = re.match(r"([SAB])-?(\d+)", s or "")
    if not m:
        return 0.0
    base = {"S": 0, "A": 48, "B": 48 + 200}[m.group(1)]
    return -math.log1p(base + int(m.group(2)))   # 上位ほど大きい


def build(rs):
    """1レース = (特徴量行列, 1着の行番号, 付帯情報)"""
    last_trial = {}   # 選手ごとの直近の試走タイム（同じ走路状況のみ）
    out = []
    for r in rs:
        res = {x["car"]: x for x in r["result"]}
        es = [e for e in r["entries"] if isinstance(e.get("trial"), float) and 2.5 < e["trial"] < 4.5
              and not e.get("absent") and e["car"] in res]
        wet = 1.0 if (r["cond"].get("track_code") or 0) in (1, 5) else 0.0
        if len(es) >= 5:
            win = market_win(r)
            dist = r.get("distance") or 3100
            trials = [e["trial"] for e in es]
            best = min(trials)
            preds = [(dist + (e.get("handicap") or 0)) / 100 * (e["trial"] + (e.get("race_dev") if e.get("race_dev") is not None else 0.08)) for e in es]
            pbest = min(preds)
            temp = r["cond"].get("temp") or 15
            X, cars, winner = [], [], None
            for e, pr in zip(es, preds):
                key = (e["player_code"], wet)
                prev = last_trial.get(key)
                up = (prev - e["trial"]) if prev else 0.0
                st = (e.get("d90") or {}).get("st_ave")
                X.append([math.log(max(win.get(e["car"], 1e-4), 1e-4)),
                          (e["trial"] - best) * 100,           # 一番時計との差（0.01秒単位）
                          (pr - pbest),                         # 予測ゴールタイム差（秒）
                          (e.get("race_dev") or 0.08) * 100,
                          (e.get("handicap") or 0) / 10,
                          (st or 0.15) * 10,
                          1.0 if e.get("trial_retry") else 0.0,
                          wet * (e["trial"] - best) * 100,
                          wet * ((e.get("d180") or {}).get("wet_rate2") or 0) / 100,
                          up * 100,                              # 前走（同じ走路状況）より試走が何0.01秒良くなったか
                          1.0 if prev else 0.0,
                          rank_num(e.get("rank")),
                          (temp - 15) / 10 * (e["trial"] - best) * 100,
                          1.0 if (st or 0) > 0.2 else 0.0])
                cars.append(e["car"])
                if res[e["car"]].get("order") == 1:
                    winner = len(cars) - 1
            if winner is not None:
                out.append((np.array(X), winner, {"r": r, "cars": cars}))
        for e in r["entries"]:
            if isinstance(e.get("trial"), float) and 2.5 < e["trial"] < 4.5:
                last_trial[(e["player_code"], wet)] = e["trial"]
    return out


def nll(w, data, l2=0.01):
    tot, g = 0.0, np.zeros_like(w)
    for X, y, _ in data:
        z = X @ w
        z -= z.max()
        p = np.exp(z)
        p /= p.sum()
        tot -= math.log(p[y] + 1e-12)
        g -= X[y] - p @ X
    tot += l2 * np.sum(w[1:] ** 2) * len(data) / 1000
    g[1:] += 2 * l2 * w[1:] * len(data) / 1000
    return tot, g


def probs(w, X):
    z = X @ w
    z -= z.max()
    p = np.exp(z)
    return p / p.sum()


def harville(p, cars):
    d = {}
    for i in range(len(p)):
        for j in range(len(p)):
            if j == i:
                continue
            for k in range(len(p)):
                if k in (i, j):
                    continue
                d[f"{cars[i]}-{cars[j]}-{cars[k]}"] = p[i] * p[j] / (1 - p[i]) * p[k] / (1 - p[i] - p[j])
    return d


def evaluate(w, test, label):
    base = np.zeros_like(w)
    base[0] = 1.0
    ll_m = sum(-math.log(probs(base, X)[y] + 1e-12) for X, y, _ in test)
    ll = sum(-math.log(probs(w, X)[y] + 1e-12) for X, y, _ in test)
    print(f"\n[{label}] {len(test)}R  対数損失/R  市場 {ll_m / len(test):.4f}  モデル {ll / len(test):.4f}  改善 {(ll_m - ll) / len(test):+.4f}")
    # 単勝
    print("  単勝: 期待値の閾値  点数  的中  回収率")
    for th in (1.0, 1.1, 1.2, 1.3, 1.5):
        n = hit = ret = 0
        for X, y, m in test:
            p = probs(w, X)
            tns = m["r"]["odds"].get("単勝") or {}
            for i, c in enumerate(m["cars"]):
                o = tns.get(str(c)) or 0
                if o and p[i] * o > th:
                    n += 1
                    if i == y:
                        hit += 1
                        ret += o
        print(f"        >{th:.1f}  {n:>6} {hit:>5}  {ret / n * 100 if n else 0:6.1f}%")
    # 3連単
    print("  3連単: 閾値 オッズ上限  点数  的中  回収率  (レースあたり点数)")
    for th, cap in ((1.2, 100), (1.5, 100), (1.2, 200), (1.5, 200), (2.0, 200)):
        n = hit = ret = races = 0
        for X, y, m in test:
            r = m["r"]
            o = sorted([x for x in r["result"] if x["order"]], key=lambda x: x["order"])
            if len(o) < 3:
                continue
            ans = f"{o[0]['car']}-{o[1]['car']}-{o[2]['car']}"
            hv = harville(probs(w, X), m["cars"])
            buy = [(k, v) for k, v in r["odds"]["3連単"].items() if v and v <= cap and hv.get(k, 0) * v > th]
            races += 1
            n += len(buy)
            for k, v in buy:
                if k == ans:
                    hit += 1
                    ret += v
        print(f"        >{th:.1f}  ≤{cap:>4}倍 {n:>7} {hit:>5}  {ret / n * 100 if n else 0:6.1f}%  ({n / max(races, 1):.1f}点/R)")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--split", default="2025-01-01")
    a = ap.parse_args(argv)
    allr = build(load(a.data))
    tr = [d for d in allr if d[2]["r"]["date"] < a.split]
    te = [d for d in allr if d[2]["r"]["date"] >= a.split]
    print(f"学習 {len(tr)}R / 検証 {len(te)}R")
    w0 = np.zeros(len(FEATS))
    w0[0] = 1.0
    opt = minimize(nll, w0, args=(tr,), jac=True, method="L-BFGS-B")
    w = opt.x
    # 係数の標準誤差（ヘッセ行列の逆行列の近似）
    se = np.sqrt(np.diag(opt.hess_inv.todense())) if hasattr(opt, "hess_inv") else np.zeros_like(w)
    print("\n係数（lp以外は『市場が織り込めていない分』。|z|>2 なら偶然ではなさそう）")
    for f_, v, s in zip(FEATS, w, se):
        print(f"  {f_:>13} {v:+.4f}  z={v / s if s else 0:+.1f}")
    evaluate(w, tr, "学習期間（参考）")
    evaluate(w, te, "検証期間（本番）")


if __name__ == "__main__":
    main()
