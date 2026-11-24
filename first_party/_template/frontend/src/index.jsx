import React from 'react';

/**
 * Minimal spa-module view for the package template.
 *
 * The generated registry imports this default export and renders it inside the
 * host shell. Keep data access behind the platform API client; never import a
 * database module or read a service credential here.
 */
export default function ExamplePage() {
  return (
    <main className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">FIRST-PARTY MCP</span>
          <h1>示例包</h1>
        </div>
      </header>
      <section className="card">
        <p>把 <code>first_party/_template</code> 复制成 <code>first_party/&lt;id&gt;/</code>，替换后端工具、页面和 <code>mcp.yaml</code>。</p>
        <p>这个空包只用于验证清单、导航和安装流程，不承载业务。</p>
      </section>
    </main>
  );
}
