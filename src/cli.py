"""CLI：短视频脚本 Agent。"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import Agent, Limits  # noqa: E402
from gateway import build_gateway, load_dotenv  # noqa: E402
from tools import load_context  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_requests() -> list[dict]:
    with open(os.path.join(ROOT, "data", "requests.json"), encoding="utf-8") as f:
        return json.load(f)


def make_ctx(req: dict) -> dict:
    ctx = load_context(os.path.join(ROOT, "data"))
    for tool, spec in (req.get("faults") or {}).items():
        ctx["faults"][tool] = dict(spec)
    if req.get("existing_saved"):
        rec = {"script_id": "S001", "request_id": req["id"], "title": "既有脚本",
               "shots": [], "total_seconds": 30.0, "target_seconds": req.get("target_seconds"),
               "platform": req.get("platform"), "materials": ["P02"], "disclaimer": None,
               "saved_at": "2026-10-30T09:00:00"}
        ctx["scripts"][req["id"]] = rec
    return ctx


def make_request_payload(req: dict, ctx: dict) -> dict:
    return {
        "id": req["id"], "scenario": req["scenario"], "request": req.get("scenario", ""),
        "acceptance": req.get("acceptance", []),
        "task": {"theme": req.get("theme"), "audience": req.get("audience"),
                 "platform": req.get("platform"), "target_seconds": req.get("target_seconds"),
                 "style": req.get("style"), "scope_materials": req.get("scope_materials")},
        "rules": ctx["rules"],
        "existing_script": ctx["scripts"].get(req["id"]),
        "trap_first_draft": bool(req.get("trap_first_draft")),
    }


def print_result(req: dict, result, mode: str) -> None:
    rep = result.report
    print(f"\n=== {req['id']}｜{req['scenario']}｜模型 {mode} ===")
    print(f"状态：{rep['status']}（{rep['status_note']}）｜步数 {rep['step_count']}｜工具调用 {rep['tool_calls']}")
    s = rep["saved_script"]
    if s:
        print(f"脚本：{s['script_id']}《{s['title']}》｜总时长 {s['total_seconds']}s / 目标 {s['target_seconds']}s"
              f"｜素材 {', '.join(s['materials'])}")
        for i, sh in enumerate(s["shots"], 1):
            print(f"  分镜{i} [{sh['seconds']}s] {sh.get('materials')}｜{sh['narration']}｜字幕：{sh['subtitle']}")
    else:
        print("脚本：未保存")
    est = rep.get("final_estimate")
    if est and est.get("total_seconds") is not None:
        print(f"最终估算：{est['total_seconds']}s（目标 {est.get('target_seconds')}s，"
              f"within_target={est.get('within_target')}，platform={est.get('within_platform')}）")
    if rep.get("final_check"):
        print(f"最终检查：{'通过' if rep['final_check']['ok'] else '不通过'}｜错误：{rep['final_check']['errors']}")
    print(f"模型总结：{rep.get('model_summary') or '（无）'}")
    if not rep["claim_check"]["passed"]:
        print(f"!! 声明核对未通过：{rep['claim_check']['issues']}")


def main() -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser(description="短视频脚本 Agent")
    ap.add_argument("--requests")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--model", choices=["mock", "real", "auto"], default=None)
    ap.add_argument("--limit-steps", type=int, default=24)
    ap.add_argument("--limit-tools", type=int, default=18)
    ap.add_argument("--trace-dir")
    args = ap.parse_args()

    reqs = load_requests()
    by_id = {r["id"]: r for r in reqs}
    mode = args.model or os.environ.get("MODEL_MODE", "mock")

    if not args.requests and not args.all:
        print("场景列表：\n")
        for r in reqs:
            print(f"{r['id']:<7}{r['scenario']}")
        print("\n运行示例：python -m src.cli --requests Q4-01")
        return 0

    chosen = reqs if args.all else [by_id[i.strip()] for i in args.requests.split(",") if i.strip() in by_id]
    if not chosen:
        print("没有可运行的请求")
        return 2

    failures = 0
    for req in chosen:
        ctx = make_ctx(req)
        payload = make_request_payload(req, ctx)
        gateway = build_gateway("mock" if mode == "mock" else "real")
        limits = Limits(max_steps=req.get("limit_steps", args.limit_steps),
                        max_tool_calls=req.get("limit_tools", args.limit_tools))
        result = Agent(gateway, ctx, limits=limits).run_request(payload)
        print_result(req, result, mode)
        if args.trace_dir:
            os.makedirs(args.trace_dir, exist_ok=True)
            with open(os.path.join(args.trace_dir, f"{req['id']}.json"), "w", encoding="utf-8") as f:
                json.dump({"request": req, "mode": mode, "status": result.status,
                           "report": result.report, "trace": result.trace}, f, ensure_ascii=False, indent=2)
        if result.status in ("model_error", "model_protocol_error"):
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
