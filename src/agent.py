"""脚本 Agent：模型选动作 → 工具（估算/检查/保存）→ 观察 → 修正或结束。

质量防线：
- 时长只认 estimate_duration 的结果；
- 保存只认 save_script 的结果（工具内部复检估算与规则）；
- 声明核对：总结中声称的时长/保存/提升百分比，与工具真值比对，不符即标注；
- 上限：步数/工具调用，超限返回明确未完成状态。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from gateway import SYSTEM_PROMPT, MockLLM, extract_json, render_user_prompt
from tools import build_registry, schema_error, compute_duration, run_rule_check

TOOL_ACTIONS = ("search_materials", "estimate_duration", "check_rules", "save_script")


@dataclass
class Limits:
    max_steps: int = 24
    max_tool_calls: int = 18
    max_protocol_errors: int = 5


@dataclass
class RunResult:
    status: str
    request_id: str
    trace: list[dict] = field(default_factory=list)
    report: dict = field(default_factory=dict)


def summarize(action: str, obs: dict) -> str:
    if not isinstance(obs, dict):
        return str(obs)[:140]
    if obs.get("error"):
        return f"error: {obs['error']}"
    if action == "search_materials":
        return f"命中 {obs.get('count')} 条：{', '.join(h['id'] for h in obs.get('hits', []))}"
    if action == "estimate_duration":
        return (f"总时长 {obs.get('total_seconds')}s（目标 {obs.get('target_seconds')}s，"
                f"ok={obs.get('ok')}，平台 {obs.get('platform_range')}）")
    if action == "check_rules":
        return f"ok={obs.get('ok')}｜错误：{'；'.join(obs.get('errors', [])) or '无'}"
    if action == "save_script":
        if obs.get("duplicate"):
            return f"duplicate=true script_id={obs.get('script_id')}"
        return f"已保存 {obs.get('script_id')}｜{obs.get('total_seconds')}s"
    return str(obs)[:140]


class Agent:
    def __init__(self, gateway, ctx: dict, limits: Limits | None = None):
        self.gateway = gateway
        self.ctx = ctx
        self.limits = limits or Limits()

    def run_request(self, request: dict) -> RunResult:
        task = request["task"]
        registry = build_registry(self.ctx, task, request["id"])
        trace: list[dict] = []
        tool_calls = 0
        protocol_errors = 0
        status = "incomplete_limit_reached"
        finish_summary = None
        is_mock = isinstance(self.gateway, MockLLM)

        while True:
            if len(trace) >= self.limits.max_steps:
                status = "incomplete_limit_reached"
                break
            if is_mock:
                decision = self.gateway.decide(request, trace)
            else:
                try:
                    raw = self.gateway.complete(SYSTEM_PROMPT, render_user_prompt(request, trace))
                except Exception as e:  # noqa: BLE001
                    trace.append({"step": len(trace) + 1, "thought": "", "action": "model_error",
                                  "arguments": {}, "observation_summary": f"模型调用失败：{e}",
                                  "decision_basis": "模型不可用，终止"})
                    status = "model_error"
                    break
                try:
                    decision = extract_json(raw)
                except Exception as e:  # noqa: BLE001
                    protocol_errors += 1
                    trace.append({"step": len(trace) + 1, "thought": "模型输出不可解析",
                                  "action": "protocol_error", "arguments": {},
                                  "observation_summary": f"输出被拒绝：{e}",
                                  "decision_basis": "解析失败，未执行"})
                    if protocol_errors >= self.limits.max_protocol_errors:
                        status = "model_protocol_error"
                        break
                    continue

            action = decision.get("action")
            args = decision.get("arguments") or {}
            thought = str(decision.get("thought", ""))[:300]

            if action == "finish":
                finish_summary = str((args or {}).get("summary", ""))[:600]
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "finish",
                              "arguments": args, "observation_summary": "给出最终结果",
                              "decision_basis": thought})
                status = "completed"
                break

            err = schema_error(action, args)
            if err:
                protocol_errors += 1
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "invalid_action",
                              "arguments": args if isinstance(args, dict) else {},
                              "observation_summary": f"动作被拒绝：{err}",
                              "decision_basis": "动作/参数不符合工具协议，未执行"})
                if protocol_errors >= self.limits.max_protocol_errors:
                    status = "model_protocol_error"
                    break
                continue

            if tool_calls >= self.limits.max_tool_calls:
                status = "incomplete_limit_reached"
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "limit_reached",
                              "arguments": {}, "observation_summary": "工具调用次数已达上限，停止",
                              "decision_basis": "达到工具调用上限"})
                break

            try:
                obs = json.loads(registry[action](**args))
            except Exception as e:  # noqa: BLE001
                obs = {"error": f"工具执行异常：{e}"}
            tool_calls += 1
            trace.append({"step": len(trace) + 1, "thought": thought, "action": action,
                          "arguments": args, "observation_summary": summarize(action, obs),
                          "observation": obs, "decision_basis": thought})

        report = self._build_report(request, trace, status, finish_summary, tool_calls)
        return RunResult(status=status, request_id=request.get("id", "request"), trace=trace, report=report)

    def _build_report(self, request, trace, status, finish_summary, tool_calls) -> dict:
        task = request["task"]
        saved = self.ctx["scripts"].get(request["id"])
        estimates = [t for t in trace if t["action"] == "estimate_duration" and "observation" in t]
        checks = [t for t in trace if t["action"] == "check_rules" and "observation" in t]
        last_est = None
        for e in reversed(estimates):
            if "total_seconds" in (e["observation"] or {}):
                last_est = e["observation"]
                break
        last_check = (checks[-1]["observation"] if checks else None)
        # 最终检查以“最后一次成功检查”为准（若之后脚本又变化，保存工具内部会再检）
        saved_ok = bool(saved)

        claim_notes = []
        text = finish_summary or ""
        negations = ("未保存", "没有保存", "保存失败", "未完成", "无法完成", "不可行", "未达标", "无法生成")
        positive = text
        for n in negations:
            positive = positive.replace(n, "")
        # 声称保存但未落库
        if any(w in positive for w in ("已保存", "保存成功")) and not saved_ok:
            claim_notes.append("模型总结声称已保存，但脚本库无记录，判定为虚报")
        # 声称“总时长 N 秒”时，以工具真值为准（保存时重算 > 最近估算；目标时长等其他数字不算声称）
        authoritative = None
        if saved_ok:
            authoritative = saved.get("total_seconds")
        elif last_est and last_est.get("total_seconds") is not None:
            authoritative = last_est["total_seconds"]
        if authoritative is not None:
            for m in re.findall(r"(?:总时长|共计|全长)[^\d]{0,6}(\d+(?:\.\d+)?)\s*秒", text):
                if abs(float(m) - float(authoritative)) > 0.51:
                    claim_notes.append(
                        f"总结声称总时长 {m} 秒，与工具真值 {authoritative} 秒不一致")
        # 数字声明必须能在引用素材事实中找到（总结同样适用）
        cited = set()
        for t in trace:
            if t["action"] in ("estimate_duration", "check_rules", "save_script"):
                for mid in ((t.get("arguments", {}).get("script") or {}).get("shots") or []):
                    cited.update(mid.get("materials") or [])
        fact_text = " ".join(" ".join(self.ctx["materials"][m]["facts"])
                             for m in cited if m in self.ctx["materials"])
        # 只拦截“效果类百分比”（提升/提高/增长/降低/省/快/增强 N%）；偏差、容差等计算值不算
        for m in re.finditer(r"(提升|提高|增长|降低|减少|省下|节省|更快|增强|下降)[^\d]{0,6}(\d+(?:\.\d+)?)\s*%", text):
            num = m.group(2)
            if f"{num}%" not in fact_text:
                claim_notes.append(f"总结中出现效果类百分比 {num}%（{m.group(1)}），素材事实中不存在，判定为编造效果数据")

        steps_view = [{"step": t["step"], "action": t["action"], "arguments": t["arguments"],
                       "observation_summary": t["observation_summary"],
                       "decision_basis": t.get("decision_basis", "")} for t in trace]

        return {
            "request_id": request.get("id"),
            "task": task,
            "status": status,
            "status_note": {
                "completed": "本次运行已结束",
                "incomplete_limit_reached": "达到执行步数/工具调用上限，存在未完成步骤",
                "model_protocol_error": "模型连续输出非法协议内容，运行中止",
                "model_error": "模型调用失败，运行中止",
            }.get(status, status),
            "model_summary": finish_summary,
            "claim_check": {"passed": not claim_notes, "issues": claim_notes},
            "saved_script": saved,
            "final_estimate": last_est,
            "final_check": last_check,
            "estimate_history": [{"total_seconds": e["observation"].get("total_seconds"),
                                  "ok": e["observation"].get("ok")}
                                 for e in estimates
                                 if "observation" in e and isinstance(e["observation"], dict)
                                 and "total_seconds" in e["observation"]],
            "check_history": [{"ok": c["observation"].get("ok"),
                               "errors": c["observation"].get("errors")}
                              for c in checks if "observation" in c],
            "steps": steps_view,
            "tool_calls": tool_calls,
            "step_count": len(trace),
            "publishing": "模拟环境：save_script 不产生任何外部动作",
        }
