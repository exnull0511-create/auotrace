"""
新しい指数の検証（2026-11-26）
  ① 市場の確率のゆがみ補正: 3連単の各組み合わせの確率を q^β で補正（大穴の買われすぎを割り引く）。1着確率は q^α。
  ③ 抜く力の指数: 周回ごとの順位から、選手ごとの「スタートで何人抜くか」「1周目以降に何人抜くか」を、
     スタート位置・出走数ごとの平均との差として、その日より前のレースだけで積み上げる（先読みなし）。

  python -m ai.indices --data DIR [--split 2025-01-01]

学習期間（split より前）で β・α・係数を決め、検証期間（split 以降）で
  - 1着予想の当てはまり（対数損失。小さいほど良い）
  - 期待値1.2超の目を買った回収率（確定オッズ。賭式別）
を「補正なし」「①補正あり」「①＋③」で比べる。
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import minimize, minimize_scalar

POOLS = {"単勝": (0, 1e9), "ワイド": (0, 1e9), "2連単": (10, 100), "2連複": (0, 30), "3連複": (10, 100)}
K = 10.0   # 指数の縮小（出走が少ない選手は平均に寄せる）


def load(data: Path):
    rs = []
    for fp in sorted((data / "races").glob("*/*.jsonl.gz")):
        for line in gzip.open(fp, "rt", encoding="utf-8"):
            r = json.loads(line)
            if not r.get("finished") or not r["odds"].get("3連単") or not (r.get("odds_meta") or {}).get("final"):
                continue
            o = sorted([x for x in r["result"] if x.get("order")], key=lambda x: x["order"])
            if len(o) < 3:
                continue
            ent = [e for e in r["entries"] if e.get("player_code") and e.get("handicap") is not None]
            if len(ent) < 5:
                continue
            wide = {}
            for x in ((r["refund"].get("ワイド") or {}).get("list") or []):
                if x.get("1thCarNo") and x.get("2thCarNo"):
                    wide["-".join(sorted((str(x["1thCarNo"]), str(x["2thCarNo"])), key=int))] = x["refund"] / 100
            rs.append({"date": r["date"], "id": r["race_id"], "ent": [(str(e["car"]), e["player_code"], e["handicap"]) for e in ent],
                       "order": [str(x["car"]) for x in o], "laps": r.get("laps") or {},
                       "odds": {p: r["odds"].get(p) or {} for p in ("3連単", "単勝", "2連単", "2連複", "3連複", "ワイド")},
                       "wide": wide})
    rs.sort(key=lambda r: (r["date"], r["id"]))
    return rs


# ---------------- ① 確率の補正 ----------------

def combo_probs(t3: dict, beta: float = 1.0) -> dict:
    q = {k: (1 / v) ** beta for k, v in t3.items() if v}
    s = sum(q.values())
    return {k: x / s for k, x in q.items()}


def win_from(cp: dict) -> dict:
    w = collections.Counter()
    for k, x in cp.items():
        w[k.split("-")[0]] += x
    return w


def fit_beta(rs):
    def nll(b):
        t = 0.0
        for r in rs:
            cp = combo_probs(r["odds"]["3連単"], b)
            t -= math.log(cp.get("-".join(r["order"][:3]), 1e-9))
        return t
    return minimize_scalar(nll, bounds=(0.5, 1.6), method="bounded").x


# ---------------- ③ 抜く力の指数 ----------------

def start_rank(ent):
    """スタート位置の順（前から）。ハンデが小さいほど前、同じハンデなら車番が小さいほど内・前"""
    return {c: i + 1 for i, (c, _, _) in enumerate(sorted(ent, key=lambda x: (x[2], int(x[0]))))}


def gains(r):
    """選手ごとの (スタートで上げた順位, 1周目以降に上げた順位)。周回データが無ければ None"""
    laps = r["laps"]
    if not laps:
        return None
    first = laps.get("1") or laps[min(laps, key=int)]
    pos1 = {str(c): i + 1 for i, c in enumerate(first)}
    fin = {c: i + 1 for i, c in enumerate(r["order"])}
    sr = start_rank(r["ent"])
    out = {}
    for c, pc, _ in r["ent"]:
        if c in pos1 and c in fin:
            out[pc] = (sr[c], pos1[c], fin[c])
    return out


def build_features(rs, split):
    """各レース・各選手の指数（そのレースより前の情報だけ）。基準の平均は学習期間だけで作る"""
    base = collections.defaultdict(lambda: [0, 0.0, 0.0])   # (スタート位置, 出走数) -> [件数, スタートでの上げ, その後の上げ]
    for r in rs:
        if r["date"] >= split:
            break
        g = gains(r)
        if not g:
            continue
        n = len(r["ent"])
        for pc, (s, p1, f) in g.items():
            b = base[(s, n)]
            b[0] += 1
            b[1] += s - p1
            b[2] += p1 - f
    bmean = {k: (v[1] / v[0], v[2] / v[0]) for k, v in base.items() if v[0]}
    hist = collections.defaultdict(lambda: [0, 0.0, 0.0])   # 選手 -> [件数, スタート残差の和, 抜き残差の和]
    day_buf, cur = [], None
    for r in rs:
        if r["date"] != cur:            # 同じ日のレースは翌日から反映（同日の後のレースで先読みしない）
            for pc, a, b in day_buf:
                h = hist[pc]
                h[0] += 1
                h[1] += a
                h[2] += b
            day_buf, cur = [], r["date"]
        n = len(r["ent"])
        r["idx"] = {}
        for c, pc, _ in r["ent"]:
            h = hist[pc]
            r["idx"][c] = (h[1] / (h[0] + K), h[2] / (h[0] + K), math.log1p(h[0]))
        g = gains(r)
        if g:
            for pc, (s, p1, f) in g.items():
                m = bmean.get((s, n))
                if m:
                    day_buf.append((pc, (s - p1) - m[0], (p1 - f) - m[1]))


# ---------------- 1着の条件付きロジット ----------------

def design(r, beta, use_idx):
    cp = combo_probs(r["odds"]["3連単"], beta)
    w = win_from(cp)
    cars = [c for c, _, _ in r["ent"] if w.get(c)]
    X = []
    sr = start_rank(r["ent"])
    for c in cars:
        row = [math.log(max(w[c], 1e-6))]
        if use_idx:
            st, ov, ln = r["idx"].get(c, (0, 0, 0))
            back = (sr[c] - 1) / max(len(sr) - 1, 1)      # 後ろのスタートほど1
            row += [st, ov, ov * back, st * (1 - back)]
        X.append(row)
    y = cars.index(r["order"][0]) if r["order"][0] in cars else None
    return np.array(X), y, cars, cp


def fit_logit(D):
    def f(wt):
        tot, g = 0.0, np.zeros_like(wt)
        for X, y, _, _ in D:
            z = X @ wt
            z -= z.max()
            p = np.exp(z)
            p /= p.sum()
            tot -= math.log(p[y] + 1e-12)
            g -= X[y] - p @ X
        return tot, g
    w0 = np.zeros(D[0][0].shape[1])
    w0[0] = 1.0
    o = minimize(f, w0, jac=True, method="L-BFGS-B")
    return o.x


def probs(wt, X):
    z = X @ wt
    z -= z.max()
    p = np.exp(z)
    return p / p.sum()


def pool_ev(cp, odds, win_adj=None):
    """組み合わせ確率 → 各賭式の目の確率。win_adj があれば1着の車ごとに確率を掛け直す（モデルの1着確率に合わせる）"""
    if win_adj:
        cp = {k: v * win_adj.get(k.split("-")[0], 1.0) for k, v in cp.items()}
        s = sum(cp.values())
        cp = {k: v / s for k, v in cp.items()}
    srt = lambda *v: "-".join(sorted(v, key=int))  # noqa: E731
    imp = {p: collections.Counter() for p in POOLS}
    for k, x in cp.items():
        a, b, c = k.split("-")
        imp["単勝"][a] += x
        imp["2連単"][f"{a}-{b}"] += x
        imp["2連複"][srt(a, b)] += x
        imp["3連複"][srt(a, b, c)] += x
        for k2 in {srt(a, b), srt(a, c), srt(b, c)}:
            imp["ワイド"][k2] += x
    return imp


def roi_table(D, rs_by_id, how, th=1.2):
    st = {p: [0, 0, 0.0] for p in POOLS}
    for (X, y, cars, cp), rid in D:
        r = rs_by_id[rid]
        adj = None
        if how["w"] is not None:
            pm = probs(how["w"], X)
            w0 = win_from(cp)
            adj = {c: pm[i] / max(w0[c], 1e-9) for i, c in enumerate(cars)}
        imp = pool_ev(cp, r["odds"], adj)
        o = r["order"]
        srt = lambda *v: "-".join(sorted(v, key=int))  # noqa: E731
        hit = {"単勝": o[0], "2連単": f"{o[0]}-{o[1]}", "2連複": srt(o[0], o[1]), "3連複": srt(o[0], o[1], o[2])}
        for pool, (lo, hi) in POOLS.items():
            for k, p in imp[pool].items():
                v = r["odds"][pool].get(k)
                od = v[0] if isinstance(v, list) else v
                if not od or not (lo <= od < hi) or p * od <= th:
                    continue
                s = st[pool]
                s[0] += 1
                pay = r["wide"].get(k, 0) if pool == "ワイド" else (r["odds"][pool].get(k) if k == hit[pool] else 0)
                if pay:
                    s[1] += 1
                    s[2] += pay
    return st


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--split", default="2025-01-01")
    a = ap.parse_args(argv)
    rs = load(a.data)
    build_features(rs, a.split)
    tr = [r for r in rs if r["date"] < a.split and r["date"] >= "2019-01-01"]   # 2018年は指数の助走期間
    te = [r for r in rs if r["date"] >= a.split]
    print(f"学習 {len(tr)}R（2019〜） / 検証 {len(te)}R（{a.split}〜）")

    beta = fit_beta(tr[::3])
    print(f"\n① 3連単の確率の補正 β = {beta:.3f}（1未満なら大穴を割り引き、人気側を上げる）")

    res = {}
    for name, b, use in (("補正なし", 1.0, False), ("①補正", beta, False), ("①＋③抜く力", beta, True)):
        Dtr = [design(r, b, use) for r in tr]
        Dtr = [d for d in Dtr if d[1] is not None]
        w = fit_logit(Dtr) if (use or b != 1.0) else np.array([1.0])
        Dte = [(design(r, b, use), r["id"]) for r in te]
        Dte = [(d, rid) for d, rid in Dte if d[1] is not None]
        ll = sum(-math.log(probs(w, d[0])[d[1]] + 1e-12) for d, _ in Dte) / len(Dte)
        res[name] = (w, Dte, ll)
        print(f"\n[{name}] 係数 {np.round(w, 3).tolist()}  検証の対数損失/R {ll:.4f}")
    base_ll = res["補正なし"][2]
    for n in ("①補正", "①＋③抜く力"):
        print(f"  {n}: 対数損失の改善 {base_ll - res[n][2]:+.4f}/R")
    print("  （③の係数の並び: [市場の対数確率, スタート力, 抜く力, 抜く力×後ろのスタート, スタート力×前のスタート]）")

    by_id = {r["id"]: r for r in te}
    print("\n検証期間の回収率（確定オッズ、期待値1.2超、賭式ごとのオッズ帯は従来どおり）")
    print("  賭式     " + "  ".join(f"{n:>16}" for n in res))
    tabs = {}
    for n, (w, Dte, _) in res.items():
        tabs[n] = roi_table(Dte, by_id, {"w": w if n == "①＋③抜く力" else (None if n == "補正なし" else w)})
    for pool in POOLS:
        print(f"  {pool:5} " + "  ".join(
            f"{t[pool][0]:>6}点 {t[pool][2] / t[pool][0] * 100 if t[pool][0] else 0:5.1f}%" for t in tabs.values()))
    json.dump({"beta": beta, "w_calib": res["①補正"][0].tolist(), "w_idx": res["①＋③抜く力"][0].tolist()},
              open("/tmp/indices_params.json", "w"))


if __name__ == "__main__":
    main()
