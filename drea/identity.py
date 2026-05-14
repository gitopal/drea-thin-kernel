from __future__ import annotations

"""
identity.py — DREA v1.3 身份管理

职责：
- 生成并持久化身份卡（identity_card.json）
- 身份卡包含：drea_id、name、内核版本、基因版本、迁徙能力声明
- 身份卡哈希保证完整性
- 身份卡幂等：多次调用ensure()返回相同结果

设计原则：
- 身份是DREA生命的锚点，不可随意修改
- 身份卡一旦创建，drea_id不可变更
- 迁徙时身份卡随节点迁移
"""

from .file_protocol import DREAHome
from .util import atomic_write_json, read_json, now_iso, sha256_obj


class IdentityManager:
    """DREA 身份管理器。"""

    KERNEL_VERSION = "1.3.0"
    GENE_VERSION = "2.0"

    def __init__(
        self,
        home: DREAHome,
        drea_id: str = "drea_001",
        name: str = "DREA-Thin",
    ):
        self.home = home
        self.drea_id = drea_id
        self.name = name
        self.card_path = self.home.identity / "identity_card.json"
        self.signature_path = self.home.identity / "signature.txt"

    def ensure(self) -> dict:
        """
        确保身份卡存在。
        已存在则直接返回，不存在则创建。
        幂等操作。
        """
        if self.card_path.exists():
            return self.get()
        return self._create()

    def get(self) -> dict:
        """读取身份卡。不存在时自动创建。"""
        data = read_json(self.card_path, None)
        if data is None:
            return self._create()
        return data

    def _create(self) -> dict:
        """创建并持久化身份卡。"""
        card_body = {
            "identity_version": "1.0",
            "drea_id": self.drea_id,
            "name": self.name,
            "kernel_version": self.KERNEL_VERSION,
            "gene_version": self.GENE_VERSION,
            "born": now_iso(),
            "creator": "Topal",
            "generation": 1,
            "parent": None,
            "migration": {
                "identity_portable": True,
                "memory_seed_portable": True,
                "checkpoint_portable": True,
                "skills_portable": True,
            },
        }
        card_hash = "sha256:" + sha256_obj(card_body)
        card_body["card_hash"] = card_hash
        card_body["signature_algorithm"] = "sha256-dev"

        atomic_write_json(self.card_path, card_body)
        self.signature_path.write_text(card_hash, encoding="utf-8")
        return card_body

    def verify(self) -> bool:
        """验证身份卡哈希完整性。"""
        card = read_json(self.card_path, None)
        if card is None:
            return False
        stored_hash = card.get("card_hash", "")
        body = {k: v for k, v in card.items()
                if k not in {"card_hash", "signature_algorithm"}}
        expected = "sha256:" + sha256_obj(body)
        return stored_hash == expected
