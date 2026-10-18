"""レイアウトプリセット(TOML)の定義・読込・保存

ここの pydantic モデルが唯一の仕様。JSON Schema(schema/preset.schema.json)、GUI のプロパティ
フォーム、`ecorun-layout widgets` の一覧、GUI サーバーの OpenAPI はすべてここから生成する。
フィールドを足すときは title / description / 範囲を必ず付ける(エージェントと GUI がそれを読む)。
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Union

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SCHEMA_VERSION = 1
ROOT_DIR = Path(__file__).resolve().parent.parent
PRESET_DIR = Path(os.environ.get("ECORUN_PRESET_DIR", ROOT_DIR / "presets"))
SCHEMA_PATH = ROOT_DIR / "schema" / "preset.schema.json"
SCHEMA_DIRECTIVE = "#:schema ../schema/preset.schema.json"
DEFAULT_PRESET = "default"
PRESET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

Color = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$",
                             json_schema_extra={"format": "color"})]


class Strict(BaseModel):
    # 未知のキーはエラーにする(エージェントや手編集のタイポを検出するため)
    model_config = ConfigDict(extra="forbid")


# =====================================================================
#  全体設定
# =====================================================================
class Canvas(Strict):
    width: int = Field(1920, ge=320, le=7680, title="幅", description="レイアウト基準の幅 [px]。出力時に動画解像度へスケールされる")
    height: int = Field(1080, ge=240, le=4320, title="高さ", description="レイアウト基準の高さ [px]")


class Theme(Strict):
    accent: Color = Field("#FF8C00", title="強調色", description="エンジンON・燃費の数値・方位の主要点")
    speed: Color = Field("#00C8FF", title="速度の色", description="速度ゲージと速度グラフ")
    text: Color = Field("#FFFFFF", title="文字色", description="数値など主要な文字")
    sub: Color = Field("#BEC4CD", title="補助文字色", description="ラベル・目盛り")
    alt: Color = Field("#8CDC6E", title="標高グラフの色")
    fuel_p: Color = Field("#EB6EC8", title="燃圧グラフの色")
    panel: Color = Field("#0A0E14AA", title="パネル背景", description="#RRGGBBAA の AA が不透明度")
    grid: Color = Field("#FFFFFF28", title="グラフの目盛り線")
    marker: Color = Field("#FF2828", title="現在地・方位マーカー")
    font: str = Field("", title="フォント", description="日本語フォント (.ttf/.ttc) のパス。空なら自動検出。CLI の --font が優先")


class DataParams(Strict):
    rpm_on_threshold: float = Field(300, ge=0, title="ENGINE ON 判定 [rpm]", description="回転数がこれを超えたらエンジンONとみなす")
    fuel_eco_min_dist_m: float = Field(300, ge=0, title="燃費表示開始距離 [m]", description="これより短い距離では燃費を --- 表示(走り出しの外れ値対策)")
    heading_lookback_s: float = Field(3.0, gt=0, le=30, title="方位の参照秒数 [s]", description="方位をこの秒数だけ前の位置からのベクトルで求める")


class KeepOut(Strict):
    name: str = Field("", title="名前")
    x: int = Field(0, title="X")
    y: int = Field(0, title="Y")
    w: int = Field(1, ge=1, title="幅")
    h: int = Field(1, ge=1, title="高さ")


# =====================================================================
#  ウィジェット
# =====================================================================
def _x(v): return Field(v, title="X", description="左上の X 座標 [px](キャンバス基準)")
def _y(v): return Field(v, title="Y", description="左上の Y 座標 [px](キャンバス基準)")
def _w(v): return Field(v, ge=8, title="幅", description="[px]")
def _h(v): return Field(v, ge=8, title="高さ", description="[px]")


class WidgetBase(Strict):
    # 文字サイズや余白はこの基準サイズのときの値。scale = min(w / REF[0], h / REF[1]) で拡縮する
    # None なら拡縮しない
    REF: ClassVar[tuple[int, int] | None] = None
    id: str = Field("", pattern=r"^[A-Za-z0-9_-]*$", title="ID",
                    description="プリセット内で一意。省略すると type から自動で付ける")
    enabled: bool = Field(True, title="表示", description="false にすると描画しない(設定は残る)")


class TimerWidget(WidgetBase):
    """走行時間・現在ラップのタイム・前周タイム"""
    model_config = ConfigDict(title="走行時間・ラップ")
    REF: ClassVar = (530, 100)
    type: Literal["timer"] = "timer"
    x: int = _x(30); y: int = _y(30); w: int = _w(530); h: int = _h(100)
    lap_total: int = Field(0, ge=0, title="総周回数", description="1 以上で LAP 2/3 のように表示。0 なら LAP 2")


class SpeedGraphWidget(WidgetBase):
    """出力区間全体の速度グラフ。エンジンON区間を帯で表示し、現在位置にカーソル"""
    model_config = ConfigDict(title="速度グラフ")
    REF: ClassVar = (530, 210)
    type: Literal["speed_graph"] = "speed_graph"
    x: int = _x(30); y: int = _y(150); w: int = _w(530); h: int = _h(210)
    title: str = Field("スピード / エンジンON", title="タイトル")
    max: int = Field(60, ge=1, title="縦軸上限 [km/h]")
    grid_step: int = Field(20, ge=1, title="目盛り間隔 [km/h]")
    show_on_bands: bool = Field(True, title="エンジンON帯を表示")


EnvItem = Literal["eng_temp", "air_temp", "hum", "press"]


class EnvWidget(WidgetBase):
    """エンジン温度・気温・湿度・気圧を 1 行で表示"""
    model_config = ConfigDict(title="環境")
    REF: ClassVar = (680, 44)
    type: Literal["env"] = "env"
    x: int = _x(620); y: int = _y(146); w: int = _w(680); h: int = _h(44)
    items: list[EnvItem] = Field(["eng_temp", "air_temp", "hum", "press"], title="表示項目",
                                 description="左からの表示順。eng_temp=エンジン温度, air_temp=気温, hum=湿度, press=気圧")


class FuelEcoWidget(WidgetBase):
    """燃費(km/L)。走り出しの短い距離では --- 表示"""
    model_config = ConfigDict(title="燃費")
    REF: ClassVar = (340, 160)
    type: Literal["fuel_eco"] = "fuel_eco"
    x: int = _x(780); y: int = _y(900); w: int = _w(340); h: int = _h(160)
    show_odometer: bool = Field(True, title="距離・燃料も表示", description="パネル左下に距離と積算燃料を出す。odometer ウィジェットを別に置くなら false")


class OdometerWidget(WidgetBase):
    """走行距離と積算燃料の 2 行表示"""
    model_config = ConfigDict(title="距離・燃料")
    REF: ClassVar = (220, 60)
    type: Literal["odometer"] = "odometer"
    x: int = _x(1532); y: int = _y(992); w: int = _w(220); h: int = _h(60)
    panel: bool = Field(False, title="背景パネル")
    stroke: int = Field(2, ge=0, le=6, title="文字の縁取り [px]", description="暗い背景(mask など)の上なら 0")


class MaskWidget(WidgetBase):
    """不透明な帯。ドラレコの焼き込み表示(日時・速度)を隠す"""
    model_config = ConfigDict(title="マスク帯")
    type: Literal["mask"] = "mask"
    x: int = _x(1128); y: int = _y(992); w: int = _w(632); h: int = _h(60)
    color: Color = Field("#0A0E14FF", title="色")
    radius: int = Field(12, ge=0, title="角の丸み [px]")


class MapWidget(WidgetBase):
    """GPS 軌跡のコース図と現在地。エンジンON区間をオレンジで重ねる"""
    model_config = ConfigDict(title="コース図")
    REF: ClassVar = (350, 340)
    type: Literal["map"] = "map"
    x: int = _x(1540); y: int = _y(30); w: int = _w(350); h: int = _h(340)
    title: str = Field("COURSE", title="タイトル")
    show_on_segments: bool = Field(True, title="エンジンON区間を表示")
    show_legend: bool = Field(True, title="凡例を表示")


class SpeedGaugeWidget(WidgetBase):
    """速度の円形ゲージ。円は box に内接する"""
    model_config = ConfigDict(title="速度ゲージ")
    type: Literal["speed_gauge"] = "speed_gauge"
    x: int = _x(380); y: int = _y(700); w: int = _w(360); h: int = _h(360)
    max: int = Field(60, ge=1, title="上限 [km/h]")
    step: int = Field(10, ge=1, title="目盛り間隔 [km/h]")


class RpmGaugeWidget(WidgetBase):
    """回転数の円形ゲージと ENGINE ON / COAST バッジ"""
    model_config = ConfigDict(title="回転ゲージ")
    type: Literal["rpm_gauge"] = "rpm_gauge"
    x: int = _x(1160); y: int = _y(700); w: int = _w(360); h: int = _h(360)
    max: int = Field(6000, ge=1, title="上限 [rpm]")
    step: int = Field(1000, ge=1, title="目盛り間隔 [rpm]")
    show_badge: bool = Field(True, title="ENGINE ON / COAST バッジ")


class AltGraphWidget(WidgetBase):
    """出力区間全体の標高プロファイル(GPS 測位中のみ)と現在位置"""
    model_config = ConfigDict(title="標高グラフ")
    REF: ClassVar = (330, 210)
    type: Literal["alt_graph"] = "alt_graph"
    x: int = _x(30); y: int = _y(770); w: int = _w(330); h: int = _h(210)
    title: str = Field("標高", title="タイトル")
    min_span_m: float = Field(5.0, gt=0, title="縦軸の最小幅 [m]", description="GPS 標高の小さな揺れを山に見せないための下限")


class FuelPressureGraphWidget(WidgetBase):
    """直近の燃圧をスクロール表示。燃圧列の無いログでは表示しない"""
    model_config = ConfigDict(title="燃圧グラフ")
    REF: ClassVar = (330, 210)
    type: Literal["fuel_p_graph"] = "fuel_p_graph"
    x: int = _x(1560); y: int = _y(770); w: int = _w(330); h: int = _h(210)
    title: str = Field("燃圧", title="タイトル")
    max: float = Field(0.4, gt=0, title="縦軸上限 [MPa]")
    grid_step: float = Field(0.1, gt=0, title="目盛り間隔 [MPa]")
    window_s: float = Field(60, gt=0, title="表示する秒数", description="右端を現在として直近この秒数を描く")


class HeadingWidget(WidgetBase):
    """方位テープと方位角(GPS の移動方向から算出)"""
    model_config = ConfigDict(title="方位")
    REF: ClassVar = (680, 110)
    type: Literal["heading"] = "heading"
    x: int = _x(620); y: int = _y(30); w: int = _w(680); h: int = _h(110)
    span_deg: float = Field(70, gt=5, le=180, title="表示範囲 [°]", description="中心から左右それぞれに表示する角度")


class TextWidget(WidgetBase):
    """任意の固定テキスト(チーム名・大会名など)"""
    model_config = ConfigDict(title="テキスト")
    type: Literal["text"] = "text"
    x: int = _x(30); y: int = _y(1000); w: int = _w(400); h: int = _h(50)
    text: str = Field("ちびTインターナショナル", title="テキスト")
    size: int = Field(28, ge=6, le=300, title="文字サイズ [px]")
    color: Color = Field("#FFFFFF", title="色")
    align: Literal["left", "center", "right"] = Field("left", title="揃え")
    panel: bool = Field(False, title="背景パネル")
    stroke: int = Field(2, ge=0, le=6, title="文字の縁取り [px]")


WIDGET_MODELS: tuple[type[WidgetBase], ...] = (
    TimerWidget, SpeedGraphWidget, EnvWidget, FuelEcoWidget, OdometerWidget, MaskWidget, MapWidget,
    SpeedGaugeWidget, RpmGaugeWidget, AltGraphWidget, FuelPressureGraphWidget, HeadingWidget, TextWidget,
)
WIDGET_BY_TYPE: dict[str, type[WidgetBase]] = {m.model_fields["type"].default: m for m in WIDGET_MODELS}

Widget = Annotated[Union[tuple(WIDGET_MODELS)], Field(discriminator="type")]


class Preset(Strict):
    model_config = ConfigDict(title="ecorun_overlay レイアウトプリセット")
    schema_version: Literal[1] = Field(1, title="スキーマのバージョン")
    name: str = Field("", title="表示名")
    description: str = Field("", title="説明")
    canvas: Canvas = Field(default_factory=Canvas, title="キャンバス")
    theme: Theme = Field(default_factory=Theme, title="配色・フォント")
    data: DataParams = Field(default_factory=DataParams, title="判定の閾値")
    keepout: list[KeepOut] = Field(default_factory=list, title="配置禁止帯",
                                   description="ここにウィジェットが重なると lint が警告する(地平線付近など映像で見せたい領域)")
    widgets: list[Widget] = Field(default_factory=list, title="ウィジェット",
                                  description="配列の順番が重なり順(後ろほど上)。静的部分を全ウィジェット分描いてから動的部分を描く")

    @model_validator(mode="before")
    @classmethod
    def _fill_ids(cls, data):
        """id を省略したウィジェットに type から一意な id を付ける"""
        if isinstance(data, dict) and isinstance(data.get("widgets"), list):
            ws = [dict(w) if isinstance(w, dict) else w for w in data["widgets"]]
            used = {w.get("id") for w in ws if isinstance(w, dict) and w.get("id")}
            for w in ws:
                if isinstance(w, dict) and not w.get("id") and isinstance(w.get("type"), str):
                    w["id"] = unique_id(w["type"], used)
                    used.add(w["id"])
            data = {**data, "widgets": ws}
        return data

    @model_validator(mode="after")
    def _check_ids(self):
        seen = set()
        for w in self.widgets:
            if w.id in seen:
                raise ValueError(f"ウィジェットの id '{w.id}' が重複しています")
            seen.add(w.id)
        return self


def unique_id(base: str, used) -> str:
    if base not in used:
        return base
    k = 2
    while f"{base}_{k}" in used:
        k += 1
    return f"{base}_{k}"


def widget_ref(w: WidgetBase) -> tuple[int, int] | None:
    return type(w).REF


# =====================================================================
#  読み書き
# =====================================================================
class PresetError(Exception):
    """プリセットの読み込み・検証エラー。errors は [{"loc": "widgets[2].max", "msg": ...}]"""

    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []

    def __str__(self):
        if not self.errors:
            return self.args[0]
        lines = [self.args[0]] + [f"  {e['loc'] or '(root)'}: {e['msg']}" for e in self.errors]
        return "\n".join(lines)


def format_loc(loc: tuple) -> str:
    """('widgets', 3, 'speed_gauge', 'max') → 'widgets[3].max'(判別用の型名は省く)"""
    out = ""
    for i, part in enumerate(loc):
        if isinstance(part, int):
            out += f"[{part}]"
        elif i > 0 and isinstance(loc[i - 1], int) and part in WIDGET_BY_TYPE and loc[0] == "widgets":
            continue
        else:
            out += f".{part}" if out else str(part)
    return out


def validation_errors(e: ValidationError) -> list[dict]:
    return [{"loc": format_loc(err["loc"]), "msg": err["msg"]} for err in e.errors()]


def parse_preset(data: dict, source: str = "preset") -> Preset:
    try:
        return Preset.model_validate(data)
    except ValidationError as e:
        raise PresetError(f"{source}: プリセットの内容が不正です", validation_errors(e)) from None


def resolve_preset_path(name_or_path: str) -> Path:
    """'default' → presets/default.toml。パス(/ を含む・.toml で終わる)ならそのまま"""
    p = Path(name_or_path)
    if name_or_path.endswith(".toml") or os.sep in name_or_path or "/" in name_or_path:
        return p
    return PRESET_DIR / f"{name_or_path}.toml"


def read_preset_file(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PresetError(f"プリセットが見つかりません: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise PresetError(f"{path}: TOML の構文エラー: {e}") from None


def load_preset(name_or_path: str) -> Preset:
    path = resolve_preset_path(name_or_path)
    return parse_preset(read_preset_file(path), str(path))


def list_presets() -> list[dict]:
    out = []
    for p in sorted(PRESET_DIR.glob("*.toml")):
        item = {"name": p.stem, "path": str(p), "title": "", "description": "", "valid": True}
        try:
            data = read_preset_file(p)
            item["title"] = str(data.get("name", ""))
            item["description"] = str(data.get("description", ""))
            parse_preset(data, str(p))
        except PresetError:
            item["valid"] = False
        out.append(item)
    return out


def preset_to_dict(p: Preset) -> dict:
    """TOML / JSON 出力用。ウィジェットは id, type, enabled, x, y, w, h を先頭に並べる"""
    d = p.model_dump(mode="json")
    head = ("id", "type", "enabled", "x", "y", "w", "h")
    d["widgets"] = [{**{k: w[k] for k in head}, **{k: v for k, v in w.items() if k not in head}}
                    for w in d["widgets"]]
    return d


def _sync_table(container, data: dict):
    """tomlkit のテーブルを data に合わせて書き換える。変わっていない値とコメントはそのまま残す"""
    for k in [k for k in container if k not in data]:
        del container[k]
    for k, v in data.items():
        cur = container.get(k)
        if isinstance(v, dict):
            if isinstance(cur, dict):
                _sync_table(cur, v)
            else:
                t = tomlkit.table()
                _sync_table(t, v)
                container[k] = t
        elif isinstance(v, list) and v and all(isinstance(e, dict) for e in v):
            old = list(cur) if isinstance(cur, list) else []
            if len(old) == len(v) and all(isinstance(o, dict) and o.get("id") == e.get("id")
                                          for o, e in zip(old, v)):
                for o, e in zip(old, v):    # 並びが同じなら書式を崩さないよう、その場で更新
                    _sync_table(o, e)
                continue
            by_id = {e.get("id"): e for e in old if isinstance(e, dict) and e.get("id")}
            aot = tomlkit.aot()
            for i, e in enumerate(v):
                t = by_id.get(e.get("id")) if e.get("id") else (old[i] if i < len(old) else None)
                if not isinstance(t, dict):
                    t = tomlkit.table()
                _sync_table(t, e)
                aot.append(t)
            container[k] = aot
        elif isinstance(v, list) and not v and k in ("widgets", "keepout"):
            if k in container:
                del container[k]
        else:
            if cur is None or _plain(cur) != v or type(_plain(cur)) is not type(v):
                container[k] = v


def _plain(v):
    return v.unwrap() if hasattr(v, "unwrap") else v


def dump_preset(p: Preset, original: str | None = None) -> str:
    """Preset → TOML 文字列。original(既存ファイルの内容)があればコメントとキー順を保つ"""
    data = preset_to_dict(p)
    if original:
        doc = tomlkit.parse(original)
        _sync_table(doc, data)
        # 配列の並べ替え・追加・削除をすると tomlkit が空行を足すので詰める
        return re.sub(r"\n{3,}", "\n\n", tomlkit.dumps(doc))
    doc = tomlkit.document()
    _sync_table(doc, data)
    return SCHEMA_DIRECTIVE + "\n" + tomlkit.dumps(doc)


def save_preset(path: Path, p: Preset) -> None:
    original = path.read_text(encoding="utf-8") if path.exists() else None
    text = dump_preset(p, original)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def preset_json_schema() -> dict:
    s = Preset.model_json_schema()
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return s
