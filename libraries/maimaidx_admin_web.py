"""内置管理 WebUI 与 JSON API。

WebUI 默认独立监听配置端口，也可挂载在 NoneBot FastAPI Driver 上。
所有数据 API 强制 Bearer Token。
页面不加载第三方脚本，不向浏览器返回二维码或任何 Token 原文。
"""

from __future__ import annotations

import asyncio
import hmac
import threading
import time
from typing import Optional

from ..config import driver, log, maiconfig
from .maimaidx_account_db import account_db
from .maimaidx_admin_audit import admin_audit
from .maimaidx_break import DEFAULT_CONFIG, break_db
from .maimaidx_qq_bind import qq_bind_db


_REGISTERED = False
_WEB_APP = None
_WEB_SERVER = None
_WEB_THREAD: Optional[threading.Thread] = None


def _mask(value: str, head: int = 4, tail: int = 3) -> str:
    if not value:
        return ""
    if len(value) <= head + tail:
        return "*" * len(value)
    return value[:head] + "…" + value[-tail:]


def _resolve_user_id(user_id: str) -> tuple[str, str]:
    """Resolve an official QQ openid to its bound legacy QQ number."""
    requested = str(user_id).strip()
    if requested.isdigit():
        return requested, requested
    mapped = qq_bind_db.get_legacy_qq(requested)
    return (str(mapped), requested) if mapped is not None else (requested, requested)


_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>maimaiDX QueryBot 管理台</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#151c32;--line:#29334f;--text:#edf2ff;--muted:#9cabc9;--accent:#7aa2ff;--bad:#ff7b86;--ok:#68d391}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,-apple-system,sans-serif}
header{position:sticky;top:0;background:#10172acc;padding:14px 22px;border-bottom:1px solid var(--line);backdrop-filter:blur(12px);z-index:2}
h1{font-size:19px;margin:0 0 10px}.auth{display:flex;gap:8px;max-width:760px}input,button,select,textarea{background:#0e1528;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 10px}input{flex:1}textarea{width:100%;min-height:130px}button{cursor:pointer}button:hover{border-color:var(--accent)}
nav{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.active{background:var(--accent);color:#071127}
main{padding:20px;max-width:1500px;margin:auto}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px}.metric{font-size:28px;font-weight:700;margin-top:8px}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted)}.toolbar{display:flex;gap:8px;margin:0 0 12px}.muted{color:var(--muted)}.bad{color:var(--bad)}.ok{color:var(--ok)}pre{white-space:pre-wrap;word-break:break-word;background:#080d1a;padding:12px;border-radius:8px;max-height:500px;overflow:auto}.hidden{display:none}
</style></head>
<body><header><h1>maimaiDX QueryBot 管理台</h1><div class="auth"><input id="token" type="password" placeholder="管理 Bearer Token"><button onclick="saveToken()">保存并刷新</button></div>
<nav id="nav"><button data-view="dashboard">概览</button><button data-view="users">用户</button><button data-view="traces">REF 链路</button><button data-view="groups">群组</button><button data-view="messages">消息排行</button><button data-view="economy">BREAK 报表</button><button data-view="agreement">用户协议</button></nav></header><main id="app">请输入管理 Token。</main>
<script>
const base=location.pathname.replace(/\/$/,'')+'/api';let token=localStorage.getItem('maidx_admin_token')||'';document.querySelector('#token').value=token;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(path,opt={}){const r=await fetch(base+path,{...opt,headers:{'Authorization':'Bearer '+token,'Content-Type':'application/json',...(opt.headers||{})}});if(!r.ok)throw new Error((await r.text())||r.status);return r.json()}
function saveToken(){token=document.querySelector('#token').value.trim();localStorage.setItem('maidx_admin_token',token);show('dashboard')}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>show(b.dataset.view));
async function show(view){document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===view));const app=document.querySelector('#app');app.innerHTML='加载中…';try{if(view==='dashboard')await dashboard(app);if(view==='users')await users(app);if(view==='traces')await traces(app);if(view==='groups')await groups(app);if(view==='messages')await messages(app);if(view==='economy')await economy(app);if(view==='agreement')await agreement(app)}catch(e){app.innerHTML='<p class="bad">'+esc(e.message)+'</p>'}}
async function dashboard(app){const d=await api('/summary');app.innerHTML='<div class="cards">'+Object.entries(d).map(([k,v])=>`<div class="card"><div class="muted">${esc(k)}</div><div class="metric">${esc(v)}</div></div>`).join('')+'</div><h2>命令调用（7日）</h2>'+table(await api('/commands?days=7'))}
function table(rows,actions=''){if(!rows.length)return '<p class="muted">暂无数据</p>';const keys=Object.keys(rows[0]);return '<table><thead><tr>'+keys.map(k=>'<th>'+esc(k)+'</th>').join('')+(actions?'<th>操作</th>':'')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+keys.map(k=>'<td>'+esc(r[k])+'</td>').join('')+(actions?'<td>'+actions.replaceAll('{id}',esc(r.user_id||r.qqid||''))+'</td>':'')+'</tr>').join('')+'</tbody></table>'}
async function users(app){app.innerHTML='<div class="toolbar"><input id="q" placeholder="QQ/用户/UID"><button id="go">搜索</button></div><div id="rows"></div>';async function load(){const rows=await api('/users?search='+encodeURIComponent(document.querySelector('#q').value));document.querySelector('#rows').innerHTML=table(rows,'<button onclick="balance(\'{id}\')">BREAK</button> <button onclick="ban(\'{id}\')">封禁</button> <button onclick="unban(\'{id}\')">解封</button>')}document.querySelector('#go').onclick=load;await load()}
async function balance(id){const mode=prompt('输入 set 或 add','add');if(!mode)return;const amount=Number(prompt('数值','0'));await api('/users/'+encodeURIComponent(id)+'/break',{method:'POST',body:JSON.stringify({mode,amount})});show('users')}
async function ban(id){const reason=prompt('封禁原因','管理员封禁');if(reason===null)return;const hours=Number(prompt('小时，0=永久','0'));await api('/users/'+encodeURIComponent(id)+'/ban',{method:'POST',body:JSON.stringify({reason,hours})});show('users')}
async function unban(id){await api('/users/'+encodeURIComponent(id)+'/ban',{method:'DELETE'});show('users')}
async function traces(app){app.innerHTML='<div class="toolbar"><input id="tq" placeholder="REF/用户/命令"><button id="tgo">搜索</button></div><div id="trs"></div><pre id="detail" class="hidden"></pre>';async function load(){const rows=await api('/traces?search='+encodeURIComponent(document.querySelector('#tq').value));document.querySelector('#trs').innerHTML=table(rows.map(r=>({ref_id:r.ref_id,status:r.status,user:r.user_id,group:r.group_id,command:r.command,duration_ms:r.duration_ms,started:new Date(r.started_at*1000).toLocaleString()}))) ;document.querySelectorAll('#trs tbody tr').forEach((tr,i)=>tr.onclick=async()=>{const d=await api('/traces/'+rows[i].ref_id);const p=document.querySelector('#detail');p.classList.remove('hidden');p.textContent=JSON.stringify(d,null,2)})}document.querySelector('#tgo').onclick=load;await load()}
async function groups(app){const d=await api('/groups');app.innerHTML=table(d.groups)+'<h2>功能开关</h2><div class="toolbar"><input id="gid" placeholder="群ID"><select id="feat">'+d.features.map(x=>`<option>${esc(x)}</option>`).join('')+'</select><select id="enabled"><option value="true">启用</option><option value="false">禁用</option></select><button id="setf">保存</button></div><pre id="fout"></pre>';document.querySelector('#setf').onclick=async()=>{const gid=document.querySelector('#gid').value,feature=document.querySelector('#feat').value,enabled=document.querySelector('#enabled').value==='true';document.querySelector('#fout').textContent=JSON.stringify(await api('/groups/'+encodeURIComponent(gid)+'/features/'+feature,{method:'POST',body:JSON.stringify({enabled})}),null,2)}}
async function messages(app){const rows=await api('/messages?days=7');app.innerHTML='<h2>近 7 日群消息排行</h2>'+table(rows)}
async function economy(app){const [rows,cfg,logs]=await Promise.all([api('/economy?days=30'),api('/config/break'),api('/break/logs?limit=200')]);app.innerHTML='<h2>BREAK 经济配置</h2><table><thead><tr><th>配置</th><th>当前值</th><th>操作</th></tr></thead><tbody>'+Object.entries(cfg).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(v)}</td><td><button onclick="setBreakConfig('${esc(k)}','${esc(v)}')">修改</button></td></tr>`).join('')+'</tbody></table><h2>近 30 日 BREAK 经济</h2>'+table(rows)+'<h2>最近 BREAK 调用流水</h2>'+table(logs)}
async function setBreakConfig(key,current){const value=prompt('新值',current);if(value===null)return;await api('/config/break/'+encodeURIComponent(key),{method:'POST',body:JSON.stringify({value})});show('economy')}
async function agreement(app){const d=await api('/config/agreement');app.innerHTML=`<h2>用户协议</h2><p class="muted">修改确认词时会自动更新版本，已同意用户需重新确认。</p><label>协议链接</label><input id="aurl" value="${esc(d.url)}"><br><br><label>版本</label><input id="aver" value="${esc(d.version)}"><br><br><label>网页确认词</label><textarea id="atext">${esc(d.accept_text)}</textarea><br><button id="asave">保存并生效</button><pre id="aout"></pre>`;document.querySelector('#asave').onclick=async()=>{const body={url:document.querySelector('#aurl').value,version:document.querySelector('#aver').value,accept_text:document.querySelector('#atext').value};document.querySelector('#aout').textContent=JSON.stringify(await api('/config/agreement',{method:'POST',body:JSON.stringify(body)}),null,2)}}
if(token)show('dashboard');
</script></body></html>"""


def register_admin_web() -> bool:
    global _REGISTERED, _WEB_APP
    if _REGISTERED or not bool(getattr(maiconfig, "maimaidx_admin_web_enabled", False)):
        return False
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import HTMLResponse
    except ImportError:
        log.warning("管理 WebUI 已启用，但未安装 FastAPI")
        return False
    # FastAPI resolves postponed annotations on nested routes from module globals.
    globals()["Request"] = Request

    port = int(getattr(maiconfig, "maimaidx_admin_web_port", 8099) or 0)
    if not 0 <= port <= 65535:
        log.error("MAIMAIDX_ADMIN_WEB_PORT 必须是 0～65535；0 表示使用共享端口")
        return False
    if port > 0:
        app = FastAPI(
            title="maimaiDX QueryBot Admin",
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )
        _WEB_APP = app
    else:
        app = getattr(driver, "server_app", None)
        if app is None or not hasattr(app, "add_api_route"):
            log.warning("WebUI port=0 时需要 NoneBot FastAPI Driver")
            return False

    root = str(getattr(maiconfig, "maimaidx_admin_web_path", "/maimaidx/admin") or "/maimaidx/admin")
    if not root.startswith("/"):
        root = "/" + root
    root = root.rstrip("/")
    api_root = root + "/api"

    def authorize(request: Request) -> None:
        expected = str(getattr(maiconfig, "maimaidx_admin_web_token", "") or "")
        if len(expected) < 24:
            raise HTTPException(status_code=503, detail="管理 Token 未配置或长度不足 24 位")
        header = request.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def audit_action(command: str, target: str, detail: dict) -> str:
        ref = admin_audit.start_trace(
            command=command, user_id=target, matcher="admin_web", input_summary=detail
        )
        admin_audit.add_step(command, "success", detail, ref_id=ref)
        admin_audit.finish_trace(ref, "success")
        return ref

    async def page():
        return HTMLResponse(_HTML)

    async def summary(request: Request):
        authorize(request)
        data, ticket, break_users = await asyncio.gather(
            asyncio.to_thread(admin_audit.summary),
            asyncio.to_thread(account_db.get_ticket_stats, days=7),
            asyncio.to_thread(break_db.count_users),
        )
        data.update({
            "break_users": break_users,
            "bound_accounts": account_db.count_accounts(),
            "ticket_7d_total": ticket["total"],
            "ticket_7d_success_rate": f'{ticket["success_rate"]}%',
            "ticket_7d_error_rate": f'{ticket["error_rate"]}%',
            "ticket_7d_returnCode_0": ticket["return_code_0"],
            "ticket_7d_returnCode_null": ticket["return_code_null"],
        })
        return data

    async def runtime(request: Request):
        authorize(request)
        from nonebot import get_bots

        bots = get_bots()
        qq_bots = []
        adapters = set()
        for key, bot in bots.items():
            adapter = getattr(bot, "adapter", None)
            get_name = getattr(adapter, "get_name", None) if adapter is not None else None
            adapter_name = str(get_name() if callable(get_name) else (get_name or type(bot).__name__))
            blob = " ".join((str(key), type(bot).__module__, type(bot).__name__, type(adapter).__module__ if adapter is not None else "")).lower()
            adapters.add(adapter_name)
            if str(key).lower().startswith("qq:") or "adapters.qq" in blob:
                qq_bots.append(str(key))
        return {
            "bot_count": len(bots),
            "qq_bot_count": len(qq_bots),
            "qq_connected": bool(qq_bots),
            "adapters": sorted(adapters),
            "platform": str(getattr(maiconfig, "maimaidx_platform", "")),
            "awmc_api_mode": str(getattr(maiconfig, "awmc_api_mode", "")),
            "awmc_api_token_configured": bool(str(getattr(maiconfig, "awmc_public_gateway_token", "") or "")),
        }

    async def users(request: Request, search: str = "", limit: int = 100, offset: int = 0):
        authorize(request)
        raw_break_rows = await asyncio.to_thread(
            break_db.list_users, limit=500, search=search
        )
        break_rows = {str(r["qqid"]): r for r in raw_break_rows}
        account_rows = {r.user_key: r for r in account_db.list_accounts(limit=500, search=search)}
        ids = sorted(set(break_rows) | set(account_rows))
        result = []
        for uid in ids[offset:offset + min(max(limit, 1), 500)]:
            br, ac = break_rows.get(uid, {}), account_rows.get(uid)
            ban = admin_audit.get_active_ban(uid)
            result.append({
                "user_id": uid,
                "name": ac.user_name if ac else "",
                "rating": ac.rating if ac else 0,
                "mai_uid": _mask(ac.mai_uid) if ac else "",
                "bound": bool(ac and ac.qrcode),
                "fish_token": bool(ac and ac.fish_token),
                "lxns_token": bool(ac and ac.lxns_token),
                "break": int(br.get("balance", 0)),
                "streak": int(br.get("streak", 0)),
                "banned": bool(ban),
            })
        return result

    async def update_break(user_id: str, request: Request):
        authorize(request)
        body = await request.json()
        source = str(body.get("source") or "webui").strip()
        actor = str(body.get("actor") or source).strip()
        if source not in {"webui", "feishu_ops"} or not actor or len(actor) > 128:
            raise HTTPException(status_code=400, detail="来源或操作者无效")
        try:
            qqid, amount = int(user_id), int(body.get("amount", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="用户 ID 与数值必须为整数")
        mode = str(body.get("mode", "add"))
        if mode == "set":
            balance = await asyncio.to_thread(
                break_db.admin_set_balance, qqid, amount
            )
        elif mode == "add":
            reason = "feishu_admin" if source == "feishu_ops" else "web_admin"
            balance = await asyncio.to_thread(
                break_db.add_balance, qqid, amount, reason,
                meta={"source": source, "by": actor},
            )
        else:
            raise HTTPException(status_code=400, detail="mode 仅支持 add/set")
        ref = audit_action(
            "web.update_break", user_id,
            {
                "mode": mode,
                "amount": amount,
                "balance": balance,
                "source": source,
                "actor": actor,
            },
        )
        return {"ok": True, "balance": balance, "ref_id": ref}

    async def ban(user_id: str, request: Request):
        authorize(request)
        resolved_user_id, requested_user_id = _resolve_user_id(user_id)
        body = await request.json()
        source = str(body.get("source") or "webui").strip()
        actor = str(body.get("actor") or source).strip()
        if source not in {"webui", "feishu_ops"} or not actor or len(actor) > 128:
            raise HTTPException(status_code=400, detail="来源或操作者无效")
        hours = max(0.0, float(body.get("hours", 0) or 0))
        reason = str(body.get("reason") or "WebUI 管理员封禁")
        expires = time.time() + hours * 3600 if hours else None
        admin_audit.ban_user(resolved_user_id, reason, actor, expires_at=expires)
        ref = audit_action(
            "web.ban_user", resolved_user_id,
            {
                "requested_user_id": requested_user_id,
                "resolved_user_id": resolved_user_id,
                "reason": reason,
                "hours": hours,
                "expires_at": expires,
                "source": source,
                "actor": actor,
            },
        )
        return {
            "ok": True,
            "requested_user_id": requested_user_id,
            "user_id": resolved_user_id,
            "expires_at": expires,
            "ref_id": ref,
        }

    async def unban(user_id: str, request: Request):
        authorize(request)
        resolved_user_id, requested_user_id = _resolve_user_id(user_id)
        source = str(request.query_params.get("source") or "webui").strip()
        actor = str(request.query_params.get("actor") or source).strip()
        if source not in {"webui", "feishu_ops"} or not actor or len(actor) > 128:
            raise HTTPException(status_code=400, detail="来源或操作者无效")
        changed = admin_audit.unban_user(resolved_user_id)
        ref = audit_action(
            "web.unban_user",
            resolved_user_id,
            {
                "requested_user_id": requested_user_id,
                "resolved_user_id": resolved_user_id,
                "changed": changed,
                "source": source,
                "actor": actor,
            },
        )
        return {
            "ok": changed,
            "requested_user_id": requested_user_id,
            "user_id": resolved_user_id,
            "ref_id": ref,
        }

    async def traces(request: Request, search: str = "", status: str = "", limit: int = 100, offset: int = 0):
        authorize(request)
        return admin_audit.list_traces(limit=limit, offset=offset, status=status, search=search)

    async def trace_detail(ref_id: str, request: Request):
        authorize(request)
        data = admin_audit.get_trace(ref_id.upper())
        if not data:
            raise HTTPException(status_code=404, detail="REF_ID 不存在")
        return data

    async def commands(request: Request, days: int = 7):
        authorize(request)
        return admin_audit.command_ranking(days=days)

    async def api_report(request: Request, days: int = 1):
        authorize(request)
        return admin_audit.api_report(days=days)

    async def analysis_tokens(request: Request, days: int = 1):
        authorize(request)
        return await asyncio.to_thread(break_db.analysis_token_report, days=days)

    async def groups(request: Request):
        authorize(request)
        from .maimaidx_music import feature_manager

        return {"groups": admin_audit.list_groups(), "features": feature_manager._get_all_feature_names()}

    async def set_feature(group_id: str, feature: str, request: Request):
        authorize(request)
        from .maimaidx_music import feature_manager

        body = await request.json()
        raw_enabled = body.get("enabled")
        enabled = raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled).lower() == "true"
        try:
            message = await (feature_manager.enable(group_id, feature) if enabled else feature_manager.disable(group_id, feature))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        actual = feature_manager.is_enabled(group_id, feature)
        ref = audit_action(
            "web.set_group_feature", group_id,
            {"feature": feature, "enabled": actual},
        )
        return {"ok": True, "enabled": actual, "message": message, "ref_id": ref}

    async def messages(request: Request, group_id: str = "", days: int = 7, limit: int = 100):
        authorize(request)
        rows = admin_audit.message_ranking(group_id=group_id, days=days, limit=limit)
        normalized = []
        for row in rows:
            item = dict(row)
            raw_user_id = str(item.get("user_id") or "")
            raw_group_id = str(item.get("group_id") or "")
            if not raw_user_id.isdigit():
                mapped_user_id = qq_bind_db.get_legacy_qq(raw_user_id)
                if mapped_user_id is not None:
                    item["user_id"] = str(mapped_user_id)
            if not raw_group_id.isdigit():
                mapped_group_id = qq_bind_db.get_group_legacy_id(raw_group_id)
                if mapped_group_id is not None:
                    item["group_id"] = str(mapped_group_id)
            normalized.append(item)
        return normalized

    async def economy(request: Request, days: int = 30):
        authorize(request)
        return await asyncio.to_thread(break_db.economy_report, days=days)

    async def break_logs(
        request: Request, user_id: str = "", reason: str = "",
        limit: int = 200, offset: int = 0,
    ):
        authorize(request)
        return await asyncio.to_thread(
            break_db.list_break_calls,
            limit=limit, offset=offset, user_id=user_id, reason=reason,
        )

    async def bans(request: Request, active_only: bool = True):
        authorize(request)
        return admin_audit.list_bans(active_only=active_only)

    async def break_config(request: Request):
        authorize(request)
        values = await asyncio.gather(*(
            asyncio.to_thread(break_db.get_config, key, default)
            for key, default in DEFAULT_CONFIG.items()
        ))
        return dict(zip(DEFAULT_CONFIG, values))

    async def set_break_config(key: str, request: Request):
        authorize(request)
        if key not in DEFAULT_CONFIG:
            raise HTTPException(status_code=404, detail="未知 BREAK 配置项")
        body = await request.json()
        value = str(body.get("value", "")).strip()
        try:
            if key == "b50_llm_reasoning_effort":
                value = value.lower()
                if value not in {"none", "low", "medium", "high"}:
                    raise ValueError
            elif key == "streak_bonus":
                values = [int(item.strip()) for item in value.split(",")]
                if not values or len(values) > 31 or any(item < 0 or item > 20 for item in values):
                    raise ValueError
                value = ",".join(str(item) for item in values)
            elif key == "streak_bonus_growth":
                number = int(value)
                if not 0 <= number <= 1000:
                    raise ValueError
                value = str(number)
            elif key.startswith("bonus_"):
                number = float(value)
                if not 0 <= number <= 5:
                    raise ValueError
                value = str(number)
            else:
                number = int(value)
                if number < 0 or number > 1000:
                    raise ValueError
                value = str(number)
        except ValueError:
            raise HTTPException(status_code=400, detail="配置值格式或范围不正确")
        await asyncio.to_thread(break_db.set_config, key, value)
        if key == "divingfish_oauth_enabled":
            from .maimaidx_divingfish_oauth import reload_oauth_config

            reload_oauth_config()
        ref = audit_action("web.set_break_config", key, {"key": key, "value": value})
        return {"ok": True, "key": key, "value": value, "ref_id": ref}

    async def agreement_config(request: Request):
        authorize(request)
        from ..command.mai_agreement import agreement_policy

        return agreement_policy()

    async def set_agreement_config(request: Request):
        authorize(request)
        from ..command.mai_agreement import agreement_policy

        body = await request.json()
        current = agreement_policy()
        url = str(body.get("url") or "").strip()
        accept_text = str(body.get("accept_text") or "").strip()
        version = str(body.get("version") or current["version"]).strip()
        if not url.startswith(("https://", "http://")) or len(url) > 500:
            raise HTTPException(status_code=400, detail="协议链接必须是 HTTP(S) URL")
        if not 8 <= len(accept_text) <= 2000:
            raise HTTPException(status_code=400, detail="确认词长度必须为 8～2000 字符")
        if accept_text != current["accept_text"] and version == current["version"]:
            version = time.strftime("%Y.%m.%d.%H%M%S")
        if not version or len(version) > 80:
            raise HTTPException(status_code=400, detail="协议版本格式不正确")
        admin_audit.set_setting("agreement_url", url)
        admin_audit.set_setting("agreement_accept_text", accept_text)
        admin_audit.set_setting("agreement_version", version)
        ref = audit_action(
            "web.set_agreement", "agreement",
            {"url": url, "version": version, "accept_text_changed": accept_text != current["accept_text"]},
        )
        return {"ok": True, "url": url, "version": version, "ref_id": ref}

    async def cards_stats(request: Request):
        authorize(request)
        from .maimaidx_card import card_manager
        return await asyncio.to_thread(card_manager.stats)

    async def cards_list(request: Request):
        authorize(request)
        from .maimaidx_card import card_manager
        card_type = request.query_params.get("type") or None
        status = request.query_params.get("status") or None
        batch_id = request.query_params.get("batch") or None
        limit = int(request.query_params.get("limit", 50))
        cards = await asyncio.to_thread(
            card_manager.list_cards,
            card_type=card_type, status=status, batch_id=batch_id, limit=limit,
        )
        return {"cards": cards}

    async def card_detail(code: str, request: Request):
        authorize(request)
        from .maimaidx_card import card_manager
        card = await asyncio.to_thread(card_manager.get_card, code)
        if not card:
            cards = await asyncio.to_thread(
                card_manager.list_cards, batch_id=code, limit=200
            )
            if cards:
                return {"batch": code, "cards": cards}
            raise HTTPException(status_code=404, detail="卡密不存在")
        return card

    async def card_create(request: Request):
        authorize(request)
        from .maimaidx_card import (
            CARD_TYPE_BREAK, card_manager, parse_duration,
        )
        body = await request.json()
        source = str(body.get("source") or "webui").strip()
        actor = str(body.get("actor") or source).strip()
        if source not in {"webui", "feishu_ops"} or not actor or len(actor) > 128:
            raise HTTPException(status_code=400, detail="来源或操作者无效")
        card_type = str(body.get("type") or "").strip()
        if card_type not in {"break", "double_break", "freedom"}:
            raise HTTPException(status_code=400, detail="卡密类型无效")
        try:
            quantity = int(body.get("quantity", 1))
            if card_type == CARD_TYPE_BREAK:
                value = int(body.get("value", 0))
            else:
                value = parse_duration(str(body.get("value", "")))
            note = str(body.get("note") or "")[:200]
            result = await asyncio.to_thread(
                card_manager.create_cards,
                card_type, value, quantity, created_by=actor, note=note,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        ref = audit_action(
            "web.card_create", result["batch_id"],
            {"type": card_type, "value": value, "quantity": quantity,
             "source": source, "actor": actor},
        )
        result["ref_id"] = ref
        return result

    async def card_disable(code: str, request: Request):
        authorize(request)
        from .maimaidx_card import CardError, card_manager
        body = await request.json()
        actor = str(body.get("actor") or body.get("source") or "webui").strip()
        try:
            card = await asyncio.to_thread(
                card_manager.disable_card, code, actor=actor
            )
        except CardError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        ref = audit_action("web.card_disable", code, {"actor": actor})
        return {"ok": True, "code": card["code"], "ref_id": ref}

    routes = [
        (root, page, ["GET"]),
        (api_root + "/summary", summary, ["GET"]),
        (api_root + "/runtime", runtime, ["GET"]),
        (api_root + "/users", users, ["GET"]),
        (api_root + "/users/{user_id}/break", update_break, ["POST"]),
        (api_root + "/users/{user_id}/ban", ban, ["POST"]),
        (api_root + "/users/{user_id}/ban", unban, ["DELETE"]),
        (api_root + "/traces", traces, ["GET"]),
        (api_root + "/traces/{ref_id}", trace_detail, ["GET"]),
        (api_root + "/commands", commands, ["GET"]),
        (api_root + "/api-report", api_report, ["GET"]),
        (api_root + "/analysis/tokens", analysis_tokens, ["GET"]),
        (api_root + "/groups", groups, ["GET"]),
        (api_root + "/groups/{group_id}/features/{feature}", set_feature, ["POST"]),
        (api_root + "/messages", messages, ["GET"]),
        (api_root + "/economy", economy, ["GET"]),
        (api_root + "/break/logs", break_logs, ["GET"]),
        (api_root + "/bans", bans, ["GET"]),
        (api_root + "/config/break", break_config, ["GET"]),
        (api_root + "/config/break/{key}", set_break_config, ["POST"]),
        (api_root + "/config/agreement", agreement_config, ["GET"]),
        (api_root + "/config/agreement", set_agreement_config, ["POST"]),
        (api_root + "/cards/stats", cards_stats, ["GET"]),
        (api_root + "/cards", cards_list, ["GET"]),
        (api_root + "/cards/create", card_create, ["POST"]),
        (api_root + "/cards/{code}", card_detail, ["GET"]),
        (api_root + "/cards/{code}/disable", card_disable, ["POST"]),
    ]
    for index, (path, endpoint, methods) in enumerate(routes):
        app.add_api_route(
            path, endpoint, methods=methods,
            name=f"maimaidx_admin_{index}_{endpoint.__name__}",
        )
    _REGISTERED = True
    if port > 0:
        host = str(getattr(maiconfig, "maimaidx_admin_web_host", "127.0.0.1"))
        log.info(f"maimaiDX 管理 WebUI 已注册：http://{host}:{port}{root}")
    else:
        log.info(f"maimaiDX 管理 WebUI 已挂载到 NoneBot Driver：{root}")
    return True


register_admin_web()


@driver.on_startup
async def _start_standalone_admin_web() -> None:
    global _WEB_SERVER, _WEB_THREAD
    if not _REGISTERED or _WEB_APP is None:
        return
    try:
        import uvicorn
    except ImportError:
        log.error("管理 WebUI 独立端口已启用，但未安装 uvicorn")
        return
    host = str(getattr(maiconfig, "maimaidx_admin_web_host", "127.0.0.1") or "127.0.0.1")
    port = int(getattr(maiconfig, "maimaidx_admin_web_port", 8099) or 8099)
    config = uvicorn.Config(
        _WEB_APP,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    _WEB_SERVER = uvicorn.Server(config)

    def run_server() -> None:
        try:
            _WEB_SERVER.run()
        except Exception:
            log.exception("管理 WebUI 独立服务异常退出")

    _WEB_THREAD = threading.Thread(
        target=run_server,
        name="maimaidx-admin-web",
        daemon=True,
    )
    _WEB_THREAD.start()
    for _ in range(100):
        if bool(getattr(_WEB_SERVER, "started", False)):
            log.info(f"管理 WebUI 正在监听 http://{host}:{port}")
            return
        if not _WEB_THREAD.is_alive():
            log.error(f"管理 WebUI 启动失败，请检查 {host}:{port} 是否被占用")
            return
        await asyncio.sleep(0.05)
    log.warning(f"管理 WebUI 启动超时，请检查 {host}:{port}")


@driver.on_shutdown
async def _stop_standalone_admin_web() -> None:
    if _WEB_SERVER is not None:
        _WEB_SERVER.should_exit = True
    if _WEB_THREAD is not None and _WEB_THREAD.is_alive():
        await asyncio.to_thread(_WEB_THREAD.join, 5)
