import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CompletionStatRow, CompletionStats } from "./CompletionStats";
import { PatternStudySummary, ReturnStudyRow } from "./PatternPanel";
import { StructureForecast } from "./StructureForecast";
import { sampleCountLabel, studySampleSummary, type CompletionRow, type PatternEvent, type PatternMeta, type PatternStudy, type StudyRow } from "./patternModel";

const meta: PatternMeta = { id: "double_bottom", name: "双底", group: "反转", family: "chart" };
const complete: CompletionRow = { pattern: meta.id, phase: "FORMED", score_bucket: "ALL", total: 150, pending: 10, overlap_excluded: 40, FORMING: 0, FORMED: 10, CONFIRMED: 90, INVALIDATED: 30, EXPIRED: 20, CENSORED: 0, formed_count: 150, sample_count: 100, successes: 60, rate: .6, probability: .6, interval: { lower: .4, upper: .8 }, calibration_count: 0, brier: null };
const returns: StudyRow = { pattern: meta.id, phase: "CONFIRMED", horizon: 20, detected: 150, pending: 10, overlap_excluded: 40, neutral_excluded: 0, sample_count: 100, win_rate: .6, interval: null, mean_net_return: .001, mean_mfe: .01, mean_mae: -.01, recent_samples: [] };
const report = { candidate_count: 3688, rows: [returns, { ...returns, horizon: 50, sample_count: 99 }, { ...returns, phase: "FORMED", sample_count: 88 }], partial: false } as PatternStudy["report"];

describe("historical sample presentation", () => {
  it("summarizes distinct recognized candidates separately from the selected return cohort and horizon", () => {
    expect(studySampleSummary(report, "CONFIRMED", 20)).toEqual({ detected: 3688, effective: 100, overlap: 40, pending: 10 });
    expect(studySampleSummary(report, "CONFIRMED", 50).effective).toBe(99);
    expect(studySampleSummary(report, "FORMED", 20).effective).toBe(88);
    const html = renderToStaticMarkup(<PatternStudySummary study={{ state: "READY", cursor: 8760, total: 8760, report }} cohort="CONFIRMED" horizon={20} />);
    for (const label of ["识别总量", "有效收益样本", "重叠排除", "等待走完", "本地回放已完成"]) expect(html).toContain(label);
  });
  it("keeps unknown summary counts absent instead of reporting a completed zero-sample replay", () => {
    expect(studySampleSummary(undefined, "CONFIRMED", 20)).toEqual({ detected: undefined, effective: null, overlap: null, pending: null });
    const html = renderToStaticMarkup(<PatternStudySummary cohort="CONFIRMED" horizon={20} />);
    expect(html).toContain("等待历史回放"); expect(html).toContain("<dd>—</dd>"); expect(html).not.toContain("<dd>0</dd>");
    expect(sampleCountLabel(0)).toBe("0"); expect(sampleCountLabel(undefined)).toBe("—");
  });
  it("marks partial reports independently of the worker state", () => {
    const html = renderToStaticMarkup(<PatternStudySummary study={{ state: "RUNNING", cursor: 5000, total: 8760, report: { ...report!, partial: true } }} cohort="CONFIRMED" horizon={20} />);
    expect(html).toContain("部分完成"); expect(html).not.toContain("本地回放已完成");
  });
  it("defaults completion statistics to the formed stage", () => {
    const html = renderToStaticMarkup(<CompletionStats metas={[meta]} rows={[complete]} />);
    expect(html).toContain('value="FORMED" selected=""'); expect(html).toContain("100 / 40"); expect(html).toContain("60.0%");
  });
  it("uses explicit stage capability for unsupported forming rows without pretending their zero is a probability", () => {
    const html = renderToStaticMarkup(<table><tbody><CompletionStatRow meta={{ ...meta, supports_forming: false }} phase="FORMING" row={{ ...complete, phase: "FORMING", sample_count: 0, total: 0 }} /></tbody></table>);
    expect(html).toContain("此形态不支持形成中预判"); expect(html).not.toContain("0.0%"); expect(html).not.toContain("<td>0</td>");
  });
  it("does not guess unsupported stages when capability metadata is absent", () => {
    const html = renderToStaticMarkup(<table><tbody><CompletionStatRow meta={meta} phase="FORMING" partial /></tbody></table>);
    expect(html).toContain("回放进行中，该阶段暂无统计"); expect(html).not.toContain("不支持"); expect(html).not.toContain("<td>0</td>");
  });
  it("keeps genuine zero samples visible but never converts them into zero win rate", () => {
    const html = renderToStaticMarkup(<table><tbody><CompletionStatRow meta={meta} phase="FORMED" row={{ ...complete, sample_count: 0, successes: 0, total: 0, probability: null, rate: null, interval: null }} /></tbody></table>);
    expect(html).toContain("0 / 0"); expect(html).toContain("无同类有效样本"); expect(html).not.toContain("0.0%");
  });
  it("shows overlapping and pending return exclusions without hiding valid samples", () => {
    const html = renderToStaticMarkup(<table><tbody><ReturnStudyRow row={returns} name={meta.name} group={meta.group} onDetail={vi.fn()} /></tbody></table>);
    expect(html).toContain("100 / 150"); expect(html).toContain("40 / 10"); expect(html).toContain("60.0%");
  });
  it("distinguishes a missing return row from a genuinely evaluated zero-sample cohort", () => {
    const html = renderToStaticMarkup(<table><tbody><ReturnStudyRow name={meta.name} group={meta.group} partial onDetail={vi.fn()} /></tbody></table>);
    expect(html).toContain("尚未生成此组统计"); expect(html).not.toContain("0 / 0");
  });
  it("labels a zero target calibration count as the original issue-time count without replacing its value", () => {
    const event = { forecast: { id: "original", source: "THEORY_ONLY", history_count: 0, target: null }, historical_reference: { sample_count: 100 } } as PatternEvent;
    const html = renderToStaticMarkup(<StructureForecast event={event} compact />);
    expect(html).toContain("本次预测发布时的目标校准样本 n=0"); expect(html).not.toContain("n=100");
    expect(event.forecast?.history_count).toBe(0);
  });
});
