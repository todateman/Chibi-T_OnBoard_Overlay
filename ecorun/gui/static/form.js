// JSON Schema のプロパティ(または /api/widget-types の params)から入力欄を作る

export function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) e.append(c);
  return e;
}

const clampHex = (s) => /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(s);

function colorControl(value, def, onChange) {
  const hasAlpha = (value || def || "").length === 9;
  const rgb = el("input", { type: "color", value: (value || "#000000").slice(0, 7) });
  const alpha = hasAlpha ? el("input", { type: "range", min: 0, max: 255, value: parseInt((value || "#000000ff").slice(7, 9), 16), title: "不透明度" }) : null;
  const hex = el("input", { value: value || "", class: "mono hex" });
  const compose = () => rgb.value.toUpperCase() + (alpha ? Number(alpha.value).toString(16).padStart(2, "0").toUpperCase() : "");
  rgb.addEventListener("change", () => { hex.value = compose(); onChange(hex.value); });
  rgb.addEventListener("input", () => { hex.value = compose(); });
  alpha?.addEventListener("change", () => { hex.value = compose(); onChange(hex.value); });
  alpha?.addEventListener("input", () => { hex.value = compose(); });
  hex.addEventListener("change", () => {
    const v = hex.value.trim();
    if (!clampHex(v)) { hex.parentElement.parentElement.classList.add("invalid"); return; }
    hex.parentElement.parentElement.classList.remove("invalid");
    rgb.value = v.slice(0, 7);
    if (alpha && v.length === 9) alpha.value = parseInt(v.slice(7, 9), 16);
    onChange(v.toUpperCase());
  });
  return [rgb, alpha, hex];
}

/**
 * p: { name, title, description, type, default, minimum, maximum, exclusiveMinimum, enum, format, items }
 * value: 現在の値 / onChange(newValue)
 */
export function field(p, value, onChange) {
  const id = `f_${p.name}_${Math.random().toString(36).slice(2, 7)}`;
  let ctl;
  const t = p.type;
  if (t === "boolean") {
    ctl = [el("input", { type: "checkbox", id, checked: !!value, onchange: (e) => onChange(e.target.checked) })];
  } else if (t === "integer" || t === "number") {
    const min = p.minimum ?? p.exclusiveMinimum;
    const inp = el("input", {
      type: "number", id, value: value ?? "", step: t === "integer" ? 1 : "any",
      min, max: p.maximum ?? p.exclusiveMaximum,
    });
    inp.addEventListener("change", () => {
      if (inp.value === "") return;
      const v = t === "integer" ? Math.round(Number(inp.value)) : Number(inp.value);
      onChange(v);
    });
    ctl = [inp];
  } else if (t === "string" && p.format === "color") {
    ctl = colorControl(value, p.default, onChange);
  } else if (p.enum) {
    ctl = [el("select", { id, onchange: (e) => onChange(e.target.value) },
      p.enum.map((v) => el("option", { value: v, selected: v === value }, v)))];
  } else if (t === "array") {
    const opts = p.items?.enum;
    const inp = el("input", { id, value: (value || []).join(", "), class: "mono" });
    inp.addEventListener("change", () => {
      const v = inp.value.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean);
      if (opts && v.some((x) => !opts.includes(x))) { inp.parentElement.parentElement.classList.add("invalid"); return; }
      inp.parentElement.parentElement.classList.remove("invalid");
      onChange(v);
    });
    ctl = [inp];
  } else {
    const inp = el("input", { id, value: value ?? "" });
    inp.addEventListener("change", () => onChange(inp.value));
    ctl = [inp];
  }
  const help = [p.description, p.items?.enum ? `選択肢: ${p.items.enum.join(", ")}` : null].filter(Boolean).join(" / ");
  return el("div", { class: "field", title: `${p.name}${p.description ? " — " + p.description : ""}` },
    el("label", { for: id }, p.title || p.name),
    el("div", { class: "ctl" }, ctl.filter(Boolean)),
    help ? el("div", { class: "help" }, help) : null);
}

/** JSON Schema の $defs[name].properties を params 形式の配列にする */
export function schemaParams(schema, defName) {
  const props = schema.$defs?.[defName]?.properties || {};
  return Object.entries(props).map(([name, p]) => ({ name, ...p }));
}
