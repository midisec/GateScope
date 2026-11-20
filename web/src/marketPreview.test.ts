import {afterEach,describe,expect,it,vi} from "vitest";
import {readMarketPreview,saveMarketPreview} from "./marketPreview";
import type {MarketPage} from "./marketCache";

function storage(){const values=new Map<string,string>();vi.stubGlobal("localStorage",{getItem:(k:string)=>values.get(k)??null,setItem:(k:string,v:string)=>values.set(k,v)});return values}
const items:Array<MarketPage["items"][number]>=Array.from({length:1500},(_,i)=>({open_time:new Date(i*60000).toISOString(),open:"99",high:"101",low:"98",close:"100",volume:"2",quality_flags:[],is_final:true}));
const page:MarketPage={items,next_cursor:null,has_more:false,quality_flags:[],data_watermark:"2026-09-11T00:00:00Z",
  cache:{active:true,stale:false,target_bars:525600,history_status:"BACKFILLING",live_bar:{...items[0],is_final:false}}};
afterEach(()=>vi.unstubAllGlobals());
describe("instant public chart preview",()=>{
  it("bounds persisted bars, excludes live candle, and never claims fresh",()=>{
    storage();saveMarketPreview("owner","btc:1h",page);
    const p=readMarketPreview("owner","btc:1h")!;
    expect(p.items.length).toBe(480);expect(p.cache?.stale).toBe(true);expect(p.cache?.live_bar).toBe(null);
    expect(readMarketPreview("another-user","btc:1h")).toBeUndefined();
    expect(readMarketPreview("owner","btc:4h")).toBeUndefined();
  });
  it("keeps only six pairs and ignores corrupt or unavailable storage",()=>{
    const values=storage();for(let i=0;i<8;i++)saveMarketPreview("u",String(i),page);
    expect(readMarketPreview("u","0")).toBeUndefined();expect(readMarketPreview("u","7")).toBeDefined();
    values.set("gatescope.chart-preview.v1:u","broken");expect(readMarketPreview("u","7")).toBeUndefined();
    vi.stubGlobal("localStorage",{getItem(){throw Error("disabled")},setItem(){throw Error("quota")}});
    expect(()=>saveMarketPreview("u","k",page)).not.toThrow();
  });
});
