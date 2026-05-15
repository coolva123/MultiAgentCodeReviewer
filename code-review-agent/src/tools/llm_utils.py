"""
跨 Provider 的结构化输出工具。

DeepSeek / GLM 不支持 response_format=json_schema，
统一改为：在 Prompt 里要求输出 JSON，然后手动解析 + Pydantic 验证。

Day 7：call_structured 增加简单重试（指数退避，最多 2 次）。
"""
import json
import logging
import re
import time
from typing import Type, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

JSON_SUFFIX = """

IMPORTANT: Respond with ONLY valid JSON that matches the schema below.
Do NOT include markdown fences, explanations, or any text outside the JSON object.

Schema:
{schema}
"""

_MAX_RETRIES = 2
_RETRY_DELAY = 1.0   # 秒，每次翻倍


def call_structured(
    llm: BaseChatModel,
    messages: list,
    output_model: Type[T],
) -> T | None:
    """
    调用 LLM 并将输出解析为 Pydantic 模型。
    先尝试 with_structured_output（OpenAI / Claude），
    失败则回退到 JSON prompt + 手动解析（DeepSeek / GLM）。
    失败时做指数退避重试，最多 _MAX_RETRIES 次。
    """
    # ── 方案 A：原生结构化输出 ─────────────────────────────────────────────────
    try:
        structured_llm = llm.with_structured_output(output_model, method="function_calling")
        result = structured_llm.invoke(messages)
        if isinstance(result, output_model):
            return result
    except Exception as e:
        logger.debug("[llm_utils] with_structured_output 失败，回退 JSON prompt: %s", e)

    # ── 方案 B：JSON prompt + 手动解析（带重试）──────────────────────────────
    schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False, indent=2)
    patched = list(messages)
    last = patched[-1]
    if hasattr(last, "content"):
        from langchain_core.messages import HumanMessage
        patched[-1] = HumanMessage(content=last.content + JSON_SUFFIX.format(schema=schema))

    raw = "N/A"
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response: BaseMessage = llm.invoke(patched)
            raw = response.content if hasattr(response, "content") else str(response)
            json_str = _extract_json(raw)
            data = json.loads(json_str)
            return output_model.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            logger.warning(
                "[llm_utils] 解析失败 (attempt %d/%d): %s",
                attempt + 1, _MAX_RETRIES + 1, e,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * (2 ** attempt))
        except Exception as e:
            logger.error("[llm_utils] LLM 调用异常 (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES + 1, e)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY * (2 ** attempt))

    logger.error("[llm_utils] 所有重试均失败 | 原始响应: %s", raw[:300])
    return None


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取 JSON 字符串。"""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        return brace.group(0)
    return text.strip()
