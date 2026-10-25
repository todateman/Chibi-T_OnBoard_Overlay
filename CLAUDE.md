# CLAUDE.md

Claude Code がこのリポジトリで作業するときの前提情報。

## 概要

Honda エコマイレッジチャレンジ(チーム: ちびTインターナショナル)の車両ロガー CSV を、車載カメラ映像にオーバーレイするツール。`ecorun` パッケージに実体があり、`ecorun_overlay.py` は後方互換のラッパ。使い方とログ仕様は README.md を参照。実行は `uv run ...`。

## 構成

- `ecorun/telemetry.py` : `load_log()` で CSV 読込 → 列名を `COL` の内部キー(`speed`, `rpm`, `fuel_p` など)へ変換 → 経過秒 `t` の配列、GPS 未測位の補完、`smooth_held()` で同値の連続を間引き、方位算出、走行開始時刻(`走行時間` が 0 から動き出した時刻)とラップ開始時刻・エンジンON区間の抽出 → `Telemetry`
- `ecorun/config.py` : レイアウトプリセット(TOML)の pydantic モデル。**これが唯一の仕様**で、`schema/preset.schema.json`・GUI のフォーム・`ecorun-layout widgets`・API の OpenAPI はここから生成される。読込(tomllib)と保存(tomlkit でコメント・並びを保持)
- `ecorun/widgets.py` : ウィジェットの描画実装(`IMPLS` が type → クラス)。各ウィジェットは自分の box(x, y, w, h)の内側だけに描く
  - `static(img)` : パネル・目盛り・区間全体のグラフなど毎フレーム共通の部分。1 回だけ呼ばれる
  - `dynamic(img, ts)` : ログ秒 `ts` の値に依存する部分
  - 文字サイズ・余白は基準サイズ(モデルの `REF`)での値を書き、`self.S()` / `self.F()` / `self.T()` で拡縮する。ゲージは基準半径 180
  - `dynamic` は書き出しで毎フレーム呼ばれるので、box 全体の SS レイヤを毎回作らない(ゲージの値の弧は 1 回だけ描いた 270° の弧を角度で切り、針は外接矩形だけ SS で描く)
- `ecorun/render.py` : `OverlayRenderer` が全ウィジェットの static を重ねた静的レイヤを作り、`render(ts)` でそのコピーに dynamic を順に描いて 1920×1080 RGBA を返す。`debug_boxes` で枠と id を描く
- `ecorun/video.py` : ffprobe、RGBA の raw フレームを stdin パイプで ffmpeg に渡す(`--video` ありなら overlay フィルタで合成して H.264、`.mov` なら ProRes 4444)。H.264 は `--encoder auto` で macOS なら h264_videotoolbox(libx264 medium は 1080p で約 70 fps が上限で、描画と CPU を取り合う)、1 フレーム抽出。複数の動画(フォルダ可)は `probe_videos()` で `VideoSource` にまとめ、concat demuxer で 1 本として読む(`locate()` で通し秒 → ファイルと秒)
- `ecorun/parallel.py` : `render_frames()` で描画を `--jobs` 個のプロセスに分ける。各ワーカーが `OverlayRenderer.from_spec()` で作り直し、共有メモリ(RawArray)のスロットに書く。キューで渡すのはスロット番号だけ(8 MB のフレームを pickle するとメインが詰まる)
- `ecorun/cli.py` : `ecorun-overlay`(従来の CLI + `--preset` / `--debug-boxes`、`--video` は複数可、`--concat-only`、`--start` / `--end` で書き出し区間を動画の再生位置で指定)
- `ecorun/timemap.py` : 動画秒 → ログ秒の対応 `TimeMap`(区間ごとに offset。従来の `--sync` は `TimeMap.constant`)と、OCR 結果 `OsdSegment` の JSON 入出力
- `ecorun/ocrsync.py` : `ecorun-sync`。焼き込み日時(`OSD_TIME_BOX`)を Tesseract で読む → 差(焼き込み時刻 − 動画秒)が揃う連続を区間に → 境目を二分探索 → 秒の表示が変わるフレームで秒未満を補正。`--sync auto` / `--sync FILE.json` から使う
- `ecorun/layout.py` : `ecorun-layout`(list / widgets / schema / validate / lint)。lint のルールもここ
- `ecorun/gui/server.py` + `static/` : `ecorun-gui`。FastAPI + ビルド不要の素の JS。プレビューは `/api/render` で本番と同じ描画。`/api/events`(SSE)で presets/ の外部変更を通知。再生中の背景は `/api/video-file` を `<video>` で流し、一時停止中は `/api/frame` の静止画
- `ecorun/gui/export.py` : GUI の「書き出し」。`/api/export` が画面のプリセット(と OCR の区間)を一時ファイルに書き、`python -m ecorun.cli` を別プロセスで実行して進捗(`N/M frames ETA`)を読む。1 本ずつ、中止はプロセスグループごと
- `presets/*.toml` : default / f9_mask / minimal
- `tests/` : pytest。合成ログ(`conftest.py`)を使うので sampledata が無くても動く

### ウィジェットを追加・変更するとき

1. `config.py` にモデル(`type` の Literal、x/y/w/h の既定値、`REF`、各フィールドの title / description / 範囲)を足し、`WIDGET_MODELS` に登録
2. `widgets.py` に描画クラスを足し、`IMPLS` に登録
3. `uv run ecorun-layout schema --write` で JSON Schema を更新
4. `uv run pytest`(box の外に描いていないか、拡縮しても崩れないかを全型で検査する)

## データの特性

### 現行フォーマット(sampledata/LOG0650.CSV、2026/10/10 取得、10 Hz)

- 行は 0.1 秒間隔(約 50 秒ごとに 0.3 秒の欠けがある)。ただし値の更新はセンサごとに異なる: 回転数 10 Hz、速度・GPS・距離 約 5 Hz、燃費 2 Hz、燃圧 2〜5 Hz、標高 0.5 Hz 程度。間の行は同じ値が続く → `SMOOTH_COLS` の列は `smooth_held()` で値が変わった行を節点に補間し直す
- 列名に単位が付いた(`回転数(rpm)`、`lng(経度)` など)。`COL` に現行・旧の両方の列名を候補として持つ
- 燃料噴射時間・点火進角・燃料噴射終了角度・場所・1次/2次空気圧は表示しない(`COL` に載せず読み捨て)
- 燃圧はエンジン ON 中に約 0.31 MPa、惰行中は 0.26 前後まで下がる。ポンプ停止時は約 0.13 MPa
- GPS 未測位でも標高に値が入っている → 標高グラフも `has_fix` の区間だけ描く
- 発進直後は速度が 0.1 → 7.4 km/h のように 1 秒近く更新されないことがある(補間で直線になる)

### 旧フォーマット(sampledata/LOG0612.CSV、2026/06/20 鈴鹿で取得、1 Hz)

- サンプリングは約 1 Hz。タイムスタンプは 0.7 / 1.3 秒間隔の揺れや小数部 .00 / .80 の混在がある → 必ず `np.interp` で補間する
- GPS 未測位の間は lat/lon = 0(先頭の約 400 行)。`has_fix` で判定し、コース図・現在地では除外する
- 回転数 0 = エンジン停止(パルス&グライドの惰行中)。ON 時間は全体の約 2 割。ON/OFF 判定は補間せず step 値を使う
- 燃費は走行開始直後に外れ値(最大 2926 km/L)が出る → `FUEL_ECO_MIN_DIST_M` 未満では非表示
- `ラップ数` = 0 はスタート前。1 周は約 2.2 km
- `loc` 列(`to` / `su`)の意味は未確認。現状は未使用
- 燃圧の列が無い → 燃圧グラフは表示しない(`OPTIONAL_COLS`)

### 2025 年のフォーマット(LOG0443.CSV、2025/06/14 鈴鹿で取得、1 Hz。リポジトリ外)

- 列は `記録日時,速度(km/h),ラップ数,走行時間,回転数,走行距離,積算燃料,燃費,lat,lon,温度` のみ。標高・気圧・気温・湿度・燃圧が無い → `alt_graph` / `fuel_p_graph` は省き、`env` はある項目(エンジン温度)だけ並べる
- GPS 測位前(先頭の約 380 行)は `記録日時` が空 → その行は捨てられ、測位後がログ開始になる
- `温度` に 0.00 / 7.00 / 8.00 が混ざる(ロガー側の桁落ちと思われる)。未対処

### VSYSTO F9 の .TS(2026/10/25 茂木、`REC_261025-122957-000119F.TS` ほか 8 本 + LOG0672、リポジトリ外)

- 1920×1080 H.264、約 25 fps(`r_frame_rate` 30000/1201、`avg_frame_rate` 25/1。1 本 300.29 秒で 7498 フレームなので前者が実態)、AAC 32 kHz。各ファイルの start_time は 1.4 秒
- 音声に壊れた ADTS フレームが混ざる → `-c:a copy` で mp4 に入れると途中で止まるので、音声は AAC で再エンコードする(`AUDIO_ARGS`)
- 焼き込みは分割の境目でも連続(録画の欠けなし)。OCR で「12:55」を 25 秒間「12:53」と読み続けた区間があった → 前後の区間の差が揃っていれば間の区間は誤読として捨てる(`_drop_sandwiched`)

## レイアウトの方針(VSYSTO F9 ドラレコ、1920×1080、ローアングル前方カメラ)

- 地平線付近の帯(y≈380〜640)はコース前方・マーシャル・前走車が映るので、何も置かない → 各プリセットの `[[keepout]]` に入れてあり、`ecorun-layout lint` が重なりを警告する
- 映像の下半分はほぼ路面なので、ゲージ類はここに集める。標高グラフは左下、燃圧グラフは右下(回転計の隣)
- 右下の焼き込み表示(日時・機種名・速度)はおよそ x 1140〜1720、y 1000〜1040 の位置にある → `f9_mask` プリセットの `mask` ウィジェットで隠す。日時部分(x 1157〜1410、y 1002〜1028)は自動同期の OCR に使う。白い太字が路面に直接載るので、路面の白い粒が重なると 1 桁誤読することがある(約 2%)。ocrsync は誤読を前後の読みとの整合で捨てる
- 座標はすべて canvas(1920×1080)基準で書く。出力時に ffmpeg で動画解像度へスケールされる

## 開発時の確認方法

レイアウトを変えるときは、GUI(`uv run ecorun-gui`)を使うか、`presets/*.toml` を直接編集する。直接編集したら次の順で確認する(GUI を開いていれば自動で再読込される)。

```bash
uv run ecorun-layout widgets speed_gauge          # 使えるパラメータと範囲
uv run ecorun-layout validate default             # スキーマ検証(未知キー・範囲・id 重複)
uv run ecorun-layout lint default --json          # はみ出し・配置禁止帯・重なり
# 静止画で見た目を確認(ログ秒を指定)。--debug-boxes で枠と id が出る
uv run ecorun-overlay sampledata/LOG0650.CSV --preset default --preview 150 --debug-boxes -o preview.png
uv run ecorun-overlay sampledata/LOG0612.CSV --preview 1040 -o preview_old.png  # 旧フォーマット

# 短い区間で合成・同期を確認
uv run ecorun-overlay sampledata/LOG0650.CSV --preset f9_mask --video sample.mp4 --sync 15:12:20 --duration 10 -o test.mp4
# 書き出す区間は動画の再生位置で(--start / --end、秒か [H:]MM:SS)
uv run ecorun-overlay sampledata/LOG0650.CSV --preset f9_mask --video sample.mp4 --sync 15:12:20 --start 1:00 --end 1:10 -o test.mp4
# 分割動画はフォルダか複数ファイルで(連結だけなら --concat-only)
uv run ecorun-overlay sampledata/LOG0650.CSV --preset f9_mask --video F9/ --sync 15:12:20 -o test.mp4
# 焼き込み日時の OCR で自動同期(要 tesseract)。区間の表だけ見るなら ecorun-sync
uv run ecorun-sync F9/ --csv sampledata/LOG0650.CSV -o sync.json   # --save-crop crop.png で枠の確認
uv run ecorun-overlay sampledata/LOG0650.CSV --preset f9_mask --video F9/ --sync sync.json -o test.mp4

uv run pytest
```

描画を変更したら、必ずプレビュー画像を生成して目視で確認する。

## TODO・改善候補

- 描画の高速化の残り(Apple M5 で書き出し約 200 fps。上限は h264_videotoolbox の約 240 fps)。x264 の環境(クラウドなど)では x264 自体が律速なので `--x264-preset veryfast` が効く。候補: 残りの文字ウィジェット(env・timer)を値が変わったときだけ描き直す、pycairo へ移行する
- 焼き込み日時の無い動画の同期: 動画のオプティカルフローから発進点を検出し、ログと突き合わせる(VSYSTO F9 の焼き込み日時は `--sync auto` で OCR 済み)
- GUI: 配置禁止帯をキャンバス上でドラッグ編集できるようにする、複数ウィジェットの同時選択・整列
- 勾配の表示(標高はグラフ表示済み。GPS 標高は 0.1 m 刻みで揺れるので平滑化が必要)
  