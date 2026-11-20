import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ChartStudyReadouts, profilePriceLabel } from "./ChartStudyReadouts";
import { computeVolumeProfile } from "./volumeProfile";
import { buildCoinglassChartStudies } from "./curatedCoinglass";
import { fineFixture, fineRequest } from "./fineVolumeProfile.testFixtures";

describe("selected chart study readouts",()=>{
  it("uses top-level fine time coverage rather than valid-row coverage",()=>{
    const bundle=fineFixture(fineRequest,{used_bars:15});
    const html=renderToStaticMarkup(<ChartStudyReadouts studies={[]} profileEnabled fineProfile={{key:"bound",phase:"READY",bundle,reload:vi.fn()}} />);
    expect(bundle.profile.coverage).toBe(1);expect(html).toContain("50.000%");expect(html).toContain("部分数据");
    expect(html).toContain('data-vpvr-source="1m"');expect(html).toContain('data-vpvr-bars="15"');expect(html).toContain('data-vpvr-time-coverage="0.5"');
    expect(html).toContain("不是计算准确率");
  });
  it("does not hide a single missing minute through percentage rounding",()=>{
    const bundle={...fineFixture(),status:"PARTIAL" as const,coverage:17279/17280,used_bars:17279,expected_bars:17280};
    const html=renderToStaticMarkup(<ChartStudyReadouts studies={[]} profileEnabled fineProfile={{key:"bound",phase:"READY",bundle,reload:vi.fn()}} />);
    expect(html).toContain("99.994%");expect(html).not.toContain("100.0%");expect(html).toContain("部分数据");
  });
  it("shows unit exclusions instead of falsely reporting absent cache",()=>{
    const bundle=fineFixture(fineRequest,{used_bars:0,quality_flags:["SOURCE_VOLUME_UNIT_UNVERIFIED"]});
    const html=renderToStaticMarkup(<ChartStudyReadouts studies={[]} profileEnabled fineProfile={{key:"bound",phase:"READY",bundle,reload:vi.fn()}} />);
    expect(html).toContain("单位未核实");expect(html).not.toContain("暂无细周期缓存");expect(html).not.toContain("POC 0");
  });
  it("does not show an older local profile while fine mode loads or fails",()=>{
    const local=computeVolumeProfile([{open_time:"2026-01-01T00:00:00Z",open:100,high:102,low:98,close:101,volume:100,is_final:true}],{from:0,to:0});
    const html=renderToStaticMarkup(<ChartStudyReadouts studies={[]} profileEnabled profile={local} fineProfile={{key:"next",phase:"ERROR",error:"细周期分布接口尚不可用",reload:vi.fn()}} />);
    expect(html).toContain('data-vpvr-status="ERROR"');expect(html).toContain('data-vpvr-bars="0"');expect(html).toContain("接口尚不可用");expect(html).toContain("重试");expect(html).not.toContain("POC 100");
  });
  it("does not round very small but nonzero price levels to zero",()=>{
    expect(profilePriceLabel(1.125e-20)).toBe("1.125000e-20");
    expect(profilePriceLabel(0.12)).toBe("0.12");
  });
  it("never replaces unavailable CG data with a zero",()=>{
    const studies=buildCoinglassChartStudies(undefined,["funding","oi_change"],[],"15m",1800000000);
    const html=renderToStaticMarkup(<ChartStudyReadouts studies={studies} onManage={vi.fn()} />);
    expect(html).toContain("UNAVAILABLE");expect(html).toContain("—");expect(html).not.toContain("0%");expect(html).toContain("管理指标");
  });
  it("labels the OHLCV estimate and exposes the actual visible coverage",()=>{
    const profile=computeVolumeProfile([{open_time:"2026-01-01T00:00:00Z",open:100,high:102,low:98,close:101,volume:100,is_final:true}],{from:0,to:0});
    const html=renderToStaticMarkup(<ChartStudyReadouts studies={[]} profileEnabled profile={profile} />);
    expect(html).toContain("K线估算");expect(html).toContain("POC");expect(html).toContain("VAH");expect(html).toContain("VAL");expect(html).toContain('data-vpvr-bars="1"');expect(html).toContain("不是逐笔成交");
  });
  it("does not show levels when all visible volume is zero",()=>{
    const profile=computeVolumeProfile([{open_time:"2026-01-01T00:00:00Z",open:1,high:1,low:1,close:1,volume:0,is_final:true}],{from:0,to:0});
    const html=renderToStaticMarkup(<ChartStudyReadouts studies={[]} profileEnabled profile={profile} />);
    expect(html).toContain("成交量为零");expect(html).not.toContain("POC 0");
  });
});
