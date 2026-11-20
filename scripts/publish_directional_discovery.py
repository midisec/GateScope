"""Verify and append non-hedged research, including failures, without orders."""
import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from prepare_service_envs import SECRET_DIR,read_env
from gatescope.quant.artifacts import report_digest
from gatescope.quant.directional import FAMILIES
from gatescope.quant.performance import daily_performance

STUDIES=(Path("data/research-studies/directional-20260911T060217Z"),Path("data/research-studies/contextual-20260911T060921Z"))


def build_report():
    rows,details,plans=[],{},[]
    trials=0
    for batch,folder in enumerate(STUDIES,1):
        summary=json.loads((folder/"summary.json").read_text())
        plan=summary["plan"]
        assert report_digest(plan)==plan["artifact_digest"]
        plans.append(plan)
        source=Path("data/research-market")/("binance-directional-20210101-20260911" if batch==1 else "binance-contextual-20210101-20260911")
        for name,digest in plan["source_hashes"].items():
            raw=json.loads((source/name).read_text())
            assert hashlib.sha256(json.dumps(raw["rows"],separators=(",",":")).encode()).hexdigest()==digest==raw["sha256"]
        trials+=summary["total_trial_count"]
        for row in summary["finalists"]:
            r=json.loads((folder/f"finalist-{row['id']:02d}.json").read_text())
            assert report_digest(r)==r["artifact_digest"]==row["artifact_digest"]
            for segment in ("validation","full"):
                for cost in ("1","2"):
                    outcome=r[segment][cost]
                    assert abs(sum(t["net_pnl"] for t in outcome["trades"])-outcome["metrics"]["net_pnl"])<1e-6
                    assert all(outcome["metrics"][k]==v for k,v in daily_performance(outcome["equity_curve"]).items())
            key=f"batch-{batch}-model-{row['id']}"
            family=row["rule"]["family"]
            item=dict(key=key,name=FAMILIES[family][0],category=FAMILIES[family][1],dataset=row["dataset"],timeframe=r["timeframe"],
                rule=row["rule"],metrics=row["validation"]["1"],stress_metrics=row["validation"]["2"],
                screening=row["screening"]["1"],stress_screening=row["screening"]["2"],source_digest=r["artifact_digest"])
            rows.append(item)
            keep=("symbol","side","entry_time","exit_time","signal_time","entry","exit_price","stop","target","quantity","net_pnl","fees","funding","exit_reason","indicators","signal","exit_time_basis")
            detail=dict(row=item,trades=[{k:t[k] for k in keep if k in t} for t in r["validation"]["1"]["trades"]],
                equity_curve=r["validation"]["1"]["equity_curve"],full_metrics=r["full"]["1"]["metrics"],source_digest=r["artifact_digest"])
            origin=datetime(2021,1,1,tzinfo=timezone.utc)
            hours=1 if item["timeframe"]=="1h" else 4
            for t in detail["trades"]:
                if "signal" in t:
                    t["signal"]={**t["signal"],"anchors":[{**p,"time":(origin+timedelta(hours=p["index"]*hours)).isoformat(),"known_time":(origin+timedelta(hours=(p["known_index"]+1)*hours)).isoformat()} for p in t["signal"]["anchors"]]}
            details[key]=detail
    rows.sort(key=lambda r:(not r["screening"]["shortlisted"],-(r["metrics"]["sharpe_ratio"] or -100)))
    report=dict(kind="DIRECTIONAL",title="非对冲 · 指标 / 形态 / 波浪策略挖掘",from_time="2024-01-01",to_time="2026-09-11",
        development_trials=4176,frozen_count=len(rows),qualified_count=sum(r["screening"]["shortlisted"] for r in rows),
        stress_qualified_count=sum(r["screening"]["shortlisted"] and r["stress_screening"]["shortlisted"] for r in rows),
        rows=rows,details=details,detail_digests={k:report_digest(v) for k,v in details.items()},
        plan_digests=[p["artifact_digest"] for p in plans],trial_count=trials,hedged=False,deployable_count=0,
        notes=["本批只有方向性指标、形态和波浪规则，不含现货/永续对冲、套利、马丁或网格补仓。四币组合为固定现金份额只做多，各币独立空仓或持多，不用空头抵消多头。",
            "开发2021-07-01—2024-01-01；冻结候选后才评价2024—2026后段。分割处隔离72根K线；首批BNB/ADA/LTC/DOGE，迁移检验LINK/ETC/AVAX/DOT。BTC/ETH已看过区间不充当新验证证据。",
            "枢轴必须等待右侧3根已收盘K线。三浪和五浪是待完成候选，不把后来的波浪标签倒写进入场。记录保留原始锚点、确认索引、结构失效位和投影目标。",
            "指标：RSI2趋势回踩、MACD动量、布林回归、波动收缩突破。形态：吞没、双底双顶、头肩反转、收敛三角、旗形。波浪：三浪候选、五浪候选、ABC修正反转。",
            "每策略只持一个方向仓位，次根开盘入场，单笔风险0.5%，总名义仓位不超过账户净值；有结构失效位时用该位置止损。QUICK为0.8R/24根、SWING为1.4R/72根、MEASURED使用当时结构投影/72根；无投影则1.4R。",
            "基础每边手续费5bp+滑点5bp，压力组翻倍；计入历史有符号资金费，缺结算标记价使用该周期开盘代理，入场/退出同根不领取不确定资金费收入。止损止盈同根触及先按止损，开盘跳空按不利实际开盘处理。",
            "夏普与t来自完整UTC日净值收益（含空仓日），现金基准0，保守HAC方差。最低90日/30笔；筛选回撤取全期与后段较大值，按1h/4h收盘盯市，不是逐tick回撤。",
            "这是便利抽样的存续流动币种，仍有生存者偏差。未覆盖逐笔成交、交易所故障、ADL、历史接收延迟和真实账户费率。数值筛选不能替代多重检验、前向纸面跟踪和原有实盘认证。",
            "表格排序仅帮助检查，不将查看后挑出的较好结果伪装成新独立验证。完整下载包含全部56个冻结方案的验证交易与失败信息，开发组合原件保存在研究目录。"],
        source_paths=[str(p) for p in STUDIES],method_version="directional-discovery-report-v1")
    return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--verify-only",action="store_true");args=parser.parse_args()
    report=build_report();digest=report_digest(report)
    print("Verified directional report:",report["development_trials"],"development",report["frozen_count"],"frozen",report["qualified_count"],"qualified",report["trial_count"],"counted trials; bytes",len(json.dumps(report)),flush=True)
    if args.verify_only:return
    with psycopg.connect(read_env(SECRET_DIR/"formal-runtime.env")["GATESCOPE_MIGRATION_DATABASE_URL"]) as conn:
        tenant,user=conn.execute("SELECT t.id,u.id FROM tenants t JOIN memberships m ON m.tenant_id=t.id JOIN users u ON u.id=m.user_id WHERE t.slug='default' AND u.normalized_username='owner' AND m.role='OWNER' AND m.status='ACTIVE' AND u.status='ACTIVE'").fetchone()
        conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.subject_id',%s,true)",(str(tenant),str(user)))
        conn.execute("SET LOCAL ROLE gatescope_strategy")
        row=conn.execute("INSERT INTO research_discovery_reports(id,tenant_id,owner_user_id,name,trial_count,report_json,artifact_digest) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,owner_user_id,artifact_digest) DO NOTHING RETURNING id",(uuid4(),tenant,user,report["title"],report["trial_count"],Jsonb(report),digest)).fetchone()
        print("Published:",row[0] if row else "already exists")


if __name__=="__main__":main()
