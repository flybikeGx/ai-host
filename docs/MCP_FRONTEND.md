# 带人类前端的 MCP：挂载、导航、登录态与安全边界

本文冻结 AIHO-33 D1 的前端挂载契约，配套 [MCP_PACKAGE.md](MCP_PACKAGE.md)。结论面向第一方实现，同时回答第三方 MCP 自带前端时如何接入；本期不要求公开市场或远程模块分发。

## 1. 现状与约束

- 前端是单个 Vite/React SPA（`frontend/src/main.jsx`），构建产物在 `src/agent_base/static/chat/`，由 FastAPI 固定路由返回同一个 `index.html`；
- 页面、侧栏导航、`routeView()` 都是硬编码：`/chat`、`/cron`、`/dashboard`、`/knowledge`、`/services`、`/login`；
- 登录由 `AuthShell` + 同源 HttpOnly `agent_base_session` cookie 完成；管理 API 在 `web.py` 统一 `authorize`；
- 设计 token、Markdown 渲染、API client、页面卡片样式分别散在 `style.css`、`api.js`、各页面 CSS；
- 无组件库、无微前端运行时、无独立前端构建流程；
- 生产构建物有意入 Git，改前端要同时提交源码与 `static/chat` 构建物。

约束：不重写三个 MCP 的 UI 视觉和交互；不为统一引入新框架；第一方页面继续在同一工作区、同一登录态、同一视觉风格下工作；第三方前端不能与主应用共享 cookie/存储，不能直接读库。

## 2. 方案对比

| 方案 | 第一方体验 | 安装/停用是否需重建 | 共享壳/token | 第三方隔离 | 改动成本 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| A. build-time registry（包源码编译进主 SPA） | 完全一致，复用现有壳 | 停用不需要；新增/删除包需要在发版时重建 | 直接 import `_shared` | 弱：第三方代码会进主 bundle，不能接受 | 低：只加清单生成脚本和动态导航 | **第一方采用** |
| B. 独立静态包 + 共享壳（每个包一个 SPA，同源） | 需要把壳抽到 `_shared` 并由每个包自带 | 停用不需要；包更新只替换静态资源 | 通过 `_shared` npm/相对导入 | 中：同源，仍能读 cookie/存储 | 中高：要抽壳、改构建、改登录渲染 | 作为 A 的演进方向保留 |
| C. 远程模块（Module Federation / import maps） | 可运行时加载，体验接近 A | 不需要 | 需共享依赖版本管理 | 取决于运行时沙箱，默认弱 | 高：构建链和运行时都要改 | 本期不做 |
| D. iframe（同源或独立源 + sandbox） | 独立页面壳，和主 SPA 有边界感 | 不需要 | 需 postMessage/能力票据 | 强（无 `allow-same-origin` 时） | 中：需宿主页、握手和 CSP | **第三方采用** |

关键取舍：

- A 让第一方代码继续走现有构建/样式/登录，不为了“包化”重写 UI；代价是新增包必须随主应用发版，但第一方包本来就随仓库发版，停用/启用仍可运行时门控。
- D 给第三方真正的隔离边界：不同源或 opaque origin、无 cookie、能力票据 + `postMessage`；代价是第三方要按宿主协议写页面，不能直接复用主应用组件。
- B 是未来把第一方壳也抽出去时的路径；本期不承诺，避免一次性重构 SPA。
- 不允许“第一方用 A、第三方也用 A”的省事方案：那会让不可信代码进主 bundle，破坏隔离承诺。

## 3. 冻结的挂载契约

清单 `frontend.mount` 选择挂载方式，平台按同一套生命周期处理：

```yaml
frontend:
  mount: spa-module | static-iframe
  route: /knowledge
  navigation: {label: 知识库, icon: book-open, order: 30}
  entry: frontend/src/index.jsx      # spa-module
  assets: frontend/dist              # static-iframe
  requires: [knowledge.read]
  standalone: false
```

|  | `spa-module`（第一方） | `static-iframe`（第三方/隔离） |
| --- | --- | --- |
| 代码来源 | 包内 React 源码，构建时经生成的 registry 进入主 SPA | 包内预构建静态目录，平台原样托管 |
| 页面壳 | 主应用壳（侧栏、顶栏、登录、错误提示） | 包内自带完整页面 |
| 路由 | 平台按清单注册，服务端校验 `enabled` | 平台按清单注册宿主页，内部 iframe 指向 `/_mcp-ui/<id>/` |
| 安装/停用 | 停用即时隐藏导航、路由 404；新增包需重建 | 停用即时隐藏/404；新增只放静态文件 |
| 登录态 | 同源 cookie；沿用 `AuthShell`/`authorize` | 不读 cookie；宿主 postMessage 下发短期能力票据 |
| 共享代码 | `_shared/frontend` 直接 import | 可选，只能打包进自己的静态产物 |
| 安全边界 | 可信第一方，同源 | sandbox iframe + 能力票据 + CSP，按不可信处理 |
| 构建产物 | 进主 SPA 的 `static/chat` | 包内 `frontend/dist`，平台挂到 `/_mcp-ui/<id>/` |

`spa-module` 的 `standalone` 必须为 false，`static-iframe` 必须为 true；清单校验器强制。第一方如果将来需要独立页面，应显式改成 `static-iframe`，不能保留“第一方同源特例”。

## 4. 第一方：build-time registry

### 4.1 构建

- 新增脚本（D2）扫描 `first_party/*/mcp.yaml`，生成 `frontend/src/mcp-registry.generated.js`：
  ```js
  export const mcpModules = [
    {id: 'knowledge', route: '/knowledge', label: '知识库', icon: 'book-open', order: 30,
     requires: ['knowledge.read'], load: () => import('../../first_party/knowledge/frontend/src/index.jsx')},
  ];
  ```
- `main.jsx` 不再硬编码 `KnowledgePage`/`FilesPage`；改为从 registry 取当前路由的组件，导航项也由 registry 生成；
- 构建仍是一次 `npm run build`，产物仍提交到 `src/agent_base/static/chat/`；
- 新增/删除第一方包需要重建并提交产物；**启用/停用不需要重建**。

### 4.2 运行时门控

- 主应用启动时请求 `/api/mcp/packages`（只返回当前用户可见且 `enabled` 的包，含 route/nav/icon/order/requires）；
- 侧栏只渲染返回项；访问未启用/未安装的 route 时，服务端直接 404，前端不再渲染组件；
- 接口失败时回退到构建期 registry 的“全部第一方包”列表，但服务端路由仍会 404；不把失败当成“全部可用”；
- `requires` 与用户权限取交集：无权限的包不显示，直接访问返回 404（不是 403，避免泄露包是否存在）。

### 4.3 URL 兼容

- `/knowledge`、`/files`、`/info` 等路径不变；`/chat`、`/cron`、`/dashboard`、`/services`、`/login` 仍由平台保留，清单不得占用；
- 现有 `location.pathname` 路由、`history.pushState`、移动端侧栏行为保持；
- 知识库的一次性连接片段 `#connect=` 仍由宿主在挂载前消费，包组件不直接读 URL 凭据。

### 4.4 `_shared/frontend`

D2 起从现有 `frontend/src` 抽取，先搬后改，保持视觉不变：

| 内容 | 来源 | 说明 |
| --- | --- | --- |
| 设计 token/基础样式 | `style.css`、`auth.css` | 变量、间距、颜色、响应式断点；包只引用，不复制 |
| 壳组件 | `AuthShell`、侧栏/顶栏/错误提示 | 第一方 `spa-module` 由主应用提供，不要求每个包自带 |
| Markdown | `main.jsx` 中的 `Markdown` 配置 | 统一 `skipHtml`、远程图片禁用、链接策略 |
| API client | `api.js` | 同源 cookie、401 事件、缓存禁用、错误形状 |
| 测试工具 | Playwright 登录/导航 helper | 第一方页面共用 |

`_shared/frontend` 不是 npm 包，第一阶段用相对路径 import；不复制到每个包。

## 5. 第三方：静态包 + sandbox iframe

### 5.1 托管

- 包构建产物放 `frontend/dist`，安装时校验后复制/挂载到 `<state>/mcp/packages/<id>/dist`；
- 平台只读托管 `GET /_mcp-ui/<id>/...`，带 `Content-Security-Policy`、`X-Content-Type-Options: nosniff`，禁止目录列表和源映射外泄；
- 页面路由（如 `/apps/<id>`）返回一个最小宿主 HTML：加载宿主握手脚本，创建 `iframe` 指向 `/_mcp-ui/<id>/index.html`；
- 不把第三方 HTML 拼进主 SPA，也不把第三方 JS 放进主 bundle。

### 5.2 sandbox 与能力票据

- `iframe sandbox="allow-scripts allow-forms"`：**不加** `allow-same-origin`，使第三方文档运行在 opaque origin，无法读主应用 cookie、localStorage、IndexedDB；
- 宿主在加载后通过 `postMessage` 做一次握手：宿主 → iframe 发送 `{type:"mcp-ui/init", route, capability}`，iframe 回 `{type:"mcp-ui/ready"}`；
- `capability` 是服务端签发的不透明短期票据（默认 15 分钟，可续期），绑定：当前登录用户/工作区、包 `id`、`permissions.api` 子集、过期时间；服务端只存哈希；
- iframe 调平台 API 时带 `X-MCP-Capability: <token>`，不带 cookie；服务端按票据范围校验，不依赖 `Origin`（iframe 的 Origin 可能是 `null`）；
- 票据不放进 URL、日志、`localStorage`；只存在 iframe 内存。登出、停用、撤权时服务端立即拒绝续期；
- 需要上传/下载等高风险操作时，走平台签发的一次性对象票据，不把通用能力票据交给第三方长期持有。

这比“同源 iframe + 共享 cookie”更严格：同源 iframe 即使有 sandbox，只要同时有 `allow-same-origin` 和 `allow-scripts` 就能绕过 sandbox 读 cookie，因此禁止该组合。

### 5.3 CSP

第一方页面（主 SPA）：

```text
default-src 'self';
script-src 'self';
style-src 'self' 'unsafe-inline';   /* 现有内联 style 属性 */
img-src 'self' data:;
connect-src 'self';
frame-ancestors 'self';
base-uri 'none';
object-src 'none';
```

第三方宿主页/静态包：

```text
default-src 'none';
script-src 'self';
style-src 'self' 'unsafe-inline';
img-src 'self' data:;
connect-src 'self';
frame-ancestors 'self';
sandbox allow-scripts allow-forms;
```

- 第一方不放开远程脚本/图片；Markdown 中的 HTML 仍被移除，远程图片仍不加载；
- 第三方 `connect-src 'self'` 只允许调平台 API/自身静态资源，不允许任意外网出口；
- CSP 由 FastAPI 中间件按路由写入，开发环境同样生效；不靠前端 meta 标签。

## 6. 登录态与权限

| 场景 | 登录态来源 | API 鉴权 | 登出/撤权 |
| --- | --- | --- | --- |
| 第一方 `spa-module` | 同源 `agent_base_session` cookie；`AuthShell` 处理登录/刷新/过期 | `web.py` 的 `authorize` + 路由级权限 | cookie 失效/登出即拒绝；页面随 `/api/mcp/packages` 刷新 |
| 第三方 `static-iframe` | 宿主校验会话后签发能力票据 | `X-MCP-Capability` + 票据范围 | 票据过期/撤权/停用立即失效；iframe 回退到未授权态 |

- 第一方页面不额外申请 token，不把管理 Bearer token 写进页面；`agent_base_session` 为 HttpOnly、SameSite 严格；
- 权限显示与真实校验分离：按钮按 `/me` 或包 `requires` 隐藏，服务端始终按 `permissions.api` 校验；
- 包 `permissions.anonymous` 在 v1 恒为 false；登录前不渲染包页面，未登录访问重定向 `/login?next=...`（沿用现有 allowlist）；
- 审计：页面写操作沿用现有审计入口，记录 actor、action、资源 id，不记录正文/凭据；第三方票据的包 id 进入审计元数据。

## 7. 安全边界（必须满足）

1. **无直接数据访问**：包前端只能调平台 HTTP API；不允许传数据根路径、打开 SQLite、读 blob 目录。
2. **服务凭证不进浏览器**：`service-token`、MCP grant、对象存储凭证只用于平台→服务或服务→服务。
3. **第三方不共享会话**：无 cookie、无同源存储；必须走能力票据。
4. **路径与内容**：静态资源只从包 `assets` 目录按白名单扩展名提供；拒绝 `..`、符号链接越界、目录列表；HTML/JS 不内联到主应用。
5. **上传/下载**：沿用现有大小上限、Range、sha256、对象级票据；前端不把整个大文件读入内存。
6. **MCP 端点**：知识库/文件枢纽服务继续拒绝浏览器 `Origin`；前端只调人类 API，不直连 MCP。
7. **失败关闭**：清单无效、包未安装/停用、票据无效、CSP 配置缺失时，页面/服务不可用，不降级为“全部开放”。

## 8. 测试矩阵

- 清单：route 唯一、保留路由拒绝、`standalone`/`assets`/`entry` 规则、`requires` 必须是 `api` 子集；
- 第一方：登录后导航出现、停用后导航消失/路由 404、重新启用恢复、URL 不变、现有页面 Playwright 用例通过；
- 第三方：iframe sandbox 属性、无 cookie 可读、能力票据有效期/撤权、`postMessage` 握手、CSP 头、越权 API 403/404；
- 安全：路径穿越、目录列表、MIME 嗅探、远程图片/HTML 注入、登出后 iframe 失效；
- 构建：registry 生成、缺失 `entry` 构建失败、静态包缺失 `assets` 安装失败；
- 窄屏与键盘：侧栏抽屉、iframe 高度、焦点进入/退出。

## 9. 实施顺序与回退

- D2（知识库）：先加 `_shared` 抽取与 registry 生成，再把 `KnowledgePage` 搬到 `first_party/knowledge/frontend`，`/knowledge` 不变；不引入 iframe；
- D3（文件枢纽）：同样走 `spa-module`，补按需安装门控；
- D4（第三方示例）：新增 `static-iframe` 示例包，验证隔离与票据；
- 回退：删除生成的 registry/清单条目即可回到硬编码路由；已提交的主 SPA 构建物随 Git 回滚；停用状态只影响运行时，不迁移数据。

## 10. 评审确认点

1. 第一方 `spa-module` + 第三方 `static-iframe` 的分层是否可以接受；
2. 第三方页面路由前缀用 `/apps/<id>` 还是 `/_mcp/<id>`（本文用 `/apps/<id>`，静态资源用 `/_mcp-ui/<id>/`）；
3. 第三方能力票据用服务端不透明票据 + `X-MCP-Capability`，是否需要再叠一层 `postMessage` 的 nonce/来源校验；
4. 第一方新增/删除包允许“随发版重建”，不接受运行时远程模块，是否符合本期预期。
