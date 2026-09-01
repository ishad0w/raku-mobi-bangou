# ラク・モビ・バンゴウ

[日本語](README.md) | [English](README_EN.md)

**raku-mobi-bangou** は、4桁のマスクごとに日本の携帯電話番号候補を収集し、
通常読みの響きと語呂合わせを別々に順位付けする Python プロジェクトです。

## クイックスタート

Python 3.10 以上で動作し、追加パッケージは不要です。候補を返す HTTPS JSON
エンドポイントを設定して実行します。

```bash
export PHONE_NUMBER_API_URL="https://example.invalid/path"
python3 raku-mobi-bangou.py --rounds 300
```

特定のマスクだけを収集する場合:

```bash
python3 raku-mobi-bangou.py 1111 2222 --rounds 300
```

URL にクエリやフラグメントは指定できません。CLI が `mask=XXXX` を追加します。
全オプションは `python3 raku-mobi-bangou.py --help` で確認できます。

## 収集方法

- リクエストは並列化せず、実リクエスト間に1.1〜2.0秒のランダム待機を入れます。
  同じマスクへの論理リクエストは最低30秒間隔です。
- 履歴があるマスクでは、開始時の active coverage pool にある番号の
  90%を再観測することを目標に、
  過去と今回の応答から必要回数を適応的に見積もります。目標に達した時点で停止し、
  達しなければ `--rounds` のマスク別上限まで続けます。全マスクと再試行を合わせた
  既定の上限は5,000 HTTP 試行です。
- すべてのマスクを最初に5回ずつ公平に確認した後、coverage の不足と
  最近の新規番号の発見率に応じて残りの予算を配分します。低優先度のマスクも
  定期的に再確認します。
- 5回連続で空なら、そのマスクを停止します。履歴がないマスクは、
  成功応答が合計15回以上で、最後の新規発見以降の空でない応答が15回に達したら
  飽和とみなします。履歴があるマスクでも44個の番号 sample に新しい観測が
  なければその実行では停止しますが、これは未観測の負の evidence には使いません。
- 一時的な通信エラーやデコード失敗は、最初の試行に加えて最大3回再試行します。
  `Retry-After` と指数バックオフを尊重し、恒久的なエラーは再試行しません。
- `phoneNumber + id` は重複排除され、マスク別 CSV はロックとアトミック置換で
  更新されます。

標準出力には開始・進捗・完了サマリー、または致命的エラーだけを表示します。
詳細と再試行は `run/logs/` に保存し、
エンドポイント URL、Cookie、ヘッダー、レスポンス本文は記録しません。

## 履歴と未観測

`run/all_numbers.csv` は累積データではなく、今回の実行で実際に返された番号だけです。
同じスキャン範囲の前回 snapshot があれば `run/diff.csv` を作成します。
`not_observed` はランダムな返却で今回見えなかったという意味で、購入済み・予約済み・
利用不可を意味しません。

未観測を負の evidence として数えるのは、統計的に比較可能な scheduled full scan
だけです。各マスクで qualified evidence になるのは1日に最大1件です。
manual run と特定マスクランは新規発見と再出現には使いますが、欠落 evidence を
増やしません。3回連続の qualified miss で番号は
`possibly_unavailable` になります。`statistically_stale` には、5日以上の未観測、
5回以上の qualified miss、マスク別の包含確率から積み上げた10,000:1相当の evidence
をすべて必要とします。

番号は物理削除しません。stale になっても `run/lifecycle.csv` に tombstone として残り、
再観測すれば履歴を保ったまま復帰します。これらはすべて統計的な状態であり、
エンドポイントによる権威ある利用可否判定ではありません。

## 出力

| パス | 内容 |
|---|---|
| `csv/XXXX.csv` | マスク別の累積 `phoneNumber,id` |
| `run/all_numbers.csv` | 今回観測した番号の重複排除済み一覧 |
| `run/diff.csv` | 同じ範囲の前回 snapshot との差分 |
| `run/mask_summary.csv` | マスク別の coverage、予算、停止理由 |
| `run/lifecycle.csv` | tombstone を含む既知番号の状態 |
| `run/lifecycle_events.csv` | 状態変更と再出現の監査ログ |
| `run/logs/` | collector の完全ログとエラーログ |
| `run/TOP.md` | 通常読みの音と読みやすさ TOP |
| `run/GOROAWASE.md` | 語呂合わせ TOP |

Actions Artifacts には、収集結果、差分・coverage 診断、ログ、マスク別 CSV の
ZIP、ランキング成果物を30日間保存します。公開 Release に添付するのは
`TOP.md`、`GOROAWASE.md`、`all_numbers.csv` の3ファイルだけです。

## マスク

[masks.txt](masks.txt) は次の2形式を受け付けます。

```text
1235
1122 | いいふうふ（いい夫婦）
```

- `MASK` — 通常読みのリズムまたは数値パターンとして収集
- `MASK | かな読み（表記）` — 4桁すべてを表す語呂合わせ読みも付与

読みを持つマスクも通常読みの TOP 候補になります。空行と `#` で始まる行は無視します。

## 自動リリース

[GitHub Actions](.github/workflows/release.yml) は毎日10:10
（Asia/Tokyo）に完全スキャンを実行します。manual dispatch では、マスク別上限、
1〜9,000の全体リクエスト上限、反復プールの早期停止を無効にする `deep_scan`、
`1111,2222,3322` のようなカンマ区切りのマスクを
指定できます。指定マスクランは完全スキャンとは別の cache scope を使う prerelease です。
`skip_collection` と以前の `collection_artifact` を指定すると、再収集せずに同じ結果を
再ランキングできます。

ランキング対象は今回の `run/all_numbers.csv` だけです。

- **TOP 30 — 音と読みやすさ**: 通常の数字読みの滑らかさ、リズム、聞き取りやすさ
- **TOP 30 — 語呂合わせ**: [masks.txt](masks.txt) の読みを使った日本語の語呂合わせ
- **TOP 10 — 前回 snapshot からの追加**: 同じ scope の前回 snapshot に存在しなかった
  番号を通常読みで順位付けし、候補が10件以上ある時だけ Release 本文に掲載

完全スキャンは通常読みと語呂合わせをそれぞれ30件選びます。特定マスクランでは、
候補が少なければその件数までです。

Codex は候補 ID だけを順位付けします。Python が現在の snapshot に対して ID、件数、
読みを検証して Markdown を生成し、不正な選択は最大3回まで再試行します。

収集、ランキング、公開は別 job です。ランキングが失敗した場合は
**Re-run failed jobs** で収集 Artifact を再利用でき、エンドポイントを再度呼びません。
完全スキャンの Release は東京時刻の `YYYY-MM-DD_HH-MM` を tag と title に使います。
本文先頭は常に `# ラク・モビ・バンゴウ` です。

状態は schema v3 の Actions cache に保存します。cache がない、壊れている、または
互換性がない場合は空の状態から開始します。Repository に番号の seed は保存しません。

push と pull request では、ネットワーク収集を行わずに unit tests と workflow lint
を実行します。

## GitHub の設定

Repository の Actions secrets に `PHONE_NUMBER_API_URL` を追加します。
