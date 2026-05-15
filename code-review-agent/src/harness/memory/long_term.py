"""
长期记忆：pgvector 向量存储封装。

使用用户本地 PostgreSQL（localhost:15432）+ pgvector 0.5.0 扩展。
向量化模型：ZhiPu embedding-3（2048 维）。

设计：
  - 嵌入模型懒加载，复用（一次初始化）
  - 每次 query/store 使用独立连接，避免并行节点共享连接的线程安全问题
  - schema 初始化只运行一次（类变量 _schema_ready 标记）

职责：
  store()  — 审查完成后，将所有 findings 向量化写入 review_findings 表
  query()  — 新 PR 审查前，按语义相似度检索历史 findings 注入 Prompt
"""
import logging
from contextlib import contextmanager
from typing import Optional

import numpy as np
import psycopg2
from pgvector.psycopg2 import register_vector

from config.settings import PG_DATABASE_URL, PG_EMBEDDING_DIM

logger = logging.getLogger(__name__)

# ── DDL ───────────────────────────────────────────────────────────────────────

_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS review_findings (
    id          SERIAL PRIMARY KEY,
    repo_name   TEXT NOT NULL,
    filename    TEXT NOT NULL,
    category    TEXT NOT NULL,
    severity    TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    suggestion  TEXT NOT NULL,
    embedding   VECTOR({PG_EMBEDDING_DIM}),
    created_at  TIMESTAMPTZ DEFAULT NOW()
)
"""

# HNSW 对 >2000 维度不支持；退回到顺序扫描即可（小规模数据足够快）
_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS review_findings_hnsw_idx
    ON review_findings
    USING hnsw (embedding vector_cosine_ops)
"""


# ── LongTermMemory ────────────────────────────────────────────────────────────

class LongTermMemory:
    _schema_ready: bool = False  # 类变量，进程内只初始化一次 schema

    def __init__(self):
        self._embed_model = None

    # ── 连接（每次操作独立，避免并行节点共享）─────────────────────────────────

    @contextmanager
    def _connection(self):
        """上下文管理器：打开独立连接，用完自动关闭。"""
        conn = psycopg2.connect(PG_DATABASE_URL)
        register_vector(conn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self):
        if LongTermMemory._schema_ready:
            return
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(_CREATE_TABLE)
            # 尝试建 HNSW 索引（可选，维度超 2000 时会跳过）
            try:
                with self._connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(_CREATE_INDEX)
                logger.info("[LongTermMemory] schema ready (table + hnsw index)")
            except Exception:
                logger.info("[LongTermMemory] schema ready (table only, hnsw skipped — dim > 2000)")
            LongTermMemory._schema_ready = True
        except Exception as exc:
            logger.error("[LongTermMemory] schema init failed: %s", exc)

    # ── 嵌入模型（懒加载，进程内复用）────────────────────────────────────────

    def _get_embed(self):
        if self._embed_model is None:
            from config.settings import get_embeddings
            self._embed_model = get_embeddings()
        return self._embed_model

    def _embed(self, text: str) -> np.ndarray:
        vec = self._get_embed().embed_query(text)
        return np.array(vec, dtype=np.float32)

    # ── 公开 API ───────────────────────────────────────────────────────────────

    def store(self, repo_name: str, findings: list[dict]) -> int:
        """
        将本次审查的 findings 向量化后写入 pgvector。
        返回实际写入条数。
        """
        if not findings:
            return 0
        self._ensure_schema()
        try:
            count = 0
            with self._connection() as conn:
                with conn.cursor() as cur:
                    for f in findings:
                        text = f"{f.get('title', '')}. {f.get('description', '')}"
                        vec = self._embed(text)
                        cur.execute("""
                            INSERT INTO review_findings
                                (repo_name, filename, category, severity, title, description, suggestion, embedding)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """, (
                            repo_name,
                            f.get("file", "unknown"),
                            f.get("category", "general"),
                            f.get("severity", "info"),
                            f.get("title", ""),
                            f.get("description", ""),
                            f.get("suggestion", ""),
                            vec,
                        ))
                        count += 1
            logger.info("[LongTermMemory] stored %d findings | repo=%s", count, repo_name)
            return count
        except Exception as exc:
            logger.error("[LongTermMemory] store failed: %s", exc)
            return 0

    def query(
        self,
        repo_name: str,
        query_text: str,
        top_k: int = 5,
    ) -> list[str]:
        """
        检索与 query_text 语义最近的历史 findings（同一 repo）。
        返回格式化字符串列表，可直接追加到 Prompt 中。
        """
        self._ensure_schema()
        try:
            vec = self._embed(query_text)
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT title, description, suggestion, severity, filename
                        FROM review_findings
                        WHERE repo_name = %s
                        ORDER BY embedding <=> %s
                        LIMIT %s
                    """, (repo_name, vec, top_k))
                    rows = cur.fetchall()

            if not rows:
                return []

            results = [
                f"[{severity.upper()}] {filename} — {title}\n"
                f"  Issue: {desc}\n"
                f"  Fix: {suggestion}"
                for title, desc, suggestion, severity, filename in rows
            ]
            logger.info("[LongTermMemory] query returned %d historical findings", len(results))
            return results
        except Exception as exc:
            logger.warning("[LongTermMemory] query failed: %s", exc)
            return []


# ── 模块级单例 ─────────────────────────────────────────────────────────────────

_instance: Optional[LongTermMemory] = None


def get_long_term_memory() -> LongTermMemory:
    global _instance
    if _instance is None:
        _instance = LongTermMemory()
    return _instance
