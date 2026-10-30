# 短视频脚本 Agent

帮助创作者生成**有事实依据、满足时长限制、可编辑**的短视频脚本：素材检索、时长估算（按字数/读速）、
品牌规则与事实追溯检查；超时长自动压缩重估；编造产品事实会被规则拦截；全程可追溯。

## 1. 环境与安装

- Python ≥ 3.10，仅用标准库。
- 默认 `mock` 离线可跑；真实模型需 OpenAI 兼容接口（已在 DeepSeek 验证）。

```bash
git clone <repo-url> && cd agent-campus-submission-q4
python3 -m unittest discover -s tests -v    # 23 个测试，全部应通过
```

## 2. 运行（CLI 方案）

```bash
# ① 场景列表
python3 -m src.cli

# ② 参考实例：首稿 38.5s → 压缩 → 28.5s（目标 30s），分镜/引用/时长齐全
python3 -m src.cli --requests Q4-01

# ③ 编造事实被拦后修正（P01 仅含“支持字幕”，首稿故意写“效率提升 80%”会被规则拦截）
python3 -m src.cli --requests Q4-06

# ④ 全部场景与证据重建
python3 -m src.cli --all
python3 -m src.run_examples
```

真实模型：

```bash
cp .env.example .env    # 填 MODEL_MODE=real 与 DEEPSEEK_API_KEY
python3 -m src.cli --requests Q4-01 --model real
python3 -m src.run_examples --model real   # 写入 examples/real-model/
```

Mock 边界：确定性决策器，只验证流程、时长闭环与事实追溯；不证明真实模型创意质量。两种模式共用同一循环与工具。

## 3. 评审入口

- 证据索引（Mock）：`examples/index.md`；真实模型：`examples/real-model/index.md`
- 参考实例：`examples/traces/Q4-01.trace.json`（38.5 → 28.5s）
- 编造拦截：`examples/traces/Q4-06.trace.json`（check_rules 先失败、修正后通过）
- 验收点与文件对应：`docs/03-evaluation.md`
- 测试输出：`examples/test-results.txt`

## 4. 目录

```
src/    gateway.py  tools.py（时长估算/规则检查/事实追溯/保存）  agent.py（循环+声明核对）  cli.py  run_examples.py
tests/  test_tools.py  test_scenarios.py
examples/ traces/ requests/ index.md test-results.txt / real-model/
data/   materials.json brand_rules.json requests.json
docs/   01-requirements ~ 05-retrospective
```

## 5. 已知限制

- 素材为 6 条合成数据；时长按 4.5 字/秒固定读速估算；
- 事实追溯覆盖数字与产品属性词表，无法覆盖所有语义级表述；
- 压缩策略为确定性降级（删场景句→减产品事实），不追求创意最优；
- 真实模型创意与合规依赖模型本身，系统只保证时长、规则与事实底线。
