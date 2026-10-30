"""场景测试：六类验收 + 参考实例（38→28）+ 事实编造拦截 + 改参后动作变化 + 防御。"""
import json
import unittest

from helpers import ScriptLLM, get_request, setup
from agent import Agent, Limits
from gateway import MockLLM
from tools import load_context


def run(rid, limits=None, gateway=None):
    req, ctx, payload = setup(rid)
    result = Agent(gateway or MockLLM(), ctx, limits=limits or Limits()).run_request(payload)
    return result, ctx


class NormalGenerationTest(unittest.TestCase):
    def test_reference_instance_compress_to_target(self):
        """参考实例：首稿 38.5s → 压缩 → 28.5s（目标 30s），保存且引用 P02。"""
        result, ctx = run("Q4-01")
        rep = result.report
        self.assertEqual(result.status, "completed")
        self.assertTrue(rep["saved_script"])
        self.assertEqual(rep["estimate_history"][0]["total_seconds"], 38.5)
        self.assertFalse(rep["estimate_history"][0]["ok"])
        final = rep["estimate_history"][-1]
        self.assertTrue(final["ok"])
        self.assertEqual(final["total_seconds"], 28.5)
        self.assertEqual(rep["saved_script"]["total_seconds"], 28.5)
        self.assertIn("P02", rep["saved_script"]["materials"])
        for sh in rep["saved_script"]["shots"]:
            self.assertTrue(sh["materials"])
            self.assertTrue(sh["subtitle"])
            self.assertGreaterEqual(sh["seconds"], 2.0)
        self.assertTrue(rep["final_check"]["ok"])
        self.assertTrue(rep["claim_check"]["passed"])

    def test_p01_only_facts_respected(self):
        """参考实例：P01 唯一事实“支持字幕”，脚本不得出现亮度/价格/效果数据。"""
        result, _ = run("Q4-06")
        rep = result.report
        self.assertTrue(rep["saved_script"])
        text = " ".join(sh["narration"] + sh["subtitle"] for sh in rep["saved_script"]["shots"])
        for banned in ("80%", "亮度", "价格", "续航"):
            self.assertNotIn(banned, text)
        self.assertIn("字幕", text)
        self.assertEqual(set(rep["saved_script"]["materials"]), {"P01"})


class MissingInfoTest(unittest.TestCase):
    def test_missing_platform_and_target(self):
        result, ctx = run("Q4-02")
        rep = result.report
        self.assertIsNone(rep["saved_script"])
        self.assertIn("平台", rep["model_summary"])
        self.assertIn("目标时长", rep["model_summary"])
        self.assertEqual(ctx["scripts"], {})


class NoMaterialTest(unittest.TestCase):
    def test_unrelated_theme_no_script(self):
        result, ctx = run("Q4-03")
        rep = result.report
        self.assertIsNone(rep["saved_script"])
        self.assertIn("没有与该主题相关", rep["model_summary"])
        # 不得用台灯素材硬凑美容仪脚本
        self.assertEqual(ctx["scripts"], {})


class ToolFailureTest(unittest.TestCase):
    def test_estimate_failure_retries_then_stops(self):
        result, _ = run("Q4-04")
        rep = result.report
        self.assertIsNone(rep["saved_script"])
        est_calls = [t for t in result.trace if t["action"] == "estimate_duration"]
        self.assertEqual(len(est_calls), 2)          # 一次重试
        self.assertIn("无法确认时长达标", rep["model_summary"])


class DurationUnsatisfiableTest(unittest.TestCase):
    def test_target_below_platform_floor_declared_unsatisfiable(self):
        result, ctx = run("Q4-05")
        rep = result.report
        self.assertIsNone(rep["saved_script"])
        self.assertIn("低于平台最短", rep["model_summary"])
        self.assertEqual(ctx["scripts"], {})


class RepeatTest(unittest.TestCase):
    def test_existing_script_not_regenerated(self):
        result, ctx = run("Q4-07")
        self.assertEqual(result.report["tool_calls"], 0)
        self.assertEqual(len(ctx["scripts"]), 1)


class ParameterChangeTest(unittest.TestCase):
    """审核要点：改变时长或检索结果，下一步动作应随之调整。"""

    def test_target_shorter_causes_more_compression(self):
        req = get_request("Q4-01")
        ctx = load_context()
        task = dict(req, target_seconds=20)
        payload = {"id": "T20", "task": {"theme": "Lumi 智能台灯秋季推广", "audience": "年轻白领",
                                         "platform": "视频号", "target_seconds": 20, "style": "轻快"},
                   "rules": ctx["rules"], "existing_script": None}
        result = Agent(MockLLM(), ctx).run_request(payload)
        hist = result.report["estimate_history"]
        self.assertGreater(len(hist), 2)             # 需要更多轮压缩
        self.assertTrue(hist[-1]["ok"])
        self.assertLessEqual(hist[-1]["total_seconds"], 22)


class LimitTest(unittest.TestCase):
    def test_step_limit_incomplete(self):
        result, _ = run("Q4-08", limits=Limits(max_steps=2))
        self.assertEqual(result.status, "incomplete_limit_reached")
        self.assertFalse(result.report["saved_script"])


class GuardrailTest(unittest.TestCase):
    def test_fabricated_claim_percentage_flagged(self):
        """模型声称“效率提升 80%”但素材无此事实 → 声明核对拦截。"""
        script = {"title": "x", "shots": [{"visual": "v", "narration": "3000K 暖光。",
                                           "subtitle": "3000K 暖光。", "materials": ["P02"]}]}
        fake_model = ScriptLLM([
            {"thought": "检索", "action": "search_materials", "arguments": {"query": "台灯"}},
            {"thought": "估算", "action": "estimate_duration", "arguments": {"script": script}},
            {"thought": "保存", "action": "save_script", "arguments": {"script": script}},
            {"thought": "完成", "action": "finish",
             "arguments": {"summary": "脚本已保存，效率提升 80%，总时长 99 秒。"}},
        ])
        req, ctx, payload = setup("Q4-01")
        result = Agent(fake_model, ctx).run_request(payload)
        issues = result.report["claim_check"]["issues"]
        self.assertTrue(any("80%" in i for i in issues))
        self.assertTrue(any("99" in i or "时长" in i for i in issues))

    def test_fake_save_claim_flagged(self):
        script = {"title": "x", "shots": [{"visual": "v", "narration": "3000K 暖光。",
                                           "subtitle": "3000K 暖光。", "materials": ["P02"]}]}
        fake_model = ScriptLLM([
            {"thought": "检索", "action": "search_materials", "arguments": {"query": "台灯"}},
            {"thought": "完成", "action": "finish", "arguments": {"summary": "脚本已保存，总时长 6.9 秒。"}},
        ])
        req, ctx, payload = setup("Q4-01")
        result = Agent(fake_model, ctx).run_request(payload)
        self.assertFalse(result.report["saved_script"])
        self.assertFalse(result.report["claim_check"]["passed"])

    def test_publish_tool_rejected(self):
        fake_model = ScriptLLM([
            {"thought": "直接发布", "action": "publish_video", "arguments": {}},
            {"thought": "收尾", "action": "finish", "arguments": {"summary": "无法完成"}},
        ])
        req, ctx, payload = setup("Q4-02")
        result = Agent(fake_model, ctx).run_request(payload)
        self.assertIn("invalid_action", [t["action"] for t in result.trace])


if __name__ == "__main__":
    unittest.main()
