"""工具测试：时长估算、事实追溯、禁用素材、免责声明、保存双检、幂等。"""
import json
import unittest

from helpers import setup
from tools import load_context, build_registry, compute_duration, run_rule_check, schema_error


def fresh(task=None):
    ctx = load_context()
    task = task or {"theme": "Lumi 智能台灯", "platform": "视频号", "target_seconds": 30}
    return ctx, build_registry(ctx, task, "T1")


def script(shots, title="测试", disclaimer=None):
    return {"title": title, "disclaimer": disclaimer, "shots": shots}


def shot(narr, mats=("P02",), sub=None):
    return {"visual": "特写", "narration": narr, "subtitle": sub or narr, "materials": list(mats)}


class DurationTest(unittest.TestCase):
    def test_duration_by_chars(self):
        ctx, _ = fresh()
        s = script([shot("一" * 45), shot("二" * 9)])   # 10.0s + 2.0s(下限)
        est = compute_duration(ctx, s, {"target_seconds": 12, "platform": "小红书"})  # 小红书 10~90s
        self.assertEqual(est["total_seconds"], 12.0)
        self.assertTrue(est["ok"])

    def test_platform_limits(self):
        ctx, _ = fresh()
        s = script([shot("一" * 300)])   # 66.7s
        est = compute_duration(ctx, s, {"target_seconds": 66, "platform": "抖音"})   # 抖音上限 60s
        self.assertFalse(est["within_platform"])
        self.assertFalse(est["ok"])


class RuleTest(unittest.TestCase):
    def test_fabricated_number_rejected(self):
        ctx, _ = fresh()
        s = script([shot("效率提升 80%，画面更清晰。")])
        chk = run_rule_check(ctx, s, {})
        self.assertFalse(chk["ok"])
        self.assertIn("80", " ".join(chk["errors"]))

    def test_fact_number_accepted(self):
        ctx, _ = fresh()
        s = script([shot("3000K 暖光，5 档亮度调节。")])
        chk = run_rule_check(ctx, s, {})
        self.assertTrue(chk["ok"], chk["errors"])

    def test_blocked_material(self):
        ctx, _ = fresh()
        s = script([shot("对比测试。" , ["P04"])])
        chk = run_rule_check(ctx, s, {})
        self.assertFalse(chk["ok"])
        self.assertIn("禁用", " ".join(chk["errors"]))

    def test_promo_disclaimer_required(self):
        ctx, _ = fresh()
        s = script([shot("双十一 8 折。", ["P05"])])
        chk = run_rule_check(ctx, s, {})
        self.assertFalse(chk["ok"])
        s["disclaimer"] = "活动规则以页面为准"
        chk2 = run_rule_check(ctx, s, {})
        self.assertTrue(chk2["ok"], chk2["errors"])

    def test_forbidden_term(self):
        ctx, _ = fresh()
        s = script([shot("全网第一的台灯。", ["P02"])])
        chk = run_rule_check(ctx, s, {})
        self.assertFalse(chk["ok"])
        self.assertIn("全网第一", " ".join(chk["errors"]))

    def test_missing_subtitle(self):
        ctx, _ = fresh()
        s = script([shot("3000K 暖光。", sub=" ")])
        chk = run_rule_check(ctx, s, {})
        self.assertFalse(chk["ok"])


class SaveTest(unittest.TestCase):
    def test_save_requires_duration_and_rules(self):
        ctx, reg = fresh({"theme": "台灯", "platform": "视频号", "target_seconds": 10})
        bad = script([shot("3000K 暖光，" + "很好" * 30 + "。")])   # 超时长
        out = json.loads(reg["save_script"](bad))
        self.assertFalse(out["ok"])
        self.assertIn("时长", out["error"])

    def test_save_then_duplicate(self):
        ctx, reg = fresh({"theme": "台灯", "platform": "小红书", "target_seconds": 10})
        good = script([shot("一" * 45)])   # 恰好 45 字 = 10.0 秒
        first = json.loads(reg["save_script"](good))
        second = json.loads(reg["save_script"](good))
        self.assertTrue(first["ok"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(ctx["scripts"]), 1)


class SchemaTest(unittest.TestCase):
    def test_rules(self):
        self.assertIsNotNone(schema_error("save_script", {"script": {"title": "x", "shots": []}}))
        self.assertIsNotNone(schema_error("publish_video", {}))
        ok = {"title": "x", "shots": [{"narration": "a", "subtitle": "a", "visual": "v", "materials": ["P01"]}]}
        self.assertIsNone(schema_error("estimate_duration", {"script": ok}))


if __name__ == "__main__":
    unittest.main()
