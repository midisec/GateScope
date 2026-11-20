import copy
from uuid import uuid4

import pytest

from gatescope.quant.structure_screening import evaluate,historical_stats,usable
from gatescope.application.telegram_market import market_command,callback_update,build_market_reply,notification_market_buttons


def record():
    return {'id':'a'*64,'phase':'FORMED','deadline_time':20000,'observed_time':10000,
        'latest':{'confidence':.8,'signal_direction':'bullish','levels':{'invalid_side':-1,'invalid_price':90,'buffer':1}}}


def test_screen_requires_actual_net_performance_and_adjusted_uncertainty():
    r=record();stats=historical_stats(800,1000,.01,2)
    assert evaluate(r,stats)['qualified']
    for bad in (historical_stats(20,20,.1,2),historical_stats(550,1000,.1,2),
                historical_stats(80,100,.1,2),historical_stats(800,1000,-.01,2)):
        assert not evaluate(r,bad)['qualified']
    assert historical_stats(8,10,.1,2)['interval'] is None
    assert historical_stats(800,1000,.1,200)['interval']['lower']<stats['interval']['lower']
    for phase in ('FORMING',):
        assert not evaluate({**r,'phase':phase},stats)['qualified']
    for score in (None,.69,float('nan')):
        bad=copy.deepcopy(r);bad['latest']['confidence']=score
        assert not evaluate(bad,stats)['qualified']


def test_current_usable_vetoes_failed_expired_and_stale_confirmed_geometry():
    bar={'ts':10800,'high':110,'low':99,'close':100};r=record()
    assert usable(r,bar,11000,3600)
    for change in ({'phase':'INVALIDATED'},{'deadline_time':11000},{'observed_time':12000},
                   {'phase':'CONFIRMED','observed_time':1}):
        assert not usable({**r,**change},bar,11000,3600)
    assert not usable(r,{**bar,'close':88},11000,3600)
    r['latest']['levels']['hard']=True
    assert not usable(r,{**bar,'low':89},11000,3600)


def test_static_push_buttons_only_open_private_bound_read_menus():
    assert market_command('/opportunities')['action']=='opportunities'
    assert market_command('/patterns BTC 4h')['action']=='pattern_overview'
    assert market_command('/chart BTC 4h')['action']=='overview'
    with pytest.raises(ValueError):market_command('/opportunities BTC')
    q={'update_id':123,'callback_query':{'id':'cb','from':{'id':42},'message':{'chat':{'id':42,'type':'private'}},'data':'gm:opportunities'}}
    assert callback_update(q)[4]=='gm:opportunities'
    q['callback_query']['data']='gm:order'
    with pytest.raises(ValueError):callback_update(q)
    q['callback_query']['data']='gm:opportunities';q['callback_query']['message']['chat']['type']='group'
    with pytest.raises(ValueError):callback_update(q)
    assert len(notification_market_buttons()['inline_keyboard'][0])==2


def test_screen_empty_state_and_pagination_keep_observations_separate():
    r=record();stats=historical_stats(800,1000,.01,2)
    item={'instrument_id':str(uuid4()),'candidate':'a'*64,'symbol':'BTC_USDT','timeframe':'1h','name':'双底',
        'phase':'FORMED','direction':'bullish','geometry':.8,'observed_time':10000,**evaluate(r,stats)}
    result={'items':[dict(item) for _ in range(6)],'coins':[{'id':item['instrument_id'],'symbol':'BTC_USDT'}],
        'total_coins':3,'coin_offset':0,'as_of':1700000000,'pending_pairs':2}
    class Repo:
        def opportunities(self,*a):return result
    reply=build_market_reply(Repo(),uuid4(),uuid4(),uuid4(),{'action':'opportunities'})
    assert '本组达标 6' in reply['text'] and len(reply['text'])<=4000
    assert sum(a['action']=='detail' for a in reply['actions'])==5
    assert any(a.get('coin_offset')==2 for a in reply['actions'])
    assert reply['scope_instruments']==[item['instrument_id']]
    result['items']=[{**item,'qualified':False,'reasons':['SAMPLES']}]
    reply=build_market_reply(Repo(),uuid4(),uuid4(),uuid4(),{'action':'opportunities'})
    assert '没有达标' in reply['text'] and not any(a['action']=='detail' for a in reply['actions'])
    reply=build_market_reply(Repo(),uuid4(),uuid4(),uuid4(),{'action':'opportunities','mode':'observe','offset':999})
    assert '未达标：历史有效样本不足' in reply['text'] and '1. BTC_USDT' in reply['text']


def test_regular_notification_contains_screen_entry_without_running_scanner():
    from gatescope.application.notifications import NotificationDispatcher,DeliveryOutcome
    class Sender:
        def send(self,chat,text,*,reply_markup):
            assert reply_markup==notification_market_buttons()
            return DeliveryOutcome(True,None,None,1)
    dispatcher=object.__new__(NotificationDispatcher)
    dispatcher._sender_for_tenant=lambda t:Sender()
    dispatcher._due_tenants=lambda name,limit:[] if name=='notification_outbox_due_tenants' else [uuid4()]
    from gatescope.application.notifications import ClaimedDelivery
    from datetime import datetime,timezone
    delivery=ClaimedDelivery(uuid4(),uuid4(),uuid4(),'notification.requested.v1',1,42,
        {'matched':[],'timeframe':'1h'},occurred_at=datetime.now(timezone.utc))
    dispatcher._claim_deliveries=lambda t,n:[delivery]
    dispatcher._recipient_active=lambda message:True
    completed=[];dispatcher._complete=lambda d,o:completed.append(o)
    assert dispatcher.run_once()==1 and completed[0].sent
