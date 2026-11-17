"""模拟知识库 MCP 服务，用于端到端验证知识库经注册表接入中央网关。

工具契约（``knowledge_catalog`` / ``knowledge_search`` / ``knowledge_read`` /
``knowledge_submit``）与真实 ``knowledge_mcp.py`` 对齐（GOVERNANCE_DESIGN.md S-8）。
**演示文档为假数据，不承载正式知识。**

运行：``python -m agent_base knowledge-mock --port 8892``（或直接 ``agent-base knowledge-mock``）。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

# 模拟知识库演示文档（仅验证用，非正式知识）。
_KB_DOCS = [
    {"document_id": "kb-ops-001", "version_id": "v1", "namespace": "ops",
     "title": "上线发布流程", "outline": ["准备", "发布", "回滚"]},
    {"document_id": "kb-ops-002", "version_id": "v1", "namespace": "ops",
     "title": "故障响应手册", "outline": ["发现", "止损", "复盘"]},
    {"document_id": "kb-dev-001", "version_id": "v1", "namespace": "dev",
     "title": "编码规范", "outline": ["命名", "日志", "错误处理"]},
]


def create_knowledge_mcp() -> FastMCP:
    mcp = FastMCP("knowledge-mock")

    @mcp.tool()
    def knowledge_catalog(namespace: str = "", offset: int = 0) -> dict:
        """先发现获准项目或知识目录；只含摘要，不含正文。"""
        items = [d for d in _KB_DOCS if not namespace or d["namespace"] == namespace]
        return {"items": [{"document_id": d["document_id"], "version_id": d["version_id"],
                           "namespace": d["namespace"], "title": d["title"],
                           "outline": d["outline"]} for d in items[offset:offset + 10]],
                "total": len(items)}

    @mcp.tool()
    def knowledge_search(namespace: str, query: str, limit: int = 5, offset: int = 0) -> dict:
        """搜索指定项目的已发布知识，先看候选，再按版本读取章节。"""
        hits = [{"document_id": d["document_id"], "version_id": d["version_id"],
                 "namespace": d["namespace"], "title": d["title"]}
                for d in _KB_DOCS
                if d["namespace"] == namespace and query.lower() in d["title"].lower()]
        return {"hits": hits[offset:offset + limit], "total": len(hits)}

    @mcp.tool()
    def knowledge_read(document_id: str, version_id: str, view: str,
                       section_id: str = "", cursor: str = "", max_chars: int = 2000) -> dict:
        """按固定版本读 outline，再按 section_id 读 section；返回游标时继续读取。"""
        doc = next((d for d in _KB_DOCS
                    if d["document_id"] == document_id and d["version_id"] == version_id), None)
        if doc is None:
            return {"error": "document not found"}
        if view == "outline":
            return {"document_id": document_id, "version_id": version_id,
                    "title": doc["title"], "sections": doc["outline"]}
        return {"document_id": document_id, "version_id": version_id,
                "section_id": section_id or (doc["outline"][0] if doc["outline"] else ""),
                "text": f"（模拟正文）{doc['title']} / {section_id or '§'} 章节内容"}

    @mcp.tool()
    def knowledge_submit(namespace: str, operation: str, candidate: dict, reason: str,
                         idempotency_key: str, document_id: str = "",
                         base_version_id: str = "") -> dict:
        """投稿新增或修改知识（模拟：直接受理，返回受理号）。"""
        return {"accepted": True, "submission_id": "kb-sub-" + idempotency_key[:12],
                "namespace": namespace, "operation": operation}

    return mcp


def knowledge_http_app():
    """knowledge 模拟 MCP 的 streamable-http ASGI app（挂载于 ``/mcp``）。"""
    return create_knowledge_mcp().streamable_http_app()


def serve_knowledge(host: str = "127.0.0.1", port: int = 8892):
    import uvicorn

    uvicorn.run(knowledge_http_app(), host=host, port=port)


if __name__ == "__main__":
    serve_knowledge()
