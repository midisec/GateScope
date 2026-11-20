import type { MarketPage } from "./marketCache";
import type { Bar } from "./types";

type Entry={key:string;savedAt:number;page:MarketPage};
const prefix="gatescope.chart-preview.v1:";
const MAX_AGE=86400000;
function entries(scope:string):Entry[]{
  try{
    const raw=JSON.parse(localStorage.getItem(prefix+scope)??"[]");
    return Array.isArray(raw)?raw.filter(e=>e && typeof e.key==="string" && Number.isFinite(e.savedAt)
      && Date.now()-e.savedAt<MAX_AGE && e.savedAt<=Date.now()+60000 && Array.isArray(e.page?.items) && e.page.items.length<=480):[];
  }catch{return []}
}
export function readMarketPreview(scope:string,key:string):MarketPage|undefined{
  const saved=entries(scope).find(e=>e.key===key)?.page;
  if(!saved?.items.length)return;
  if(saved.items.some((b,i)=>!Number.isFinite(Date.parse(b.open_time)) || (i>0 && Date.parse(b.open_time)<=Date.parse(saved.items[i-1].open_time))
    || [b.open,b.high,b.low,b.close].some(v=>!Number.isFinite(+v)||+v<=0)))return;
  return {...saved,cache:saved.cache?{...saved.cache,stale:true,live_bar:null}:saved.cache};
}
export function saveMarketPreview(scope:string,key:string,page:MarketPage){
  if(!page.items.length)return;
  const items:Bar[]=page.items.slice(-480).map(b=>({open_time:b.open_time,close_time:b.close_time,available_at:b.available_at,
    open:b.open,high:b.high,low:b.low,close:b.close,volume:b.volume,is_final:b.is_final,revision:b.revision,quality_flags:b.quality_flags}));
  // Only public OHLC display data: no tokens, settings, credentials or user data.
  const preview={items,quality_flags:page.quality_flags,data_watermark:page.data_watermark,next_cursor:null,has_more:false,
    cache:page.cache?{...page.cache,stale:true,live_bar:null}:page.cache};
  try{localStorage.setItem(prefix+scope,JSON.stringify([...entries(scope).filter(e=>e.key!==key).slice(-5),{key,savedAt:Date.now(),page:preview}]))}catch{/* Quota/private mode must not break charts. */}
}
