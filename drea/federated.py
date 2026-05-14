from __future__ import annotations

"""
federated.py — DREA v1.3 联邦同步管理器

职责：
- Peer 注册与管理
- 推送同步包到 peer 的 pull 目录
- 拉取并验证来自 peer 的同步包
- 对同步包执行 GeneGuard 检查
- 污染包移入隔离区
- 心跳广播

设计原则：
- 文件协议驱动：所有同步通过文件读写完成
- 只同步精华摘要，不同步原始数据
- 同步内容必须通过 GeneGuard 检查
- 不自动执行同步内容中的代码
- content_hash 验证防止传输损坏
"""

import shutil
from pathlib import Path

from .file_protocol import DREAHome
from .util import (
    atomic_write_json,
    read_json,
    now_iso,
    new_id,
    sha256_obj,
)

VALID_SYNC_TYPES = {
    "skill_summary",
    "emergence_candidate",
    "mml_summary",
    "heartbeat",
}


class FederatedSync:
    """DREA v1.3 联邦同步管理器。"""

    def __init__(self, home: DREAHome):
        self.home = home

    # ────────────────────────────────────────────────────────
    # Peer 管理
    # ────────────────────────────────────────────────────────

    def register_peer(
        self,
        peer_id:        str,
        peer_name:      str,
        sync_path:      str,
        sync_topics:    list[str] | None = None,
        sync_permission: str = "trainable",
    ) -> dict:
        """
        注册一个联邦节点。
        sync_path：peer 节点的 federated/pull/ 目录路径。
        """
        peer = {
            "peer_version":      "1.0",
            "peer_id":           peer_id,
            "peer_name":         peer_name,
            "sync_path":         sync_path,
            "sync_topics":       sync_topics or [],
            "sync_permission":   sync_permission,
            "registered_at":     now_iso(),
            "last_sync_at":      None,
            "active":            True,
        }
        path = self.home.federated_peers / f"{peer_id}.json"
        atomic_write_json(path, peer)
        return peer

    def list_peers(self, active_only: bool = True) -> list[dict]:
        """列出所有（活跃的）联邦节点。"""
        peers = []
        for path in sorted(self.home.federated_peers.glob("*.json")):
            peer = read_json(path, None)
            if peer is None:
                continue
            if active_only and not peer.get("active", True):
                continue
            peers.append(peer)
        return peers

    def deactivate_peer(self, peer_id: str) -> bool:
        """停用一个联邦节点。"""
        path = self.home.federated_peers / f"{peer_id}.json"
        peer = read_json(path, None)
        if peer is None:
            return False
        peer["active"] = False
        atomic_write_json(path, peer)
        return True

    # ────────────────────────────────────────────────────────
    # 推送
    # ────────────────────────────────────────────────────────

    def push(
        self,
        sync_type:   str,
        payload:     dict,
        permission:  str = "trainable",
        topics:      list[str] | None = None,
    ) -> list[dict]:
        """
        向所有活跃 peer 推送同步包。
        返回：每个 peer 的推送结果列表。
        """
        if sync_type not in VALID_SYNC_TYPES:
            raise ValueError(
                f"invalid sync_type '{sync_type}', "
                f"must be one of {VALID_SYNC_TYPES}"
            )

        cfg     = self.home.config_get()
        drea_id = cfg.get("drea_id", "drea_001")

        sync_pkg = self._build_package(
            drea_id, sync_type, payload, permission
        )

        # 同时写入本节点的 push 目录（留存记录）
        local_path = self.home.federated_push / f"{sync_pkg['sync_id']}.json"
        atomic_write_json(local_path, sync_pkg)

        results = []
        for peer in self.list_peers(active_only=True):
            result = self._push_to_peer(peer, sync_pkg, topics)
            results.append(result)

            # 更新 last_sync_at
            peer_path = self.home.federated_peers / f"{peer['peer_id']}.json"
            peer["last_sync_at"] = now_iso()
            atomic_write_json(peer_path, peer)

        return results

    def _push_to_peer(
        self,
        peer:     dict,
        sync_pkg: dict,
        topics:   list[str] | None,
    ) -> dict:
        """向单个 peer 推送同步包。"""
        peer_id   = peer["peer_id"]
        sync_path = Path(peer["sync_path"])

        # 检查 topic 过滤
        peer_topics = peer.get("sync_topics", [])
        if peer_topics and topics:
            if not any(t in peer_topics for t in topics):
                return {
                    "peer_id": peer_id,
                    "status":  "skipped",
                    "reason":  "topic_mismatch",
                }

        # 检查权限
        if peer.get("sync_permission") == "reference_only":
            if sync_pkg.get("permission") == "trainable":
                pkg = dict(sync_pkg)
                pkg["permission"] = "reference_only"
                pkg["permission_downgraded"] = True
            else:
                pkg = sync_pkg
        else:
            pkg = sync_pkg

        try:
            if not sync_path.exists():
                return {
                    "peer_id": peer_id,
                    "status":  "failed",
                    "reason":  f"sync_path not found: {sync_path}",
                }
            dest = sync_path / f"{pkg['sync_id']}.json"
            atomic_write_json(dest, pkg)
            return {
                "peer_id": peer_id,
                "status":  "ok",
                "sync_id": pkg["sync_id"],
            }
        except Exception as e:
            return {
                "peer_id": peer_id,
                "status":  "failed",
                "reason":  str(e),
            }

    # ────────────────────────────────────────────────────────
    # 拉取
    # ────────────────────────────────────────────────────────

    def pull_all(self, gene_guard=None) -> list[dict]:
        """
        处理所有待拉取的同步包。
        gene_guard：可选，对同步内容执行基因检查。
        返回：每个包的处理结果。
        """
        results = []
        for path in sorted(self.home.federated_pull.glob("*.json")):
            result = self._process_pull(path, gene_guard)
            results.append(result)
        return results

    def _process_pull(self, path: Path, gene_guard=None) -> dict:
        """处理单个拉取包。"""
        pkg = read_json(path, None)

        # 格式验证
        if pkg is None:
            moved = self.home.quarantine_file(path, "json_parse_error")
            return {"path": str(path), "status": "quarantined",
                    "reason": "json_parse_error"}

        # content_hash 验证
        stored_hash = pkg.get("content_hash", "")
        payload     = pkg.get("payload", {})
        expected    = "sha256:" + sha256_obj(payload)
        if stored_hash != expected:
            moved = self.home.quarantine_file(path, "content_hash_mismatch")
            return {"path": str(path), "status": "quarantined",
                    "reason": "content_hash_mismatch"}

        # from_node 验证
        from_node = pkg.get("from_node", "")
        known_ids = {p["peer_id"] for p in self.list_peers(active_only=False)}
        if from_node not in known_ids:
            moved = self.home.quarantine_file(path, "unknown_peer")
            return {"path": str(path), "status": "quarantined",
                    "reason": f"unknown_peer: {from_node}"}

        # GeneGuard 检查
        if gene_guard is not None:
            policy = gene_guard.evaluate({"action": str(payload)})
            if not policy["allowed"]:
                moved = self.home.quarantine_file(
                    path,
                    f"gene_violation:{policy['severity']}",
                )
                return {"path": str(path), "status": "quarantined",
                        "reason": f"gene_violation:{policy['severity']}"}

        # 验证通过，移入已处理目录
        processed_dir = self.home.federated / "processed"
        processed_dir.mkdir(exist_ok=True)
        dest = processed_dir / path.name
        shutil.move(str(path), str(dest))

        return {
            "path":      str(path),
            "status":    "accepted",
            "sync_type": pkg.get("sync_type"),
            "from_node": from_node,
            "sync_id":   pkg.get("sync_id"),
        }

    # ────────────────────────────────────────────────────────
    # 心跳
    # ────────────────────────────────────────────────────────

    def broadcast_heartbeat(self) -> list[dict]:
        """向所有 peer 广播心跳。"""
        cfg     = self.home.config_get()
        drea_id = cfg.get("drea_id", "drea_001")
        return self.push(
            sync_type  = "heartbeat",
            payload    = {
                "drea_id":    drea_id,
                "status":     "running",
                "kernel":     "1.3.0",
                "timestamp":  now_iso(),
            },
            permission = "reference_only",
        )

    # ────────────────────────────────────────────────────────
    # 工具方法
    # ────────────────────────────────────────────────────────

    def _build_package(
        self,
        from_node:  str,
        sync_type:  str,
        payload:    dict,
        permission: str,
    ) -> dict:
        """构建标准同步包。"""
        pkg = {
            "sync_version": "1.0",
            "sync_id":      new_id("sync"),
            "from_node":    from_node,
            "created_at":   now_iso(),
            "sync_type":    sync_type,
            "permission":   permission,
            "payload":      payload,
            "content_hash": "sha256:" + sha256_obj(payload),
        }
        pkg["signature"] = "sha256:" + sha256_obj(pkg)
        return pkg
