"""脚本工具：search_materials / estimate_duration / check_rules / save_script。

- estimate_duration：按旁白字数与读速（4.5 字/秒）逐分镜计算，最短 2 秒；
  返回每分镜时长、总时长、目标偏差、平台上下限是否满足。模型不能自行声明时长。
- check_rules：品牌规则检查（极限词、禁用素材、促销免责声明、分镜引用与字幕要求），
  外加“事实追溯”检查：脚本中的每个数字必须出现在所引用素材的事实（facts）中；
  每个引用素材之外的事实表述无法被机械识别，但数字与禁用词可完全拦截。
- save_script：保存前再次运行估算与规则检查，双检通过才落库（幂等）。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

READ_SPEED = 4.5          # 字/秒
MIN_SHOT_SECONDS = 2.0


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_context(data_dir: str = "data") -> dict:
    return {
        "materials": {m["id"]: m for m in load_json(os.path.join(data_dir, "materials.json"))},
        "rules": load_json(os.path.join(data_dir, "brand_rules.json")),
        "scripts": {},              # request_id -> saved script（幂等）
        "estimates": [],            # 估算历史
        "checks": [],               # 规则检查历史
        "faults": {},
        "seq": 0,
    }


def _num(value) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def validate_script_shape(script) -> str | None:
    if not isinstance(script, dict):
        return "script 必须是 JSON 对象"
    if not isinstance(script.get("title"), str) or not script["title"].strip():
        return "script.title 不能为空"
    shots = script.get("shots")
    if not isinstance(shots, list) or not 1 <= len(shots) <= 12:
        return "script.shots 必须是 1~12 个分镜的数组"
    for i, sh in enumerate(shots, 1):
        if not isinstance(sh, dict):
            return f"第 {i} 个分镜必须是对象"
        if not isinstance(sh.get("narration"), str) or not sh["narration"].strip():
            return f"第 {i} 个分镜缺少 narration"
        if not isinstance(sh.get("subtitle"), str) or not sh["subtitle"].strip():
            return f"第 {i} 个分镜缺少 subtitle"
        mats = sh.get("materials")
        if not isinstance(mats, list) or not mats or not all(isinstance(m, str) for m in mats):
            return f"第 {i} 个分镜必须引用至少一个素材 ID（materials 数组）"
        if not isinstance(sh.get("visual"), str):
            return f"第 {i} 个分镜的 visual 必须是字符串"
    return None


def compute_duration(ctx: dict, script: dict, task: dict) -> dict:
    rules = ctx["rules"]
    shots = []
    total = 0.0
    for i, sh in enumerate(script["shots"], 1):
        chars = len(re.sub(r"\s+", "", sh.get("narration", "")))
        secs = max(MIN_SHOT_SECONDS, round(chars / READ_SPEED, 1))
        total += secs
        shots.append({"idx": i, "chars": chars, "seconds": secs,
                      "materials": sh.get("materials", []),
                      "subtitle_chars": len(re.sub(r"\s+", "", sh.get("subtitle", "")))})
    total = round(total, 1)
    target = task.get("target_seconds")
    platform = task.get("platform")
    tol = rules.get("duration_tolerance", 0.1)
    pmin = rules["platforms"].get(platform, {}).get("min_seconds")
    pmax = rules["platforms"].get(platform, {}).get("max_seconds")
    within_target = None
    if _num(target):
        target = float(target)
        within_target = abs(total - target) <= round(target * tol, 1) + 1e-9
    within_platform = None
    if pmin is not None and pmax is not None and _num(target) is not None:
        within_platform = pmin <= total <= pmax
    return {"shots": shots, "total_seconds": total,
            "target_seconds": target, "tolerance": tol,
            "within_target": within_target, "platform": platform,
            "platform_range": [pmin, pmax] if pmin is not None else None,
            "within_platform": within_platform,
            "ok": bool(within_target and within_platform)}


def collect_cited(script: dict) -> list[str]:
    out = []
    for sh in script.get("shots", []):
        for m in sh.get("materials", []) or []:
            if m not in out:
                out.append(m)
    return out


def run_rule_check(ctx: dict, script: dict, task: dict) -> dict:
    rules = ctx["rules"]
    materials = ctx["materials"]

    def number_scope_pre(s):
        return s.get("title", "") + " " + " ".join(
            sh.get("narration", "") + " " + sh.get("subtitle", "") for sh in s.get("shots", []))

    def all_facts_for_cited():
        texts = []
        for sh in script.get("shots", []):
            for m in sh.get("materials", []) or []:
                if m in materials:
                    texts.extend(materials[m].get("facts", []))
        return " ".join(texts)
    errors, warnings = [], []
    cited = collect_cited(script)

    # 1) 素材存在性
    unknown = [m for m in cited if m not in materials]
    if unknown:
        errors.append(f"引用了不存在的素材：{', '.join(unknown)}")

    # 2) 禁用素材
    blocked = [m for m in cited if m in rules.get("blocked_materials", {})]
    for m in blocked:
        errors.append(f"引用了禁用素材 {m}：{rules['blocked_materials'][m]}")

    # 3) 极限词
    text_all = script.get("title", "") + " " + " ".join(
        (sh.get("narration", "") + " " + sh.get("subtitle", "") + " " + sh.get("visual", ""))
        for sh in script.get("shots", []))
    text_all += " " + (script.get("disclaimer") or "")
    for term in rules.get("forbidden_terms", []):
        if term in text_all:
            errors.append(f"出现违禁表述「{term}」")

    # 4) 促销免责声明
    for m in cited:
        req = rules.get("disclaimer_requirements", {}).get(m)
        if req and req["must_include"] not in (script.get("disclaimer") or ""):
            errors.append(f"使用素材 {m} 必须包含免责声明「{req['must_include']}」")

    # 5) 分镜级引用与字幕
    for i, sh in enumerate(script.get("shots", []), 1):
        if not sh.get("materials"):
            errors.append(f"分镜 {i} 未引用任何素材")
        if not (sh.get("subtitle") or "").strip():
            errors.append(f"分镜 {i} 缺少字幕")

    # 6) 产品属性词追溯：属性词必须出现在所引用素材事实中（防非数字类编造）
    attribute_terms = rules.get("product_attribute_terms", [])
    for term in attribute_terms:
        if term in number_scope_pre(script) and term not in all_facts_for_cited():
            errors.append(f"产品属性「{term}」无法在所引用素材的事实中找到（疑似编造产品事实）")

    # 7) 事实追溯：脚本中的每个数字必须出现在所引用素材的事实中
    material_facts = {}
    for m in cited:
        if m in materials:
            material_facts[m] = " ".join(materials[m]["facts"])
    all_facts = " ".join(material_facts.values())
    number_scope = script.get("title", "") + " " + " ".join(
        sh.get("narration", "") + " " + sh.get("subtitle", "") for sh in script.get("shots", []))
    script_numbers = set(re.findall(r"\d+(?:\.\d+)?%?", number_scope))
    fact_numbers = set(re.findall(r"\d+(?:\.\d+)?%?", all_facts))
    fabricated = sorted(n for n in script_numbers if n not in fact_numbers)
    for n in fabricated:
        errors.append(f"数字 {n} 无法在所引用素材的事实中找到（疑似编造产品事实/效果数据）")

    # 8) 引用素材但事实为空（P06 类）不能贡献任何信息
    cites_visual = [m for m in cited if m in materials and not materials[m]["facts"]
                    and m != "P03"]  # P03 是场景素材，事实为场景描述
    for m in cites_visual:
        warnings.append(f"素材 {m} 仅含画面（事实为空），不能支撑任何文字表述")

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "cited_materials": cited,
            "checked_numbers": sorted(script_numbers),
            "fabricated_numbers": fabricated}


# ---------------------------------------------------------------- 工具
def build_search_materials(ctx: dict, scope):
    def search_materials(query: str) -> str:
        fault = ctx["faults"].get("search_materials")
        if fault and fault.get("remaining", 0) > 0:
            fault["remaining"] -= 1
            return json.dumps({"error": fault.get("error", "素材检索服务不可用")}, ensure_ascii=False)
        q = (query or "").strip()
        if not q:
            return json.dumps({"error": "检索关键词为空"}, ensure_ascii=False)
        docs = ctx["materials"] if not scope else {k: v for k, v in ctx["materials"].items() if k in scope}
        def grams(text):
            out = set()
            for tok in re.findall(r"[一-龥]+|[A-Za-z0-9]+", text):
                if re.match(r"[A-Za-z0-9]+", tok):
                    out.add(tok.lower())
                else:
                    for i in range(len(tok) - 1):
                        out.add(tok[i:i + 2])
            return out

        q_grams = grams(q)
        hits = []
        for m in sorted(docs.values(), key=lambda d: d["id"]):
            hay = m["title"] + " ".join(m["tags"]) + " ".join(m["facts"]) + m["type"]
            score = len(q_grams & grams(hay)) / max(len(q_grams), 1)
            tag_hit = any(tag in q for tag in m["tags"])
            if score >= 0.25 or tag_hit or q in hay:
                hits.append({"id": m["id"], "title": m["title"], "type": m["type"],
                             "tags": m["tags"], "facts": m["facts"],
                             "score": round(score, 3)})
        return json.dumps({"query": q, "count": len(hits), "hits": hits}, ensure_ascii=False)
    return search_materials


def build_estimate_duration(ctx: dict, task: dict):
    def estimate_duration(script) -> str:
        fault = ctx["faults"].get("estimate_duration")
        if fault and fault.get("remaining", 0) > 0:
            fault["remaining"] -= 1
            return json.dumps({"error": fault.get("error", "时长估算服务不可用")}, ensure_ascii=False)
        err = validate_script_shape(script)
        if err:
            return json.dumps({"error": err}, ensure_ascii=False)
        est = compute_duration(ctx, script, task)
        ctx["estimates"].append(est)
        return json.dumps(est, ensure_ascii=False)
    return estimate_duration


def build_check_rules(ctx: dict, task: dict):
    def check_rules(script) -> str:
        err = validate_script_shape(script)
        if err:
            return json.dumps({"error": err}, ensure_ascii=False)
        result = run_rule_check(ctx, script, task)
        ctx["checks"].append(result)
        return json.dumps(result, ensure_ascii=False)
    return check_rules


def build_save_script(ctx: dict, task: dict, request_id: str):
    def save_script(script) -> str:
        existing = ctx["scripts"].get(request_id)
        if existing:
            return json.dumps({"ok": True, "duplicate": True, "script_id": existing["script_id"],
                               "note": "该请求已有脚本，未重复保存"}, ensure_ascii=False)
        err = validate_script_shape(script)
        if err:
            return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
        est = compute_duration(ctx, script, task)
        if not est["ok"]:
            return json.dumps({"ok": False, "error": "时长检查未通过，禁止保存",
                               "estimate": est}, ensure_ascii=False)
        chk = run_rule_check(ctx, script, task)
        if not chk["ok"]:
            return json.dumps({"ok": False, "error": "规则检查未通过，禁止保存", "check": chk},
                              ensure_ascii=False)
        ctx["seq"] += 1
        record = {
            "script_id": f"S{ctx['seq']:03d}",
            "request_id": request_id,
            "title": script["title"],
            "shots": [{**sh, "seconds": e["seconds"]} for sh, e in zip(script["shots"], est["shots"])],
            "total_seconds": est["total_seconds"],
            "target_seconds": est["target_seconds"],
            "platform": task.get("platform"),
            "materials": chk["cited_materials"],
            "disclaimer": script.get("disclaimer"),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        ctx["scripts"][request_id] = record
        return json.dumps({"ok": True, "duplicate": False, **record,
                           "note": "脚本已保存（可编辑文本，模拟环境）"}, ensure_ascii=False)
    return save_script


SCHEMAS = {
    "search_materials": {"required": ["query"], "optional": []},
    "estimate_duration": {"required": ["script"], "optional": []},
    "check_rules": {"required": ["script"], "optional": []},
    "save_script": {"required": ["script"], "optional": []},
}


def schema_error(action: str, args: dict) -> str | None:
    schema = SCHEMAS.get(action)
    if schema is None:
        return f"未知动作 {action}"
    if not isinstance(args, dict):
        return f"{action} 的参数必须是 JSON 对象"
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        return f"{action} 缺少参数：{', '.join(missing)}"
    extra = [k for k in args if k not in schema["required"] + schema["optional"]]
    if extra:
        return f"{action} 不接受参数：{', '.join(extra)}"
    if action in ("estimate_duration", "check_rules", "save_script"):
        err = validate_script_shape(args.get("script"))
        if err:
            return f"script 结构不合法：{err}"
    return None


def build_registry(ctx: dict, task: dict, request_id: str) -> dict:
    return {
        "search_materials": build_search_materials(ctx, task.get("scope_materials")),
        "estimate_duration": build_estimate_duration(ctx, task),
        "check_rules": build_check_rules(ctx, task),
        "save_script": build_save_script(ctx, task, request_id),
    }
