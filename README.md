# オートレース分析システム

過去レースを集めて、**オッズに対して割安な買い目**（市場のゆがみ）を探す。
競輪攻略（[keirinbusines](https://github.com/exnull0511-create/keirinbusines)）と同じ作りにする。

- 収集は GitHub Actions で自動実行（スマホから操作できる）
- データは `data` ブランチに `races/YYYY/YYYY-MM-DD.jsonl.gz`（1行=1レース）で溜める
- 分析・AIは `ai/`、オッズに対する割安・割高の採点も競輪版と同じ形にする

オートレースの基本と、検証する仮説は [docs/autorace_primer.md](docs/autorace_primer.md)。

## 進め方

1. **サイト構造の調査**（`tools/probe.py` → `probe-samples` ブランチに実ページを保存）
2. 収集プログラム `collector/`：出走表・試走タイム・偏差・走路状況・結果・払戻・確定オッズ
3. 過去分のバックフィル（数年分）
4. 仮説の検証：試走順位・偏差・ハンデ換算 × 人気 × 回収率
5. 買い目の採点（自分で組んだフォーメーションをAIが割安/割高で判定）
