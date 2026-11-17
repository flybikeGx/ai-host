import React,{useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {Activity,CircleAlert,ChevronRight,CirclePlay,CircleStop,ClipboardList,KeyRound,LoaderCircle,Menu,Pause,Play,RefreshCw,RefreshCcw,Radio,Server,ShieldCheck,TriangleAlert,Wrench,X} from 'lucide-react';
import {api} from './api';
import './services.css';

const STATE={running:'按期望运行',stopped:'已主动停止',removed:'已标记删除',published:'已发布',unpublished:'仅安装未发布',withdrawn:'已撤销发布',healthy:'健康',degraded:'降级',unhealthy:'异常',unknown:'未知',starting:'启动中',not_verified:'未验证',protocol_verified:'协议已验证',business_verified:'业务已验证',never_run:'从未运行',stale:'心跳过期',failed:'失败',disabled:'未启用'};
const text=value=>STATE[value]||value||'—';
const stamp=value=>value?new Date(value*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}):'—';
const bytes=value=>{if(value===null||value===undefined)return '—';const units=['B','KiB','MiB','GiB','TiB'];let n=value,u=0;while(n>=1024&&u<units.length-1){n/=1024;u++;}return `${n.toFixed(u>1?1:0)} ${units[u]}`;};
const dotState=code=>['healthy','published','running'].includes(code)?'ok':['unhealthy','failed','withdrawn','removed'].includes(code)?'bad':['degraded','stale','maintenance'].includes(code)?'warn':'';

function Metric({metrics}){
  if(!metrics)return <span className="svc-muted">—</span>;
  if(metrics.state==='remote')return <span className="svc-muted">远端不可测</span>;
  const resources=metrics.resources;
  if(!resources)return <span className="svc-muted">{metrics.process_state==='alive'?'进程未归属':'未运行'}</span>;
  return <div className="svc-metric"><strong>{resources.rss_bytes!==null&&!resources.rss_complete?'≈ ':''}{bytes(resources.rss_bytes)}</strong><small>{resources.cpu_percent===null?'CPU 采样中':`${resources.cpu_percent.toFixed(1)}% CPU`} · {resources.process_count} 进程</small></div>;
}

function Actions({item,busy,act}){
  const disabled=busy!==null;
  const action=(name,body)=>{
    if(name==='withdraw'&&!confirm('撤销发布将阻止新的接入并立即使未用授权失效，进程继续运行。继续？'))return;
    act(name,body);
  };
  return <div className="svc-actions">
    <button disabled={disabled} onClick={()=>action('start')}><CirclePlay size={13}/>{item.mode==='external'?'启用路由':'启动'}</button>
    <button disabled={disabled} onClick={()=>action('stop')}><CircleStop size={13}/>停止</button>
    <button disabled={disabled||item.mode==='external'} onClick={()=>action('restart',{reason:'web-admin'})}><RefreshCcw size={13}/>重启</button>
    <button disabled={disabled} onClick={()=>action('probe')}><Radio size={13}/>探测</button>
    {item.publish_state==='published'?<button disabled={disabled} onClick={()=>action('withdraw',{reason:'web-admin'})}>撤销发布</button>:<button disabled={disabled||item.publish_state==='withdrawn'} onClick={()=>action('publish')}>发布</button>}
    <button disabled={disabled} onClick={()=>action('maintenance',{enabled:!item.maintenance})}>{item.maintenance?'结束维护':'进入维护'}</button>
  </div>;
}

function BindForm({item,agents,onBind,onError}){
  const [agentId,setAgentId]=useState(''),[alias,setAlias]=useState('service'),[tools,setTools]=useState([]),[scope,setScope]=useState('{}'),[busy,setBusy]=useState(false);
  const known=item.tools||[];
  useEffect(()=>{setTools(known.slice(0,1))},[item.deployment_id]);
  const submit=async event=>{
    event.preventDefault();
    try{JSON.parse(scope)}catch{onError('数据范围必须是 JSON 对象');return;}
    if(!agentId){onError('先选择 Agent 身份');return;}
    if(!tools.length){onError('至少选择一个工具');return;}
    setBusy(true);
    try{await onBind({agent_id:agentId,alias,tools,contract:item.contract,scope:JSON.parse(scope)});}
    finally{setBusy(false);}
  };
  return <form className="svc-bind" onSubmit={submit}>
    <label>Agent 身份<select value={agentId} onChange={e=>setAgentId(e.target.value)}><option value="">选择平台登记的 Agent</option>{agents.map(([id,name])=><option key={id} value={id}>{name}（{id}）</option>)}</select></label>
    <label>别名<input value={alias} onChange={e=>setAlias(e.target.value)} maxLength={64}/></label>
    <fieldset><legend>工具范围</legend>{known.length?known.map(tool=><label key={tool} className="svc-check"><input type="checkbox" checked={tools.includes(tool)} onChange={e=>setTools(e.target.checked?[...tools,tool]:tools.filter(x=>x!==tool))}/>{tool}</label>):<span className="svc-muted">服务目录尚未登记工具，先完成验证与发布。</span>}</fieldset>
    <label>数据范围（JSON）<textarea value={scope} onChange={e=>setScope(e.target.value)} rows={3} spellCheck={false}/></label>
    <button type="submit" disabled={busy}>{busy?<LoaderCircle className="spin" size={13}/>:<KeyRound size={13}/>}创建绑定</button>
  </form>;
}

function Detail({detail,agents,act,busy,onClose,onError,refresh,actionError,onDismissError}){
  const [log,setLog]=useState(null),[logName,setLogName]=useState(''),[bindingBusy,setBindingBusy]=useState(false);
  useEffect(()=>{if(detail.logs?.length&&!logName)setLogName(detail.logs[0])},[detail]);
  const openLog=async name=>{
    setLogName(name);
    try{const value=await api(`services/${detail.deployment_id}/log?name=${encodeURIComponent(name)}`,{token:agents.token});setLog(value.text);}catch(e){onError(e.message)}
  };
  const unbind=async id=>{
    setBindingBusy(true);
    try{await api(`services/bindings/${id}/disable`,{token:agents.token,method:'POST'});await refresh();}catch(e){onError(e.message)}finally{setBindingBusy(false)}
  };
  const health=detail.health||{};
  return <aside className="svc-detail" aria-label="服务详情">
    <header><div><span className="eyebrow">SHARED SERVICE</span><h2>{detail.title||detail.service_id}</h2><p>{detail.deployment_id} · {detail.contract||'未登记契约'}</p></div><button className="icon-button" aria-label="关闭详情" onClick={onClose}><X size={16}/></button></header>
    <div className="svc-detail-scroll">
      {actionError&&<div className="svc-action-error" role="alert">{actionError}<button className="icon-button" aria-label="关闭操作错误" onClick={onDismissError}>×</button></div>}
      <section className="svc-panel"><h3>期望 / 发布 / 观测</h3><dl className="svc-facts">
        <dt>期望状态</dt><dd>{text(detail.desired_state)}</dd>
        <dt>发布状态</dt><dd>{text(detail.publish_state)}</dd>
        <dt>观测状态</dt><dd>{text(detail.observed_state)} <small>{stamp(detail.observed_at)}</small></dd>
        <dt>验证等级</dt><dd>{text(detail.verification)}</dd>
        <dt>代际</dt><dd>{detail.generation} · {detail.maintenance?'维护中':'正常'}</dd>
        <dt>Owner</dt><dd>{detail.owner||'未登记'}</dd>
      </dl></section>
      <section className="svc-panel"><h3>健康层次</h3><dl className="svc-facts">
        <dt>进程</dt><dd>{health.gateway_alive?'存活':'未运行'}{health.backend?.pid?` · PID ${health.backend.pid}`:''}</dd>
        <dt>MCP 协议</dt><dd>{health.protocol_ok?'可用':health.gateway_alive?'未通过':'无活动实例'}</dd>
        <dt>业务就绪</dt><dd>{detail.verification==='business_verified'?'已通过声明的只读探针':'仅协议/未验证'}</dd>
        <dt>在途调用</dt><dd>{health.in_flight??0}</dd>
        {health.stats&&<><dt>业务统计</dt><dd>{health.stats.documents??0} 篇 · {health.stats.pending??0} 份待审</dd></>}
      </dl></section>
      <section className="svc-panel"><h3>操作</h3><Actions item={detail} busy={busy} act={act}/><p className="svc-hint">启动/停止/重启在服务端校验锁、代际、维护与恢复策略；页面显示的是持久状态，不是请求回执。</p></section>
      <section className="svc-panel"><h3>绑定 Agent</h3>{!detail.bindings?.length?<p className="svc-muted">尚未绑定。已发布不等于已授权。</p>:<ul className="svc-bindings">{detail.bindings.map(binding=><li key={binding.id}><div><strong>{binding.agent_name||binding.agent_id}</strong><small>{binding.alias} · 修订 {binding.revision} · {binding.enabled?'启用':'已停用'}</small><small className="svc-tools">{binding.tools.join(', ')}</small></div><button className="svc-inline" disabled={bindingBusy||!binding.enabled} onClick={()=>unbind(binding.id)}>撤权</button></li>)}</ul>}
      {detail.publish_state==='published'&&<BindForm item={detail} agents={agents.list} onError={onError} onBind={async body=>{await api(`services/${detail.deployment_id}/bind`,{token:agents.token,method:'POST',body});await refresh();}}/>}</section>
      <section className="svc-panel"><h3>故障单</h3>{!detail.incidents?.length?<p className="svc-muted">没有故障记录。</p>:detail.incidents.map(incident=><article key={incident.id} className="svc-incident"><div><strong>{incident.summary}</strong><small>{text(incident.status)} · 开启 {stamp(incident.opened_at)} · 影响 {incident.affected_agents.length} 个 Agent</small></div>{incident.close_evidence&&<p className="svc-hint">关闭证据：{incident.close_evidence.slice(0,400)}</p>}<details><summary>动作与诊断（{incident.actions.length}）</summary><pre>{JSON.stringify(incident.actions,null,2)}</pre></details></article>)}</section>
      <section className="svc-panel"><h3>安装计划与操作进度</h3>{!detail.operations?.length?<p className="svc-muted">没有操作记录。安装由 Admin 管理工具 prepare_service_install / apply_service_install 发起，本页显示其计划、进度和结果。</p>:<ul className="svc-ops">{detail.operations.map(op=>{const plan=op.plan||{};const progress=plan.progress||{};const result=plan.result||{};return <li key={op.id}><div><strong>{op.kind} · {text(op.status)}</strong><small>{stamp(op.created)}{op.finished?` · 完成 ${stamp(op.finished)}`:''}</small>{op.error&&<small className="svc-error">{op.error}</small>}<details><summary>计划 / 进度 / 结果</summary><pre>{JSON.stringify({commit:plan.commit,component:plan.component,revision_id:plan.revision_id,manifest_digest:plan.manifest_digest,verification:plan.verification||result.verification,tools:result.tools,missing:plan.missing,risks:plan.risks,progress:Object.keys(progress),result:Object.keys(result)},null,2)}</pre></details></div></li>})}</ul>}</section>
      <section className="svc-panel"><h3>脱敏日志</h3>{detail.logs?.length?<><div className="svc-log-tabs">{detail.logs.map(name=><button key={name} className={name===logName?'active':''} onClick={()=>openLog(name)}>{name}</button>)}</div><pre className="svc-log">{log??'选择日志文件读取末尾内容。'}</pre></>:<p className="svc-muted">尚无日志。</p>}</section>
    </div>
  </aside>;
}

export default function ServicesPage({token,onError,onOpenNav}){
  const [data,setData]=useState(null),[error,setError]=useState(''),[auto,setAuto]=useState(true),[refreshing,setRefreshing]=useState(false),[selected,setSelected]=useState(null),[detail,setDetail]=useState(null),[busy,setBusy]=useState(null),[actionError,setActionError]=useState('');
  const detailEpoch=useRef(0);
  const closeDetail=()=>{detailEpoch.current+=1;setSelected(null);setDetail(null);setActionError('')};
  const load=useCallback(async signal=>{const epoch=detailEpoch.current;setRefreshing(true);try{const value=await api('services',{token,signal});setData(value);if(selected){try{const current=await api(`services/${selected}`,{token,signal});if(epoch===detailEpoch.current&&selectedRef.current===selected)setDetail(current)}catch(e){if(e.status!==404)throw e;if(epoch===detailEpoch.current){setSelected(null);setDetail(null)}}}}catch(e){if(e.name!=='AbortError'){if(e.status===401)onError(e);else setError(e.message)}}finally{if(!signal?.aborted)setRefreshing(false)}},[token,onError,selected]);
  useEffect(()=>{const controller=new AbortController();let pending=false;const update=async()=>{if(pending)return;pending=true;try{await load(controller.signal)}finally{pending=false}};update();const timer=auto?setInterval(update,4000):null;return()=>{if(timer)clearInterval(timer);controller.abort();}},[load,auto]);
  const selectedRef=useRef(selected);selectedRef.current=selected;
  const agents=useMemo(()=>({list:data?.agents||[],token}),[data,token]);
  const open=async id=>{const epoch=++detailEpoch.current;setError('');setSelected(id);setActionError('');try{const current=await api(`services/${id}`,{token});if(epoch===detailEpoch.current)setDetail(current)}catch(e){if(e.status===401)onError(e);else setError(e.message)}};
  const perform=async(item,name,body)=>{
    if(!item)return;
    const epoch=detailEpoch.current;
    setBusy(name);setActionError('');
    try{if(name==='maintenance')await api(`services/${item.deployment_id}/maintenance`,{token,method:'POST',body});
    else await api(`services/${item.deployment_id}/${name}`,{token,method:'POST',body:body||{}});
    setError('');await load();
    if(epoch===detailEpoch.current&&selectedRef.current===item.deployment_id){
      const next=await api(`services/${item.deployment_id}`,{token});
      if(epoch===detailEpoch.current)setDetail(next);
    }}
    catch(e){if(e.status===401)onError(e);else if(epoch===detailEpoch.current&&selectedRef.current===item.deployment_id)setActionError(e.message);else if(epoch===detailEpoch.current)setError(e.message)}
    finally{setBusy(null)}
  };
  const counts=data?.services?{total:data.services.length,published:data.services.filter(s=>s.publish_state==='published').length,healthy:data.services.filter(s=>s.observed_state==='healthy').length,incidents:data.services.reduce((n,s)=>n+(s.open_incidents||0),0)}:{total:0,published:0,healthy:0,incidents:0};
  return <main className="main-panel svc-panel-wrap">
    <header className="topbar"><button className="icon-button mobile-only" aria-label="打开侧栏" onClick={onOpenNav}><Menu size={20}/></button><div className="conversation-heading"><div className="breadcrumb">运行中心<ChevronRight size={12}/><span>共享服务</span></div><div className="title-row"><h1>共享服务</h1></div></div><div className="dash-header-actions"><button className="dash-auto" aria-label={auto?'暂停自动刷新':'恢复自动刷新'} onClick={()=>setAuto(!auto)}>{auto?<Pause size={12}/>:<Play size={12}/>}<span>{auto?'自动刷新':'已暂停'}</span></button><button className="icon-button" aria-label="刷新共享服务" disabled={refreshing} onClick={()=>{setError('');load()}}><RefreshCw size={15} className={refreshing?'spin':''}/></button></div></header>
    <div className="svc-scroll">
      <div className="svc-intro"><div><span className="eyebrow">AGENT BASE · SERVICE CATALOG</span><h2>共享 MCP，显式授权。</h2><p>托管安装、发布、绑定与故障闭环集中在这里；页面只读采样，不触发模型调用。</p></div><div className="svc-health"><span className={'svc-dot '+dotState(data?.supervisor?.state)}/>监督器 {text(data?.supervisor?.state)}<span className="svc-sep"/><span className={'svc-dot '+dotState(data?.guardian?.state)}/>看护 {text(data?.guardian?.state)}</div></div>
      {error&&<div className="error-banner" role="alert">{error}<button className="icon-button" aria-label="关闭错误" onClick={()=>setError('')}>×</button></div>}
      {!data?<div className="svc-loading"><LoaderCircle className="spin" size={22}/>正在读取服务目录…</div>:<>
      <section className="svc-stats"><article><span><Server size={15}/>服务部署</span><strong>{counts.total}</strong><small>{counts.published} 个已发布</small></article><article><span><Activity size={15}/>观测健康</span><strong>{counts.healthy}<em>/{counts.total}</em></strong><small>外部服务只报告可观测状态</small></article><article><span><TriangleAlert size={15}/>未关闭故障单</span><strong>{counts.incidents}</strong><small>按部署和代际归并</small></article><article><span><ShieldCheck size={15}/>看护运行</span><strong>{text(data.guardian.state)}</strong><small>{data.guardian.enabled?`每 ${data.guardian.interval_seconds} 秒巡检`:'未启用（监督器仍探测）'}</small></article></section>
      {data.guardian.state==='stale'||data.guardian.state==='failed'?<div className="svc-alert"><CircleAlert size={14}/>看护 Agent {text(data.guardian.state)}：确定性监督器继续探测，管理页仍可见服务状态。</div>:null}
      <section className="svc-list">{data.services.map(item=><article key={item.deployment_id} className={'svc-card '+(selected===item.deployment_id?'selected':'')}>
        <header><div className="svc-card-title"><span className={'svc-dot '+dotState(item.observed_state)}/><div><h3>{item.title||item.service_id}</h3><small>{item.deployment_id} · {item.endpoint_kind} · {item.contract||'未登记契约'}</small></div></div><span className={'svc-status '+item.observed_state}>{text(item.observed_state)}</span></header>
        <div className="svc-card-states"><span><i>期望</i>{text(item.desired_state)}</span><span><i>发布</i>{text(item.publish_state)}</span><span><i>验证</i>{text(item.verification)}</span><span><i>代际</i>{item.generation}</span></div>
        <div className="svc-card-body"><div><span className="svc-label">绑定 / 故障</span><strong>{item.bound_agents.length} 个 Agent · {item.open_incidents} 个故障单</strong><small>{item.bound_agents.map(a=>a.name).join('、')||'尚未授权任何 Agent'}</small></div><div><span className="svc-label">资源</span><Metric metrics={{state:item.metrics_state,resources:item.resources,process_state:item.process_state}}/></div></div>
        <footer><span>检查 {stamp(item.observed_at)}{item.maintenance?' · 维护中':''}</span><span className="svc-owner">{item.owner||'owner 未登记'}</span><button className="svc-open" onClick={()=>open(item.deployment_id)}>详情<ChevronRight size={13}/></button></footer>
        <details className="svc-inline-actions"><summary><Wrench size={13}/>快捷操作</summary><Actions item={item} busy={busy} act={(name,body)=>perform(item,name,body)}/></details>
      </article>)}</section>
      {!data.services.length&&<section className="svc-empty"><Server size={22}/><h3>还没有共享服务部署</h3><p>Admin 可以通过管理工具准备并应用安装计划；MCP-only repo 需要 Installation.md 的 v3 清单。</p></section>}
      <footer className="svc-footer"><span><ClipboardList size={13}/>外部服务只做登记、探测与路由停用，不由平台启动或重启。</span><span>采样 {stamp(data.sampled_at)}</span></footer>
      </>}
    </div>
    {detail&&<div className="svc-detail-overlay" onClick={event=>{if(event.target===event.currentTarget)closeDetail()}}><Detail detail={detail} agents={agents} act={(name,body)=>perform(detail,name,body)} busy={busy} onClose={closeDetail} actionError={actionError} onDismissError={()=>setActionError('')} onError={message=>{if(message?.status===401)onError(message);else setError(String(message?.message||message))}} refresh={async()=>{const epoch=detailEpoch.current;const next=await api(`services/${detail.deployment_id}`,{token});if(epoch===detailEpoch.current)setDetail(next);await load();}}/></div>}
  </main>;
}
