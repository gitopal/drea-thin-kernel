from __future__ import annotations

"""
util.py — DREA v1.3 基础工具函数

职责：
- 时间、ID、哈希、JSON序列化
- 原子文件写入（防止写入中断导致文件损坏）
- 安全路径验证（防止路径逃逸攻击）
- Token估算（用于CID报告）
- 文件锁（防止并发写入竞态）
"""

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout


# ────────────────────────────────────────────────────────────
# 时间与ID
# ────────────────────────────────────────────────────────────

def now_iso() -> str:
    """返回当前UTC时间的ISO8601格式字符串。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_ts() -> float:
    """返回当前Unix时间戳（秒）。"""
    return time.time()


def new_id(prefix: str) -> str:
    """
    生成带前缀的唯一ID。
    格式：{prefix}_{16位hex}
    示例：task_a3f2c1d4e5b6a7f8
    """
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ────────────────────────────────────────────────────────────
# JSON序列化
# ────────────────────────────────────────────────────────────

def canonical_json(data: Any) -> str:
    """
    规范化JSON序列化。
    - ensure_ascii=False：保留中文等非ASCII字符，防止关键词匹配失效
    - sort_keys=True：保证相同数据的哈希一致
    - separators无空格：最小化体积
    """
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def pretty_json(data: Any) -> str:
    """人类可读的JSON序列化，用于文件存储。"""
    return json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )


# ────────────────────────────────────────────────────────────
# 哈希
# ────────────────────────────────────────────────────────────

def sha256_text(text: str) -> str:
    """对字符串计算SHA256，返回十六进制字符串。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_obj(data: Any) -> str:
    """对任意可序列化对象计算SHA256（通过canonical_json规范化）。"""
    return sha256_text(canonical_json(data))


def sha256_file(path: Path) -> str:
    """对文件内容计算SHA256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ────────────────────────────────────────────────────────────
# 目录与文件操作
# ────────────────────────────────────────────────────────────

def ensure_dir(path: Path) -> Path:
    """确保目录存在，不存在则创建（含父目录）。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str) -> None:
    """
    原子写入文本文件。
    流程：写入临时文件 → fsync → os.replace（原子替换）
    保证：写入中断不会产生损坏的目标文件。
    """
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    """原子写入JSON文件（pretty格式 + 换行符结尾）。"""
    atomic_write_text(path, pretty_json(data) + "\n")


def atomic_write_json_locked(path: Path, data: Any, timeout: float = 5.0) -> None:
    """
    带FileLock的原子写入JSON文件。
    用于多进程/多节点并发写入同一文件的场景。
    timeout：等待锁的最大秒数，超时抛出FileLockTimeout。
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    with FileLock(str(lock_path), timeout=timeout):
        atomic_write_json(path, data)


def atomic_write_text_locked(path: Path, text: str, timeout: float = 5.0) -> None:
    """带FileLock的原子写入文本文件。"""
    lock_path = path.with_suffix(path.suffix + ".lock")
    with FileLock(str(lock_path), timeout=timeout):
        atomic_write_text(path, text)


def read_json(path: Path, default: Any = None) -> Any:
    """读取JSON文件，文件不存在或解析失败时返回default。"""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def read_text(path: Path, default: str = "") -> str:
    """读取文本文件，文件不存在时返回default。"""
    if not path.exists():
        return default
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def append_jsonl(path: Path, data: Any) -> None:
    """
    追加一行JSON到JSONL文件（append模式，带fsync）。
    注意：调用方负责在需要时持有FileLock。
    """
    ensure_dir(path.parent)
    line = canonical_json(data) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_last_jsonl_line(path: Path) -> dict | None:
    """
    从JSONL文件反向读取最后一行，时间复杂度O(1)。
    解决v1.2中全量遍历的性能问题。
    """
    if not path.exists():
        return None
    size = path.stat().st_size
    if size == 0:
        return None

    buf_size = min(8192, size)
    with open(path, "rb") as f:
        f.seek(-buf_size, 2)
        buf = f.read().decode("utf-8", errors="ignore")

    lines = [ln for ln in buf.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except (json.JSONDecodeError, KeyError):
        return None


def read_all_jsonl_lines(path: Path) -> list[dict]:
    """读取JSONL文件所有行，返回解析后的列表。"""
    if not path.exists():
        return []
    results = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return results


def copy_tree(src: Path, dst: Path) -> None:
    """复制目录树，目标已存在则先删除。"""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


# ────────────────────────────────────────────────────────────
# 安全路径验证
# ────────────────────────────────────────────────────────────

def safe_rel_path(rel: str) -> Path:
    """
    验证相对路径安全性，防止路径逃逸攻击。
    拒绝：绝对路径、包含'..'的路径、包含空字节的路径。
    """
    rel = rel.replace("\\", "/").strip()
    if not rel:
        raise ValueError("empty path")
    if "\x00" in rel:
        raise ValueError("null byte in path")
    p = Path(rel)
    if p.is_absolute():
        raise ValueError(f"absolute path not allowed: {rel}")
    if ".." in p.parts:
        raise ValueError(f"path traversal not allowed: {rel}")
    return p


def path_in_root(root: Path, rel: str) -> Path:
    """
    将相对路径解析为根目录下的绝对路径，并验证不逃逸根目录。
    """
    rel_path = safe_rel_path(rel)
    resolved = (root / rel_path).resolve()
    root_resolved = root.resolve()
    if root_resolved not in resolved.parents and resolved != root_resolved:
        raise ValueError(f"path escapes root: {rel}")
    return resolved


# ────────────────────────────────────────────────────────────
# Token估算（用于CID报告）
# ────────────────────────────────────────────────────────────

def approx_tokens(text: str) -> int:
    """
    估算文本的token数量。
    算法：ASCII字符约4个字符=1token；非ASCII字符约2个字符=1token。
    这是一个保守估算，实际token数可能略有不同。
    """
    if not text:
        return 0
    ascii_count = sum(1 for c in text if ord(c) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, ascii_count // 4 + non_ascii_count // 2)


# ────────────────────────────────────────────────────────────
# 文本相似度（用于涌现检测）
# ────────────────────────────────────────────────────────────

def simple_text_similarity(text_a: str, text_b: str) -> float:
    """
    基于词袋的简单文本相似度计算（Jaccard相似度）。
    用于涌现检测的新颖性条件判断。
    输入：两个文本字符串
    输出：0.0（完全不同）到 1.0（完全相同）
    """
    if not text_a or not text_b:
        return 0.0

    def tokenize(text: str) -> set[str]:
        import re
        # 分词：按非字母数字汉字分割
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
        return set(tokens)

    tokens_a = tokenize(text_a)
    tokens_b = tokenize(text_b)

    if not tokens_a or not tokens_b:
        return 0.0

    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)
