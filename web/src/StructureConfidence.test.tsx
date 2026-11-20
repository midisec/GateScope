import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { probabilityInterval, probabilityLabel, StructureConfidence } from "./StructureConfidence";
import type { PatternEvent, CompletionProbability } from "./patternModel";

describe("structure confidence",()=>{
  const probability:CompletionProbability={sample_count:100,successes:60,rate:.6,probability:.6,interval:{lower:.4,upper:.8}};
  it("never describes one success as a confident probability",()=>{
    expect(probabilityLabel({...probability,sample_count:1,rate:1})).toBe("样本不足");
    expect(probabilityLabel({...probability,rate:0,probability:0})).toBe("0.0%");
  });
  it("separates formation, confirmation and geometry",()=>{
    const event={phase:"FORMING",confidence:.9,historical_reference:{...probability,formation:{...probability,probability:.8}},initial_prediction:{...probability,probability:.3}} as PatternEvent;
    const html=renderToStaticMarkup(<StructureConfidence event={event}/>);
    expect(html).toContain("90 分");expect(html).toContain("80.0%");expect(html).toContain("60.0%");expect(html).toContain("40.0%–80.0%");
    const frozen=renderToStaticMarkup(<StructureConfidence event={event} frozen/>);
    expect(frozen).toContain("30.0%");expect(frozen).toContain("首次判断时");expect(frozen).not.toContain("60.0%");
  });
  it("distinguishes missing history, unmatched samples and unavailable stages from real zero probability", () => {
    expect(probabilityLabel(undefined)).toBe("等待历史统计");
    expect(probabilityLabel({ ...probability, sample_count: null, reference_state: "HISTORY_NOT_READY" })).toBe("历史回放未就绪");
    expect(probabilityLabel({ ...probability, sample_count: null, reference_state: "STAGE_UNAVAILABLE" })).toBe("该阶段暂无统计");
    expect(probabilityLabel({ ...probability, sample_count: 0, reference_state: "NO_MATCHING_SAMPLES" })).toBe("无同类有效样本");
    expect(probabilityLabel({ ...probability, sample_count: 30, successes: 0, rate: 0, probability: 0, reference_state: "READY" })).toBe("0.0%");
  });
  it.each(["CONFIRMED", "INVALIDATED", "EXPIRED"] as const)("shows %s as an entry-stage historical reference, with the original cutoff and failure denominator", phase => {
    const history: CompletionProbability = { ...probability, reference_phase: "FORMED", reference_as_of: 1720000000, reference_score_bucket: "HIGH", reference_state: "READY", reference_is_live_prediction: false };
    const event = { phase, confidence: .9, historical_reference: history } as PatternEvent;
    const html = renderToStaticMarkup(<StructureConfidence event={event} />);
    expect(html).toContain("形成阶段的历史确认率"); expect(html).not.toContain("突破确认概率");
    expect(html).toContain("n=100"); expect(html).toContain("85–100 分");
    expect(html).toContain("确认 60 / 失败 40"); expect(html).toContain("均计入有效分母");
    expect(html).toContain("样本截止"); expect(html).toContain(new Date(1720000000 * 1000).toLocaleString());
    expect(html).toContain("回放补充对照 · 非当时预测");
  });
  it("keeps a frozen zero-sample prediction unchanged when a later historical reference has samples", () => {
    const event = { phase: "CONFIRMED", confidence: .9, historical_reference: probability,
      initial_prediction: { sample_count: 0, successes: 0, rate: null, probability: null, interval: null } } as PatternEvent;
    const initial = JSON.stringify(event.initial_prediction);
    const liveHtml = renderToStaticMarkup(<StructureConfidence event={event} />);
    const frozenHtml = renderToStaticMarkup(<StructureConfidence event={event} frozen />);
    expect(liveHtml).toContain("60.0%"); expect(liveHtml).toContain("n=100");
    expect(frozenHtml).toContain("首次判断时"); expect(frozenHtml).toContain("n=0"); expect(frozenHtml).not.toContain("60.0%");
    expect(JSON.stringify(event.initial_prediction)).toBe(initial);
  });
  it("does not manufacture zero samples for a missing or not-ready reference", () => {
    for (const history of [undefined, { ...probability, sample_count: null, successes: null, reference_state: "HISTORY_NOT_READY" as const }]) {
      const html = renderToStaticMarkup(<StructureConfidence event={{ phase: "CONFIRMED", confidence: .9, historical_reference: history } as PatternEvent} />);
      expect(html).toContain("n=—"); expect(html).not.toContain("n=0"); expect(html).not.toContain("失败 0");
    }
  });
  it("does not display an interval for an unknown or insufficient denominator", () => {
    expect(probabilityInterval({ ...probability, sample_count: null })).toBe("—");
    expect(probabilityInterval({ ...probability, sample_count: 1 })).toBe("—");
    expect(probabilityInterval(probability)).toBe("40.0%–80.0%");
  });
});
