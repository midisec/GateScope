"""Notification-worker extension: bounded image work, no Web request blocking."""
from datetime import datetime,timezone
from gatescope.application.notifications import DeliveryOutcome
from gatescope.application.telegram_market import build_market_reply
from gatescope.application.telegram_chart import render_chart
from gatescope.application.telegram_paper import PAPER_READ_ACTIONS


class TelegramMarketRuntime:
    def __init__(self,repository,sender_for_tenant,worker_id,subject):
        self.repository=repository;self.sender_for_tenant=sender_for_tenant;self.worker_id=worker_id;self.subject=subject

    def run_once(self,maximum=2):
        done=0;repo=self.repository
        for tenant in repo.due_tenants():
            if done>=maximum:break
            row=repo.claim(tenant,self.worker_id,self.subject)
            if not row:continue
            identifier,user,connection,command,attempt,created,response=row
            try:
                chat=repo.recipient(tenant,user,connection)
                if not chat:outcome=DeliveryOutcome(False,None,'TG_BINDING_REVOKED')
                elif (datetime.now(timezone.utc)-created).total_seconds()>900:
                    outcome=DeliveryOutcome(False,None,'TG_QUERY_EXPIRED')
                else:
                    if response is None or command.get('action')=='opportunities' or command.get('action') in PAPER_READ_ACTIONS:
                        acceptance=bool(response and response.get('acceptance_test'))
                        try:response=build_market_reply(repo,tenant,user,identifier,command)
                        except ValueError:
                            response={'text':'当前图表数据尚未就绪或该候选暂不可用，请稍后通过 /watchlist 重试。','reply_markup':{'inline_keyboard':[]},'actions':[],'chart':None}
                        if acceptance:
                            response['text']='部署验收 · 只读测试（不下单）\n'+response['text']
                            response['acceptance_test']=True
                        repo.save_response(tenant,user,identifier,self.worker_id,response)
                    # Recheck binding and watchlist ownership on retries as well.
                    if command.get('action')=='signal' and repo.owned_signal(tenant,user,command.get('event_id')) is None:
                        response={'text':'该通知事件不可用，或不属于你的账户。','reply_markup':{'inline_keyboard':[]},'actions':[],'chart':None}
                        repo.save_response(tenant,user,identifier,self.worker_id,response)
                    if command.get('action') not in ('watchlist','opportunities','signal') and command.get('action') not in PAPER_READ_ACTIONS and repo.owned_instrument(tenant,user,command.get('instrument_id'),command.get('symbol')) is None:
                        response={'text':'该币种已移出你的自选，请通过 /watchlist 重新选择。','reply_markup':{'inline_keyboard':[]},'actions':[],'chart':None}
                        repo.save_response(tenant,user,identifier,self.worker_id,response)
                    if command.get('action')=='opportunities':
                        owned,_=repo.watchlist(tenant,user,0,1000)
                        if not set(response.get('scope_instruments',[])).issubset({r['id'] for r in owned}):
                            response={'text':'自选列表已变更，请通过 /opportunities 重新筛选。','reply_markup':{'inline_keyboard':[]},'actions':[],'chart':None}
                            repo.save_response(tenant,user,identifier,self.worker_id,response)
                    photo=render_chart(response['chart']) if response.get('chart') else None
                    if not repo.recipient(tenant,user,connection):outcome=DeliveryOutcome(False,None,'TG_BINDING_REVOKED')
                    else:outcome=self.sender_for_tenant(tenant).send_market(chat,response['text'],response['reply_markup'],photo)
            except Exception:
                # Never put a requests exception (which embeds Bot token URLs) in logs.
                outcome=DeliveryOutcome(False,5,'TG_QUERY_PROCESSING_FAILED')
            repo.finish(tenant,user,identifier,self.worker_id,outcome,attempt);done+=1
        return done
