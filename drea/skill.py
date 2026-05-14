from __future__ import annotations

"""
skill.py — DREA v1.3 技能结晶器

职责：
- 将成功执行的任务沉淀为 L3 Skill
- 验证准入条件（quality_score >= 0.65，真实执行，无安全违规）
- 管理 success_count 和 stable 升级
- 触发涌现检测候选标记
- 带 FileLock 的并发安全写入

设计原则：
- No Execution, No Memory：只有真实执行成功的任务才能沉淀
- Skill 是 DREA 自进化的核心资产
- 连续3次成功 → Stable Skill → 触发涌现检测
"""

import re
from pathlib import Path

from filelock import FileLock

from .file_protocol import DREAHome
from .util import (
    atomic_write_text,
    read_text,
    now_iso,
    new_id,
    sha256_obj,
)

SKILL_QUALITY_THRESHOLD = 0.65
STABLE_SUCCESS_COUNT    = 3


class SkillCrystallizer:
    """DREA v1.3 技能结晶器。"""

    def __init__(self, home: DREAHome):
        self.home = home

    # ────────────────────────────────────────────────────────
    # 主入口：尝试结晶
    # ────────────────────────────────────────────────────────

    def maybe_crystallize(
        self,
        task:   dict,
        result: dict,
    ) -> dict | None:
        """
        尝试将任务执行结果沉淀为 Skill。
        返回 Skill 元数据，或 None（不满足准入条件）。

        准入条件：
        1. evaluation.success == True
        2. evaluation.quality_score >= 0.65
        3. memory_candidate.target_layer == "L3_skills"
        4. 无安全违规标记
        """
        evaluation = result.get("evaluation", {})
        candidate  = result.get("memory_candidate", {})

        if not evaluation.get("success"):
            return None
        if evaluation.get("quality_score", 0.0) < SKILL_QUALITY_THRESHOLD:
            return None
        if candidate.get("target_layer") != "L3_skills":
            return None
        if result.get("gene_violation"):
            return None

        title  = candidate.get("title") or f"Skill for {task.get('task_type', 'task')}"
        domain = candidate.get("domain") or task.get("task_type", "general")
        body   = candidate.get("content") or self._default_body(task, result)

        return self.write_skill(title, domain, task, result, body)

    # ────────────────────────────────────────────────────────
    # 写入 Skill
    # ────────────────────────────────────────────────────────

    def write_skill(
        self,
        title:   str,
        domain:  str,
        task:    dict,
        result:  dict,
        body:    str,
    ) -> dict:
        """
        写入新 Skill 到 L3_skills/。
        使用 FileLock 保证并发安全。
        """
        skill_id    = new_id("skill")
        safe_domain = re.sub(r"[^a-zA-Z0-9_\-]+", "_", domain)[:40]
        filename    = f"{skill_id}_{safe_domain}.md"
        path        = self.home.memory_l3 / filename

        quality = result.get("evaluation", {}).get("quality_score", 0.0)
        content_hash = "sha256:" + sha256_obj({
            "title": title, "task": task,
            "result": result, "body": body,
        })

        markdown = self._render_markdown(
            skill_id, domain, task, quality,
            content_hash, title, body,
        )

        lock_path = self.home.memory_l3 / ".l3.lock"
        with FileLock(str(lock_path), timeout=5.0):
            atomic_write_text(path, markdown)

        self.home.append_l1_entry(
            "L3 Skills",
            f"- {filename} domain={domain} quality={quality:.2f}",
        )

        return {
            "skill_id":      skill_id,
            "filename":      filename,
            "path":          str(path),
            "domain":        domain,
            "quality_score": quality,
            "content_hash":  content_hash,
            "stable":        False,
            "emergence_candidate": False,
        }

    # ────────────────────────────────────────────────────────
    # success_count 管理
    # ────────────────────────────────────────────────────────

    def increment_success(self, filename: str) -> dict:
        """
        增加 Skill 的 success_count。
        达到 STABLE_SUCCESS_COUNT 时升级为 Stable Skill。
        返回更新后的元数据。
        """
        path = self.home.memory_l3 / filename
        if not path.exists():
            raise FileNotFoundError(f"skill not found: {filename}")

        lock_path = self.home.memory_l3 / ".l3.lock"
        with FileLock(str(lock_path), timeout=5.0):
            text = read_text(path)
            # 解析并更新 frontmatter
            text, meta = self._update_frontmatter_field(
                text, "success_count",
                lambda v: str(int(v) + 1) if v else "2",
            )
            new_count = int(meta.get("success_count", 1))

            # 升级为 Stable
            if new_count >= STABLE_SUCCESS_COUNT:
                text, meta = self._update_frontmatter_field(
                    text, "stable", lambda _: "true"
                )
                text, meta = self._update_frontmatter_field(
                    text, "emergence_candidate", lambda _: "true"
                )

            atomic_write_text(path, text)

        return {
            "filename":      filename,
            "success_count": new_count,
            "stable":        new_count >= STABLE_SUCCESS_COUNT,
            "emergence_candidate": new_count >= STABLE_SUCCESS_COUNT,
        }

    # ────────────────────────────────────────────────────────
    # 工具方法
    # ────────────────────────────────────────────────────────

    def _render_markdown(
        self,
        skill_id:     str,
        domain:       str,
        task:         dict,
        quality:      float,
        content_hash: str,
        title:        str,
        body:         str,
    ) -> str:
        return (
            f"---\n"
            f"skill_id: {skill_id}\n"
            f"version: 1.0\n"
            f"domain: {domain}\n"
            f"created_at: {now_iso()}\n"
            f"source_task_id: {task.get('task_id', 'unknown')}\n"
            f"success_count: 1\n"
            f"stable: false\n"
            f"permission: trainable\n"
            f"quality_score: {quality:.4f}\n"
            f"emergence_candidate: false\n"
            f"content_hash: {content_hash}\n"
            f"---\n\n"
            f"# {title}\n\n"
            f"{body}\n"
        )

    def _update_frontmatter_field(
        self,
        text:      str,
        field:     str,
        transform,
    ) -> tuple[str, dict]:
        """
        更新 Markdown frontmatter 中的指定字段。
        返回更新后的文本和解析后的 meta dict。
        """
        if not text.startswith("---"):
            return text, {}
        end = text.find("---", 3)
        if end == -1:
            return text, {}

        frontmatter = text[3:end]
        rest        = text[end + 3:]
        lines       = frontmatter.splitlines()
        new_lines   = []
        meta        = {}
        updated     = False

        for line in lines:
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip()
                v = v.strip()
                meta[k] = v
                if k == field:
                    new_v = transform(v)
                    meta[k] = new_v
                    new_lines.append(f"{k}: {new_v}")
                    updated = True
                    continue
            new_lines.append(line)

        if not updated:
            new_v = transform(None)
            meta[field] = new_v
            new_lines.append(f"{field}: {new_v}")

        new_frontmatter = "\n".join(new_lines)
        return f"---{new_frontmatter}---{rest}", meta

    def _default_body(self, task: dict, result: dict) -> str:
        return (
            f"## 适用场景\n\n"
            f"任务类型：`{task.get('task_type', 'unknown')}`\n\n"
            f"## 前置条件\n\n"
            f"- 任务已经成功执行。\n"
            f"- 结果已经通过 evaluation。\n\n"
            f"## 操作步骤\n\n"
            f"1. 读取任务输入。\n"
            f"2. 选择合适原子工具。\n"
            f"3. 执行并记录结果。\n"
            f"4. 验证结果质量。\n"
            f"5. 写入审计与 checkpoint。\n\n"
            f"## 验证方法\n\n"
            f"- evaluation.success == true\n"
            f"- quality_score >= {SKILL_QUALITY_THRESHOLD}\n\n"
            f"## 常见失败\n\n"
            f"- 输入不完整。\n"
            f"- 权限不足。\n"
            f"- 工具执行失败。\n\n"
            f"## 修复策略\n\n"
            f"- 补充必要输入。\n"
            f"- 降低权限或请求人类确认。\n"
            f"- 查看 fail_card。\n\n"
            f"## 审计来源\n\n"
            f"- task_id: `{task.get('task_id', 'unknown')}`\n"
            f"- result_id: `{result.get('result_id', 'unknown')}`\n"
        )
