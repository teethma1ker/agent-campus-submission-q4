# 03 评估

测试：`python3 -m unittest discover -s tests -v`（23 个测试通过，见 `examples/test-results.txt`）。
示例：`python3 -m src.run_examples`（8 场景，Mock）与 `--model real`（8 场景，真实模型）。

| 验收点 | 预期 | 实际（证据） | 结果 |
|---|---|---|---|
| 正常生成 | 分镜/旁白/字幕/素材/总时长齐全，时长达标 | `examples/traces/Q4-01.trace.json`：38.5s → 28.5s（目标 30s），4 分镜全部引用 P02，规则通过 | 通过 |
| 参考实例 | 38 秒首稿 → 压缩 → 28 秒附近，不编造效果数据 | 同上；Q4-06 P01 场景首稿含“效率提升 80%”被 check_rules 拦截，修正后 25.8s 保存 | 通过 |
| 缺关键信息 | 说明缺什么，不保存 | `examples/traces/Q4-02.trace.json` | 通过 |
| 无素材 | 说明无可用素材，不挪用无关素材 | `examples/traces/Q4-03.trace.json` + 测试 `test_unrelated_theme_no_script` | 通过 |
| 工具失败 | 估算重试一次后说明无法确认 | `examples/traces/Q4-04.trace.json`：两次估算失败，未保存 | 通过 |
| 超时长修正 | 压缩后重估达标 | `examples/traces/Q4-01.trace.json`：38.5 → 28.5；测试 `test_target_shorter_causes_more_compression`（20s 目标：38.5→28.5→20.7） | 通过 |
| 超时长明确失败 | 目标低于平台下限时说明无法满足 | `examples/traces/Q4-05.trace.json`：8s < 抖音 15s | 通过 |
| 事实可追溯 | 编造数字/属性被拦截 | 测试 `test_fabricated_number_rejected`、`test_p01_only_facts_respected`、`test_forbidden_term`、`test_blocked_material`、`test_promo_disclaimer_required` | 通过 |
| 重复执行 | 已有脚本不重复生成 | `examples/traces/Q4-07.trace.json` | 通过 |
| 执行上限 | 超限返回未完成 | `examples/traces/Q4-08.trace.json` | 通过 |
| 改参后动作变化 | 改时长/检索结果 → 动作调整 | 测试 `test_target_shorter_causes_more_compression`；Q4-01 vs Q4-03 检索结果决定后续路径 | 通过 |

## 真实模型（DeepSeek）验证

`examples/real-model/index.md`：8 个场景全部符合预期（Q4-01 真实模型首稿偏短 → 加长到 29.9s 后保存；
Q4-05 如实报告 8s 低于平台下限）。真实运行中发现并修复的问题（详见 `docs/04-ai-usage.md`）：
① 真实模型 Q4-06 曾写出“简洁设计”等 P01 素材中不存在的产品属性 → 新增产品属性词表追溯检查；
② 声明核对曾把“±10% 容差”“偏差 +4%”误判为编造百分比 → 收窄为“效果类百分比”；
③ 总时长核对口径改为以保存时工具重算值为准。

## 失败与限制

- 时长按固定读速估算，未考虑语速变化；
- 事实追溯覆盖数字与 16 个产品属性词，语义级编造（如暗示未证实的用途）无法完全拦截；
- 压缩为确定性策略，不保证创意最优；
- 故障注入为模拟，非真实服务故障。
