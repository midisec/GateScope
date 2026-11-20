# GOD 数学证据核：首个可执行切片

更新时间：2026-09-07（Asia/Shanghai）

## 1. 能证明什么

`gatescope.quant.GodEvidenceEvaluator` 对预注册的、相互独立的、净成本后且有界的 holdout outcome blocks 计算单侧 Hoeffding 下置信界。对每个 block 的净收益 $R_i\in[a,b]$：

$$
\operatorname{LCB}_{\mu}
= \bar R-(b-a)\sqrt{\frac{\log(1/\alpha_i)}{2n}}
$$

对净收益为正的指示变量 $W_i=1[R_i>0]$：

$$
\operatorname{LCB}_{p}
= \max\left(0,\bar W-\sqrt{\frac{\log(1/\alpha_i)}{2n}}\right)
$$

其中 $\alpha_i=\alpha_{family}/M$，$M$ 是同一研究 family 实际检验的全部假设数，即 Bonferroni family-wise allocation。若预注册假设成立，则各下界是有限样本、分布无关的单侧界；不要求收益服从正态分布。

这不证明未来可获利。独立、平稳、成本完整、support 正确、holdout 未被研究者反复查看等任一条件失效，统计保证就不成立。代码把这些条件作为显式输入和输出 assumptions，而不是隐藏在“胜率”标签后面。

## 2. 强制 abstain

任一条件失败都输出 `ABSTAIN + RESEARCH`：

- 独立 block 数少于预注册门槛（默认 100）；
- 收益未包含费用、点差、滑点和 funding；
- holdout 未 sealed；
- 输入数据 stale；
- 当前特征落在批准分布之外；
- 所需外部特征许可未确认；
- 获利概率 LCB 未严格高于 break-even probability 上界；
- 净期望 LCB 未严格高于策略门槛。

未来时间 observation、重复 block ID、NaN/Infinity、越过预注册收益 support 会直接报错，不会被裁剪或静默忽略。

## 3. 当前最高权限

即使全部检查通过，当前 evaluator 也只能输出 `PAPER_ELIGIBLE + PAPER`。`PostgresPaperRepository.record_god_signal` 现可由 strategy workload 把结果连同 point-in-time bar manifest 固化；Web/MCP/TG 没有该写入口。后续只能生成 PAPER execution decision/proposal，不产生 TESTNET/LIVE 凭据，也没有真实 order port。升到 `LIVE_ELIGIBLE` 仍需完成：

1. purged/embargoed walk-forward、校准段与从未查看的 sealed holdout；
2. 重叠持有期转 independent blocks 的预注册方法；
3. 执行失败、NO_FILL、部分成交、保护失败纳入净收益 outcome；
4. 概率校准、OOD、regime、漂移与多策略组合门禁；
5. strategy certification、PAPER/TESTNET shadow 证据和人工审批。

## 4. 不可变摘要

输出 `evidence_digest` 绑定 policy version、decision time、alpha allocation、每个 block 的 ID/available_at/net return、估计值、下界和 reason codes。输入排序不影响 digest；追加未来数据无法进入较早 `decision_at`，因此不能改写旧证据。

当前 evaluator 仍是纯数学核心；新增仓储和 PAPER 控制面只是消费其冻结结果，不改变统计结论。它们合起来也不是完整 GOD。详细目标仍以 [02_QUANT_DECISION_SPEC.md](./02_QUANT_DECISION_SPEC.md) 为准。
