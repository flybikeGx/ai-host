import React, {useCallback, useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {ArrowDown, ArrowUp, Archive, Bot, Check, ChevronDown, ChevronRight, Circle, Clock3, Copy, Gauge, ExternalLink, KeyRound, LayoutDashboard, LoaderCircle, Menu, MessageSquare, MoreHorizontal, PanelLeftClose, Pencil, Plus, RefreshCw, Send, ShieldCheck, SlidersHorizontal, Square, Terminal, User, X} from 'lucide-react';
import './style.css';
import './cron.css';
import CronPage from './CronPage';
import DashboardPage from './DashboardPage';
import {api} from './api';

const routeView=()=>location.pathname==='/dashboard'?'dashboard':location.pathname==='/cron'?'cron':'chat';
const busy = status => ['queued','running'].includes(status);
const statusText = {queued:'排队中',running:'正在回复',succeeded:'已完成',failed:'执行失败',cancelled:'已停止',timed_out:'已超时',interrupted:'已中断',idle:'就绪'};
const initials = name => name==='Admin Agent'?'A':(name||'Agent').slice(0,1).toUpperCase();
const time = value => new Date(value*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
const phaseText = phase => ({turn_start:'准备回复',step_start:'正在处理',step_end:'继续处理',turn_end:'正在收尾'}[phase]||'正在处理');

function applyEvent(current,event){
  if(!current||event.conversation_id!==current.conversation.id||event.seq<=current.cursor)return current;
  let turns=current.turns;
  const data=event.data;
  if(event.type==='turn.queued'&&!turns.some(t=>t.id===event.turn_id))turns=[...turns,data];
  turns=turns.map(t=>{
    if(t.id!==event.turn_id)return t;
    if(event.type==='text.delta')return {...t,assistant_text:t.assistant_text+data.text};
    if(event.type==='turn.started')return {...t,status:'running'};
    if(event.type==='turn.stopping')return {...t,stopping:true};
    if(event.type==='turn.finished')return {...t,...data,stopping:false};
    return t;
  });
  return {...current,turns,cursor:event.seq,events:[...current.events,event],conversation:event.type==='session.opened'?{...current.conversation,native_id:data.session_id}:current.conversation};
}

function ToolTrace({events,turn}){
  const calls=new Map();
  for(const event of events){if(event.type==='tool.call')calls.set(event.data.call_id,{...event.data});if(event.type==='tool.result'&&calls.has(event.data.call_id))Object.assign(calls.get(event.data.call_id),event.data);}
  if(!calls.size)return null;
  const entries=[...calls.values()];
  const running=entries.some(c=>!c.status)&&busy(turn.status);
  return <details className="tool-group"><summary><span className="trace-icon">{running?<LoaderCircle className="spin" size={14}/>:<Terminal size={14}/>}</span><span>{running?'正在使用工具':`${entries.length} 次工具调用`}</span><ChevronDown className="disclosure" size={14}/></summary><div className="tool-list">{entries.map((call,i)=>{
    const failed=call.status&&call.status!=='completed';
    const label=call.status?(failed?'失败':'完成'):(busy(turn.status)?'执行中':'未完成');
    return <details className="tool" key={call.call_id||i}><summary><span className={'tool-dot '+(failed?'failed':call.status?'done':'')} /><code>{call.tool.replace(/^mcp__repo_[a-f0-9]+__/,'').replace(/^mcp__admin__/,'admin · ')}</code><span className={'tool-label '+(failed?'failed':'')}>{label}</span><ChevronRight size={13}/></summary><div className="tool-body"><span>参数</span><pre>{call.input||'{}'}</pre>{call.result!==undefined&&<><span>返回</span><pre>{call.result}</pre></>}</div></details>})}</div></details>;
}

function App(){
  const [targets,setTargets]=useState([]),[defaults,setDefaults]=useState({model:'',effort:'high'}),[chats,setChats]=useState([]);
  const [selected,setSelected]=useState(()=>location.pathname==='/chat'?location.hash.slice(1)||null:null),[targetId,setTargetId]=useState(null);
  const [view,setView]=useState(routeView),[agentFilter,setAgentFilter]=useState('all');
  const [snapshot,setSnapshot]=useState(null),[loading,setLoading]=useState(false),[sending,setSending]=useState(false),[error,setError]=useState('');
  const [token,setToken]=useState(''),[showToken,setShowToken]=useState(false),[tokenDraft,setTokenDraft]=useState('');
  const [showSettings,setShowSettings]=useState(false),[mobileNav,setMobileNav]=useState(false),[archived,setArchived]=useState(false),[search,setSearch]=useState('');
  const [draft,setDraft]=useState(''),[model,setModel]=useState(''),[effort,setEffort]=useState('high'),[connected,setConnected]=useState(false),[copied,setCopied]=useState(null),[editing,setEditing]=useState(false),[titleDraft,setTitleDraft]=useState('');
  const [nearBottom,setNearBottom]=useState(true);
  const agentList=useRef(null);
  const viewport=useRef(null),textarea=useRef(null),cursor=useRef(0),activeId=useRef(selected),autoScroll=useRef(true),pending=useRef(null),latestToken=useRef(token);
  latestToken.current=token;activeId.current=selected;
  const target=targets.find(t=>t.id===(snapshot?.conversation.target||targetId))||{id:targetId,name:targetId==='admin'?'Admin Agent':'Agent',kind:targetId==='admin'?'admin':'repo',skills:[],mcp:[]};
  const turns=snapshot?.turns||[],activeTurn=turns.find(t=>busy(t.status));
  const isArchived=!!snapshot?.conversation.archived;
  const title=snapshot?.conversation.title||(targetId?'新的对话':'选择 Agent');

  const reportError=useCallback(e=>{if(e.name==='AbortError')return;if(e.status===401)setShowToken(true);setError(e.message||'连接失败，请重试');},[]);
  const reload=useCallback(async()=>{
    const [catalog,list]=await Promise.all([api('chat/targets',{token}),api('conversations?archived='+archived,{token})]);
    setTargets(catalog.targets);setDefaults(catalog.defaults);setChats(list);setModel(old=>old||catalog.defaults.model);
  },[token,archived]);
  useEffect(()=>{reload().catch(reportError);const timer=setInterval(()=>reload().catch(reportError),15000);return()=>clearInterval(timer)},[reload,reportError]);
  useEffect(()=>{const change=()=>{setView(routeView());if(location.pathname==='/chat')setSelected(location.hash.slice(1)||null);};addEventListener('hashchange',change);addEventListener('popstate',change);return()=>{removeEventListener('hashchange',change);removeEventListener('popstate',change)}},[]);

  useEffect(()=>{
    const control=new AbortController();let cancelled=false;cursor.current=0;setSnapshot(null);setError('');setLoading(!!selected);setConnected(false);autoScroll.current=true;
    if(!selected){setDraft(sessionStorage.getItem('agent-draft:new:'+targetId)||'');return()=>control.abort();}
    setDraft(sessionStorage.getItem('agent-draft:'+selected)||'');
    const sleep=ms=>new Promise(resolve=>{const timer=setTimeout(resolve,ms);control.signal.addEventListener('abort',()=>{clearTimeout(timer);resolve()},{once:true});});
    async function follow(){
      try{
        const initial=await api('conversations/'+selected,{token,signal:control.signal});
        if(cancelled)return;
        cursor.current=initial.cursor;setSnapshot(initial);setTargetId(initial.conversation.target);setModel(initial.conversation.spec.runtime.model);setEffort(initial.conversation.spec.runtime.effort||'high');setLoading(false);
      }catch(e){if(!cancelled){setLoading(false);reportError(e)}return;}
      let backoff=1000;
      while(!cancelled){
        try{
          const response=await fetch(`/api/conversations/${selected}/events?after=${cursor.current}`,{headers:token?{Authorization:'Bearer '+token}:{},signal:control.signal});
          if(!response.ok){const e=new Error(response.status===401?'请填写管理 Token':'事件连接失败');e.status=response.status;throw e;}
          setConnected(true);backoff=1000;
          const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
          while(!cancelled){const {value,done}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});let boundary;
            while((boundary=buffer.indexOf('\n\n'))>=0){const frame=buffer.slice(0,boundary);buffer=buffer.slice(boundary+2);const line=frame.split('\n').find(l=>l.startsWith('data: '));if(!line)continue;
              const event=JSON.parse(line.slice(6));if(event.seq<=cursor.current)continue;cursor.current=event.seq;
              setSnapshot(current=>applyEvent(current,event));
              if(event.type==='turn.finished'){
                reload().catch(reportError);
                api('conversations/'+selected+'/files',{token,signal:control.signal}).then(files=>setSnapshot(s=>s?.conversation.id===selected?{...s,files}:s)).catch(()=>{});
              }
            }
          }
        }catch(e){if(cancelled)return;if(e.status===401){reportError(e);return;}}
        if(cancelled)return;setConnected(false);await sleep(backoff);backoff=Math.min(backoff*2,10000);
      }
    }
    follow();return()=>{cancelled=true;control.abort();};
  },[selected,token,reportError]);

  useEffect(()=>{const element=textarea.current;if(element){element.style.height='auto';element.style.height=Math.min(element.scrollHeight,190)+'px';}},[draft]);
  useEffect(()=>{if(autoScroll.current&&viewport.current)viewport.current.scrollTop=viewport.current.scrollHeight;},[snapshot,loading]);
  const choose=(id,target)=>{if(target)setTargetId(target);setView('chat');setSelected(id);setMobileNav(false);setShowSettings(false);history.replaceState(null,'','/chat'+(id?'#'+id:''));};
  const navigate=page=>{setView(page);setMobileNav(false);history.pushState(null,'',page==='chat'?'/chat'+(selected?'#'+selected:''):'/'+page);};
  const startChat=agent=>{choose(null,agent.id);setSnapshot(null);setDraft(sessionStorage.getItem('agent-draft:new:'+agent.id)||'');setModel(defaults.model);setEffort(defaults.effort||'high');setError('');setTimeout(()=>textarea.current?.focus(),0);};
  const openAgents=()=>{if(innerWidth<=720)setMobileNav(true);else agentList.current?.querySelector('button')?.focus();};
  useEffect(()=>{const shortcut=e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openAgents();}};addEventListener('keydown',shortcut);return()=>removeEventListener('keydown',shortcut);},[]);
  const editDraft=value=>{setDraft(value);sessionStorage.setItem('agent-draft:'+(selected||'new:'+targetId),value);};
  const send=async(value=draft)=>{
    if(!targetId||!value.trim()||sending||activeTurn||isArchived)return;setError('');setSending(true);autoScroll.current=true;
    let id=selected;
    try{
      if(!id){const chat=await api('conversations',{token,method:'POST',body:{target:targetId,model:model||defaults.model,effort}});id=chat.id;choose(id,chat.target);}
      const previous=pending.current;
      const key=previous&&previous.id===id&&previous.text===value?previous.key:crypto.randomUUID();pending.current={id,text:value,key};
      const turn=await api(`conversations/${id}/messages`,{token,method:'POST',body:{text:value,client_key:key}});
      setSnapshot(current=>current&&current.conversation.id===id&&!current.turns.some(t=>t.id===turn.id)?{...current,turns:[...current.turns,turn]}:current);
      setDraft('');sessionStorage.removeItem('agent-draft:'+id);sessionStorage.removeItem('agent-draft:new:'+targetId);pending.current=null;reload().catch(reportError);
    }catch(e){reportError(e)}finally{setSending(false);}
  };
  const stop=async()=>{try{await api(`conversations/${selected}/stop`,{token,method:'POST',body:{}})}catch(e){reportError(e)}};
  const rename=async()=>{setEditing(false);if(!titleDraft.trim()||titleDraft===title)return;try{const c=await api('conversations/'+selected,{token,method:'PATCH',body:{title:titleDraft}});setSnapshot(s=>({...s,conversation:c}));await reload()}catch(e){reportError(e)}};
  const archive=async()=>{try{await api('conversations/'+selected,{token,method:'PATCH',body:{archived:!isArchived}});choose(null);setTargetId(null);setSnapshot(null);await reload()}catch(e){reportError(e)}};
  const copy=async(text,id)=>{try{await navigator.clipboard.writeText(text);setCopied(id);setTimeout(()=>setCopied(null),1800)}catch{setError('无法自动复制，请选择文本复制')}};
  const download=async path=>{try{const response=await fetch(`/api/conversations/${selected}/download?path=${encodeURIComponent(path)}`,{headers:token?{Authorization:'Bearer '+token}:{}});if(!response.ok)throw new Error('文件不可用，请检查会话产物');const url=URL.createObjectURL(await response.blob());const anchor=document.createElement('a');anchor.href=url;anchor.download=path.split('/').pop();anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){reportError(e)}};
  const starters=target.kind==='admin'?['现在安装了哪些 Agent？它们能做什么？','检查所有已安装 Agent 的 MCP 服务状态。','我想安装一个新的 Git 仓库，从哪里开始？']:['介绍你的职责、技能和可用工具。','使用你的 skill 和 MCP，运行一次示例任务。','你生成的报告会保存在哪里？'];
  const agentName=id=>targets.find(t=>t.id===id)?.name||chats.find(c=>c.target===id)?.spec?.agent_name||'Agent';
  const historyTargets=[...new Set([...targets.map(t=>t.id),...chats.map(c=>c.target)])];
  const filtered=chats.filter(c=>(agentFilter==='all'||c.target===agentFilter)&&(!search||(c.title+' '+agentName(c.target)).toLowerCase().includes(search.toLowerCase())));

  return <div className="app-shell">
    {mobileNav&&<div className="nav-overlay" onClick={()=>setMobileNav(false)}/>}
    <aside className={'sidebar '+(mobileNav?'mobile-open':'')}>
      <div className="brand"><div className="brand-mark"><span/></div><div>Agent Base<small>YOUR LOCAL AGENT WORKSPACE</small></div><button className="icon-button mobile-only" aria-label="关闭侧栏" onClick={()=>setMobileNav(false)}><X size={18}/></button></div>
      <nav className="page-nav" aria-label="页面导航"><button className={view==='dashboard'?'active':''} onClick={()=>navigate('dashboard')}><Gauge size={15}/>仪表盘</button><button className={view==='chat'?'active':''} onClick={()=>navigate('chat')}><MessageSquare size={15}/>对话</button><button className={view==='cron'?'active':''} onClick={()=>navigate('cron')}><Clock3 size={15}/>定时任务</button></nav>
      <div className="section-label">AGENTS <span>{targets.length}</span><button aria-label="刷新 Agent 列表" onClick={()=>reload().catch(reportError)}><RefreshCw size={12}/></button></div>
      <p className="agent-create-hint">选择 Agent，发起新对话</p>
      <nav ref={agentList} className="agent-list" aria-label="Agent 列表">{targets.map(agent=><button key={agent.id} aria-label={`与 ${agent.name} 新建对话`} title={`与 ${agent.name} 新建对话`} disabled={!agent.available} className={'agent-item '+(targetId===agent.id&&view==='chat'?'selected':'')} onClick={()=>startChat(agent)}><span className={'agent-avatar '+(agent.kind==='admin'?'admin':'')}>{agent.kind==='admin'?<ShieldCheck size={17}/>:<Bot size={17}/>}</span><span className="agent-item-text">{agent.name}<small>{agent.kind==='admin'?'管理与调度':agent.available?'仓库 Agent':'服务已停用'}</small></span><Plus className="agent-create-plus" size={14}/></button>)}</nav>
      <div className="history-heading"><span className="section-label">{archived?'已归档':'最近对话'}</span><button className={'icon-button '+(archived?'active':'')} aria-label={archived?'查看最近对话':'查看归档对话'} onClick={()=>setArchived(!archived)}><Archive size={14}/></button></div>
      <input className="search" aria-label="搜索对话" placeholder="搜索对话…" value={search} onChange={e=>setSearch(e.target.value)}/>
      <select className="history-filter" aria-label="按 Agent 筛选对话" value={agentFilter} onChange={e=>setAgentFilter(e.target.value)}><option value="all">所有 Agent</option>{historyTargets.map(id=><option key={id} value={id}>{agentName(id)}</option>)}</select>
      <nav className="history-list" aria-label="对话历史">{filtered.map(chat=><button key={chat.id} className={'history-item '+(selected===chat.id?'selected':'')} onClick={()=>choose(chat.id,chat.target)}><MessageSquare size={14}/><span>{chat.title}<small>{agentName(chat.target)}</small></span>{busy(chat.status)&&<span className="presence busy"/>}</button>)}{!filtered.length&&<p className="empty-history">{search?'没有找到相关对话':'你的对话会保存在这里'}</p>}</nav>
      <div className="sidebar-footer"><a href="/"><LayoutDashboard size={15}/><span>工作台与 Repo 资源</span><ExternalLink size={12}/></a><button onClick={()=>setShowToken(true)}><KeyRound size={15}/><span>连接设置</span><span className="local-badge">LOCAL</span></button></div>
    </aside>
    {view==='dashboard'?<DashboardPage token={token} onError={reportError} onOpenNav={()=>setMobileNav(true)} onOpenChat={id=>choose(id)} onOpenSchedules={()=>navigate('cron')}/>:view==='cron'?<CronPage token={token} onError={reportError} onOpenNav={()=>setMobileNav(true)}/>:<main className="main-panel">
      <header className="topbar"><button className="icon-button mobile-only" aria-label="打开侧栏" onClick={()=>setMobileNav(true)}><Menu size={20}/></button><div className="conversation-heading"><div className="breadcrumb">{target.name}<ChevronRight size={12}/><span>对话</span></div><div className="title-row">{editing?<input className="title-input" value={titleDraft} autoFocus onChange={e=>setTitleDraft(e.target.value)} onBlur={rename} onKeyDown={e=>{if(e.key==='Enter')e.currentTarget.blur();if(e.key==='Escape')setEditing(false)}}/>:<h1>{title}</h1>}{selected&&!editing&&<button className="icon-button" aria-label="重命名对话" onClick={()=>{setTitleDraft(title);setEditing(true)}}><Pencil size={13}/></button>}</div></div><div className="topbar-actions">{selected&&<span className={'connection '+(connected?'connected':'')}><span/>{connected?'已连接':'正在重连'}</span>}<button className={'icon-button '+(showSettings?'active':'')} aria-label="对话信息" onClick={()=>setShowSettings(!showSettings)}><SlidersHorizontal size={17}/></button>{selected&&<button className="icon-button" aria-label={isArchived?'恢复对话':'归档对话'} onClick={archive} disabled={!!activeTurn}><Archive size={17}/></button>}</div></header>
      {showSettings&&<section className="info-panel"><div><strong>当前 Agent</strong><span>{target.name}</span></div><div><strong>命名空间</strong><code>{target.namespace||'admin'}</code></div><div><strong>Skills</strong><span>{target.skills?.join(' · ')||'无额外技能'}</span></div><div><strong>MCP</strong><span>{target.mcp?.join(' · ')||'无额外服务'}</span></div>{snapshot?.conversation.native_id&&<div><strong>CLI Session</strong><code>{snapshot.conversation.native_id}</code></div>}{!!snapshot?.files?.length&&<div className="artifact-files"><strong>会话产物</strong>{snapshot.files.map(file=><button key={file.path} onClick={()=>download(file.path)}><ArrowDown size={12}/>{file.path}<small>{file.size} B</small></button>)}</div>}<p>每个对话保留独立上下文；仓库 Agent 复用已安装的技能和常驻 MCP。对话结束不会停止服务。</p></section>}
      <div className="transcript" ref={viewport} onScroll={e=>{const node=e.currentTarget;const close=node.scrollHeight-node.scrollTop-node.clientHeight<120;autoScroll.current=close;setNearBottom(close)}}>
        {loading?<div className="loading-state"><LoaderCircle size={22} className="spin"/>正在加载对话</div>:!targetId&&!selected?<section className="welcome agent-picker"><div className="welcome-avatar"><Bot size={29}/></div><span className="eyebrow">YOUR AGENTS, ONE WORKSPACE</span><h2>选择一个 Agent，开始新对话</h2><p>从左侧 AGENTS 列表发起对话，或从最近对话中继续。<br/>每个对话保留独立的上下文。</p><button className="cron-secondary" onClick={openAgents}>打开 Agent 列表</button></section>:!turns.length?<section className="welcome"><div className={'welcome-avatar '+(target.kind==='admin'?'admin':'')}>{target.kind==='admin'?<ShieldCheck size={29}/>:<Bot size={29}/>}</div><span className="eyebrow">MEET YOUR AGENT</span><h2>和 {target.name} 开始对话</h2><p>{target.kind==='admin'?'告诉它你想安装、运行或管理哪个仓库。':'讨论任务、调用技能，或让它使用自己的 MCP 完成工作。'}<br/>对话会持续保存，随时回来继续。</p><div className="starter-list">{starters.map((text,index)=><button key={text} onClick={()=>editDraft(text)}><span>{String(index+1).padStart(2,'0')}</span>{text}<ChevronRight size={15}/></button>)}</div><div className="welcome-foot"><span className="presence"/>运行于你的本地 Agent 基座</div></section>:<div className="message-list">{turns.map(turn=>{
          const events=snapshot.events.filter(e=>e.turn_id===turn.id);const progress=[...events].reverse().find(e=>e.type==='progress');
          return <React.Fragment key={turn.id}><article className="message user-message"><div className="message-avatar user"><User size={16}/></div><div className="message-content"><div className="message-meta">你<span>{time(turn.created)}</span></div><div className="user-bubble">{turn.user_text}</div></div></article><article className="message assistant-message"><div className={'message-avatar '+(target.kind==='admin'?'admin':'')}><Bot size={17}/></div><div className="message-content"><div className="message-meta">{target.name}<span>{statusText[turn.status]}</span></div><ToolTrace events={events} turn={turn}/>{turn.assistant_text?<div className={'markdown '+(busy(turn.status)?'streaming':'')}><Markdown remarkPlugins={[remarkGfm]} skipHtml components={{img:props=><span>{props.alt||'图片'}</span>,a:props=><a {...props} target="_blank" rel="noopener noreferrer" onClick={e=>{const path=props.href?.replace(/^\.\//,'');if(path?.startsWith('artifacts/')){e.preventDefault();download(path);}}}/>}}>{turn.assistant_text}</Markdown></div>:busy(turn.status)?<div className="thinking-indicator"><span/><span/><span/><small>{turn.stopping?'正在停止':turn.status==='queued'?'等待执行位置':phaseText(progress?.data.phase)}</small></div>:null}{turn.error&&<div className="turn-error"><Circle size={13}/><div>{turn.error}<small>已保留本轮内容。确认情况后，可继续发送新消息。</small></div></div>}{!busy(turn.status)&&turn.assistant_text&&<button className="copy-message" aria-label="复制回复" onClick={()=>copy(turn.assistant_text,turn.id)}>{copied===turn.id?<Check size={13}/>:<Copy size={13}/>}<span>{copied===turn.id?'已复制':'复制'}</span></button>}</div></article></React.Fragment>})}</div>}
      </div>
      {!nearBottom&&turns.length>0&&<button className="jump-bottom" aria-label="回到底部" onClick={()=>{autoScroll.current=true;viewport.current.scrollTo({top:viewport.current.scrollHeight,behavior:'smooth'})}}><ArrowDown size={16}/></button>}
      {(targetId||selected)&&<section className="composer-area">{error&&<div className="error-banner" role="alert"><span>{error}</span><button className="icon-button" aria-label="关闭提示" onClick={()=>setError('')}><X size={14}/></button></div>}{isArchived&&<div className="archived-note">此对话已归档。点击右上角归档按钮可恢复。</div>}<div className="composer"><textarea ref={textarea} aria-label="消息" placeholder={`给 ${target.name} 发送消息…`} rows={2} value={draft} disabled={isArchived||target.available===false} onChange={e=>editDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing){e.preventDefault();send();}}}/><div className="composer-toolbar"><div className="model-controls"><Bot size={14}/><input aria-label="模型" title={selected?'模型在创建对话时确定；新建对话可更改':'模型 ID'} value={model} placeholder="模型 ID" disabled={!!selected} onChange={e=>setModel(e.target.value)}/><span className="toolbar-divider"/><select aria-label="推理强度" value={effort} disabled={!!selected} onChange={e=>setEffort(e.target.value)}>{['low','medium','high','max'].map(value=><option key={value} value={value}>{({low:'低',medium:'中',high:'高',max:'最高'})[value]}强度</option>)}</select></div>{activeTurn?<button className="stop-button" aria-label="停止回复" onClick={stop} disabled={activeTurn.stopping}><Square size={12} fill="currentColor"/>{activeTurn.stopping?'停止中':'停止'}</button>:<button className="send-button" aria-label="发送消息" onClick={()=>send()} disabled={sending||!draft.trim()||isArchived||target.available===false}>{sending?<LoaderCircle size={17} className="spin"/>:<ArrowUp size={18}/>}</button>}</div></div><div className="composer-caption"><span><kbd>Enter</kbd> 发送 · <kbd>Shift Enter</kbd> 换行</span><span>{activeTurn?'切换对话或刷新页面不会中断回复':'请核对 Agent 的输出与执行结果'}</span></div></section>}
    </main>}
    {showToken&&<div className="modal-overlay"><form className="connection-modal" onSubmit={e=>{e.preventDefault();setToken(tokenDraft);setShowToken(false);setError('')}}><div className="modal-header"><KeyRound size={22}/><button className="icon-button" type="button" aria-label="关闭连接设置" onClick={()=>setShowToken(false)}><X size={18}/></button></div><h2>连接本地服务</h2><p>如果服务配置了管理 Token，在这里填写。Token 仅保留在当前页面内存中。</p><label>管理 Token<input type="password" autoComplete="off" placeholder="未配置时留空" value={tokenDraft} onChange={e=>setTokenDraft(e.target.value)} autoFocus/></label><button className="primary-button" type="submit">连接</button></form></div>}
  </div>;
}

createRoot(document.getElementById('root')).render(<App/>);
