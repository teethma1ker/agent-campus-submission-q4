"""批量运行示例并产出证据。"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import Agent, Limits  # noqa: E402
from cli import load_requests, make_ctx, make_request_payload, print_result  # noqa: E402
from gateway import build_gateway, load_dotenv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["mock", "real"], default="mock")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only")
    ap.add_argument("--index-only", action="store_true")
    args = ap.parse_args()
    if args.out is None:
        args.out = os.path.join(ROOT, "examples" if args.model == "mock" else "examples/real-model")

    reqs = load_requests()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        reqs = [r for r in reqs if r["id"] in wanted]
    os.makedirs(os.path.join(args.out, "traces"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "requests"), exist_ok=True)

    rows = []
    for req in reqs:
        trace_path = os.path.join(args.out, "traces", f"{req['id']}.trace.json")
        if args.index_only:
            try:
                data = json.load(open(trace_path, encoding="utf-8"))
            except OSError:
                print(f"[{req['id']}] 缺少 trace，跳过")
                continue
        else:
            ctx = make_ctx(req)
            payload = make_request_payload(req, ctx)
            gateway = build_gateway(args.model)
            limits = Limits(max_steps=req.get("limit_steps", 24), max_tool_calls=req.get("limit_tools", 18))
            result = Agent(gateway, ctx, limits=limits).run_request(payload)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_result(req, result, args.model)
            with open(os.path.join(args.out, "traces", f"{req['id']}.console.txt"), "w", encoding="utf-8") as f:
                f.write(buf.getvalue())
            with open(os.path.join(args.out, "requests", f"{req['id']}.request.json"), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            data = {"request": payload, "mode": args.model, "status": result.status,
                    "report": result.report, "trace": result.trace}
            with open(trace_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            rep = result.report
            print(f"[{req['id']}] {req['scenario']} → {result.status}｜"
                  f"脚本 {'S ' + str(rep['saved_script']['total_seconds']) + 's' if rep['saved_script'] else '未保存'}｜"
                  f"估算 {[h['total_seconds'] for h in rep['estimate_history']]}｜"
                  f"claim {'通过' if rep['claim_check']['passed'] else '未通过'}")
        rows.append(data)

    lines = ["# 证据索引（由 src/run_examples.py 生成）", "",
             f"模型模式：`{args.model}`｜场景数：{len(rows)}", "",
             "| 场景 | 验收点 | 状态 | 脚本 | 时长轨迹 | 规则检查 | 声明核对 | 关键结论 |",
             "|---|---|---|---|---|---|---|---|"]
    for data in rows:
        rep = data["report"]
        req = data["request"]
        s = rep["saved_script"]
        script_txt = f"{s['script_id']}@{s['total_seconds']}s" if s else "未保存"
        hist = " → ".join(f"{h['total_seconds']}s{'✓' if h['ok'] else ''}" for h in rep["estimate_history"]) or "—"
        chk = rep.get("final_check")
        chk_txt = ("通过" if chk and chk.get("ok") else
                   "不通过" if chk else "—")
        concl = ("已保存：" + ", ".join(s["materials"])) if s else (rep["model_summary"] or "")[:60]
        lines.append(f"| [{req['id']}](traces/{req['id']}.trace.json) | {'、'.join(req.get('acceptance', []))} | "
                     f"{data['status']} | {script_txt} | {hist} | {chk_txt} | "
                     f"{'通过' if rep['claim_check']['passed'] else '未通过'} | {concl} |")
    lines += ["", "每份 trace 含逐步动作/参数/观察、最终脚本（分镜/旁白/字幕/素材/时长）、",
              "估算轨迹、规则检查、声明核对结果。"]
    with open(os.path.join(args.out, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"证据已写入 {args.out}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
