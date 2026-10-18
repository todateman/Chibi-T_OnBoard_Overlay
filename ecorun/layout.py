"""ecorun-layout: レイアウトプリセットを扱う非対話ツール(AI エージェント・CI 向け)

  ecorun-layout list [--json]                 プリセット一覧
  ecorun-layout widgets [TYPE] [--json]       ウィジェット型とパラメータ(既定値・範囲・説明)
  ecorun-layout schema [--write | --check]    JSON Schema を出力 / schema/preset.schema.json を更新・照合
  ecorun-layout validate PRESET... [--json]   スキーマ検証(未知キー・型・範囲・id 重複)
  ecorun-layout lint PRESET... [--json] [--strict]
                                              キャンバス外・配置禁止帯・ウィジェット同士の重なりを検査

PRESET はプリセット名(presets/NAME.toml)または .toml のパス。
終了コード: 0 = 問題なし(lint の warning は 0)、1 = エラー(--strict では warning も 1)、2 = 使い方の誤り。
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import (SCHEMA_PATH, WIDGET_BY_TYPE, Preset, PresetError, list_presets, load_preset,
                     parse_preset, preset_json_schema, resolve_preset_path)

COMMON_KEYS = ("id", "type", "enabled", "x", "y", "w", "h")


# =====================================================================
#  ウィジェット型の説明
# =====================================================================
def widget_types() -> list[dict]:
    """ウィジェット型ごとの説明・既定の配置・パラメータ一覧(GUI とエージェント向け)"""
    out = []
    for t, m in WIDGET_BY_TYPE.items():
        schema = m.model_json_schema()
        props = schema.get("properties", {})
        params = []
        for k, p in props.items():
            if k in ("type",):
                continue
            item = {"name": k, "common": k in COMMON_KEYS}
            for key in ("title", "description", "default", "minimum", "maximum",
                        "exclusiveMinimum", "exclusiveMaximum", "enum", "format", "pattern"):
                if key in p:
                    item[key] = p[key]
            if "type" in p:
                item["type"] = p["type"]
            if p.get("type") == "array" and "items" in p:
                it = p["items"]
                if "$ref" in it:
                    it = schema.get("$defs", {}).get(it["$ref"].split("/")[-1], it)
                item["items"] = {k2: v for k2, v in it.items() if k2 in ("type", "enum")}
            params.append(item)
        defaults = m()
        out.append({
            "type": t,
            "title": m.model_config.get("title", t),
            "description": (m.__doc__ or "").strip(),
            "default_box": {k: getattr(defaults, k) for k in ("x", "y", "w", "h")},
            "ref_size": list(m.REF) if m.REF else None,
            "params": params,
        })
    return out


# =====================================================================
#  lint
# =====================================================================
def _rect(o):
    return (o.x, o.y, o.x + o.w, o.y + o.h)


def _overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return (w, h) if w > 0 and h > 0 else None


def lint_preset(p: Preset) -> list[dict]:
    """レイアウトの問題を返す。level: error | warning | info"""
    issues = []
    W, H = p.canvas.width, p.canvas.height
    ws = [w for w in p.widgets if w.enabled]

    def add(level, code, ids, msg):
        issues.append({"level": level, "code": code, "widgets": ids, "msg": msg})

    for w in ws:
        x0, y0, x1, y1 = _rect(w)
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
            add("error", "out-of-canvas", [w.id],
                f"{w.id}: キャンバス {W}x{H} からはみ出しています (x={w.x}, y={w.y}, w={w.w}, h={w.h})")
        for k in p.keepout:
            ov = _overlap(_rect(w), _rect(k))
            if ov:
                add("warning", "keepout", [w.id],
                    f"{w.id}: 配置禁止帯「{k.name or '(無名)'}」と {ov[0]}x{ov[1]} px 重なっています")
        if w.type in ("speed_gauge", "rpm_gauge") and w.w != w.h:
            add("info", "gauge-aspect", [w.id],
                f"{w.id}: ゲージは box に内接する円で描くため、w と h が違うと余白ができます ({w.w}x{w.h})")
    for i, a in enumerate(ws):
        for b in ws[i + 1:]:
            if "mask" in (a.type, b.type):
                continue  # マスク帯はゲージなどの下に敷く前提
            ov = _overlap(_rect(a), _rect(b))
            if ov:
                add("warning", "overlap", [a.id, b.id], f"{a.id} と {b.id} が {ov[0]}x{ov[1]} px 重なっています")
    if any(w.type == "odometer" for w in ws) and any(w.type == "fuel_eco" and w.show_odometer for w in ws):
        add("info", "duplicate-odometer", [w.id for w in ws if w.type in ("odometer", "fuel_eco")],
            "距離・燃料が odometer と fuel_eco(show_odometer=true)の両方に表示されます")
    return issues


# =====================================================================
#  CLI
# =====================================================================
def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _check_one(name: str):
    """→ (path, Preset | None, errors)"""
    path = resolve_preset_path(name)
    try:
        return path, load_preset(name), []
    except PresetError as e:
        return path, None, e.errors or [{"loc": "", "msg": e.args[0]}]


def cmd_list(args):
    items = list_presets()
    if args.json:
        _print_json(items)
        return 0
    for it in items:
        mark = "" if it["valid"] else "  [不正: ecorun-layout validate で確認]"
        print(f"{it['name']:<16} {it['title']}  — {it['description']}{mark}")
    return 0


def cmd_widgets(args):
    items = widget_types()
    if args.type:
        items = [i for i in items if i["type"] == args.type]
        if not items:
            print(f"不明なウィジェット型: {args.type}(候補: {', '.join(WIDGET_BY_TYPE)})", file=sys.stderr)
            return 2
    if args.json:
        _print_json(items)
        return 0
    for it in items:
        b = it["default_box"]
        print(f"[{it['type']}] {it['title']} — {it['description']}")
        print(f"    既定の配置: x={b['x']} y={b['y']} w={b['w']} h={b['h']}"
              + (f"  基準サイズ {it['ref_size'][0]}x{it['ref_size'][1]}(文字はこの比で拡縮)" if it["ref_size"] else ""))
        for p in it["params"]:
            if p["common"]:
                continue
            rng = []
            for k, sym in (("minimum", ">="), ("exclusiveMinimum", ">"), ("maximum", "<="), ("exclusiveMaximum", "<")):
                if k in p:
                    rng.append(f"{sym}{p[k]}")
            enum = p.get("enum") or p.get("items", {}).get("enum")
            extra = (f" 範囲 {' '.join(rng)}" if rng else "") + (f" 選択肢 {enum}" if enum else "")
            desc = f" — {p['description']}" if p.get("description") else ""
            print(f"    {p['name']} = {json.dumps(p.get('default'), ensure_ascii=False)}  {p.get('title', '')}{extra}{desc}")
        print()
    print(f"共通キー: {', '.join(COMMON_KEYS)}(座標はキャンバス基準 px、配列の順 = 重なり順)")
    return 0


def cmd_schema(args):
    text = json.dumps(preset_json_schema(), ensure_ascii=False, indent=2) + "\n"
    if args.write:
        SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {SCHEMA_PATH}")
    elif args.check:
        cur = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if cur != text:
            print(f"{SCHEMA_PATH} が古くなっています。ecorun-layout schema --write で更新してください", file=sys.stderr)
            return 1
        print("schema is up to date")
    else:
        sys.stdout.write(text)
    return 0


def cmd_validate(args):
    results, rc = [], 0
    for name in args.presets:
        path, p, errors = _check_one(name)
        results.append({"preset": str(path), "valid": p is not None, "errors": errors})
        rc |= p is None
    if args.json:
        _print_json(results)
    else:
        for r in results:
            print(f"{'OK ' if r['valid'] else 'NG '} {r['preset']}")
            for e in r["errors"]:
                print(f"    {e['loc'] or '(root)'}: {e['msg']}")
    return int(rc)


def cmd_lint(args):
    results, rc = [], 0
    for name in args.presets:
        path, p, errors = _check_one(name)
        issues = lint_preset(p) if p else []
        bad = bool(errors) or any(i["level"] == "error" for i in issues) \
            or (args.strict and any(i["level"] == "warning" for i in issues))
        rc |= bad
        results.append({"preset": str(path), "valid": p is not None, "errors": errors, "issues": issues})
    if args.json:
        _print_json(results)
    else:
        for r in results:
            print(r["preset"])
            for e in r["errors"]:
                print(f"    error   {e['loc'] or '(root)'}: {e['msg']}")
            for i in r["issues"]:
                print(f"    {i['level']:<7} [{i['code']}] {i['msg']}")
            if not r["errors"] and not r["issues"]:
                print("    問題なし")
    return int(rc)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ecorun-layout", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list", help="プリセット一覧")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("widgets", help="ウィジェット型とパラメータ")
    s.add_argument("type", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_widgets)
    s = sub.add_parser("schema", help="JSON Schema")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--write", action="store_true", help="schema/preset.schema.json を更新")
    g.add_argument("--check", action="store_true", help="schema/preset.schema.json が最新か確認")
    s.set_defaults(fn=cmd_schema)
    s = sub.add_parser("validate", help="スキーマ検証")
    s.add_argument("presets", nargs="+")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("lint", help="レイアウト検査")
    s.add_argument("presets", nargs="+")
    s.add_argument("--json", action="store_true")
    s.add_argument("--strict", action="store_true", help="warning でも終了コード 1")
    s.set_defaults(fn=cmd_lint)
    args = ap.parse_args(argv)
    sys.exit(args.fn(args))


__all__ = ["lint_preset", "widget_types", "parse_preset", "main"]

if __name__ == "__main__":
    main()
