# CLAUDE.md — オートレース分析システム（auotrace）

現在の進捗・決定事項・次のタスクは [docs/HANDOFF.md](docs/HANDOFF.md)。オートレースの基礎は [docs/autorace_primer.md](docs/autorace_primer.md)。

## 目的

オートレースの過去データと締切前オッズを集めて、**オッズに対して割安な買い目（市場のゆがみ）**を見つけ、勝ち続ける仕組みを作る。
姉妹プロジェクトの競輪攻略（[keirinbusines](https://github.com/exnull0511-create/keirinbusines)）と同じ作り（GitHub Actions で収集 → `data` ブランチに蓄積 → 分析 → ダッシュボード）。

ユーザーの方針: オートレース自体に興味は無く、「勝手にお金が増える」仕組みが欲しい。主観ではなくデータで判断する。

## ユーザーとのやりとり

- ユーザーは Shota。**日本語**で、結論を先に、平易に、短く。オートレースの知識はほぼ無い前提で専門用語は説明する。
- ほぼ外出先で**スマホから**操作する。GitHub のマージ操作ができないので、**修正は main に直接コミットして push する**（ブランチ・PRは作らない）。
- GitHub の設定画面（Secrets・Pages など）が必要なときは、スマホのブラウザで開けるURLと手順を示す。
- **秘密情報（LINE のトークン等）をチャットに貼らせない**。GitHub の Secrets に直接登録してもらう。
- 悪い結果（勝ち筋が無い、検証で崩れた等）も正直に伝える。

## 守るルール

1. **確定オッズと締切前オッズを混同しない。** 払戻は締切後の確定オッズで決まる。確定オッズでの回収率は「参考値」であり、実際に買える時点（締切◯分前）のオッズで判断して利益が残るかを別に検証する。
2. **検証は学習期間と検証期間を日付で分ける**（例: 〜2024年で作り、2025年以降で確かめる）。同じデータで作って測った数字を成果として報告しない。件数が少ないときは偶然の幅を明記する。
3. **自動投票はしない。** 投票サイトの規約で禁止されていることが多い。システムは候補の提示・通知まで。購入はユーザーが自分で行う。
4. **LINE 通知は停止中**（`odds-watch.yml` の `LINE_NOTIFY: "off"`）。再開や推奨ルールの変更は勝手にせず、検証結果をもとに提案し、ユーザーの了承を得てから。
5. **取得元サイトに負荷をかけすぎない。** 1リクエストごとに約1秒待つ（`Client(sleep=1.0)`）。過去分の並列取得は同時5本まで。
6. 単勝は売上が小さい（1レース約1,300票）。推奨するときは**1点数百円まで**という上限を添える。

## 技術構成

### データ取得元
公式 **autorace.jp の内部API（JSON）**。HTMLは読まない。
| API | 内容 | 備考 |
|---|---|---|
| `GET /race_info/XML/Calendar/?date=YYYY-MM` | 月の開催一覧 | 開催が始まったばかりの日は `finalRaceNo` が空 → 1Rの OtherRaceInfo から取る |
| `POST /race_info/RaceResult` | 着順・試走・競走タイム・ST・周回順位・払戻 | 2014年まで遡れる |
| `POST /race_info/OtherRaceInfo` | 天候・気温・走路温度・走路状況・締切時刻（`telvoteTime`） | |
| `POST /race_info/Odds` | 全賭式のオッズ・票数・試走タイム・公式/AI予想 | **CSRF必要**。発走後は確定オッズ |
| `POST /race_info/Program` | 出走表（ランク・ハンデ・試走・偏差・90/180日成績） | **CSRF必要** |

- CSRF: `/race_info/` を GET して `XSRF-TOKEN` クッキーを受け取り、`X-XSRF-TOKEN` ヘッダに載せる（`collector/autorace.py` の `Client`）。419 なら取り直し。
- POST の本文は `{placeCode, raceDate, raceNo}`。場コード: 1船橋(廃止) 2川口 3伊勢崎 4浜松 5飯塚 6山陽（placeKey の山陽は `sanyou`）。
- 走路状況コード: 0良 / 1湿 / 5斑。
- オッズパーク・WINTICKET は GitHub Actions（海外IP）から 403 になるので使わない。
- **この作業環境（Claude のサンドボックス）からは autorace.jp にも github.io にも直接つながらない。** サイトの中身を見るときは `tools/probe*.py` を Actions で動かし、結果をブランチに保存して clone する。github.com（git・API）はつながる。

### コード
| パス | 役割 |
|---|---|
| `collector/autorace.py` | APIクライアント（CSRF・リトライ・SiteDown）と1レース分の整形 `build_race` |
| `collector/run.py` | 収集CLI: `collect` / `backfill` / `remaining` / `repair` |
| `collector/odds_watch.py` | 締切前オッズの時系列記録・直前判定（ペーパートレード）・LINE通知・状態ファイル・health と自分自身の起動 |
| `collector/notify_line.py` | LINE Messaging API の push（`LINE_NOTIFY=off` で停止） |
| `tools/health.py` | 30分ごとの健康チェック、自動再起動、Issue通知、ダッシュボード用データ、ペーパートレード成績 |
| `tools/data_branch.sh` | `data` ブランチの checkout / push（並列書き込み対策つき） |
| `ai/explore.py` | 人気・試走順位などごとの的中率と回収率 |
| `ai/model.py` | 市場＋14項目の条件付きロジット（学習/検証分割） |
| `ai/pool_gap.py` | 単勝と3連単のオッズの食い違い（確定オッズ） |
| `ai/odds_timing.py` | 同じ食い違いが締切◯分前のオッズでも残るか |
| `ai/indices.py` | 新しい指数の検証: ①3連単の確率の補正（q^β）③抜く力の指数（周回順位から）。学習/検証分割 |
| `ai/pool_gap2.py` | 3連単と他の賭式（ワイド・2連単・2連複・3連複・単勝）の食い違い（学習/検証分割） |
| `docs/index.html` | ダッシュボード（GitHub Pages）。`data` ブランチの `health/dashboard.json` を読む |

### GitHub Actions
| ワークフロー | 起動 | 内容 |
|---|---|---|
| `collect.yml` | 毎日 0:40 JST / 手動（日付指定可） | 直近3日分の結果・確定オッズ |
| `backfill.yml` | 6時間ごと / 手動 | `repair`（取りこぼし点検）→ 年ごとに並列（最大5）で過去分。残りがあれば自分で次を起動 |
| `odds-watch.yml` | 自己起動（24時間）/ 手動 / 予備の予約 9:23・9:41・10:07・15:17・20:17 JST | 約5時間ごとに区切りのよい瞬間で自分で次を起動し、夜も止まらない。30分ごとに health も起動 |
| `health.yml` | 毎時7分・37分 / odds-watch から起動 | 見張り係 |
| `pages.yml` | docs/ の push | ダッシュボード公開 |
| `line-test.yml` | 手動 | LINE のテスト送信 |
| `probe.yml` | 手動 | サイト構造の調査 |

- **GitHub の予約実行（cron）は当てにならない**（0分・30分ちょうどは遅延・スキップが多い）。重要な処理は自己起動・相互起動で補う。
- 同じワークフローは `concurrency` で同時1本・待ち1本まで。
- 実行は**起動時のコミット**で動く。コードを直しても、動いている/待っている実行には反映されない。反映するには odds-watch を dispatch するだけでよい（今の回が締切直前のレースの無い瞬間に保存して終わり、待っていた新しい回が始まる）。
- Actions の手動起動は `gh workflow run` が使えないので `gh api -X POST repos/exnull0511-create/auotrace/actions/workflows/<file>/dispatches -f ref=main` を使う。

### data ブランチ
```
races/YYYY/YYYY-MM-DD.jsonl.gz   1行=1レース（entries/cond/result/laps/refund/odds/odds_meta/expect）
state/done.txt                   取得完了日（merge=union）
state/checked.txt                取り直し済みの日（merge=union）
state/calendar/YYYY-MM.json      開催カレンダーのキャッシュ
odds_ts/YYYY/YYYY-MM-DD/<場key>_<R>.jsonl   締切前オッズ（1行=1回の取得。m_left=締切まで何分）
paper/YYYY-MM-DD.jsonl           直前判定（単勝: 全車の期待値・推奨・notified / pool_picks: ワイド・2連単・2連複・3連複の割安な目）
paper/summary.json               ペーパートレード成績（by_pool: 賭式別 first=締切3分前ごろ / last=締切直前）
health/status.json, log.jsonl, dashboard.json, watch_status.json
health/charts/index.json, health/charts/YYYY-MM-DD/<場key>_<R>.json   オッズの動きグラフ用（直近7日・health.py が作る）
```
- 並列で書き込むため、push は `git pull --rebase -X theirs` で再試行し、`done.txt`/`checked.txt` は `.gitattributes` の `merge=union` で行を合流させる。
- レコードの形を変えたら `collector/run.py` の `SCHEMA` を上げる（全日付を取り直す）。

## コーディング方針

- Python 3.11。収集は標準ライブラリ＋`requests` のみ。分析は numpy / scipy。
- コメント・ログ・コミットメッセージは**日本語**。ユーザーがログを読んでも分かる言葉で。
- 長時間動くループは**1回の失敗で止めない**（例外を捕まえてログに出し、次へ）。サイトが連続で応答しないときは `SiteDown` で中断し、次回再開。
- 取得した生の値は捨てずに残す（分からないコードは生のまま保存）。数値化は `f()` / `i()` で安全に。
- 変更したら、ネットにつながらない手元で `tools/probe*.py` の保存サンプルや `data` ブランチの実データを使って動かして確かめてから push する。その後 Actions のログで実際の動作を確認する。
- 競輪版と同じ形・同じ言葉づかいにそろえる（ダッシュボード、health の判定、Issue通知）。
