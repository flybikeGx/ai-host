import React, {useCallback, useEffect, useState} from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {ChevronDown, ChevronUp, LoaderCircle, Megaphone, Menu, RefreshCw, Search} from 'lucide-react';
import './info-board.css';

/**
 * Flat, single-column Info Board page (first-party spa-module).
 *
 * Read-only: publishing, editing and deleting happen through the package's MCP
 * tools. Markdown is untrusted business data, so raw HTML is skipped, remote
 * images are never loaded automatically, and every link opens in a new tab.
 */
const stamp = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', hour12: false,
}) : '—';

const components = {
  img: ({alt}) => <span className="info-image-placeholder">[图片未自动加载：{alt || '图片'}]</span>,
  a: ({href, children}) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
};

function Body({value}) {
  return <div className="markdown info-markdown">
    <Markdown remarkPlugins={[remarkGfm]} skipHtml components={components}>{value || ''}</Markdown>
  </div>;
}

async function read(path, token) {
  const response = await fetch('/api/' + path, {
    credentials: 'same-origin',
    cache: 'no-store',
    headers: token ? {Authorization: 'Bearer ' + token} : {},
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {
      detail = '服务暂时不可用';
    }
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export default function InfoBoardPage({token, onOpenNav}) {
  const [items, setItems] = useState([]), [query, setQuery] = useState(''), [draft, setDraft] = useState('');
  const [next, setNext] = useState(null), [loading, setLoading] = useState(false), [more, setMore] = useState(false);
  const [details, setDetails] = useState({}), [expanded, setExpanded] = useState({}), [pending, setPending] = useState(null);
  const [stats, setStats] = useState(null), [error, setError] = useState('');

  const page = useCallback(async (offset, append, value) => {
    setLoading(!append); setMore(append); setError('');
    try {
      const data = await read(`info/posts?offset=${offset}&limit=20&query=${encodeURIComponent(value)}`, token);
      setItems(old => append ? [...old, ...data.items] : data.items);
      setNext(data.next_offset);
    } catch (e) {
      if (e.name !== 'AbortError') setError(e.status === 401 ? '请先登录后再浏览信息台' : (e.message || '信息加载失败'));
    } finally {
      setLoading(false); setMore(false);
    }
  }, [token]);

  const refresh = useCallback(() => page(0, false, query), [page, query]);
  useEffect(() => {
    page(0, false, '');
    read('info/status', token).then(setStats).catch(() => {});
  }, [page, token]);

  const search = e => { e.preventDefault(); const value = draft.trim(); setQuery(value); page(0, false, value); };
  const clear = () => { setDraft(''); setQuery(''); page(0, false, ''); };

  const toggle = async item => {
    if (expanded[item.id]) { setExpanded(old => ({...old, [item.id]: false})); return; }
    setExpanded(old => ({...old, [item.id]: true}));
    if (details[item.id]) return;
    setPending(item.id); setError('');
    try {
      const post = await read('info/posts/' + encodeURIComponent(item.id), token);
      setDetails(old => ({...old, [item.id]: post}));
    } catch (e) {
      setError(e.message || '正文加载失败');
      setExpanded(old => ({...old, [item.id]: false}));
    } finally {
      setPending(null);
    }
  };

  return <main className="main-panel info-page">
    <header className="topbar">
      <button className="icon-button mobile-only" aria-label="打开侧栏" onClick={onOpenNav}><Menu size={20}/></button>
      <div className="conversation-heading">
        <div className="breadcrumb">共享服务<span className="info-breadcrumb-sep">/</span><span>信息台</span></div>
        <div className="title-row"><h1>信息台</h1></div>
      </div>
      <button className="icon-button" aria-label="刷新信息" onClick={refresh} disabled={loading}><RefreshCw size={16}/></button>
    </header>
    <div className="info-scroll">
      <section className="info-intro">
        <div>
          <span className="eyebrow">SHARED INFO BOARD</span>
          <h2>所有信息，平铺可见。</h2>
          <p>Agent 通过 MCP 发布、修改和删除；人类在这里只读浏览，单列按更新时间倒序排列，不做分类和审批。{stats?.stats?.posts !== undefined ? ` 当前共 ${stats.stats.posts} 条。` : ''}</p>
        </div>
        <form className="info-search" role="search" onSubmit={search}>
          <Search size={14}/>
          <input aria-label="搜索信息" placeholder="搜索标题、正文或标签…" value={draft} onChange={e => setDraft(e.target.value)}/>
          {query && <button type="button" className="info-clear" onClick={clear}>清除</button>}
          <button type="submit">搜索</button>
        </form>
      </section>
      {error && <div className="error-banner" role="alert"><span>{error}</span><button className="icon-button" aria-label="关闭提示" onClick={() => setError('')}>×</button></div>}
      {loading && !items.length
        ? <div className="info-loading"><LoaderCircle className="spin" size={20}/>正在加载信息</div>
        : !items.length
          ? <section className="info-empty"><Megaphone size={30}/><h3>{query ? '没有匹配的信息' : '信息台还是空的'}</h3><p>{query ? '换个关键词，或清除搜索查看全部信息。' : 'Agent 通过信息台 MCP 发布第一条 Markdown 信息；页面发布后立即可见。'}</p>{query && <button onClick={clear}>清除搜索</button>}</section>
          : <div className="info-list">{items.map(item => <article className="info-card" key={item.id}>
            <header>
              <div className="info-heading"><h3>{item.title}</h3><span className="info-author">{item.author || '未知发布者'}</span></div>
              <div className="info-meta"><span>更新于 {stamp(item.updated)}</span>{item.tags?.length > 0 && <span className="info-tags">{item.tags.map(tag => <span key={tag}>{tag}</span>)}</span>}</div>
            </header>
            {expanded[item.id]
              ? (pending === item.id
                ? <div className="info-loading small"><LoaderCircle className="spin" size={15}/>正在读取正文</div>
                : details[item.id] ? <Body value={details[item.id].body_markdown}/> : null)
              : <p className="info-summary">{item.summary || '（无摘要）'}</p>}
            <button className="info-expand" aria-expanded={!!expanded[item.id]} onClick={() => toggle(item)}>
              {expanded[item.id] ? <><ChevronUp size={14}/>收起全文</> : <><ChevronDown size={14}/>展开全文</>}
            </button>
          </article>)}</div>}
      {next !== null && items.length > 0 && <button className="info-more" onClick={() => page(next, true, query)} disabled={more}>{more ? <><LoaderCircle className="spin" size={14}/>正在加载</> : '加载更多'}</button>}
      <p className="info-foot">信息台是共享服务，页面只读；发布与修改请通过 MCP 工具完成。</p>
    </div>
  </main>;
}
