import React,{useCallback,useEffect,useRef,useState} from 'react';
import {ArrowRight,ArrowUpRight,Check,Eye,EyeOff,LoaderCircle,LogOut,ShieldCheck,User} from 'lucide-react';
import {api,currentDomain,selectDomain} from './api';
import './auth.css';

const destinations=['/dashboard','/chat','/cron','/knowledge','/services','/workbench'];
function returnPath(value){
  if(!value?.startsWith('/')||value.startsWith('//'))return '/dashboard';
  try{const url=new URL(value,location.origin);return url.origin===location.origin&&destinations.includes(url.pathname)?url.pathname+url.search+url.hash:'/dashboard';}
  catch{return '/dashboard';}
}

// §7.2: the selector defaults to the stored value, then the home domain, then
// the only joined domain. The server still validates every request; this only
// picks the initial value. A multi-domain user without a home domain must pick
// one before page requests stop returning domain_required.
function preferredDomain(identity){
  const list=identity?.domains||[],ids=list.map(domain=>domain.id),stored=currentDomain();
  if(stored&&ids.includes(stored))return stored;
  if(identity?.home_domain_id&&ids.includes(identity.home_domain_id))return identity.home_domain_id;
  return ids[0]||'';
}

// The page gates only on `domains[].permissions` from /me (§3.6). Nothing here
// infers a permission or a domain from roles, URLs or cached state.
function pageIdentity(me,domainId){
  const domains=me?.domains||[];
  const domain=domains.find(item=>item.id===domainId)||domains.find(item=>item.id===preferredDomain(me))||domains[0]||null;
  const permissions=new Set(domain?.permissions||[]);
  return {
    me,user:me?.user||null,domains,domain,domainId:domain?.id||'',
    permissions,platformAdmin:!!me?.platform_admin,
    chatAccess:domains.some(item=>(item.permissions||[]).includes('agent.read')),
    can:permission=>permissions.has(permission),
  };
}

function LoginPage({session,notice,onLogin}){
  const [username,setUsername]=useState(''),[password,setPassword]=useState(''),[visible,setVisible]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const submit=async event=>{
    event.preventDefault();if(busy)return;setBusy(true);setError('');
    try{await api('auth/login',{method:'POST',body:{username:username.trim(),password}});setPassword('');await onLogin();}
    catch(e){setPassword('');setError(e.status===401?e.message:'暂时无法登录，请稍后重试。');}
    finally{setBusy(false);}
  };
  return <main className="auth-page">
    <section className="auth-story" aria-label="Agent Base">
      <a className="auth-brand" href="/dashboard"><span className="auth-logo">a<span>.</span></span>Agent Base</a>
      <div className="auth-story-copy"><p className="auth-eyebrow">YOUR AGENTS. ONE WORKSPACE.</p><h1>让想法，<br/>在这里开始运行<span>。</span></h1><p>连接你的 Agent、知识与工具。<br/>从一次对话，到持续运转的工作。</p></div>
      <div className="auth-orbit" aria-hidden="true"><div className="auth-orbit-ring"/><span className="auth-orbit-center"><ShieldCheck size={32}/></span><span className="auth-orbit-label label-agents">AGENTS</span><span className="auth-orbit-label label-knowledge">KNOWLEDGE</span><span className="auth-orbit-label label-tools">TOOLS</span><i/><i/><i/></div>
      <footer>为专注工作而构建 <span>CONNECTED BY DESIGN</span></footer>
    </section>
    <section className="auth-form-panel">
      <div className="auth-form-wrap"><span className="auth-step"><span/> 安全连接你的工作空间</span><h2>欢迎回来</h2><p className="auth-subtitle">使用已有账号登录 Agent Base。</p>
        {notice&&!error&&<div className="auth-notice" role="status">{notice}</div>}
        <form onSubmit={submit} aria-label="账号登录">
          <label htmlFor="login-username">账号</label><div className="auth-input"><User size={17}/><input id="login-username" name="username" autoComplete="username" placeholder="输入你的账号" value={username} onChange={e=>setUsername(e.target.value)} maxLength={256} required autoFocus disabled={busy}/></div>
          <label htmlFor="login-password">密码</label><div className="auth-input"><ShieldCheck size={17}/><input id="login-password" name="password" type={visible?'text':'password'} autoComplete="current-password" placeholder="输入密码" value={password} onChange={e=>setPassword(e.target.value)} maxLength={4096} required disabled={busy}/><button type="button" aria-label={visible?'隐藏密码':'显示密码'} aria-pressed={visible} onClick={()=>setVisible(!visible)}>{visible?<EyeOff size={17}/>:<Eye size={17}/>}</button></div>
          {error&&<div className="auth-error" role="alert">{error}</div>}
          <button className="auth-submit" type="submit" disabled={busy||!username.trim()||!password}>{busy?<><LoaderCircle size={17} className="spin"/>正在登录…</>:<>登录工作空间<ArrowRight size={18}/></>}</button>
        </form>
        <div className="auth-account-note"><ShieldCheck size={16}/><p>账号由管理员分配。<br/>无法登录时，请联系你的平台管理员。</p></div>
        <div className="auth-sso-note">{session.sso?.identity_verification_configured?'企业账号身份校验已配置，浏览器单点登录入口尚未接入。':'当前使用平台账号登录，企业单点登录尚未配置。'}</div>
        {!session.login_required&&<a className="auth-local-link" href="/dashboard">返回本地工作台<ArrowUpRight size={13}/></a>}
      </div><footer className="auth-form-footer">AGENT BASE <span>一个工作空间，连接每一次行动。</span></footer>
    </section>
  </main>;
}

function AccountHome({session,onLogout}){
  const user=session.user;
  return <main className="auth-account-page"><section className="auth-account-card"><div className="auth-account-avatar"><Check size={26}/></div><span className="auth-eyebrow">SIGNED IN</span><h1>你好，{user.display_name||user.username}</h1><p>你已成功登录 Agent Base。</p><dl><dt>账号</dt><dd>{user.username}</dd><dt>当前角色</dt><dd>{user.roles?.map(role=>role.role_name||role.name).filter(Boolean).join('、')||'待分配'}</dd></dl><div className="auth-notice">当前账号尚未获准访问工作台。请联系管理员确认所需权限。</div><button className="auth-submit" onClick={onLogout}><LogOut size={17}/>退出登录</button></section></main>;
}

export default function AuthShell({children}){
  const [session,setSession]=useState(null),[me,setMe]=useState(null),[domainId,setDomainId]=useState(()=>currentDomain()),[failure,setFailure]=useState(''),[path,setPath]=useState(location.pathname),[notice,setNotice]=useState('');
  // The hybrid local-bootstrap token is memory-only (§11) and never persisted.
  const [token,setToken]=useState(''),[needsConnection,setNeedsConnection]=useState(false);
  const epoch=useRef(0),current=useRef(session),tokenRef=useRef(token);current.current=session;tokenRef.current=token;

  const applyDomain=useCallback(id=>{selectDomain(id);setDomainId(id||'');},[]);

  // The identity is the single source of page permissions (§3.6). A 401 on the
  // probe is not routed through app:auth-required, so an expired session takes
  // exactly one redirect path (the session refresh below). In the account-free
  // local workspace a failed probe re-opens the hybrid bootstrap entry instead.
  const loadIdentity=useCallback(async version=>{
    try{
      const value=await api('v1/me',{token:tokenRef.current});
      if(version===epoch.current){const id=preferredDomain(value);selectDomain(id);setDomainId(id);setMe(value);setNeedsConnection(false);}
      return value;
    }catch(error){
      if(version!==epoch.current)return null;
      if(error.status===401){
        setMe(null);setNeedsConnection(!current.current?.login_required);return null;
      }
      if(error.status===403){setMe(null);return null;}
      throw error;
    }
  },[]);

  const refresh=useCallback(async()=>{
    const version=++epoch.current;
    const value=await api('auth/session');
    if(version!==epoch.current)return value;
    setFailure('');setSession(value);
    if(!value.authenticated&&value.login_required){
      // A lost account session clears the cached identity and domain selector
      // once; the local account-free bootstrap keeps its identity while probing.
      setMe(null);selectDomain('');setDomainId('');setNeedsConnection(false);
    }
    if(value.authenticated||!value.login_required)await loadIdentity(version);
    return value;
  },[loadIdentity]);

  // Connecting the hybrid shared token re-reads /me WITH the token. On success
  // the identity (BOOTSTRAP) supplies the same frozen permission set as /me.
  const connect=useCallback(async value=>{
    const trimmed=typeof value==='string'?value.trim():'';
    tokenRef.current=trimmed;setToken(trimmed);
    const version=++epoch.current;
    try{
      const identity=await api('v1/me',{token:trimmed});
      if(version!==epoch.current)return identity;
      const id=preferredDomain(identity);selectDomain(id);setDomainId(id);setMe(identity);setNeedsConnection(false);
      return identity;
    }catch(error){
      if(version!==epoch.current)return null;
      if(error.status===401){setMe(null);setNeedsConnection(!current.current?.login_required);}
      throw error;
    }
  },[]);

  useEffect(()=>{document.title=path==='/login'?'登录 · Agent Base':'Agent Base · 工作空间';},[path]);
  useEffect(()=>{
    refresh().catch(()=>setFailure('无法连接服务，请检查服务是否已启动。'));
    const focus=()=>refresh().catch(()=>{});
    const required=event=>{
      if(event.detail?.status!==401)return;
      if(current.current?.authenticated)setNotice('登录已过期，请重新登录。');
      refresh().catch(()=>{});
    };
    const invalidDomain=()=>{selectDomain('');setDomainId('');setNotice('当前域不可用，已重新选择可用的域。');refresh().catch(()=>{});};
    const pop=()=>setPath(location.pathname);
    addEventListener('focus',focus);addEventListener('app:auth-required',required);
    addEventListener('app:domain-invalid',invalidDomain);addEventListener('popstate',pop);
    return()=>{epoch.current++;removeEventListener('focus',focus);removeEventListener('app:auth-required',required);removeEventListener('app:domain-invalid',invalidDomain);removeEventListener('popstate',pop)};
  },[refresh]);

  useEffect(()=>{if(!session?.authenticated)return;const timer=setTimeout(()=>{setNotice('登录已过期，请重新登录。');refresh().catch(()=>{});},Math.max(0,session.expires_at*1000-Date.now())+100);return()=>clearTimeout(timer);},[session?.expires_at,session?.authenticated,refresh]);

  const identity=me?pageIdentity(me,domainId):null;
  const canManage=!!session?.can_manage_workspace||!!identity?.platformAdmin;
  const chatAccess=!!identity?.chatAccess;

  useEffect(()=>{
    if(!session)return;
    if(!session.authenticated&&session.login_required&&path!=='/login'){
      const next=returnPath(location.pathname+location.search+location.hash);history.replaceState(null,'','/login?next='+encodeURIComponent(next));setPath('/login');
    }else if(session.authenticated&&path==='/login'){
      let next=returnPath(new URLSearchParams(location.search).get('next'));
      if(new URL(next,location.origin).pathname==='/workbench'){
        if(canManage){location.replace(next);return;}
        next='/dashboard';
      }
      history.replaceState(null,'',next);setPath(location.pathname);dispatchEvent(new PopStateEvent('popstate'));
    }
  },[session,path,canManage]);

  // Chat is the only surface C6 wires to the user system. Users who can only
  // chat stay there instead of hitting 403 responses on global admin pages.
  useEffect(()=>{
    if(!session?.authenticated||!identity||path==='/login')return;
    if(chatAccess&&!canManage&&path!=='/chat'){history.replaceState(null,'','/chat');setPath('/chat');dispatchEvent(new PopStateEvent('popstate'));}
  },[session?.authenticated,identity,path,chatAccess,canManage]);

  const logout=async()=>{try{await api('auth/logout',{method:'POST'});selectDomain('');setDomainId('');setToken('');tokenRef.current='';setMe(null);setNeedsConnection(false);setNotice('你已退出登录。');await refresh();history.replaceState(null,'','/login');setPath('/login');}catch{setFailure('退出未完成，请重试。');}};

  if(failure)return <main className="auth-account-page"><section className="auth-account-card"><h1>连接暂时不可用</h1><p role="alert">{failure}</p><button className="auth-submit" onClick={()=>refresh().catch(()=>{})}>重新连接</button></section></main>;
  if(!session)return <main className="auth-account-page"><div className="auth-loading" role="status"><LoaderCircle size={23} className="spin"/>正在连接工作空间…</div></main>;
  if(!session.authenticated&&(path==='/login'||session.login_required))return <LoginPage session={session} notice={notice} onLogin={refresh}/>;
  if(!identity&&!needsConnection)return <main className="auth-account-page"><div className="auth-loading" role="status"><LoaderCircle size={23} className="spin"/>正在确认权限…</div></main>;
  if(!identity){
    // Local hybrid bootstrap without a token: render the shell so the token
    // entry is reachable. It carries no permissions until /me accepts it.
    const pending={me:null,user:null,domains:[],domain:null,domainId:'',permissions:new Set(),platformAdmin:false,chatAccess:false,can:()=>false,canManage:false,setDomain:applyDomain,refresh:()=>refresh().catch(()=>{})};
    return children({session,onLogout:logout,notice,clearNotice:()=>setNotice(''),token,connect,autoConnect:true,identity:pending});
  }
  if(!canManage&&!chatAccess)return <AccountHome session={session} onLogout={logout}/>;
  return children({session,onLogout:logout,notice,clearNotice:()=>setNotice(''),token,connect,autoConnect:false,identity:{...identity,canManage,setDomain:applyDomain,refresh:()=>refresh().catch(()=>{})}});
}
