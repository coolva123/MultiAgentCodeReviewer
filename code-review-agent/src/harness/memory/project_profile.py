import json
import logging
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS project_profiles (
    repo_name       TEXT PRIMARY KEY,
    tech_stack      TEXT,
    project_type    TEXT,
    security_level  TEXT DEFAULT 'medium',
    frameworks      TEXT,
    conventions     TEXT,
    summary         TEXT,
    readme_sha      TEXT,
    raw_profile     JSONB,
    updated_at      TIMESTAMPTZ DEFAULT now()
)
"""


class ProjectProfileStore:
    _schema_ready: bool = False

    @contextmanager
    def _connection(self):
        import psycopg2
        from config.settings import PG_DATABASE_URL
        conn = psycopg2.connect(PG_DATABASE_URL)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self):
        if ProjectProfileStore._schema_ready:
            return
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(_CREATE_TABLE)
            ProjectProfileStore._schema_ready = True
            logger.info("[ProjectProfileStore] schema ready")
        except Exception as exc:
            logger.error("[ProjectProfileStore] schema init failed: %s", exc)

    def get_profile(self, repo_name: str, current_readme_sha: str) -> Optional[dict]:
        """缓存命中返回画像 dict；未命中返回 None。
        命中条件：readme_sha 匹配，或 updated_at 在 30 天内。
        """
        self._ensure_schema()
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT tech_stack, project_type, security_level, frameworks,
                               conventions, summary, readme_sha, raw_profile
                        FROM project_profiles
                        WHERE repo_name = %s
                          AND (readme_sha = %s OR updated_at > NOW() - INTERVAL '30 days')
                        """,
                        (repo_name, current_readme_sha),
                    )
                    row = cur.fetchone()
            if row:
                return {
                    "tech_stack":      row[0] or "",
                    "project_type":    row[1] or "",
                    "security_level":  row[2] or "medium",
                    "frameworks":      row[3] or "",
                    "conventions":     row[4] or "",
                    "summary":         row[5] or "",
                    "readme_sha":      row[6] or "",
                    "from_cache":      True,
                }
            return None
        except Exception as exc:
            logger.warning("[ProjectProfileStore] get_profile failed: %s", exc)
            return None

    def save_profile(self, repo_name: str, profile: dict, readme_sha: str) -> None:
        self._ensure_schema()
        try:
            with self._connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO project_profiles
                            (repo_name, tech_stack, project_type, security_level,
                             frameworks, conventions, summary, readme_sha, raw_profile, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (repo_name) DO UPDATE SET
                            tech_stack     = EXCLUDED.tech_stack,
                            project_type   = EXCLUDED.project_type,
                            security_level = EXCLUDED.security_level,
                            frameworks     = EXCLUDED.frameworks,
                            conventions    = EXCLUDED.conventions,
                            summary        = EXCLUDED.summary,
                            readme_sha     = EXCLUDED.readme_sha,
                            raw_profile    = EXCLUDED.raw_profile,
                            updated_at     = NOW()
                        """,
                        (
                            repo_name,
                            profile.get("tech_stack", ""),
                            profile.get("project_type", ""),
                            profile.get("security_level", "medium"),
                            profile.get("frameworks", ""),
                            profile.get("conventions", ""),
                            profile.get("summary", ""),
                            readme_sha,
                            json.dumps(profile),
                        ),
                    )
            logger.info("[ProjectProfileStore] saved profile | repo=%s", repo_name)
        except Exception as exc:
            logger.error("[ProjectProfileStore] save_profile failed: %s", exc)


_instance: Optional[ProjectProfileStore] = None


def get_project_profile_store() -> ProjectProfileStore:
    global _instance
    if _instance is None:
        _instance = ProjectProfileStore()
    return _instance
