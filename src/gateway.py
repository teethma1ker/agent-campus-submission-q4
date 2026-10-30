"""模型网关：真实 LLM 与离线 Mock 决策器（短视频脚本域）。"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


def load_dotenv(path: str = ".env") -> None:
    import os
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


@dataclass
class GatewayConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.0
    timeout: float = 60.0

    @staticmethod
    def from_env() -> "GatewayConfig":
        import os
        return GatewayConfig(
            base_url=os.environ.get("MODEL_BASE_URL", "https://api.deepseek.com").strip(),
            model=os.environ.get("MODEL_NAME", "deepseek-chat").strip(),
            api_key_env=os.environ.get("MODEL_API_KEY_ENV", "DEEPSEEK_API_KEY").strip(),
            temperature=float(os.environ.get("MODEL_TEMPERATURE", "0") or 0))


SYSTEM_PROMPT = """你是短视频脚本助手。根据产品素材与品牌规则，生成可编辑、有依据、满足时长的脚本。

# 可用工具
1. search_materials{"query":"台灯 参数"}                 检索产品素材（返回 ID/标签/事实 facts）
2. estimate_duration{"script":{...}}                     估算时长（按旁白字数/4.5 字每秒，逐分镜）
3. check_rules{"script":{...}}                           品牌规则检查（禁词/禁用素材/免责声明/事实追溯）
4. save_script{"script":{...}}                           保存脚本（仅当估算与规则都通过）

# 脚本结构（script 对象）
{"title":"标题","disclaimer":"可选免责声明","shots":[
  {"narration":"旁白（决定时长）","subtitle":"字幕文本","visual":"画面描述","materials":["P02"]}]}

# 硬性规则
- 每个分镜必须引用至少一个素材 ID；脚本中出现的每个数字必须能在所引用素材的 facts 中找到。
  严禁编造产品事实、效果数据（如“效率提升 80%”）——素材里没有的信息不要写。
- 使用 P05 促销素材时必须包含 disclaimer“活动规则以页面为准”；P04 为禁用素材。
- 时长：写完后必须调用 estimate_duration；超目标 ±10% 或超平台时长时压缩旁白（删句/缩句），
  再次估算，直到通过；无法满足时说明无法满足的原因并结束，不得虚报时长。
- 保存前必须 check_rules 通过；保存工具也会复检，失败时按错误修正再试。
- 若提供 existing_script（该请求已有脚本）：直接结束并报告已有脚本，不要重新生成或重复保存。
- 缺关键信息（平台/目标时长/主题）时：检索一次素材后结束，说明缺什么。
- 检索无结果时：可换一次关键词；仍无结果则说明无可用素材，不得凭空创作产品事实。
- 工具报错时：重试一次；仍失败则说明无法完成。

# 输出格式：只输出一个 JSON 对象
{"thought":"判断","action":"search_materials|estimate_duration|check_rules|save_script|finish","arguments":{...}}
结束时：{"thought":"...","action":"finish","arguments":{"summary":"产出说明（含最终时长/素材/检查结论）；无法完成时说明原因"}}
"""


def render_user_prompt(request: dict, trace: list[dict]) -> str:
    task = request["task"]
    lines = [
        "## 任务",
        f"主题：{task.get('theme')}｜受众：{task.get('audience')}｜平台：{task.get('platform')}"
        f"｜目标时长：{task.get('target_seconds')} 秒｜风格：{task.get('style')}",
    ]
    if task.get("scope_materials"):
        lines.append(f"可用素材范围：{', '.join(task['scope_materials'])}")
    lines += ["", "## 品牌规则", json.dumps(request.get("rules", {}), ensure_ascii=False)]
    lines += ["", "## 本轮工具轨迹（含完整观察）"]
    for st in trace[-10:]:
        lines.append(f"[{st['step']}] {st['action']} 参数={json.dumps(st['arguments'], ensure_ascii=False)[:900]}")
        obs = st.get("observation")
        if obs is not None:
            body = json.dumps(obs, ensure_ascii=False)
            lines.append(f"  观察：{body[:1500]}{'…' if len(body) > 1500 else ''}")
        else:
            lines.append(f"  观察：{st['observation_summary']}")
    if not trace:
        lines.append("（尚无动作）")
    lines += ["", "请输出下一个动作（JSON）。"]
    return "\n".join(lines)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg

    def _key(self):
        import os
        key = os.environ.get(self.cfg.api_key_env, "").strip()
        if not key:
            raise LLMError(f"缺少环境变量 {self.cfg.api_key_env}")
        return key

    def complete(self, system: str, user: str) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {"model": self.cfg.model, "temperature": self.cfg.temperature,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self._key()}"})
        last: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as r:
                    return json.loads(r.read().decode())["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                last = LLMError(f"HTTP {e.code}")
                if e.code in (400, 401, 403, 404):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as e:
                last = LLMError(str(e))
            time.sleep(1.5 * (attempt + 1))
        raise last or LLMError("模型调用失败")


def extract_json(text: str) -> dict:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.S)
    if fence:
        stripped = fence.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型输出中没有 JSON 对象")
    return json.loads(stripped[start:end + 1])


def build_gateway(mode: str):
    return MockLLM() if mode == "mock" else LLMClient(GatewayConfig.from_env())


# ---------------------------------------------------------------- 离线 Mock 决策器
class MockLLM:
    """确定性决策器：检索（可补充检索）→ 按目标时长写首稿 → 估算 → 超距则逐级压缩 → 规则检查 → 保存。

    _draft 的旁白只使用素材 facts；首稿目标约 1.25×目标时长，压缩分四档（删场景句/缩事实句/减分镜），
    使参考实例呈现“首稿 38 秒 → 压缩 → 28 秒”的可核查轨迹。
    """

    TOOLS = ("search_materials", "estimate_duration", "check_rules", "save_script")

    def decide(self, request: dict, trace: list[dict]) -> dict:
        task = request["task"]
        tools = [t for t in trace if t["action"] in self.TOOLS]
        searches = [t for t in tools if t["action"] == "search_materials"]
        estimates = [t for t in tools if t["action"] == "estimate_duration"]
        checks = [t for t in tools if t["action"] == "check_rules"]
        saves = [t for t in tools if t["action"] == "save_script"]

        if request.get("existing_script"):
            return self._finish(f"该请求已有脚本 {request['existing_script']['script_id']}，未重复生成。")

        missing = [k for k in ("theme", "platform", "target_seconds") if task.get(k) in (None, "")]
        if missing:
            if not searches:
                return self._act("缺少关键信息，先检索相关素材供参考。", "search_materials",
                                 {"query": task.get("theme") or "产品"})
            names = {"theme": "主题", "platform": "平台", "target_seconds": "目标时长"}
            return self._finish(f"缺少 {'、'.join(names[m] for m in missing)}，无法生成满足平台与时长要求的脚本；请补充后重试。")

        # ---- 检索：主题检索 + 场景补充检索（最多两轮）
        if not searches:
            return self._act("先检索与主题相关的产品素材。", "search_materials",
                             {"query": f"{task['theme']} {task.get('audience') or ''}"})
        pool = []
        for s in searches:
            obs = s.get("observation") or {}
            if obs.get("error"):
                continue
            pool.extend(h for h in obs.get("hits", []) if h["id"] not in {x["id"] for x in pool})
        if not searches or (len(searches) == 1 and not pool):
            if len(searches) >= 2:
                return self._finish("知识库中没有与该主题相关的产品素材，无法生成有事实依据的脚本；请补充素材。")
            return self._act("首轮检索无结果，改写关键词再试一次。", "search_materials", {"query": task.get("theme", "产品")})
        # 补充场景素材（没有可引用画面的素材时）
        has_broll = any(h["type"] in ("broll", "product") for h in pool)
        if len(searches) == 1 and has_broll:
            return self._act("补充检索空镜/场景素材，用于分镜画面。", "search_materials",
                             {"query": "场景 空镜 桌面"})
        last_error = searches[-1].get("observation", {}).get("error") if searches else None
        if last_error and not pool:
            if len(searches) < 3:
                return self._act(f"素材检索失败（{last_error}），换关键词重试一次。",
                                 "search_materials", {"query": task["theme"]})
            return self._finish(f"素材检索服务连续失败（{last_error}），无法生成脚本。")
        # 相关性校验：主题与素材的字二元组重叠（避免“美容仪”误用“台灯”素材）
        def grams(text):
            out = set()
            for tok in re.findall(r"[一-龥]+|[A-Za-z0-9]+", text):
                if re.match(r"[A-Za-z0-9]+", tok):
                    out.add(tok.lower())
                else:
                    for i in range(len(tok) - 1):
                        out.add(tok[i:i + 2])
            return out

        theme_grams = grams(task["theme"])
        def relevant(h):
            hay = h["title"] + " ".join(h["tags"]) + " ".join(h["facts"])
            return len(theme_grams & grams(hay)) >= 2

        relevant_pool = [h for h in pool if relevant(h)]
        usable = [h for h in relevant_pool if h["id"] != "P04"]
        if not usable:
            if len(searches) < 3:
                return self._act("检索结果与主题不相关或均为禁用素材，改写关键词再找一次。", "search_materials",
                                 {"query": task["theme"]})
            return self._finish("知识库中没有与该主题相关且可用的素材（检索结果不相关或为禁用素材），无法生成有事实依据的脚本；请补充素材。")

        if not estimates:
            return self._act("按素材事实写出首稿（约目标时长 1.25 倍），先估算时长。",
                             "estimate_duration", {"script": self._draft(usable, task, level=0)})
        est = estimates[-1].get("observation") or {}
        if est.get("error"):
            if len(estimates) < 2:
                return self._act(f"时长估算失败（{est['error']}），重试一次。", "estimate_duration",
                                 {"script": self._draft(usable, task, level=0)})
            return self._finish(f"时长估算服务连续失败（{est['error']}），无法确认时长达标，未保存脚本。")
        if not est.get("ok"):
            pr = est.get("platform_range") or [0, 0]
            target = est.get("target_seconds")
            total = est.get("total_seconds")
            if pr[0] and target and float(target) < pr[0]:
                return self._finish(f"目标时长 {target} 秒低于平台最短 {pr[0]} 秒，无法满足；请调整目标时长或平台。")
            if target and total is not None and float(total) < float(target) * 0.9:
                # 偏短无法靠压缩修正；素材可支撑的最大脚本已给出
                return self._finish(
                    f"按现有素材可支撑的脚本最长约 {total} 秒，低于目标 {target} 秒的允许范围，无法满足；"
                    f"请补充素材或降低目标时长。")
            level = min(len(estimates), 4)
            return self._act(
                f"总时长 {total} 秒与目标 {target} 秒偏差超限（超出），压缩旁白后重新估算（第 {level} 次修正）。",
                "estimate_duration", {"script": self._draft(usable, task, level=level)})

        level = len(estimates) - 1
        trap = bool(request.get("trap_first_draft")) and not checks
        current = self._draft(usable, task, level=level, trap=trap)
        if not checks:
            return self._act("时长达标，进行品牌规则与事实追溯检查。", "check_rules", {"script": current})
        chk = checks[-1].get("observation") or {}
        if not chk.get("ok"):
            errors = "；".join(chk.get("errors", []))
            if len(checks) >= 3:
                return self._finish(f"规则检查多次未通过：{errors}；无法在保证事实准确的前提下满足规则，请人工介入。")
            return self._act(f"规则检查发现：{errors}；删除无依据表述后重新检查。",
                             "check_rules", {"script": self._draft(usable, task, level=level, sanitize=True)})

        if not saves:
            return self._act("估算与规则均通过，保存脚本。", "save_script", {"script": current})
        save_obs = saves[-1].get("observation") or {}
        if save_obs.get("ok"):
            return self._finish(
                f"脚本已保存（{save_obs.get('script_id')}），总时长 {save_obs.get('total_seconds')} 秒，"
                f"素材 {', '.join(save_obs.get('materials', []))}，估算与规则检查均通过。")
        if len(saves) < 2:
            return self._act(f"保存失败（{save_obs.get('error')}），修正后重试一次。", "save_script",
                             {"script": current})
        return self._finish(f"脚本保存连续失败（{save_obs.get('error')}），任务未完成。")

    # ---- 基于素材事实的确定性草稿；level 0=首稿(~1.25×目标)，1~3 逐级压缩
    def _draft(self, usable, task, level=0, sanitize=False, trap=False):
        ids = {h["id"] for h in usable}
        has_p01_only = "P01" in ids and "P02" not in ids
        has_p03 = "P03" in ids

        if has_p01_only:
            # 参考实例：P01 唯一事实“支持字幕”，不得编造亮度/价格/效果
            lines = [
                ("开场", "画面亮起，字幕同步出现。", ["P01"]),
                ("说明", "本片全程支持字幕，看片不靠听，通勤路上也能安静看完。", ["P01"]),
                ("体验", "字幕跟着画面一起走，每一句话都能读到。", ["P01"]),
                ("体验", "戴上耳机，或者调成静音，都不会错过。", ["P01"]),
                ("体验", "深夜看片不吵到家人，字幕替你把声音收好。", ["P01"]),
                ("收尾", "这一支短片，我们把字幕放在了最重要的位置。", ["P01"]),
                ("收尾", "每一帧都有字幕陪伴，看到哪里都不会掉队。", ["P01"]),
            ]
            if level >= 1:
                lines = lines[:-1]                     # 删最后一句
            if level >= 2:
                lines = lines[:-1]                     # 再删一句
            if trap:                                    # 故意编造效果数据（应为规则所拦）
                lines = lines + [("效果", "加上字幕，观看效率提升 80%。", ["P01"])]
            title = "字幕，一个都不能少"
        else:
            fact = [
                ("产品", "Lumi 智能台灯，3000K 暖光不刺眼，映在纸上是很柔和的。", ["P02"]),
                ("产品", "5 档亮度调节，轻轻一按，总有一档适合此刻的你和眼前的桌面台面。", ["P02"]),
                ("功能", "支持语音控制，动动嘴就能调亮度，手里拿着书也不用起身去够那开关。", ["P02"]),
                ("收尾", "USB-C 充电，插上就能用，放在桌面上刚刚好，深夜收工时灯火慢慢变暗。", ["P02"]),
            ]
            scene = [
                ("开场", "夜里加班到很晚，桌面只亮着一盏灯，光悄悄落在键盘上。", ["P03"] if has_p03 else ["P02"]),
                ("场景", "周末午后，翻开一本书，灯光调得很柔和。", ["P03"] if has_p03 else ["P02"]),
            ]
            lines = (fact + scene) if not sanitize else fact
            if level >= 1:
                lines = fact                          # 删场景句 → 参考实例 38→28 档
            if level >= 2:
                lines = fact[:3]                      # 再删一条产品事实
            if level >= 3:
                lines = fact[:2]                      # 只保留两条核心事实
            title = f"{task.get('theme') or '产品'}｜{task.get('style') or '轻快'}短视频"

        scenes = {"开场": "桌面/夜景空镜", "场景": "阅读场景", "结尾": "夜晚收工空镜"}
        shots = []
        for visual, narr, mats in lines:
            shots.append({
                "visual": f"{visual}：{scenes.get(visual, '产品特写')}",
                "narration": narr,
                "subtitle": narr,
                "materials": [m for m in mats if m in ids] or [usable[0]["id"]],
            })
        return {"title": title, "shots": shots}

    def _act(self, thought, action, args):
        return {"thought": thought, "action": action, "arguments": args}

    def _finish(self, summary):
        return {"thought": "收尾。", "action": "finish", "arguments": {"summary": summary}}
