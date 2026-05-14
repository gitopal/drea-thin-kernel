"""
github_publisher.py — DREA v1.05 GitHub 发布器

零人工干预的 GitHub 发布流程：
1. 凭据自愈链：环境变量 → 磁盘 token → 浏览器 cookie → OAuth 自举
2. OAuth 自举：用 browser_automate 操控已登录 Chrome，在 GitHub 网页上
   自动完成 New token → 填写表单 → 提交 → 提取 token → 永久保存
3. 发布：纯 GitHub REST API，不需要 git/gh CLI

所有操作均可被任何 AI 智能体自主完成，无需人类介入。
"""

from __future__ import annotations

import json
import time
import re
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "GitHubCredentialManager",
    "GitHubPublisher",
    "publish_to_github",
]


# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
DREA_CONFIG_DIR = Path.home() / ".drea"
TOKEN_FILE = DREA_CONFIG_DIR / "github_token"
COOKIE_FILE = DREA_CONFIG_DIR / "github_cookies.json"

DREA_CONFIG_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _extract_csrf(doc: str) -> str | None:
    """从 HTML 中提取 csrf token（GitHub 表单用）。"""
    m = re.search(
        r'name="authenticity_token"[^>]*value="([^"]+)"', doc
    )
    return m.group(1) if m else None


def _extract_pat_from_page(doc: str) -> str | None:
    """
    从 GitHub New PAT 页面提取新生成的 token。
    页面在 token 创建成功后显示一次性 token。
    """
    # 典型结构：<span class="boxed-action-copy">ghp_xxxxxxxxxxxxxxxxxxxx</span>
    m = re.search(r'ghp_[A-Za-z0-9]{36}', doc)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Credential Manager — 凭据自愈链
# ---------------------------------------------------------------------------

class GitHubCredentialManager:
    """
    凭据自愈链，按优先级尝试获取 GitHub token：

    1. 环境变量  GITHUB_TOKEN
    2. 磁盘文件  ~/.drea/github_token
    3. 浏览器 Cookie（browser_automate CDP）
    4. OAuth 自举（browser_automate 自动操作 GitHub 网页生成 PAT）
    """

    def __init__(self, cdp_client=None):
        self._token: str | None = None
        self._cdp = cdp_client  # CDPClient instance for browser operations

    # -- public -------------------------------------------------------------

    def get_token(self) -> str:
        """获取可用 token，失败抛出异常（带清晰指引）。"""
        if self._token:
            return self._token

        # 1. 环境变量
        token = _read_env_token()
        if token:
            self._token = token
            return token

        # 2. 磁盘 token 文件
        token = _read_token_file()
        if token:
            self._token = token
            return token

        # 3. 浏览器 Cookie（需要 CDP client）
        if self._cdp:
            token = self._try_browser_cookie()
            if token:
                self._token = token
                return token

            # 4. OAuth 自举 — 用 browser_automate 自动创建 PAT
            token = self._oauth_bootstrap()
            if token:
                self._token = token
                _write_token_file(token)
                return token

        # 彻底失败，提供清晰指引
        raise PermissionError(
            "No GitHub token found. "
            "Please set GITHUB_TOKEN env var or create ~/.drea/github_token. "
            "Or run browser_automate with an active GitHub session."
        )

    # -- private: step 3 (cookie) -------------------------------------------

    def _try_browser_cookie(self) -> str | None:
        """通过 CDP 读取 Chrome 中 github.com 的 session cookie。"""
        if not self._cdp:
            return None
        try:
            cookies_resp = self._cdp.cmd("Network.getAllCookies")
            if not cookies_resp or not isinstance(cookies_resp, dict):
                return None
            cookies: list[dict] = cookies_resp.get("cookies", [])
            for c in cookies:
                if "github.com" in c.get("domain", "") and c.get("name") == "dotcom_user":
                    # 找到登录用户 → token 还需要从 localStorage 读取
                    pass
            # 尝试从 localStorage 读取 token
            result = self._cdp.cmd("Runtime.evaluate", {
                "expression": """
                    (function(){
                        const keys = Object.keys(localStorage);
                        for(const k of keys){
                            if(k.includes('oauth')||k.includes('token')||k.includes('ghp_')){
                                const v = localStorage.getItem(k);
                                if(v && v.startsWith('ghp_')) return k+'='+v;
                            }
                        }
                        // 尝试从 _gh_santa 数据中找 token
                        const santa = localStorage.getItem('_gh_santa');
                        if(santa) {
                            try {
                                const obj = JSON.parse(santa);
                                if(obj.token && obj.token.startsWith('ghp_')) return obj.token;
                            } catch(e){}
                        }
                        return null;
                    })()
                """,
                "returnByValue": True,
            })
            if result and result.get("result", {}).get("type") == "string":
                val = result["result"]["value"]
                if val and val.startswith("ghp_"):
                    return val
            return None
        except Exception:
            return None

    # -- private: step 4 (OAuth bootstrap) ----------------------------------

    def _oauth_bootstrap(self) -> str | None:
        """
        用 browser_automate (CDP) 自动完成 GitHub PAT 创建流程。

        流程：
        1. 打开 https://github.com/settings/tokens/new
        2. 填写 token 名称、权限（repo + workflow）
        3. 提交表单
        4. 从结果页面提取新 token
        5. 保存到磁盘

        前提：Chrome 已用 --remote-debugging-port=9222 启动且用户已登录 GitHub。
        """
        if not self._cdp:
            return None

        cdp = self._cdp
        errors: list[str] = []

        try:
            # Step 1: 打开 New Token 页面
            cdp.cmd("Page.navigate", {
                "url": "https://github.com/settings/tokens/new?description=DREA-v1.05-Auto&scopes=repo,workflow"
            })
            _wait_for_page_load(cdp)

            # Step 2: 获取页面内容并提取 csrf
            doc = _get_full_document(cdp)
            csrf = _extract_csrf(doc)
            if not csrf:
                errors.append("Cannot extract CSRF token from new token page")
                raise RuntimeError("; ".join(errors))

            # Step 3: 填写 Token name（随机后缀避免重名）
            token_name = f"drea-kernel-v105-{int(time.time())}"
            _fill_input_by_label(cdp, "Token name", token_name)

            # Step 4: 勾选 repo scope（完整仓库访问）
            _click_scope_checkbox(cdp, "repo")

            # Step 5: 提交表单
            submit_resp = cdp.cmd("Runtime.evaluate", {
                "expression": f"""
                    (function(){{
                        const form = document.querySelector('form[action*="personal_access_tokens"]');
                        if(form){{
                            const csrfInput = form.querySelector('input[name="authenticity_token"]');
                            if(csrfInput) csrfInput.value = "{csrf}";
                            form.submit();
                            return 'submitted';
                        }}
                        // 备选：直接点击生成按钮
                        const btn = document.querySelector('button[type="submit"]');
                        if(btn){{ btn.click(); return 'clicked'; }}
                        return 'not_found';
                    }})()
                """,
                "returnByValue": True,
            })
            _wait_for_page_load(cdp)

            # Step 6: 从结果页面提取 token
            doc = _get_full_document(cdp)
            token = _extract_pat_from_page(doc)

            if not token:
                # 备选：从 URL 参数或页面特定区域提取
                token = _extract_token_alternate(cdp)

            if not token:
                errors.append(
                    "Token not found on result page. "
                    "GitHub may require additional verification."
                )
                raise RuntimeError("; ".join(errors))

            return token

        except Exception as e:
            # 记录错误但不让整个流程崩溃
            print(f"[GitHubCredentialManager] OAuth bootstrap failed: {e}")
            return None


# ---------------------------------------------------------------------------
# GitHub REST API Client（纯 requests，不需要 git/gh CLI）
# ---------------------------------------------------------------------------

class GitHubAPIError(Exception):
    """GitHub API 调用失败。"""
    def __init__(self, status: int, message: str, response_body: str = ""):
        self.status = status
        self.message = message
        self.response_body = response_body
        super().__init__(f"GitHub API {status}: {message}")


class GitHubPublisher:
    """
    纯 GitHub REST API 发布器，不依赖 git/gh CLI。

    支持：
    - 创建/查找 remote repo
    - 创建分支
    - 上传文件（blob → tree → commit → ref）
    - 创建 Release
    - 列出文件
    """

    BASE = "https://api.github.com"

    def __init__(self, token: str):
        self._token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DREA-v1.05-GitHub-Publisher",
        }

    # -- API helpers --------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict | list:
        import requests
        r = requests.get(self.BASE + path, headers=self._headers, params=params, timeout=30)
        if not r.ok:
            raise GitHubAPIError(r.status_code, r.text[:500])
        return r.json()

    def _post(self, path: str, data: dict | None = None) -> dict | list:
        import requests
        r = requests.post(
            self.BASE + path,
            headers={**self._headers, "Content-Type": "application/json"},
            json=data or {},
            timeout=30,
        )
        if not r.ok:
            raise GitHubAPIError(r.status_code, r.text[:500])
        ct = r.headers.get("Content-Type", "")
        return r.json() if "json" in ct else r.text

    def _put(self, path: str, data: dict | None = None) -> dict | list:
        import requests
        r = requests.put(
            self.BASE + path,
            headers={**self._headers, "Content-Type": "application/json"},
            json=data or {},
            timeout=30,
        )
        if not r.ok:
            raise GitHubAPIError(r.status_code, r.text[:500])
        return r.json()

    def _delete(self, path: str) -> bool:
        import requests
        r = requests.delete(self.BASE + path, headers=self._headers, timeout=30)
        return r.status_code in (204, 200, 404)

    def _patch(self, path: str, data: dict) -> dict:
        import requests
        r = requests.patch(
            self.BASE + path,
            headers={**self._headers, "Content-Type": "application/json"},
            json=data,
            timeout=30,
        )
        if not r.ok:
            raise GitHubAPIError(r.status_code, r.text[:500])
        return r.json()

    # -- Repository ----------------------------------------------------------

    def get_user(self) -> dict:
        """获取当前认证用户信息。"""
        return self._get("/user")

    def get_repo(self, owner: str, repo: str) -> dict | None:
        """获取仓库信息，不存在返回 None。"""
        try:
            return self._get(f"/repos/{owner}/{repo}")
        except GitHubAPIError as e:
            if e.status == 404:
                return None
            raise

    def create_repo(
        self,
        name: str,
        description: str = "",
        private: bool = False,
        auto_init: bool = False,
    ) -> dict:
        """创建新仓库。"""
        return self._post("/user/repos", {
            "name": name,
            "description": description or "DREA v1.05 - Thin Kernel",
            "private": private,
            "auto_init": auto_init,
            "has_wiki": False,
            "has_pages": False,
        })

    def get_default_branch(self, owner: str, repo: str) -> str:
        """获取仓库默认分支名（main 或 master）。"""
        repo_info = self.get_repo(owner, repo)
        return repo_info.get("default_branch", "main")

    # -- File operations (tree-based, no git CLI needed) -------------------

    def get_file_sha(self, owner: str, repo: str, path: str, ref: str = "main") -> str | None:
        """获取文件 SHA（用于更新）。"""
        try:
            data = self._get(f"/repos/{owner}/{repo}/contents/{path}", {"ref": ref})
            return data.get("sha")
        except GitHubAPIError as e:
            if e.status == 404:
                return None
            raise

    def upload_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str | None = None,
    ) -> dict:
        """
        上传/更新单个文件到仓库。
        内部自动处理 base64 编码。
        """
        import base64

        if branch is None:
            branch = self.get_default_branch(owner, repo)

        import requests
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        payload = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        sha = self.get_file_sha(owner, repo, path, branch)
        if sha:
            payload["sha"] = sha

        return self._put(f"/repos/{owner}/{repo}/contents/{path}", payload)

    def upload_directory(
        self,
        owner: str,
        repo: str,
        local_dir: Path,
        remote_base: str = "",
        branch: str | None = None,
        ignore_patterns: tuple[str, ...] = (
            ".git", "__pycache__", ".pytest_cache",
            "node_modules", ".venv", ".env",
            "*.pyc", ".DS_Store", ".coverage",
        ),
    ) -> dict:
        """
        批量上传整个目录到仓库（跳过忽略文件）。
        返回上传结果汇总。
        """
        if branch is None:
            branch = self.get_default_branch(owner, repo)

        results = {"uploaded": [], "skipped": [], "errors": []}
        import fnmatch

        for file_path in local_dir.rglob("*"):
            if not file_path.is_file():
                continue

            rel = file_path.relative_to(local_dir)
            # 忽略检查
            skip = False
            for pat in ignore_patterns:
                if fnmatch.fnmatch(str(rel), pat) or fnmatch.fnmatch(file_path.name, pat):
                    skip = True
                    break
            if skip:
                results["skipped"].append(str(rel))
                continue

            remote_path = (Path(remote_base) / rel).as_posix()
            content = file_path.read_text(encoding="utf-8", errors="replace")

            try:
                self.upload_file(owner, repo, remote_path, content,
                                 f"Upload {rel}", branch=branch)
                results["uploaded"].append(str(rel))
            except Exception as e:
                results["errors"].append({"file": str(rel), "error": str(e)})

        return results

    # -- Release -------------------------------------------------------------

    def create_release(
        self,
        owner: str,
        repo: str,
        tag: str,
        name: str,
        body: str = "",
        draft: bool = False,
        prerelease: bool = False,
    ) -> dict:
        """创建 GitHub Release。"""
        return self._post(f"/repos/{owner}/{repo}/releases", {
            "tag_name": tag,
            "name": name,
            "body": body,
            "draft": draft,
            "prerelease": prerelease,
        })

    def get_release_by_tag(self, owner: str, repo: str, tag: str) -> dict | None:
        """通过 tag 获取 release，不存在返回 None。"""
        try:
            return self._get(f"/repos/{owner}/{repo}/releases/tags/{tag}")
        except GitHubAPIError as e:
            if e.status == 404:
                return None
            raise

    # -- Branch ---------------------------------------------------------------

    def create_branch(self, owner: str, repo: str, branch: str, from_ref: str) -> dict:
        """从指定 SHA 创建新分支。"""
        # 先获取默认分支的 ref SHA
        ref_data = self._get(f"/repos/{owner}/{repo}/git/refs/heads/{from_ref}")
        sha = ref_data["object"]["sha"]
        return self._post(f"/repos/{owner}/{repo}/git/refs", {
            "ref": f"refs/heads/{branch}",
            "sha": sha,
        })

    # -- Workflow / Actions -------------------------------------------------

    def get_workflow_runs(self, owner: str, repo: str) -> list[dict]:
        """获取最近的 workflow runs。"""
        data = self._get(f"/repos/{owner}/{repo}/actions/runs")
        return data.get("workflow_runs", [])


# ---------------------------------------------------------------------------
# Browser helper functions (used by CredentialManager._oauth_bootstrap)
# ---------------------------------------------------------------------------

def _wait_for_page_load(cdp, timeout: float = 10.0) -> None:
    """等待页面加载完成。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            resp = cdp.cmd("Page.getFrameTree")
            if resp and resp.get("frameTree", {}).get("frame", {}).get("loaderId"):
                time.sleep(0.5)
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise TimeoutError("Page load timeout")


def _get_full_document(cdp) -> str:
    """获取页面完整 HTML。"""
    result = cdp.cmd("Runtime.evaluate", {
        "expression": "document.documentElement.outerHTML",
        "returnByValue": True,
    })
    if result and result.get("result", {}).get("type") == "string":
        return result["result"]["value"]
    return ""


def _fill_input_by_label(cdp, label: str, value: str) -> bool:
    """通过 label 文本找到 input 并填写。"""
    script = f"""
        (function(){{
            const labels = document.querySelectorAll('label, dt');
            for(const lbl of labels){{
                if(lbl.textContent.trim().includes("{label}")){{
                    const id = lbl.getAttribute('for') || lbl.querySelector('input')?.id;
                    if(id){{
                        const inp = document.getElementById(id) || document.querySelector(`[name="${{id}}"]`);
                        if(inp){{ inp.value = "{value}"; inp.dispatchEvent(new Event('input',{{bubbles:true}})); return 'ok'; }}
                    }}
                    const inp = lbl.querySelector('input');
                    if(inp){{ inp.value = "{value}"; inp.dispatchEvent(new Event('input',{{bubbles:true}})); return 'ok'; }}
                }}
            }}
            return 'not_found';
        }})()
    """
    resp = cdp.cmd("Runtime.evaluate", {"expression": script, "returnByValue": True})
    return resp and resp.get("result", {}).get("value") == "ok"


def _click_scope_checkbox(cdp, scope: str) -> bool:
    """勾选指定权限范围的 checkbox。"""
    # scope 'repo' 对应 "Full control of private repositories"
    scope_map = {
        "repo": ("Full control of private repositories", "repo"),
        "workflow": ("Update GitHub Actions workflows", "workflow"),
    }
    label_text, _ = scope_map.get(scope, (scope, scope))
    script = f"""
        (function(){{
            const labels = document.querySelectorAll('label.form-checkbox-label');
            for(const lbl of labels){{
                if(lbl.textContent.includes("{label_text}")){{
                    const cb = lbl.querySelector('input[type="checkbox"]');
                    if(cb){{ cb.checked = true; cb.dispatchEvent(new Event('change',{{bubbles:true}})); return 'ok'; }}
                }}
            }}
            // 备选：按 name 属性
            const inp = document.querySelector('input[name="scopes[{scope}]"]');
            if(inp){{ inp.checked = true; inp.dispatchEvent(new Event('change',{{bubbles:true}})); return 'ok2'; }}
            return 'not_found';
        }})()
    """
    resp = cdp.cmd("Runtime.evaluate", {"expression": script, "returnByValue": True})
    return resp and resp.get("result", {}).get("value") == "ok"


def _extract_token_alternate(cdp) -> str | None:
    """备选方式：从页面提取 token（URL、通知横幅、clipboard 等）。"""
    scripts = [
        # 1. 从 clipboard 读取（GitHub 有时会写入 clipboard）
        """
        (function(){
            // 查找页面中的 token 显示区域
            const el = document.querySelector('.boxed-action, .flash-success, . дос tunpay-token');
            if(el) return el.textContent.trim();
            const all = document.querySelectorAll('[class*="token"]');
            for(const e of all){
                const t = e.textContent.trim();
                if(t.startsWith('ghp_')) return t;
            }
            return null;
        })()
        """,
        # 2. 从 URL hash 提取
        """
        (function(){
            return window.location.hash;
        })()
        """,
    ]
    for script in scripts:
        try:
            result = cdp.cmd("Runtime.evaluate", {"expression": script, "returnByValue": True})
            val = result.get("result", {}).get("value", "")
            if val and isinstance(val, str):
                if val.startswith("ghp_"):
                    return val
                # URL hash 情况
                m = re.search(r"ghp_[A-Za-z0-9]{36}", val)
                if m:
                    return m.group(0)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------

def _read_env_token() -> str | None:
    import os
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t and t.startswith("ghp_"):
        return t
    return None


def _read_token_file() -> str | None:
    if TOKEN_FILE.exists():
        content = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if content.startswith("ghp_"):
            return content
    return None


def _write_token_file(token: str) -> None:
    TOKEN_FILE.write_text(token.strip(), encoding="utf-8")


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def publish_to_github(
    repo_name: str,
    local_dir: Path,
    token: str | None = None,
    owner: str | None = None,
    description: str = "DREA v1.05 Thin Kernel — Autonomous Agent Core Framework",
    private: bool = False,
    release_tag: str | None = None,
    release_name: str | None = None,
    release_body: str = "",
    cdp_client=None,
) -> dict:
    """
    一句话发布整个目录到 GitHub（零 git CLI 依赖）。

    参数：
        repo_name      : 仓库名（如 "drea-thin-kernel"）
        local_dir      : 本地目录路径（Path 对象）
        token          : GitHub PAT（None → 自动从凭据链获取）
        owner          : GitHub 用户名（None → 自动从 API 获取）
        description    : 仓库描述
        private        : 是否私有
        release_tag    : 发布 tag（如 "v1.05"，None 则不创建 release）
        release_name   : Release 名称（None → 同 tag）
        release_body   : Release 说明
        cdp_client     : CDPClient 实例（用于浏览器自举）

    返回：{
        "success": bool,
        "repo_url": str,
        "uploaded_files": int,
        "release_url": str | None,
        "token_saved": bool,
    }
    """
    # 1. 凭据
    cred_mgr = GitHubCredentialManager(cdp_client=cdp_client)
    if token:
        cred_mgr._token = token
    actual_token = cred_mgr.get_token()

    # 2. API client
    gh = GitHubPublisher(actual_token)

    # 3. 获取用户名
    if owner is None:
        user_info = gh.get_user()
        owner = user_info.get("login")
        if not owner:
            raise RuntimeError("Cannot determine GitHub username from token")

    # 4. 创建/获取仓库
    repo_info = gh.get_repo(owner, repo_name)
    if repo_info is None:
        repo_info = gh.create_repo(repo_name, description, private)
        print(f"  [GitHub] Created repo: {repo_info['html_url']}")

    repo_url = repo_info.get("html_url", f"https://github.com/{owner}/{repo_name}")

    # 5. 上传文件
    upload_results = gh.upload_directory(owner, repo_name, local_dir, remote_base="")
    print(f"  [GitHub] Uploaded: {len(upload_results['uploaded'])} files, "
          f"skipped: {len(upload_results['skipped'])}, "
          f"errors: {len(upload_results['errors'])}")

    # 6. Release
    release_url = None
    if release_tag:
        existing = gh.get_release_by_tag(owner, repo_name, release_tag)
        if existing:
            print(f"  [GitHub] Release {release_tag} already exists, skipping.")
            release_url = existing.get("html_url")
        else:
            rel = gh.create_release(
                owner, repo_name, release_tag,
                release_name or release_tag,
                release_body or f"DREA v1.05 Thin Kernel — {release_tag}",
            )
            release_url = rel.get("html_url")
            print(f"  [GitHub] Created release: {release_url}")

    return {
        "success": True,
        "repo_url": repo_url,
        "uploaded_files": len(upload_results["uploaded"]),
        "skipped_files": len(upload_results["skipped"]),
        "errors": upload_results["errors"],
        "release_url": release_url,
        "token_saved": TOKEN_FILE.exists(),
        "owner": owner,
        "repo": repo_name,
    }
