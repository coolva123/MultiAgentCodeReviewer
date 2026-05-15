import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# ── LLM 选择 ─────────────────────────────────────────────────────────────────
# 可选值: "deepseek" | "zhipu" | "openai" | "anthropic"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek")
LLM_MODEL    = os.getenv("LLM_MODEL", "deepseek-v4-pro")

# DeepSeek（OpenAI 兼容接口）
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# ZhiPu GLM（OpenAI 兼容接口 + 原生 Embedding）
ZHIPU_API_KEY       = os.getenv("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL      = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/")
ZHIPU_EMBED_MODEL   = os.getenv("ZHIPU_EMBED_MODEL", "embedding-3")

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Anthropic Claude
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# GitHub
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# PostgreSQL + pgvector（长期记忆）
PG_DATABASE_URL = os.getenv(
    "PG_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:15432/postgres",
)
PG_EMBEDDING_DIM = int(os.getenv("PG_EMBEDDING_DIM", "2048"))

# LangGraph checkpointing
CHECKPOINT_DB_PATH = str(BASE_DIR / "data" / "checkpoints.db")


def get_llm(temperature: float = 0.1):
    """
    根据 LLM_PROVIDER 返回对应 LangChain Chat 实例。
    DeepSeek 和 ZhiPu 均使用 OpenAI 兼容接口，直接复用 ChatOpenAI。
    """
    from langchain_openai import ChatOpenAI

    if LLM_PROVIDER == "deepseek":
        if not DEEPSEEK_API_KEY:
            raise ValueError("DEEPSEEK_API_KEY is not set in .env")
        return ChatOpenAI(
            model=LLM_MODEL,
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            temperature=temperature,
        )

    elif LLM_PROVIDER == "zhipu":
        if not ZHIPU_API_KEY:
            raise ValueError("ZHIPU_API_KEY is not set in .env")
        return ChatOpenAI(
            model=LLM_MODEL,
            api_key=ZHIPU_API_KEY,
            base_url=ZHIPU_BASE_URL,
            temperature=temperature,
        )

    elif LLM_PROVIDER == "openai":
        if not OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not set in .env")
        return ChatOpenAI(
            model=LLM_MODEL,
            api_key=OPENAI_API_KEY,
            temperature=temperature,
        )

    elif LLM_PROVIDER == "anthropic":
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is not set in .env")
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=LLM_MODEL,
            api_key=ANTHROPIC_API_KEY,
            temperature=temperature,
        )

    else:
        raise ValueError(
            f"Unsupported LLM_PROVIDER: '{LLM_PROVIDER}'. "
            "Choose from: deepseek, zhipu, openai, anthropic"
        )


def get_embeddings():
    """
    返回 ZhiPu embedding-3 实例，供 Day 5 ChromaDB 使用。
    使用 langchain_community 的 ZhipuAIEmbeddings。
    """
    if not ZHIPU_API_KEY:
        raise ValueError("ZHIPU_API_KEY is not set in .env (needed for embeddings)")
    from langchain_community.embeddings import ZhipuAIEmbeddings
    return ZhipuAIEmbeddings(
        api_key=ZHIPU_API_KEY,
        model=ZHIPU_EMBED_MODEL,
    )
