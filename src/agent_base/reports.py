"""Runtime-owned reporting instructions and separately verified Run receipts."""
from __future__ import annotations

import json
import uuid
from pathlib import Path, PurePosixPath

import jsonschema

from .package import within


SCHEMA = json.loads((Path(__file__).parent / "schemas/agent-report-v1.schema.json").read_text(encoding="utf-8"))


def report_prompt() -> str:
    return (
        "\n<agent_base_report_contract>\n"
        "这是基座在本次无人值守启动时注入的最终汇报契约，不是仓库需要维护的提示词。\n"
        "完成任务后，最终回复必须是一个符合下列 schema 的 JSON 对象。不要加 Markdown 围栏或 JSON 前后的说明。"
        "执行过程仍可正常调用工具；此要求只约束最终回复，不改变 CLI 的原生事件协议。\n"
        "任务输入不足时不得等待用户交互，报告 blocked 和需要补充的信息；只完成部分工作用 partial，执行失败用 failed。"
        "只有确实完成工作才用 completed。不要盲目重试结果未知的外部操作。\n"
        "artifacts 仅列出实际生成的交付文件，使用 artifacts/ 下的相对路径，不得越界或使用绝对路径。"
        "没有交付文件时用空数组。checks 只报告实际执行过的检查；未执行用 not_run。"
        "检查失败或声明产物缺失时不得报告 completed。\n"
        "Agent 自报检查不等于基座独立检查；不要伪造基座 verification、进程状态或文件哈希。"
        "issues/next_steps 没有内容时用空数组。不得输出凭据。\n"
        + json.dumps(SCHEMA, ensure_ascii=False, indent=2)
        + "\n</agent_base_report_contract>\n"
    )


def parse_agent_report(text: str, workspace: Path) -> dict:
    if len(text.encode("utf-8")) > 256 * 1024:
        raise ValueError("Agent 结构化报告超过 256 KiB")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Agent 结构化报告包含重复字段")
            result[key] = value
        return result

    def invalid_number(_):
        raise ValueError("Agent 结构化报告不能包含 NaN/Infinity")

    try:
        report = json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_number)
        jsonschema.Draft202012Validator(SCHEMA).validate(report)
    except (ValueError, jsonschema.ValidationError) as error:
        raise ValueError("Agent 最终回复不符合结构化报告契约；查看 runtime.jsonl") from error
    seen = set()
    for artifact in report["artifacts"]:
        path = artifact["path"]
        if "\\" in path or any(part in {".", "..", ""} for part in path.split("/")):
            raise ValueError("Agent 报告产物必须是 artifacts/ 下的普通相对路径")
        within(workspace, path, exists=False)
        normalized = PurePosixPath(path).as_posix()
        if normalized in seen:
            raise ValueError("Agent 报告包含重复产物路径")
        seen.add(normalized)
    return report


def write_run_report(job: dict) -> Path | None:
    """Atomic supervisor receipt; never fabricate an Agent answer after a crash."""
    if job["status"] not in {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}:
        return None
    if job["spec"].get("conversation_id") or job["spec"].get("install_only"):
        return None
    result = job.get("result") or {}
    report = {
        "schema_version": 1, "run_id": job["id"], "status": job["status"],
        "agent_report": result.get("agent_report"),
        "verification": result.get("verification", "not_verified"),
        "artifacts": result.get("artifacts", []), "error": job.get("error"),
        "session_id": job.get("session_id"), "installation_id": job["spec"].get("installation_id"),
        "activation": job["spec"].get("activation"),
        "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
    }
    target = Path(job["root"]) / "report.json"
    temporary = target.with_name(".report-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
