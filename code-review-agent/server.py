"""
MultiAgent Code Reviewer — FastAPI Web 服务

启动方式：
    cd code-review-agent
    uvicorn server:app --host 0.0.0.0 --port 8080 --reload

访问地址：http://localhost:8080
"""
import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("server")

app = FastAPI(title="AI Code Reviewer", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 内存任务存储（单进程够用）
_tasks: dict[str, dict] = {}


def _base_state(session_id: str) -> dict:
    return {
        # 输入
        "diff_content": "",
        "pr_metadata": {},
        "repo_name": "",
        "repo_url": "",
        "session_id": session_id,
        # Agent 输出
        "diff_files": [],
        "diff_summary": {},
        "routing_decision": {},
        "security_findings": [],
        "quality_findings": [],
        "final_report": None,
        # Supervisor 多 Agent
        "research_context": "",
        "supervisor_instruction": "",
        "iteration_count": 0,
        "review_pipeline_called": False,
        # Harness
        "tool_call_log": [],
        "agent_messages": [],
        "errors": [],
        # 控制流
        "current_step": "init",
        "review_complete": False,
    }


async def _run_graph(
    session_id: str,
    initial_state: dict,
    pr_url: Optional[str] = None,
):
    _tasks[session_id]["status"] = "running"

    # ── 模式 A：后台获取 PR diff ──────────────────────────────────────────────
    if pr_url:
        _tasks[session_id]["progress"] = "正在获取 PR 信息..."
        try:
            from config.settings import GITHUB_TOKEN
            from src.tools.github_tools import fetch_pr_diff

            if not GITHUB_TOKEN:
                raise ValueError("服务器未配置 GITHUB_TOKEN")

            diff_content, repo_name, pr_metadata = await asyncio.to_thread(
                fetch_pr_diff, pr_url, GITHUB_TOKEN
            )
            initial_state["diff_content"] = diff_content
            initial_state["repo_name"] = repo_name or "unknown"
            initial_state["pr_metadata"] = pr_metadata or {}
            # 从 PR URL 提取仓库 URL 供 Research Agent 使用
            # https://github.com/owner/repo/pull/123 → https://github.com/owner/repo
            parts = pr_url.rstrip("/").split("/pull/")
            initial_state["repo_url"] = parts[0] if len(parts) == 2 else ""
        except Exception as exc:
            logger.error("获取 PR 失败 session=%s: %s", session_id, exc)
            _tasks[session_id]["status"] = "error"
            _tasks[session_id]["error"] = f"获取 PR 信息失败：{exc}"
            return

    # ── 执行 Graph ────────────────────────────────────────────────────────────
    _tasks[session_id]["progress"] = "审查进行中，通常需要 1-3 分钟..."
    try:
        from src.graph.supervisor_graph import supervisor_graph as graph

        config = {"configurable": {"thread_id": session_id}}
        result = await asyncio.to_thread(graph.invoke, initial_state, config)

        report = result.get("final_report") or "⚠️ 报告生成失败，请检查日志。"
        _tasks[session_id].update(
            status="done",
            progress="审查完成",
            report=report,
            stats={
                "security": len(result.get("security_findings", [])),
                "quality": len(result.get("quality_findings", [])),
                "tools": len(result.get("tool_call_log", [])),
            },
        )
    except Exception as exc:
        logger.exception("审查失败 session=%s", session_id)
        _tasks[session_id]["status"] = "error"
        _tasks[session_id]["error"] = str(exc)


# ── 请求模型 ──────────────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    mode: str                           # "pr_url" | "diff_text"
    pr_url: Optional[str] = None
    diff_content: Optional[str] = None
    repo_name: Optional[str] = "my-project"


# ── API 路由 ──────────────────────────────────────────────────────────────────

@app.post("/api/review")
async def submit_review(body: ReviewRequest, background_tasks: BackgroundTasks):
    session_id = str(uuid.uuid4())
    state = _base_state(session_id)

    if body.mode == "pr_url":
        if not body.pr_url or not body.pr_url.strip():
            raise HTTPException(400, "pr_url 不能为空")
        _tasks[session_id] = _new_task()
        background_tasks.add_task(_run_graph, session_id, state, body.pr_url.strip())

    elif body.mode == "diff_text":
        if not body.diff_content or not body.diff_content.strip():
            raise HTTPException(400, "diff_content 不能为空")
        repo = body.repo_name or "my-project"
        state.update(
            diff_content=body.diff_content,
            repo_name=repo,
            pr_metadata={"title": f"Review of {repo}"},
        )
        _tasks[session_id] = _new_task()
        background_tasks.add_task(_run_graph, session_id, state)

    else:
        raise HTTPException(400, "mode 必须是 pr_url 或 diff_text")

    return {"session_id": session_id}


@app.post("/api/review/upload")
async def submit_file_review(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    repo_name: str = Form(default="uploaded-project"),
):
    if not files:
        raise HTTPException(400, "至少上传一个文件")

    session_id = str(uuid.uuid4())
    diff_files = []

    for f in files:
        raw = await f.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")

        diff_files.append({
            "filename": f.filename,
            "patch": text,
            "change_type": "added",
            "change_category": "upload",
            "additions": len(text.splitlines()),
            "deletions": 0,
            "is_complex_logic": False,
            "is_security_sensitive": True,
        })

    state = _base_state(session_id)
    state.update(
        repo_name=repo_name,
        pr_metadata={"title": f"文件上传审查：{repo_name}"},
        diff_files=diff_files,
        diff_summary={
            "pr_nature": "file_upload",
            "estimated_risk": "unknown",
            "total_files": len(diff_files),
            "total_additions": sum(f["additions"] for f in diff_files),
            "total_deletions": 0,
        },
        routing_decision={
            "run_security": True,
            "run_quality": True,
            "priority": "high",
            "focus_files": [f["filename"] for f in diff_files],
            "reason": "文件上传模式：全量审查所有上传文件",
        },
    )

    _tasks[session_id] = _new_task()
    background_tasks.add_task(_run_graph, session_id, state)
    return {"session_id": session_id}


@app.get("/api/review/{session_id}")
async def get_review(session_id: str):
    task = _tasks.get(session_id)
    if not task:
        raise HTTPException(404, "Session 不存在")
    return {
        "session_id": session_id,
        "status": task["status"],
        "progress": task.get("progress", ""),
        "report": task.get("report"),
        "error": task.get("error"),
        "stats": task.get("stats"),
    }


@app.get("/api/health")
async def health():
    return {"status": "ok"}


def _new_task() -> dict:
    return {
        "status": "queued",
        "progress": "等待开始...",
        "report": None,
        "error": None,
        "stats": None,
        "created_at": datetime.now().isoformat(),
    }


# ── 静态文件（必须最后挂载）──────────────────────────────────────────────────
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
