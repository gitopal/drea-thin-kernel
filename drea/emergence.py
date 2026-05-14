from __future__ import annotations

"""
emergence.py — DREA v1.3 本地涌现检测器

职责：
- 对 Stable Skill 执行本地四条件涌现检测
- 条件A：新颖性（与L3现有Skill相似度 < novelty_threshold）
- 条件B：优越性（quality_score 超过同domain均值 >= superiority_threshold）
- 条件C：可重现性（success_count >= reproducibility_count）
- 条件D：可迁移性（本地标记为候选，等待联邦验证）
- 写入涌现候选文件
- 推送至联邦同步队列

设计原则：
- 涌现检测是DREA自进化的关键节点
- 本地检测是全网验证的前置条件
- 所有检测结果写入审计
"""

from .file_protocol import DREAHome
from .memory import MemoryRouter
from .util import (
    atomic_write_json,
    read_text,
    now_iso,
    new_id,
    sha256_obj,
    simple_text_similarity,
)


class EmergenceDetector:
    """DREA v1.3 本地涌现检测器。"""

    def __init__(self, home: DREAHome, memory: MemoryRouter):
        self.home   = home
        self.memory = memory

    # ────────────────────────────────────────────────────────
    # 主入口
    # ────────────────────────────────────────────────────────

    def check(self, skill_meta: dict) -> dict:
        """
        对一个 Skill 执行本地涌现检测。

        skill_meta：来自 SkillCrystallizer 或 MemoryRouter.list_skills()
        返回：EmergenceResult
        {
            triggered:   bool,       # 是否触发涌现候选
            emergence_id: str | None,
            conditions:  dict,       # 四条件详情
            reason:      str,        # 未触发时的原因
        }
        """
        cfg = self.home.config_get()

        novelty_threshold       = cfg.get("emergence_novelty_threshold", 0.70)
        superiority_threshold   = cfg.get("emergence_superiority_threshold", 0.15)
        reproducibility_count   = cfg.get("emergence_reproducibility_count", 3)

        skill_id   = skill_meta.get("skill_id", "unknown")
        filename   = skill_meta.get("filename", "")
        domain     = skill_meta.get("domain", "general")
        quality    = float(skill_meta.get("quality_score", 0.0))
        suc_count  = int(skill_meta.get("success_count", 0))

        # ── 条件C：可重现性 ───────────────────────────────────
        cond_c_passed = suc_count >= reproducibility_count

        # ── 条件A：新颖性 ─────────────────────────────────────
        novelty_score, cond_a_passed = self._check_novelty(
            filename, domain, novelty_threshold
        )

        # ── 条件B：优越性 ─────────────────────────────────────
        superiority_score, cond_b_passed = self._check_superiority(
            domain, quality, superiority_threshold
        )

        conditions = {
            "novelty_score":            novelty_score,
            "novelty_threshold":        novelty_threshold,
            "novelty_passed":           cond_a_passed,
            "superiority_score":        superiority_score,
            "superiority_threshold":    superiority_threshold,
            "superiority_passed":       cond_b_passed,
            "reproducibility_count":    suc_count,
            "reproducibility_required": reproducibility_count,
            "reproducibility_passed":   cond_c_passed,
            "transferability_status":   "pending_federated_validation",
        }

        all_passed = cond_a_passed and cond_b_passed and cond_c_passed

        if not all_passed:
            failed = []
            if not cond_a_passed:
                failed.append(
                    f"novelty({novelty_score:.2f}) >= {novelty_threshold}"
                )
            if not cond_b_passed:
                failed.append(
                    f"superiority({superiority_score:.2f}) < {superiority_threshold}"
                )
            if not cond_c_passed:
                failed.append(
                    f"success_count({suc_count}) < {reproducibility_count}"
                )
            return {
                "triggered":    False,
                "emergence_id": None,
                "conditions":   conditions,
                "reason":       "conditions_not_met: " + "; ".join(failed),
            }

        # ── 三条件全部通过，写入涌现候选 ─────────────────────
        emergence_id = self._write_candidate(
            skill_id, filename, domain, conditions
        )

        return {
            "triggered":    True,
            "emergence_id": emergence_id,
            "conditions":   conditions,
            "reason":       "local_emergence_detected",
        }

    # ────────────────────────────────────────────────────────
    # 条件A：新颖性检测
    # ────────────────────────────────────────────────────────

    def _check_novelty(
        self,
        target_filename: str,
        domain:          str,
        threshold:       float,
    ) -> tuple[float, bool]:
        """
        计算目标Skill与L3中所有其他Skill的最高相似度。
        最高相似度 < threshold → 新颖性通过。
        返回：(max_similarity, passed)
        """
        target_path = self.home.memory_l3 / target_filename
        if not target_path.exists():
            return 0.0, True  # 文件不存在视为新颖

        target_text = read_text(target_path)
        max_sim = 0.0

        for path in self.home.memory_l3.glob("*.md"):
            if path.name == target_filename:
                continue
            other_text = read_text(path)
            sim = simple_text_similarity(target_text, other_text)
            if sim > max_sim:
                max_sim = sim

        # novelty_score = 1 - max_similarity（越高越新颖）
        novelty_score = 1.0 - max_sim
        passed = max_sim < threshold
        return novelty_score, passed

    # ────────────────────────────────────────────────────────
    # 条件B：优越性检测
    # ────────────────────────────────────────────────────────

    def _check_superiority(
        self,
        domain:    str,
        quality:   float,
        threshold: float,
    ) -> tuple[float, bool]:
        """
        计算目标Skill的quality_score超过同domain均值的幅度。
        超过均值 >= threshold → 优越性通过。
        返回：(superiority_margin, passed)
        """
        same_domain_skills = self.memory.list_skills(domain=domain)

        if len(same_domain_skills) <= 1:
            # 同domain只有自己，视为优越
            return threshold, True

        scores = [
            float(s.get("quality_score", 0.0))
            for s in same_domain_skills
        ]
        avg = sum(scores) / len(scores)

        if avg == 0:
            return threshold, True

        superiority_margin = (quality - avg) / avg
        passed = superiority_margin >= threshold
        return superiority_margin, passed

    # ────────────────────────────────────────────────────────
    # 写入涌现候选
    # ────────────────────────────────────────────────────────

    def _write_candidate(
        self,
        skill_id:   str,
        filename:   str,
        domain:     str,
        conditions: dict,
    ) -> str:
        """写入涌现候选文件，返回 emergence_id。"""
        cfg         = self.home.config_get()
        drea_id     = cfg.get("drea_id", "drea_001")
        emergence_id = new_id("emergence")

        candidate = {
            "emergence_version":   "1.0",
            "emergence_id":        emergence_id,
            "created_at":          now_iso(),
            "skill_id":            skill_id,
            "skill_filename":      filename,
            "skill_domain":        domain,
            "detection_node":      drea_id,
            "conditions":          conditions,
            "status":              "local_candidate",
            "federated_validations": [],
            "content_hash":        "sha256:" + sha256_obj({
                "skill_id":   skill_id,
                "conditions": conditions,
            }),
        }

        path = self.home.emergence_candidates / f"{emergence_id}.json"
        atomic_write_json(path, candidate)
        return emergence_id

    # ────────────────────────────────────────────────────────
    # 列出候选
    # ────────────────────────────────────────────────────────

    def list_candidates(self) -> list[dict]:
        """列出所有本地涌现候选。"""
        from .util import read_json
        results = []
        for path in sorted(
            self.home.emergence_candidates.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            data = read_json(path, None)
            if data:
                results.append(data)
        return results

    def list_confirmed(self) -> list[dict]:
        """列出所有已确认的涌现（联邦验证通过）。"""
        from .util import read_json
        results = []
        for path in sorted(
            self.home.emergence_confirmed.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            data = read_json(path, None)
            if data:
                results.append(data)
        return results
