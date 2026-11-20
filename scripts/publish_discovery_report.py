"""Publish verified research findings to the current owner's immutable report area."""
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from prepare_service_envs import read_env,SECRET_DIR
from gatescope.quant.artifacts import report_digest
from gatescope.quant.performance import daily_performance

CARRY=Path("data/research-studies/carry-20260911T041925Z")
MINING=Path("data/research-studies/mining-20260911T035635Z")
SOURCE=Path("data/research-market/binance-carry-20210101-20260911")


def main():
    carry=json.loads((CARRY/"summary.json").read_text())
    mining=json.loads((MINING/"summary.json").read_text())
    source_files=list(SOURCE.glob("*.json"))
    assert len(source_files)==16
    sources={}
    for file in source_files:
        data=json.loads(file.read_text())
        assert hashlib.sha256(json.dumps(data["rows"],separators=(",",":")).encode()).hexdigest()==data["sha256"]
        sources[file.stem]=data["sha256"]
    assert len(carry["results"])==24
    for row in carry["results"]:
        original=json.loads((CARRY/f"{row['symbol']}-{row['family']}-{row['cost_multiplier']}.json").read_text())
        assert report_digest(original)==original["artifact_digest"]==row["artifact_digest"]
        assert original["metrics"]==row["metrics"] and original["screening"]==row["screening"]
        assert all(digest==sources[f"{row['symbol']}-{kind}"] for kind,digest in original["source_digests"].items())
    choices=[]
    names={"calendar":"定期资金费率对冲","payback":"成本回收过滤对冲","basis":"基差触发对冲"}
    for row in carry["results"]:
        if not row["screening"]["shortlisted"] or row["cost_multiplier"]!=1:continue
        original=json.loads((CARRY/f"{row['symbol']}-{row['family']}-1.json").read_text())
        stress=json.loads((CARRY/f"{row['symbol']}-{row['family']}-2.json").read_text())
        assert report_digest(original)==original["artifact_digest"]
        assert report_digest(stress)==stress["artifact_digest"]
        assert abs(sum(t["net_pnl"] for t in original["trades"])-original["metrics"]["net_pnl"])<1e-7
        assert all(v==original["metrics"][k] for k,v in daily_performance(original["equity_curve"]).items())
        annual=[]
        for year in range(2022,2027):
            left=datetime(year,1,1,tzinfo=timezone.utc)
            right=min(datetime(year+1,1,1,tzinfo=timezone.utc),datetime(2026,9,11,tzinfo=timezone.utc))
            points=[p for p in original["equity_curve"] if left<=datetime.fromisoformat(p["time"])<=right]
            if len(points)>1:
                annual.append(dict(year=year,return_fraction=points[-1]["equity"]/points[0]["equity"]-1,**daily_performance(points)))
        choices.append(dict(key=f"{row['symbol']}-{row['family']}",name=names[row["family"]],symbol=row["symbol"],family=row["family"],
            metrics=row["metrics"],screening=row["screening"],stress_metrics=stress["metrics"],stress_screening=stress["screening"],
            annualized_return=(1+row["metrics"]["total_return"])**(365/row["metrics"]["daily_observations"])-1,
            annual=annual,trades=original["trades"],fees=original["fees"],holding_days=original["holding_days"],
            funding_mark_proxy_count=original["funding_mark_proxy_count"],source_digest=original["artifact_digest"],
            deployable=False,notional_fraction=original["notional_fraction"]))
    report=dict(title="多年度策略挖掘 · 资金费率对冲候选",from_time="2022-01-01",to_time="2026-09-11",venue="BINANCE",
        development_trials=mining["development_trials"],development_finalists=len(mining["finalists"]),carry_runs=len(carry["results"]),
        candidates=choices,carry_results=carry["results"],directional_results=mining["finalists"],
        rule_families=2,independent_return_sources=1,deployable_count=0,
        notes=["基础成本达标不等于实盘认证；所有入围方案的双倍成本测试均未通过。",
            "资金费率规则预先固定，但入围选择使用完整2022—2026历史区间；这些是全样本筛选结果，不是独立留出样本通过证明。",
            "三套标的/规则方案共用资金费率收益来源，不能当成三个独立分散风险策略。",
            "现金20%购买现货、80%留作合约保证金，等币数做空永续；不借币、不加杠杆。",
            "每42天真实平掉两腿并支付费用，至少空仓一天后重新评估；不是把连续持仓切分成免费交易。",
            "历史资金费缺少结算标记价时使用同一8h标记价K线开盘作为代理；回撤为8h收盘盯市，不声称捕获盘中最大基差跳变。",
            "收益按整个账户计算，闲置现金收益率设为0，未扣除现金机会成本；高夏普不代表高收益。",
            "未覆盖交易所倒闭、稳定币脱锚、ADL、网络失联成交及实际账户费率；仅可研究，不可经当前单腿执行器下单。",
            "数据来自 Binance 官方公共接口，不代表 Gate 同条件收益；此前方向策略开发组合2304个，冻结12个入围方案均未通过后段验证。"],
        source_paths=[str(CARRY),str(MINING),str(SOURCE)],method_version="discovery-report-v1")
    digest=report_digest(report)
    with psycopg.connect(read_env(SECRET_DIR/"formal-runtime.env")["GATESCOPE_MIGRATION_DATABASE_URL"]) as conn:
        tenant,user=conn.execute("SELECT t.id,u.id FROM tenants t JOIN memberships m ON m.tenant_id=t.id JOIN users u ON u.id=m.user_id WHERE t.slug='default' AND u.normalized_username='owner' AND m.role='OWNER' AND m.status='ACTIVE' AND u.status='ACTIVE'").fetchone()
        conn.execute("SELECT set_config('app.tenant_id',%s,true),set_config('app.subject_id',%s,true)",(str(tenant),str(user)))
        conn.execute("SET LOCAL ROLE gatescope_strategy")
        identifier=uuid4()
        row=conn.execute("INSERT INTO research_discovery_reports(id,tenant_id,owner_user_id,name,trial_count,report_json,artifact_digest) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,owner_user_id,artifact_digest) DO NOTHING RETURNING id",
            (identifier,tenant,user,report["title"],mining["development_trials"]+len(mining["finalists"])*4+len(carry["results"]),Jsonb(report),digest)).fetchone()
        print("Published discovery report:",row[0] if row else "already present","base-qualified",len(choices),"deployable 0")


if __name__=="__main__":main()
