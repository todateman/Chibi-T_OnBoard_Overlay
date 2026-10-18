# CLAUDE.md

Claude Code がこのリポジトリで作業するときの前提情報。

## 概要

Honda エコマイレッジチャレンジ(チーム: ちびTインターナショナル)の車両ロガー CSV を、車載カメラ映像にオーバーレイするツール。`ecorun` パッケージに実体があり、`ecorun_overlay.py` は後方互換のラッパ。使い方とログ仕様は README.md を参照。実行は `uv run ...`。

## 構成

- `ecorun/telemetry.py` : `load_log()` で CSV 読込 → 列名を `COL` の内部キー(`speed`, `rpm`, `fuel_p` など)へ変換 → 経過秒 `t` の配列、GPS 未測位の補完、`smooth_held()` で同値の連続を間引き、方位算出、ラップ開始時刻・エンジンON区間の抽出 → `Telemetry`
- `ecorun/config.py` : レイアウトプリセット(TOML)の pydantic モデル。**これが唯一の仕様**で、`schema/preset.schema.json`・GUI のフォーム・`ecorun-layout widgets`・API の OpenAPI はここから生成される。読込(tomllib)と保存(tomlkit でコメント・並びを保持)
- `ecorun/widgets.py` : ウィジェットの描画実装(`IMPLS` が type → クラス)。各ウィジェットは自分の box(x, y, w, h)の内側だけに描く
  - `static(img)` : パネル・目盛り・区間全体のグラフなど毎フレーム共通の部分。1 回だけ呼ばれる
  - `dynamic(img, ts)` : ログ秒 `ts` の値に依存する部分
  - 文字サイズ・余白は基準サイズ(モデルの `REF`)での値を書き、`self.S()` / `self.F()` / `self.T()` で拡縮する。ゲージは基準半径 180
- `ecorun/render.py` : `OverlayRenderer` が全ウィジェットの static を重ねた静的レイヤを作り、`render(ts)` でそのコピーに dynamic を順に描いて 1920×1080 RGBA を返す。`debug_boxes` で枠と id を描く
- `ecorun/video.py` : ffprobe、RGBA の raw フレームを stdin パイプで ffmpeg に渡す(`--video` ありなら overlay フィルタで合成して H.264、`.mov` なら ProRes 4444)、1 フレーム抽出
- `ecorun/cli.py` : `ecorun-overlay`(従来の CLI + `--preset` / `--debug-boxes`)
- `ecorun/layout.py` : `ecorun-layout`(list / widgets / schema / validate / lint)。lint のルールもここ
- `ecorun/gui/server.py` + `static/` : `ecorun-gui`。FastAPI + ビルド不要の素の JS。プレビューは `/api/render` で本番と同じ描画。`/api/events`(SSE)で presets/ の外部変更を通知
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

## レイアウトの方針(VSYSTO F9 ドラレコ、1920×1080、ローアングル前方カメラ)

- 地平線付近の帯(y≈380〜640)はコース前方・マーシャル・前走車が映るので、何も置かない → 各プリセットの `[[keepout]]` に入れてあり、`ecorun-layout lint` が重なりを警告する
- 映像の下半分はほぼ路面なので、ゲージ類はここに集める。標高グラフは左下、燃圧グラフは右下(回転計の隣)
- 右下の焼き込み表示(日時・機種名・速度)はおよそ x 1140〜1720、y 1000〜1040 の位置にある → `f9_mask` プリセットの `mask` ウィジェットで隠す
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

uv run pytest
```

描画を変更したら、必ずプレビュー画像を生成して目視で確認する。

## TODO・改善候補

- 描画の高速化(クラウド環境で約 13 fps、Apple Silicon Mac で約 50 fps)。候補: 動的要素の部分領域だけ再描画する、multiprocessing でフレームを並列生成する、pycairo へ移行する
- 同期の半自動化: 動画のオプティカルフロー、またはドラレコ焼き込み速度の OCR から発進点を検出し、ログと突き合わせる
- GUI: 配置禁止帯をキャンバス上でドラッグ編集できるようにする、複数ウィジェットの同時選択・整列
- 勾配の表示(標高はグラフ表示済み。GPS 標高は 0.1 m 刻みで揺れるので平滑化が必要)
- VSYSTO F9 はドライブレコーダーであることから 5分 ごとに動画が分割される。  
  一連の動画を選択し、まとめて処理できるようにする必要がある。
  