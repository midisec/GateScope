"""Compile pure guards only: do not import browser/credential/deployment code."""
import ast
import json
import math
import traceback
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import pytest

ORIGIN="https://example.test"
ACCOUNT,INSTRUMENT,KEY=(str(UUID(int=value)) for value in (1,2,3))
ROOT=Path(__file__).resolve().parents[2]


def function(filename,name,namespace):
    source=ROOT/"scripts"/filename
    tree=ast.parse(source.read_text())
    node=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),"exec"),namespace)
    return namespace[name]


@pytest.fixture
def fence():
    original=function("smoke_paper_browser.py","allowed_request",{"urlsplit":urlsplit})
    namespace={"urlsplit":urlsplit,"existing_allowed_request":original,"UUID":UUID,"Decimal":Decimal,
               "PREVIEW_PATH":"/api/v2/paper/orders:preview"}
    predicate=function("smoke_optimization_browser.py","allowed_request",namespace)
    return lambda url,method,payload=None:predicate(ORIGIN,url,method,payload,account_id=ACCOUNT,instrument_id=INSTRUMENT)


def preview(**changes):
    return {"account_id":ACCOUNT,"instrument_id":INSTRUMENT,"timeframe":"15m","side":"LONG",
            "order_type":"MARKET","contracts":1,"time_in_force":"IOC","stop_price":"98.00",
            "take_profit_price":"104.00","max_slippage_bps":"50","ttl_seconds":60,
            "idempotency_key":KEY,**changes}


def test_only_exact_dedicated_one_contract_preview_is_permitted(fence):
    assert fence(ORIGIN+"/api/v2/paper/orders:preview","POST",preview())
    assert fence(ORIGIN+"/api/v2/research/portfolios?limit=1&offset=0","GET")
    assert fence(ORIGIN+"/auth/login","POST",{"encrypted_submission":{"ciphertext":"fixture"}})
    assert fence(ORIGIN+"/auth/mfa/verify","POST",{"code":"000000"})
    assert fence(ORIGIN+"/auth/logout","POST")


@pytest.mark.parametrize("changes",[
    {"account_id":str(UUID(int=8))},{"instrument_id":str(UUID(int=9))},{"contracts":2},{"contracts":True},
    {"contracts":"1"},{"side":"SHORT"},{"order_type":"LIMIT"},{"time_in_force":"GTC"},
    {"timeframe":"1m"},{"ttl_seconds":3600},{"ttl_seconds":True},{"max_slippage_bps":"100"},
    {"stop_price":"NaN"},{"take_profit_price":"Infinity"},{"stop_price":"105"},
    {"stop_price":"0"},{"take_profit_price":"1000000000"},{"idempotency_key":"not-a-uuid"},
    {"decision_context":{}},{"confirm_paper":True},{"encrypted_submission":{}},
])
def test_unexpected_parameters_do_not_expand_preview_authority(fence,changes):
    assert not fence(ORIGIN+"/api/v2/paper/orders:preview","POST",preview(**changes))


@pytest.mark.parametrize("path",[
    "/api/v2/paper/orders","/api/v2/paper/orders/x:cancel","/api/v2/paper/positions/x:close",
    "/api/v2/paper-accounts","/api/v2/research/portfolios","/api/v2/research/backtests",
    "/api/v2/mcp/clients","/auth/mfa-methods:enroll",
    "/api/v2/paper/orders:preview/","/api/v2/paper/orders:preview?execute=true",
    "/api/v2/paper/orders:preview#fragment","/api/v2/paper/orders%3Apreview",
])
def test_order_mutations_and_research_creation_never_pass(fence,path):
    assert not fence(ORIGIN+path,"POST",preview())
    assert not fence(ORIGIN+path,"PATCH",preview())
    assert not fence(ORIGIN+path,"DELETE",preview())


def test_cross_origin_and_plaintext_credentials_are_rejected(fence):
    assert not fence(ORIGIN+"/auth/login","POST",{"username":"owner","password":"secret"})
    for origin in ("https://external.test","http://example.test","https://user@example.test","https://example.test.evil.test"):
        assert not fence(origin+"/api/v2/paper/orders:preview","POST",preview())
        assert not fence(origin+"/anything","GET")


def test_failure_report_never_includes_raw_http_or_credential_text():
    safe=function("smoke_optimization_browser.py","safe_error",{
        "Path":Path,"traceback":traceback,"__file__":str(ROOT/"scripts/smoke_optimization_browser.py")})
    try:
        raise RuntimeError("RAW_PRIVATE_HTTP_RESPONSE_AND_PASSWORD")
    except RuntimeError as error:
        report=safe(error,"preview")
    assert report=={"step":"preview","type":"RuntimeError","lines":[]}
    assert "PRIVATE" not in json.dumps(report)


@pytest.fixture
def visible_bounds():
    return function("smoke_optimization_browser.py","control_within_bounds",{"math":math})


@pytest.mark.parametrize("control,expected",[
    ({"x":30,"y":850,"width":330,"height":44},True),
    ({"x":30,"y":960,"width":330,"height":44},False),
    ({"x":30,"y":-30,"width":330,"height":44},False),
    ({"x":-40,"y":850,"width":330,"height":44},False),
    ({"x":340,"y":850,"width":100,"height":44},False),
    ({"x":30,"y":915,"width":330,"height":44},False),  # In viewport, clipped by dialog.
    ({"x":30,"y":850,"width":0,"height":44},False),
    ({"x":30,"y":float("nan"),"width":330,"height":44},False),
    (None,False),
])
def test_entire_control_must_be_inside_phone_viewport_and_dialog(visible_bounds,control,expected):
    dialog={"x":14,"y":18,"width":362,"height":924}
    assert visible_bounds(control,dialog,{"width":390,"height":960}) is expected


def test_scroll_reachability_does_not_click_or_submit_disabled_control(visible_bounds):
    calls=[]
    class Control:
        def scroll_into_view_if_needed(self):
            calls.append("scroll")
        def bounding_box(self):
            assert calls==["scroll"]
            return {"x":30,"y":850,"width":330,"height":44}
        def click(self):
            raise AssertionError("SUBMISSION_FORBIDDEN")
    class Dialog:
        def bounding_box(self):
            return {"x":14,"y":18,"width":362,"height":924}
    class Page:
        def evaluate(self,expression):
            assert expression=="({width: innerWidth, height: innerHeight})"
            return {"width":390,"height":960}
    scroll=function("smoke_optimization_browser.py","scroll_control_into_view",{"control_within_bounds":visible_bounds})
    scroll(Page(),Dialog(),Control())
    assert calls==["scroll"]


def test_acceptance_checks_both_submit_controls_without_clicking_them():
    tree=ast.parse((ROOT/"scripts/smoke_optimization_browser.py").read_text())
    run=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=="run_browser")
    checks=[node for node in ast.walk(run) if isinstance(node,ast.Call)
            and isinstance(node.func,ast.Name) and node.func.id=="scroll_control_into_view"]
    assert {node.args[2].id for node in checks}=={"confirm","submit_button"}
    assert not any(isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute)
        and node.func.attr in {"click","press","dispatch_event"} and isinstance(node.func.value,ast.Name)
        and node.func.value.id in {"confirm","submit_button"} for node in ast.walk(run))
