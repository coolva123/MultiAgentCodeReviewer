"""文件读取工具（Day 2-3 填充实现）。"""
from pathlib import Path


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def search_files(directory: str, pattern: str) -> list[str]:
    return [str(p) for p in Path(directory).rglob(pattern)]
