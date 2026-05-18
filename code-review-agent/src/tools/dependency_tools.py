"""
依赖安全扫描工具 — 查询 OSV.dev 公开漏洞数据库。

OSV.dev 是 Google 维护的开源漏洞数据库，覆盖 PyPI / npm / Go / Maven 等生态，
无需 API Key，免费公开使用。

query_osv(packages) : 批量查询依赖包的已知 CVE，返回结构化漏洞列表
"""
import json
import logging
import re

import requests
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
_ECOSYSTEM_MAP = {
    ".txt": "PyPI",       # requirements.txt
    ".toml": "PyPI",      # pyproject.toml
    ".json": "npm",       # package.json
    ".mod": "Go",         # go.mod
}

_TIMEOUT = 15


def _detect_ecosystem(filename: str) -> str:
    for ext, eco in _ECOSYSTEM_MAP.items():
        if filename.endswith(ext):
            return eco
    return "PyPI"


def _parse_requirements(content: str) -> list[dict]:
    """从 requirements.txt 内容提取包名和版本。"""
    packages = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 匹配 name==version 或 name>=version 等
        m = re.match(r"^([A-Za-z0-9_\-\.]+)\s*([><=!~^]+)\s*([^\s;#]+)?", line)
        if m:
            packages.append({
                "name": m.group(1),
                "version": m.group(3) or "",
            })
    return packages


@tool
def query_osv(packages_json: str) -> str:
    """
    批量查询 OSV.dev 漏洞数据库，检测依赖包是否存在已知 CVE。

    packages_json: JSON 字符串，格式为：
      [{"name": "django", "version": "2.2.0", "ecosystem": "PyPI"}, ...]
    版本号可为空字符串，OSV 将返回所有已知漏洞。

    返回 JSON 字符串，包含每个包的漏洞列表。
    """
    try:
        packages = json.loads(packages_json)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"packages_json 格式错误: {e}"})

    if not packages:
        return json.dumps({"results": [], "total_vulnerable": 0})

    queries = []
    for pkg in packages:
        q: dict = {"package": {"name": pkg["name"], "ecosystem": pkg.get("ecosystem", "PyPI")}}
        if pkg.get("version"):
            q["version"] = pkg["version"]
        queries.append(q)

    try:
        resp = requests.post(
            _OSV_BATCH_URL,
            json={"queries": queries},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("[DependencyTools] OSV.dev 查询失败: %s", exc)
        return json.dumps({"error": str(exc)})

    results = []
    total_vulnerable = 0
    for pkg, result in zip(packages, data.get("results", [])):
        vulns = result.get("vulns", [])
        if vulns:
            total_vulnerable += 1
            cves = [
                {
                    "id": v.get("id", ""),
                    "summary": v.get("summary", ""),
                    "severity": _extract_severity(v),
                    "aliases": [a for a in v.get("aliases", []) if a.startswith("CVE-")][:3],
                }
                for v in vulns[:5]   # 最多取 5 条
            ]
            results.append({
                "package": pkg["name"],
                "version": pkg.get("version", "unknown"),
                "ecosystem": pkg.get("ecosystem", "PyPI"),
                "vulnerable": True,
                "cves": cves,
            })
        else:
            results.append({
                "package": pkg["name"],
                "version": pkg.get("version", "unknown"),
                "vulnerable": False,
            })

    logger.info("[DependencyTools] OSV 查询完成 | 总包数=%d | 有漏洞=%d", len(packages), total_vulnerable)
    return json.dumps({"results": results, "total_vulnerable": total_vulnerable}, ensure_ascii=False)


def _extract_severity(vuln: dict) -> str:
    """从 OSV 漏洞对象里提取最高严重等级。"""
    severity_map = {"CRITICAL": "critical", "HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
    for sev in vuln.get("severity", []):
        score = sev.get("score", "")
        for k, v in severity_map.items():
            if k in score.upper():
                return v
    # 无显式评级时，有 CVE 的默认 medium
    return "medium"
