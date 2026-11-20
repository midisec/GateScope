import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { AnnualHistoryRow, ReplayStorageNotice, type HistoryProgress } from "./AnnualHistory";

const row:HistoryProgress={instrument_id:"lobster",symbol:"龙虾_USDT",timeframe:"1m",cached_bars:9400,target_bars:267000,missing_bars:257600,coverage_ratio:9400/267000,history_status:"BACKFILLING",state:"RUNNING",cursor:500,total:9400,range_policy:"LISTING_CLIPPED",listing_time:1773138212,last_live_at:"2026-09-12T12:00:00Z"};

describe("watchlist history status",()=>{
  it("shows paused history honestly while retaining progress and completed results",()=>{
    const control={state:"PAUSED_LOW_DISK",checked_at:"2026-09-20T01:00:00Z"};
    const html=renderToStaticMarkup(<><ReplayStorageNotice control={control}/><AnnualHistoryRow row={row} control={control}/></>);
    expect(html).toContain("历史回放已暂停");expect(html).toContain("磁盘空间不足");
    expect(html).toContain("500 / 9,400");expect(html).toContain("已保留回放进度");
    expect(html).not.toContain("逐根回放中");
    const completed=renderToStaticMarkup(<AnnualHistoryRow row={{...row,state:"READY"}} control={control}/>);
    expect(completed).toContain("已完成本地样本回放");expect(completed).not.toContain("样本回放 · 已暂停");
    expect(renderToStaticMarkup(<ReplayStorageNotice control={{state:"UNKNOWN",checked_at:null}}/>)).toBe("");
  });
  it("does not claim active replay when a refresh or worker heartbeat is unavailable",()=>{
    const html=renderToStaticMarkup(<AnnualHistoryRow row={row} control={{state:"UNKNOWN",checked_at:null}}/>);
    expect(html).toContain("当前状态待确认");expect(html).toContain("500 / 9,400");
    expect(html).not.toContain("逐根回放中");expect(html).toContain("已保留回放进度");
  });
  it("shows listing-adjusted coverage separately from replay and live health",()=>{
    vi.spyOn(Date,"now").mockReturnValue(Date.parse("2026-09-12T12:00:20Z"));
    try {
      const html=renderToStaticMarkup(<AnnualHistoryRow row={row}/>);
      for(const text of ["龙虾_USDT","自上市起","上市前不计缺失","9,400 / 267,000 根","目标历史补齐中","实时行情 · 更新正常","样本回放 · 逐根回放中","500 / 9,400"])expect(html).toContain(text);
      expect(html).not.toContain("525,600");expect(html).not.toContain("一年缓存覆盖率");
    } finally { vi.restoreAllMocks(); }
  });
  it("does not imply completed replay means all historical downloads are complete",()=>{
    const html=renderToStaticMarkup(<AnnualHistoryRow row={{...row,state:"READY",history_status:"RETRYING",history_error_code:"MARKET_HISTORY_FAILED"}}/>);
    expect(html).toContain("已回放现有缓存");expect(html).toContain("历史来源暂不可用");expect(html).not.toContain("尚未覆盖完整一年");
  });
  it("shows exhausted replay and permanent archive errors without promising an automatic replay retry",()=>{
    const html=renderToStaticMarkup(<AnnualHistoryRow row={{...row,state:"FAILED",replay_error:"PATTERN_CALCULATION_FAILED",replay_retry_exhausted:true,history_error_code:"UNSUPPORTED_ARCHIVE_SYMBOL"}}/>);
    expect(html).toContain("自动重试已达上限");expect(html).toContain("历史下载不支持此币种代码");expect(html).not.toContain("回放失败，将重试");
  });
  it("retains a rolling year for unknown listings and makes live failures distinct",()=>{
    const html=renderToStaticMarkup(<AnnualHistoryRow row={{...row,range_policy:"ROLLING_YEAR",listing_time:null,live_error_code:"MARKET_POLL_FAILED"}}/>);
    expect(html).toContain("过去一年");expect(html).toContain("实时行情 · 更新延迟");expect(html).not.toContain("上市前不计缺失");
  });
  it("labels the persisted coverage snapshot and pending scope refresh without claiming current counts",()=>{
    const html=renderToStaticMarkup(<AnnualHistoryRow row={{...row,coverage_asof:"2026-09-12T12:00:00Z",range_refresh_pending:true}}/>);
    expect(html).toContain("覆盖统计截至");expect(html).toContain("历史范围更新中，暂保留上次完整统计");
  });
  it("does not round a nearly complete cache with real missing candles up to 100 percent",()=>{
    const html=renderToStaticMarkup(<AnnualHistoryRow row={{...row,cached_bars:105117,target_bars:105120,missing_bars:3,coverage_ratio:105117/105120}}/>);
    expect(html).toContain("99.99%");expect(html).not.toContain("100.0%");
  });
});
