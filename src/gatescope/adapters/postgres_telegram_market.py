"""Per-user watchlist authorization and crash-safe Telegram query queue."""
from datetime import datetime,timezone
from uuid import UUID,uuid4

from psycopg.types.json import Jsonb

from gatescope.adapters.postgres_market import PostgresMarketReadDatabase
from gatescope.adapters.postgres_structure_archive import StructureArchive,context_id
from gatescope.adapters.postgres_pattern_studies import snapshot
from gatescope.quant.structure_detection import VERSION
from gatescope.domain.market import TIMEFRAME_SECONDS
from gatescope.application.telegram_market import PHASES, STATIC_ACTIONS, signal_callback
from gatescope.application.telegram_paper import paper_callback


class TelegramMarketRepository:
    def __init__(self,pool):self.pool=pool;self.reader=PostgresMarketReadDatabase(pool)

    def enqueue(self,principal,update_id,chat_id,command=None,callback=None):
        with self.pool.transaction(principal.tenant_id,principal.user_id) as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'tg-market:{principal.tenant_id}:{principal.user_id}',))
            if c.execute('SELECT 1 FROM telegram_market_requests WHERE tenant_id=%s AND update_id=%s',(principal.tenant_id,update_id)).fetchone():return 'DUPLICATE'
            connection=c.execute("SELECT id FROM telegram_connections WHERE tenant_id=%s AND user_id=%s AND telegram_chat_id=%s AND status='ACTIVE'",(principal.tenant_id,principal.user_id,chat_id)).fetchone()
            if not connection:return 'UNBOUND'
            if callback in STATIC_ACTIONS:
                command=dict(STATIC_ACTIONS[callback])
            elif paper_callback(callback):
                # Resolve only in the owner-scoped repository at execution. A
                # guessed/forwarded identifier must never grant read authority.
                command=paper_callback(callback)
            elif signal_callback(callback):
                command=signal_callback(callback)
                if not self._signal_row(c,principal.tenant_id,principal.user_id,UUID(command['event_id'])):
                    return 'EXPIRED'
            elif callback:
                _,parent,index=callback.split(':');index=int(index)
                row=c.execute("SELECT response_json FROM telegram_market_requests WHERE id=%s AND tenant_id=%s AND user_id=%s AND connection_id=%s AND created_at>now()-interval '24 hours'",(UUID(parent),principal.tenant_id,principal.user_id,connection[0])).fetchone()
                actions=(row[0] or {}).get('actions',[]) if row else []
                if index>=len(actions):return 'EXPIRED'
                command=actions[index]
            if not command:return 'EXPIRED'
            count=c.execute("SELECT count(*) FROM telegram_market_requests WHERE user_id=%s AND created_at>now()-interval '1 minute'",(principal.user_id,)).fetchone()[0]
            if count>=12:return 'RATE_LIMITED'
            c.execute('INSERT INTO telegram_market_requests(id,tenant_id,user_id,connection_id,update_id,command_json) VALUES(%s,%s,%s,%s,%s,%s)',(uuid4(),principal.tenant_id,principal.user_id,connection[0],update_id,Jsonb(command)))
        return 'QUEUED'

    def paper_read(self,tenant,user,command):
        from gatescope.adapters.postgres_paper import PostgresPaperRepository
        from gatescope.adapters.postgres_paper_orders import PostgresPaperOrderRepository
        accounts=PostgresPaperRepository(self.pool);orders=PostgresPaperOrderRepository(self.pool)
        action=command['action'];offset=int(command.get('offset',0))
        if not 0<=offset<=10000:raise ValueError('invalid paper page')
        account=UUID(str(command['account_id'])) if command.get('account_id') else None
        if action=='paper_accounts':
            items=accounts.list_accounts(tenant,user)
            return {'items':[{'id':str(a.id),'name':a.name} for a in items[offset:offset+5]],'has_more':offset+5<len(items)}
        if action=='paper_account':return orders.account_snapshot(tenant,user,account)
        if action in ('paper_order','paper_chart'):return orders.get_order(tenant,user,UUID(str(command['order_id'])))
        if action=='paper_positions':
            state=command.get('state','OPEN')
            if state not in ('OPEN','CLOSED'):raise ValueError('invalid paper position state')
            return orders.list_positions(tenant,user,account_id=account,status=state,limit=5,offset=offset)
        if action=='paper_orders':
            result=orders.list_orders(tenant,user,account_id=account,limit=5,offset=offset)
            if offset==0 and account is None:
                result['legacy']=[{'public_trade_id':t.public_trade_id,'symbol':t.symbol,'side':t.side,'state':t.state}
                                  for t in accounts.list_trades(tenant,user,limit=5)]
            return result
        raise ValueError('unsupported paper read action')

    @staticmethod
    def _signal_row(c,tenant,user,event):
        # An opaque UUID is a lookup key, never authorization. Keep explicit
        # owner and tenant predicates even while the database enforces RLS.
        return c.execute("""SELECT f.id,f.instrument_id,f.timeframe,f.payload_json,i.symbol
            FROM alert_event_facts f JOIN alert_rules r ON r.id=f.alert_rule_id AND r.tenant_id=f.tenant_id
            JOIN instruments i ON i.id=f.instrument_id
            WHERE f.id=%s AND f.tenant_id=%s AND r.owner_user_id=%s""",(event,tenant,user)).fetchone()

    def owned_signal(self,tenant,user,event):
        try:identifier=UUID(str(event))
        except (ValueError,TypeError,AttributeError):return None
        with self.pool.transaction(tenant,user) as c:
            row=self._signal_row(c,tenant,user,identifier)
        return {'id':str(row[0]),'instrument_id':str(row[1]),'timeframe':row[2],
            'payload':row[3],'symbol':row[4]} if row else None

    def signal_chart(self,signal,*,latest=False):
        """Render the exact notified journal revision, not first/latest lookalikes.

        Later-state lookup may append future candles but never replace original
        geometry, prices, archived OHLC revisions or the frozen trade plan.
        """
        from gatescope.application.pattern_trade_notice import render_pattern_trade_notice
        from gatescope.application.telegram_chart import chart_spec,stamp
        items=[item for item in signal['payload'].get('matched',[]) if isinstance(item,dict)]
        item=next((item for item in items if isinstance(item.get('source_event'),dict)),{})
        event=item.get('source_event',{})
        notice=item.get('trade_notice')
        plan=render_pattern_trade_notice(notice) if isinstance(notice,dict) else '旧通知未冻结做单计划，不补写历史。'
        heading=f"{signal['symbol']} · {signal['timeframe']} · 通知事件 {signal['id']}"
        frozen_note='通知原始计划保留；仅形态研究，未下单，形态状态不等于实际持仓或止盈止损结果。'
        if len(items)>1:frozen_note='本图仅展示本通知的第一个已归档形态。'+frozen_note
        unavailable=(None,'\n'.join((heading,plan,'该通知的精确原始图暂不可用，不使用首次识别图或最新形态代替。',frozen_note)))
        keys=(event.get('context_id'),event.get('id'),event.get('evidence_id'))
        import re
        if any(not isinstance(key,str) or re.fullmatch(r'[a-f0-9]{64}',key) is None for key in keys):return unavailable
        tf=signal['timeframe'];iid=UUID(signal['instrument_id'])
        if tf not in TIMEFRAME_SECONDS:return unavailable
        try:
            with self.pool.public_transaction() as c:
                c.execute("SET LOCAL statement_timeout='4000ms'")
                row=c.execute("""SELECT gatescope_security.structure_event_record(event_json,event_blob) FROM market_pattern_events
                    WHERE context_id=%s AND candidate_id=%s AND instrument_id=%s AND timeframe=%s
                      AND version=%s AND source_mode='RECORDED' AND event_json->'latest'->>'evidence_id'=%s
                    ORDER BY sequence LIMIT 1""",(keys[0],keys[1],iid,tf,event.get('detector_version'),keys[2])).fetchone()
                if not row:return unavailable
                from gatescope.quant.structure_archive_codec import unpack_structure_record
                record=unpack_structure_record(row[0]);original=record['latest']
                # The source event may include metadata added outside the journal;
                # all actual geometry must still equal the archived transition.
                if any(event.get(key)!=original.get(key) for key in ('points','lines','levels','phase','pattern')):
                    return unavailable
                boundary=datetime.fromtimestamp(record['observed_time'],timezone.utc)
                as_of=datetime.fromisoformat(record['snapshot_at'])
                bars,_=snapshot(c,iid,tf,600,as_of,end_time=boundary,strict=True)
                if not bars or bars[-1]['ts']+TIMEFRAME_SECONDS[tf]!=record['observed_time']:return unavailable
                known=[p['time'] for p in original.get('points',[]) if p.get('label') not in ('✓','2?','E')]
                if known and min(known)<bars[0]['ts']:return unavailable
                mode='通知时原图 · 冻结形态线与做单计划'
                state_text=f"通知时：{PHASES.get(record['phase'],record['phase'])} · {stamp(record['observed_time'])}"
                if latest:
                    state=c.execute("""SELECT phase,event_json->>'observed_time' FROM market_pattern_events
                        WHERE context_id=%s AND candidate_id=%s AND instrument_id=%s AND timeframe=%s
                          AND version=%s AND source_mode='RECORDED' ORDER BY sequence DESC LIMIT 1""",
                        (keys[0],keys[1],iid,tf,event.get('detector_version'))).fetchone()
                    if state:state_text+=f"\n最新归档：{PHASES.get(state[0],state[0])} · {stamp(int(state[1]))}"
                    after,_=snapshot(c,iid,tf,600,datetime.now(timezone.utc),strict=False)
                    future=[b for b in after if b['ts']>=record['observed_time']]
                    combined=bars+future
                    if any(b['ts']-a['ts']!=TIMEFRAME_SECONDS[tf] for a,b in zip(combined,combined[1:])):
                        state_text+='\n后续 K 线缓存不连续，本次只展示通知原图。'
                    else:
                        bars=combined
                        mode='通知原始形态线 + 后续已收盘行情'
                chart=chart_spec(signal['symbol'],tf,bars,original,mode,layout='PATTERN')
            return chart,'\n'.join((heading,state_text,plan,frozen_note))
        except (ValueError,KeyError,TypeError,OverflowError):
            return unavailable

    def watchlist(self,tenant,user,offset,limit):
        with self.pool.transaction(tenant,user) as c:
            rows=c.execute("""SELECT DISTINCT i.id,i.symbol,wi.default_timeframe FROM watchlist_items wi
              JOIN watchlists w ON w.id=wi.watchlist_id JOIN instruments i ON i.id=wi.instrument_id
              WHERE w.tenant_id=%s AND w.owner_user_id=%s AND w.deleted_at IS NULL ORDER BY i.symbol,i.id,wi.default_timeframe""",(tenant,user)).fetchall()
        unique={}
        for r in rows:unique.setdefault(str(r[0]),{'id':str(r[0]),'symbol':r[1],'timeframe':r[2]})
        return list(unique.values())[offset:offset+limit],len(unique)

    def owned_instrument(self,tenant,user,identifier=None,symbol=None):
        # Own watchlists only, not every instrument in the shared public catalog.
        rows,_=self.watchlist(tenant,user,0,1000)
        normalized=(symbol or '').replace('/','_').replace('-','_')
        matches=[r for r in rows if r['id']==identifier] if identifier else [r for r in rows if normalized in (r['symbol'],r['symbol'].split('_')[0])]
        return matches[0] if len(matches)==1 else None

    def tracking(self,instrument,tf):
        with self.pool.public_transaction() as c:
            row=c.execute('SELECT checkpoint_json FROM market_pattern_tracking WHERE instrument_id=%s AND timeframe=%s AND version=%s',(UUID(instrument),tf,VERSION)).fetchone()
        return row[0] if row else {}

    def cache_status(self,instrument,tf):return self.reader.cache_status(instrument,tf)

    def opportunities(self,tenant,user,coin_offset=0):
        """Two coins / sixteen periods per page; never scan the shared catalog.

        JSON projection avoids moving multi-megabyte replay checkpoints into the
        notification worker. All statistics stop BEFORE the candidate's earliest
        anchor, excluding both its own replay and overlapping anatomy.
        """
        from gatescope.quant.structure_screening import HORIZON, historical_stats, usable, evaluate
        items,total=self.watchlist(tenant,user,coin_offset,2)
        now=datetime.now(timezone.utc);now_ts=int(now.timestamp())
        result={'items':[], 'total_coins':total,'coins':items,'coin_offset':coin_offset,
            'as_of':now_ts,'fresh_pairs':0,'pending_pairs':len(items)*8,'unusable':0,'study_states':{}}
        if not items:return result
        names={i['id']:i['symbol'] for i in items}
        with self.pool.public_transaction() as c:
            c.execute("SET LOCAL statement_timeout='4000ms'")
            rows=c.execute("""SELECT t.instrument_id,t.timeframe,(t.checkpoint_json->>'last_ts')::bigint,
                s.requested_at,s.state,COALESCE(s.cursor,0),COALESCE(s.total,0),
                COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id',r.value->'id','pattern',r.value->'pattern','phase',r.value->'phase',
                    'observed_time',r.value->'observed_time','deadline_time',r.value->'deadline_time',
                    'first_observed_time',r.value->'first_observed_time',
                    'start_time',r.value->'first'->'start_time',
                    'latest',jsonb_build_object('name',r.value->'latest'->'name',
                        'confidence',r.value->'latest'->'confidence','levels',r.value->'latest'->'levels',
                        'signal_direction',r.value->'latest'->'signal_direction')))
                  FROM jsonb_each(COALESCE(t.checkpoint_json->'records','{}')) r
                  WHERE r.value->>'phase' IN ('FORMING','FORMED','CONFIRMED')),'[]'::jsonb)
                FROM market_pattern_tracking t
                JOIN market_sync_state m ON m.instrument_id=t.instrument_id AND m.timeframe=t.timeframe
                LEFT JOIN market_pattern_studies s ON s.instrument_id=t.instrument_id AND s.timeframe=t.timeframe AND s.version=t.version
                WHERE t.instrument_id=ANY(%s) AND t.version=%s AND t.safe_error IS NULL
                  AND m.active AND m.live_error_code IS NULL AND m.last_live_at>=%s-interval '45 seconds'
                ORDER BY t.instrument_id,t.timeframe""",([UUID(i['id']) for i in items],VERSION,now)).fetchall()
            for iid,tf,last_ts,requested,study_state,cursor,study_total,records in rows:
                step=TIMEFRAME_SECONDS[tf];closed=now_ts//step*step
                if last_ts is None or last_ts+step!=closed:continue
                bars,_=snapshot(c,iid,tf,4,now,strict=False)
                if not bars or bars[-1]['ts']!=last_ts:continue
                result['fresh_pairs']+=1;result['pending_pairs']-=1
                result['study_states'][study_state or 'WAITING']=result['study_states'].get(study_state or 'WAITING',0)+1
                live=c.execute('SELECT open_time,high,low,close FROM market_live_candles WHERE instrument_id=%s AND timeframe=%s AND observed_at>=%s-interval \'45 seconds\' AND close_time>%s',(iid,tf,now,now)).fetchone()
                veto=([{'ts':int(live[0].timestamp()),'high':float(live[1]),'low':float(live[2]),'close':float(live[3])}] if live else [])
                candidates=[]
                for record in records:
                    # A confirmed candidate must also have survived every later
                    # cached closed candle, not just today's recovered price.
                    checks=([b for b in bars if b['ts']+step>record['observed_time']] if record['phase']=='CONFIRMED' else [bars[-1]])+veto
                    if not usable(record,bars[-1],closed,step) or any(not usable(record,b,closed,step) for b in checks):
                        result['unusable']+=1;continue
                    candidates.append(record)
                stats={}
                groups=[{'id':r['id'],'pattern':r['pattern'],'phase':r['phase'],
                    'side':1 if r['latest']['signal_direction']=='bullish' else -1,
                    'cutoff':min(r['first_observed_time'],r.get('start_time') or r['first_observed_time'],closed)}
                    for r in candidates if r['phase']!='FORMING' and r['latest']['signal_direction'] in ('bullish','bearish')]
                if requested and groups:
                    # COMPLETE alone contains entry-selected nonoverlapping cases;
                    # no merging model versions, horizons, sides or entry stages.
                    values=c.execute("""SELECT g.id,count(r.evidence_id),count(*) FILTER(WHERE r.net_return>0),avg(r.net_return)
                        FROM jsonb_to_recordset(%s) AS g(id text,pattern text,phase text,side integer,cutoff bigint)
                        LEFT JOIN market_structure_returns r ON r.context_id=%s AND r.pattern=g.pattern AND r.phase=g.phase
                          AND r.side=g.side AND r.horizon=%s AND r.status='COMPLETE'
                          AND r.observed_time>=%s AND r.exit_time<g.cutoff AND r.entry_time>=r.observed_time
                          AND r.exit_time>r.entry_time AND r.net_return>'-Infinity'::float8 AND r.net_return<'Infinity'::float8
                        GROUP BY g.id""",(Jsonb(groups),context_id(iid,tf,requested),HORIZON,closed-365*86400)).fetchall()
                    cutoffs={g['id']:g['cutoff'] for g in groups}
                    stats={key:historical_stats(w,n,mean,total,timeframe_seconds=step,
                        history_seconds=max(0,cutoffs[key]-(closed-365*86400))) for key,n,w,mean in values}
                for r in candidates:
                    cutoff=min(r['first_observed_time'],r.get('start_time') or r['first_observed_time'],closed)
                    evidence=stats.get(r['id']) or historical_stats(0,0,None,total,timeframe_seconds=step,
                        history_seconds=max(0,cutoff-(closed-365*86400)))
                    result['items'].append({'instrument_id':str(iid),'symbol':names[str(iid)],'timeframe':tf,
                        'candidate':r['id'],'name':r['latest']['name'],'phase':r['phase'],
                        'direction':r['latest']['signal_direction'],'geometry':r['latest']['confidence'],
                        'observed_time':r['observed_time'],'study_state':study_state or 'WAITING',
                        'study_cursor':cursor,'study_total':study_total,**evaluate(r,evidence)})
        result['items'].sort(key=lambda r:(r['qualified'],(r['statistics']['interval'] or {}).get('lower',-1),
            r['statistics']['sample_count'],r['observed_time'],r['candidate']),reverse=True)
        return result

    def overview_chart(self,instrument,tf,*,patterns_only=False):
        from gatescope.application.telegram_chart import chart_spec
        with self.pool.public_transaction() as c:bars,_=snapshot(c,UUID(instrument['id']),tf,600,datetime.now(timezone.utc),strict=False)
        if not bars:raise ValueError('MARKET_HISTORY_EMPTY')
        return chart_spec(instrument['symbol'],tf,bars,None,
            '形态视图 · 点击形态按钮查看对应原理线' if patterns_only else '已收盘行情 · EMA20/60 / BOLL20 / RSI14 / MACD',
            layout='PATTERN' if patterns_only else 'INDICATORS')

    def evidence_chart(self,instrument,tf,candidate,initial):
        from gatescope.application.telegram_chart import chart_spec,stamp
        detail=StructureArchive(self.pool).detail(context_id(instrument['id'],tf),candidate)
        if not detail:
            # Buttons issued before a model upgrade still refer to their frozen
            # candidate. Only search the same owned instrument/period/source.
            with self.pool.public_transaction() as c:
                versions=c.execute('SELECT version FROM market_pattern_tracking WHERE instrument_id=%s AND timeframe=%s ORDER BY requested_at DESC LIMIT 12',(UUID(instrument['id']),tf)).fetchall()
            for (version,) in versions:
                if version==VERSION:continue
                detail=StructureArchive(self.pool).detail(context_id(instrument['id'],tf,version=version),candidate)
                if detail:break
        if not detail:raise ValueError('MARKET_CANDIDATE_UNAVAILABLE')
        records=detail['transitions'];record=records[0 if initial else -1]['record']
        event=record['first'] if initial else record['latest']
        bars=detail['bars']
        if initial:bars=[b for b in bars if b['ts']<record['first_observed_time']]
        else:bars=[b for b in bars if b['ts']<record['observed_time']] if record['phase'] not in ('FORMING','FORMED') else bars
        mode='首次识别 · 原始 K 线' if initial else '最新状态 · 原始 K 线 + 后续行情'
        chart=chart_spec(instrument['symbol'],tf,bars,event,mode)
        forecast=record['first'].get('forecast') or {};target=forecast.get('target') or {}
        lines=[f"{instrument['symbol']} · {tf} · {event['name']}",f"{PHASES[record['phase']]} · {mode}",f"首次识别 {stamp(record['first_observed_time'])}",f"状态时间 {stamp(record['observed_time'])}",event.get('stage','')]
        if target.get('price') is not None:lines.append(f"首次预测点位 {target['price']:.8g} · 目标区 {target.get('lower',target['price']):.8g}–{target.get('upper',target['price']):.8g}")
        prediction=record.get('stages',{}).get('FORMING',{}).get('prediction',{})
        if not prediction:prediction=next(iter(record.get('stages',{}).values()),{}).get('prediction',{})
        score=event.get('confidence')
        lines.append(f"几何匹配 {score*100:.0f} 分（不是概率）" if score is not None else '几何未完成 · 不评分')
        for name,stats in (('形成',prediction.get('formation')),('突破确认',prediction)):
            if stats is None:continue
            n=stats.get('sample_count',0);value=stats.get('probability');interval=stats.get('interval')
            label=f'{value:.1%}' if value is not None and n>=20 else '样本不足'
            lines.append(f"首次判断时{name}概率 {label} · n={n}"+(f" · 95%校正区间 {interval['lower']:.1%}–{interval['upper']:.1%}" if interval else ''))
        lines.extend([chart['summary'],'原始图和原理线保留；确认率不等于交易盈利率。'])
        return chart,'\n'.join(lines)

    def due_tenants(self):
        with self.pool.public_transaction() as c:return [r[0] for r in c.execute('SELECT tenant_id FROM gatescope_security.telegram_market_due_tenants()').fetchall()]

    def claim(self,tenant,worker,subject):
        with self.pool.transaction(tenant,subject) as c:
            # These are temporary UI jobs, NOT immutable candidate evidence.
            c.execute("DELETE FROM telegram_market_requests WHERE id IN (SELECT id FROM telegram_market_requests WHERE created_at<now()-interval '7 days' ORDER BY created_at LIMIT 100)")
            row=c.execute("""WITH picked AS (SELECT id FROM telegram_market_requests
              WHERE status IN ('PENDING','SENDING') AND next_attempt_at<=now()
                AND (lease_expires_at IS NULL OR lease_expires_at<now()) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
              UPDATE telegram_market_requests r SET status='SENDING',attempt_count=attempt_count+1,
                lease_owner=%s,lease_expires_at=now()+interval '60 seconds',updated_at=now()
              FROM picked p WHERE r.id=p.id RETURNING r.id,r.user_id,r.connection_id,r.command_json,r.attempt_count,r.created_at,r.response_json""",(worker,)).fetchone()
        return row

    def recipient(self,tenant,user,connection):
        with self.pool.transaction(tenant,user) as c:
            row=c.execute("""SELECT tc.telegram_chat_id FROM telegram_connections tc
              JOIN users u ON u.id=tc.user_id JOIN memberships m ON m.user_id=u.id AND m.tenant_id=tc.tenant_id
              WHERE tc.id=%s AND tc.user_id=%s AND tc.tenant_id=%s AND tc.status='ACTIVE' AND u.status='ACTIVE' AND m.status='ACTIVE'""",(connection,user,tenant)).fetchone()
        return row[0] if row else None

    def save_response(self,tenant,user,identifier,worker,response):
        with self.pool.transaction(tenant,user) as c:
            c.execute("UPDATE telegram_market_requests SET response_json=%s WHERE id=%s AND lease_owner=%s AND status='SENDING'",(Jsonb(response),identifier,worker))

    def finish(self,tenant,user,identifier,worker,outcome,attempt):
        retry=not outcome.sent and outcome.retry_after_seconds is not None and attempt<4
        with self.pool.transaction(tenant,user) as c:
            c.execute("""UPDATE telegram_market_requests SET status=%s,safe_error=%s,telegram_message_id=%s,
              lease_owner=NULL,lease_expires_at=NULL,updated_at=now(),next_attempt_at=now()+make_interval(secs=>%s),
              response_json=CASE WHEN %s THEN response_json-'chart' ELSE response_json END
              WHERE id=%s AND lease_owner=%s""",('SENT' if outcome.sent else 'PENDING' if retry else 'FAILED',outcome.safe_error_code,outcome.telegram_message_id,
                min(3600,max(2,outcome.retry_after_seconds or 2)),not retry,identifier,worker))
