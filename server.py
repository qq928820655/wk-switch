"""本地 HTTP 服务：把转换引擎接到浏览器界面上。

只监听 127.0.0.1。没有登录态——这是本机单用户工具，多用户部署不在设计范围内。
文件内容走 base64 JSON 上传：账户包体积小（几 KB 到几 MB），比手写 multipart
解析可靠，也不用依赖已从标准库移除的 cgi 模块。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from wk import (auth, clock, formats, pipeline, probe, recheck, store,
                update as updater, version)
from wk.model import Account

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"

MAX_BODY = 32 * 1024 * 1024          # 单次请求上限
MAX_UPLOAD = 16 * 1024 * 1024        # 单文件解码后上限
JOB_TTL = 2 * 3600                   # 会话两小时过期

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_outputs: dict[str, dict] = {}


def _gc() -> None:
    """清理过期会话与导出缓存。账户凭据不该在内存里无限堆积。"""
    now = time.time()
    with _lock:
        for store, ttl in ((_jobs, JOB_TTL), (_outputs, JOB_TTL)):
            for key in [k for k, v in store.items() if now - v["created"] > ttl]:
                store.pop(key, None)


def _new_id() -> str:
    return secrets.token_urlsafe(12)


def _preview(accounts: list[Account], limit: int = 200) -> list[dict]:
    """界面预览：token 一律脱敏。"""
    rows = []
    for index, acc in enumerate(accounts[:limit], 1):
        row = acc.masked()
        row["index"] = index
        row["state"] = row.get("token_state") or ""
        rows.append(row)
    return rows


def _rows_for_ui(accounts: list[Account], limit: int = 200) -> list[dict]:
    from wk import token as tok

    out = []
    for index, acc in enumerate(accounts[:limit], 1):
        state = tok.token_state(acc.access_token, acc.expires_at, acc.refresh_token)
        out.append({
            "index": index,
            "uid": acc.uid,
            "nickname": acc.nickname,
            "email": acc.email or acc.phone,
            "realm": acc.realm,
            "domain": acc.domain,
            "state": state,
            "state_label": tok.STATE_LABEL.get(state, state),
            "expires": clock.stamp(acc.expires_at, "%Y-%m-%d") if acc.expires_at else "",
            "at": (acc.access_token[:8] + "…" + acc.access_token[-4:]) if len(acc.access_token) > 14 else ("*" * len(acc.access_token)),
            "rt": "有" if acc.refresh_token else "",
            "enabled": acc.enabled,
        })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "wk-switch/1.0"

    # ---- 基础响应 -------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def log_message(self, fmt: str, *args) -> None:
        # 默认实现会往 stderr 打全量请求行（含文件名）。本地工具，只留错误。
        if args and str(args[0]).startswith(("4", "5")):
            print("[http] " + (fmt % args))

    # ---- 路由 -----------------------------------------------------
    def _cookie(self, name: str) -> str:
        from http.cookies import SimpleCookie

        raw = self.headers.get("Cookie") or ""
        if not raw:
            return ""
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 — 畸形 cookie 一律当没登录
            return ""
        morsel = jar.get(name)
        return morsel.value if morsel else ""

    def _authed(self) -> bool:
        return auth.check_session(self._cookie(auth.COOKIE_NAME))

    def _need_auth(self, path: str) -> bool:
        """未登录时的处理。返回 True 表示已经应答完毕。"""
        if self._authed():
            return False
        if path.startswith("/api/"):
            self._error(401, "未登录或会话已过期")
        else:
            # 页面请求走跳转，浏览器地址栏留在 /login 更直观
            self._send(302, b"", "text/plain; charset=utf-8",
                       {"Location": "/login"})
        return True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/login", "/login.html"):
            self._login_page()
            return
        if path in ("/", "/index.html"):
            if self._need_auth(path):
                return
            self._serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            # 浏览器和不少客户端不看 <link>，直接按根路径要这个文件。
            # 放在最前面：登录页也要能显示图标，不能走鉴权。
            self._serve_file(WEB_DIR / "assets" / "favicon.ico", "image/x-icon")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (WEB_DIR / rel).resolve()
            # 目录穿越防护：只允许 web/ 下的文件
            if WEB_DIR.resolve() not in target.parents:
                self._error(403, "forbidden")
                return
            self._serve_file(target, _guess_type(target))
            return
        if path == "/api/ping":
            # 免鉴权：升级重启期间浏览器探活只认 200。只回版本号和 pid，
            # 不含任何账号或配置信息。必须放在鉴权之前——重启后新进程的
            # 会话表可能还没从磁盘补齐，要求登录的话页面永远等不到「回来了」。
            self._json(200, {"ok": True, "app": version.APP_NAME,
                             "version": version.version(), "pid": os.getpid()})
            return
        if self._need_auth(path):
            return
        if path == "/api/version":
            cfg = updater.config()
            self._json(200, {"ok": True, "version": version.info(),
                             "update": {"repo": cfg.get("repo") or "",
                                        "index_url": cfg.get("index_url") or "",
                                        "proxy": cfg.get("proxy") or ""}})
            return
        if path == "/api/update/check":
            self._update_check()
            return
        if path == "/api/update/status":
            self._update_status()
            return
        if path == "/api/profiles":
            self._json(200, {"ok": True, "profiles": formats.profile_list()})
            return
        if path == "/api/sample":
            self._sample()
            return
        if path == "/api/store/list":
            self._store_list()
            return
        if path == "/api/store/stats":
            self._json(200, {"ok": True, "stats": store.stats()})
            return
        if path == "/api/store/backup":
            self._store_backup()
            return
        if path == "/api/probe/config":
            self._json(200, {"ok": True, "config": probe_config()})
            return
        if path == "/api/recheck/config":
            self._recheck_config_get()
            return
        if path == "/api/recheck/list":
            self._recheck_list()
            return
        if path.startswith("/api/download/"):
            self._download(path[len("/api/download/"):])
            return
        self._error(404, "not found")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._error(400, str(exc))
            return

        # 登录接口本身不能要求登录
        if path == "/api/login":
            self._api_login(payload)
            return

        if self._need_auth(path):
            return

        try:
            if path == "/api/logout":
                self._api_logout()
            elif path == "/api/parse":
                self._api_parse(payload)
            elif path == "/api/convert":
                self._api_convert(payload)
            elif path == "/api/export":
                self._api_export(payload)
            elif path == "/api/store/import":
                self._store_import(payload)
            elif path == "/api/store/rename":
                self._store_rename(payload)
            elif path == "/api/store/delete":
                self._store_delete(payload)
            elif path == "/api/store/clear":
                self._store_clear()
            elif path == "/api/store/export":
                self._store_export(payload)
            elif path == "/api/store/restore":
                self._store_restore(payload)
            elif path == "/api/probe":
                self._probe(payload)
            elif path == "/api/probe/all":
                self._probe_all(payload)
            elif path == "/api/recheck/run":
                self._recheck_run(payload)
            elif path == "/api/recheck/config":
                self._recheck_config_set(payload)
            elif path == "/api/recheck/nodes":
                self._recheck_nodes(payload)
            elif path == "/api/recheck/track":
                self._recheck_track(payload)
            elif path == "/api/update/apply":
                self._update_apply()
            elif path == "/api/update/upgrade":
                self._update_upgrade()
            elif path == "/api/update/local":
                self._update_local(payload)
            elif path == "/api/update/restart":
                self._update_restart()
            elif path == "/api/update/rollback":
                self._update_rollback(payload)
            elif path == "/api/update/config":
                self._update_config(payload)
            elif path == "/api/reset":
                self._json(200, {"ok": True})
            else:
                self._error(404, "not found")
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # noqa: BLE001 — 兜底，把栈打到控制台方便定位
            traceback.print_exc()
            self._error(500, f"{type(exc).__name__}: {exc}")

    # ---- 登录 -----------------------------------------------------
    def _login_page(self) -> None:
        """登录页。带 ?token= 时直接换会话，省得手输——本机工具的一次性入口。

        重定向目标只接受站内路径：next 以 // 开头会被浏览器当成外站，
        等于把口令连带跳转一起送出去。
        """
        from urllib.parse import parse_qs

        query = parse_qs(urlparse(self.path).query)
        token = (query.get("token") or [""])[0]
        if token and auth.token_matches(token):
            sid = auth.issue_session()
            target = (query.get("next") or ["/"])[0]
            if not target.startswith("/") or target.startswith("//"):
                target = "/"
            cookie = (f"{auth.COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={auth.session_ttl()}")
            self._send(302, b"", "text/plain; charset=utf-8",
                       {"Location": target, "Set-Cookie": cookie})
            return
        self._serve_file(WEB_DIR / "login.html", "text/html; charset=utf-8")

    def _api_login(self, payload: dict) -> None:
        token = str(payload.get("token") or "")
        if not auth.token_matches(token):
            # 不区分「口令为空」「口令错误」，避免把口令长度当提示
            self._error(401, "登录口令不正确")
            return
        sid = auth.issue_session()
        self._json_with_cookie(200, {"ok": True, "expires_in": auth.session_ttl()}, sid)

    def _api_logout(self) -> None:
        auth.revoke(self._cookie(auth.COOKIE_NAME))
        self._send(200, json.dumps({"ok": True}).encode("utf-8"),
                   "application/json; charset=utf-8",
                   {"Set-Cookie": f"{auth.COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Strict"})

    def _json_with_cookie(self, status: int, payload: dict, sid: str) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        cookie = (f"{auth.COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Strict; "
                  f"Max-Age={auth.session_ttl()}")
        self._send(status, body, "application/json; charset=utf-8", {"Set-Cookie": cookie})

    # ---- 实现 -----------------------------------------------------
    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是对象")
        return data

    def _sample(self) -> None:
        """返回内置样例文件，供界面上「载入示例」使用。

        只允许 samples/ 下的白名单文件——这个接口不该变成任意文件读取。
        """
        from urllib.parse import parse_qs

        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or ["hub-export.json"])[0]
        sample_dir = ROOT / "samples"
        allowed = {p.name for p in sample_dir.glob("*") if p.suffix in (".json", ".zip", ".txt")}
        if name not in allowed:
            self._error(404, f"没有这个样例：{name}")
            return
        target = sample_dir / name
        try:
            data = target.read_bytes()
        except OSError:
            self._error(404, "样例文件读取失败")
            return
        self._json(200, {
            "ok": True,
            "name": name,
            "data": base64.b64encode(data).decode("ascii"),
            "samples": sorted(allowed),
        })

    def _serve_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self._error(404, f"missing {path.name}")
            return
        self._send(200, data, content_type)

    def _download(self, file_id: str) -> None:
        with _lock:
            item = _outputs.get(file_id)
        if not item:
            self._error(404, "下载链接已过期")
            return
        self._send(
            200, item["data"], "application/octet-stream",
            {"Content-Disposition": f'attachment; filename="{item["name"]}"'},
        )

    def _api_parse(self, payload: dict) -> None:
        files = payload.get("files") or []
        if not isinstance(files, list) or not files:
            raise ValueError("没有收到文件")

        all_rows: list[dict] = []
        sources: list[dict] = []
        notes: list[str] = []

        for item in files:
            name = str(item.get("name") or "unnamed")
            encoded = item.get("data") or ""
            try:
                data = base64.b64decode(encoded, validate=False)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{name} 的 base64 解码失败：{exc}") from exc
            if len(data) > MAX_UPLOAD:
                raise ValueError(f"{name} 超过 {MAX_UPLOAD // 1024 // 1024} MB 上限")
            parsed = formats.parse_bytes(name, data)
            all_rows.extend(parsed.rows)
            sources.append({
                "name": name,
                "source": parsed.source,
                "source_label": parsed.source_label,
                "count": len(parsed.rows),
                "files": parsed.files[:50],
            })
            notes.extend(parsed.notes)

        if not all_rows:
            raise ValueError("文件里没有解析出任何账号记录")

        accounts, errors = formats.to_accounts(all_rows, sources[0]["source"] if sources else "")
        if errors:
            notes.extend(errors[:20])

        job_id = _new_id()
        _gc()
        with _lock:
            _jobs[job_id] = {"accounts": accounts, "created": time.time()}

        self._json(200, {
            "ok": True,
            "job": job_id,
            "sources": sources,
            "notes": notes,
            "total": len(accounts),
            "preview": _rows_for_ui(accounts),
            "stats": pipeline.summarize(accounts),
        })

    def _load_job(self, payload: dict) -> list[Account]:
        job_id = str(payload.get("job") or "")
        with _lock:
            job = _jobs.get(job_id)
        if not job:
            raise ValueError("会话已过期，请重新导入文件")
        return job["accounts"]

    def _api_convert(self, payload: dict) -> None:
        accounts = self._load_job(payload)
        opts = payload.get("opts") or {}
        if not isinstance(opts, dict):
            raise ValueError("opts 必须是对象")

        result = pipeline.convert(list(accounts), opts)
        job_id = str(payload.get("job"))
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job["converted"] = result.accounts
                job["opts"] = opts
                job["created"] = time.time()

        self._json(200, {
            "ok": True,
            "total": result.input_count,
            "kept": result.kept,
            "dropped": result.dropped,
            "merged": result.merged,
            "filled_uid": result.filled_uid,
            "issues": [i.to_dict() for i in result.issues],
            "preview": _rows_for_ui(result.accounts),
            "stats": pipeline.summarize(result.accounts),
        })

    def _api_export(self, payload: dict) -> None:
        job_id = str(payload.get("job") or "")
        with _lock:
            job = _jobs.get(job_id)
        if not job:
            raise ValueError("会话已过期，请重新导入文件")

        profile_id = str(payload.get("profile") or "")
        opts = payload.get("opts") or job.get("opts") or {}
        accounts = job.get("converted")
        if accounts is None:
            accounts = pipeline.convert(list(job["accounts"]), opts).accounts
        if not accounts:
            raise ValueError("转换结果为空，没有可导出的账号")

        realm_mode = str(payload.get("realm") or "all")
        outputs = self._build_exports(accounts, profile_id, realm_mode)
        self._respond_files(outputs, profile_id, opts, realm_mode, len(accounts))

    # ---- 导出组装 -------------------------------------------------
    def _build_exports(self, accounts: list[Account], profile_id: str, realm_mode: str) -> list:
        """按版本组装导出文件。

        realm_mode:
          all   —— 全部账号一份
          cn    —— 只出国内版
          intl  —— 只出国际版
          split —— 两个版本各出一份（文件名带 -cn / -intl 后缀）

        分版本是一等需求：国内版和国际版打的是不同上游，混在一个包里导进去，
        另一半账号会在错误的出口上全 401。
        """
        if realm_mode == "split":
            outputs = []
            # 同一批导出共用时间戳，两份文件不会跨秒、排序时挨在一起
            stamp = clock.compact()
            for realm in ("cn", "intl"):
                subset = [a for a in accounts if (a.realm or "").lower() == realm]
                if not subset:
                    continue
                # 文件名里的版本段由 formats.export_filename 负责，这里不再叠后缀
                outputs.extend(formats.render(
                    subset, profile_id, {"realm_filter": realm, "stamp": stamp}))
            if not outputs:
                raise ValueError("账号里没有可识别的版本，无法分版本导出")
            return outputs

        opts = {"realm_filter": realm_mode} if realm_mode in ("cn", "intl") else {}
        return formats.render(accounts, profile_id, opts)

    def _respond_files(self, outputs: list, profile_id: str, opts: dict,
                       realm_mode: str, total: int) -> None:
        files = []
        with _lock:
            for out in outputs:
                file_id = _new_id()
                _outputs[file_id] = {"name": out.name, "data": out.data, "created": time.time()}
                files.append({
                    "name": out.name,
                    "size": out.size,
                    "download": f"/api/download/{file_id}",
                })

        self._json(200, {
            "ok": True,
            "files": files,
            "report": {
                "profile": profile_id,
                "profile_name": formats.PROFILES[profile_id].name,
                "exported_at": clock.stamp(),
                "realm": realm_mode,
                "count": total,
                "opts": opts,
            },
        })

    # ---- 本地账户库 -----------------------------------------------
    def _store_import(self, payload: dict) -> None:
        """把当前会话的转换结果写进本地库，按 (uid, 版本) 去重更新。

        `risk=True` 表示这一批导入时就已经知道是风控状态（来源系统标注过、
        或者用户自己清楚），直接进风控区；不传就是正常账号。**只有这里和
        后续验证能决定一个号进不进风控区**，导入文件里的备注措辞不再自动
        判定——那条路径会把整批正常号一起拖进风控区。
        """
        accounts = self._load_job(payload)
        opts = payload.get("opts") or {}
        result = pipeline.convert(list(accounts), opts)
        if not result.accounts:
            raise ValueError("转换结果为空，没有可留存的账号")

        source = str(payload.get("source") or "wk-switch")
        risk = bool(payload.get("risk"))
        batch = "import-" + clock.compact()
        stats = store.upsert(result.accounts, batch=batch, source=source, risk=risk)
        self._json(200, {
            "ok": True,
            "batch": batch,
            "risk": risk,
            "risk_count": stats.get("risk", 0),
            "inserted": stats["inserted"],
            "updated": stats["updated"],
            "skipped": stats["skipped"],
            "total": stats["total"],
            "stats": store.stats(),
        })

    def _store_list(self) -> None:
        from urllib.parse import parse_qs

        query = parse_qs(urlparse(self.path).query)
        realm = (query.get("realm") or [""])[0]
        keyword = (query.get("q") or [""])[0]
        batch = (query.get("batch") or [""])[0]
        try:
            limit = int((query.get("limit") or ["500"])[0] or 500)
        except ValueError:
            limit = 500
        rows = store.list_accounts(realm=realm, query=keyword,
                                   limit=min(limit, 2000), batch=batch)
        self._json(200, {"ok": True, "rows": rows, "total": len(rows),
                         "batch": batch, "stats": store.stats()})

    def _store_rename(self, payload: dict) -> None:
        row_id = int(payload.get("id") or 0)
        name = str(payload.get("name") or "")
        result = store.rename(row_id, name)
        self._json(200, {"ok": True, **result, "stats": store.stats()})

    def _store_delete(self, payload: dict) -> None:
        ids = [int(i) for i in (payload.get("ids") or []) if str(i).strip()]
        removed = store.delete(ids)
        self._json(200, {"ok": True, "removed": removed, "stats": store.stats()})

    def _store_clear(self) -> None:
        removed = store.clear()
        self._json(200, {"ok": True, "removed": removed, "stats": store.stats()})

    def _store_export(self, payload: dict) -> None:
        """从本地库直接导出，不依赖当前会话。

        范围规则（和服务端前端各判一次，任何一边算错都不会导出一整库）：

        - **勾了一部分** → 只导勾选的那些跳过版本筛选，因为用户已经用手划了
          范围，再按版本掐一遍会把选中的行悄悄剔掉；
        - **全勾选 / 一条不勾** → 导出全部并按版本选项走。全勾是「这批我都要」，
          不勾是「我没限定」，两种都等于不限制。
        """
        profile_id = str(payload.get("profile") or "")
        realm_mode = str(payload.get("realm") or "all")
        ids = payload.get("ids") or None
        # 前端会明确说这次是「只导勾的」还是「导全部」：全勾时它不会传 ids，
        # 但万一有人直接打接口传了全量 ids，scope 优先，不然版本选项会被跳过。
        asked = str(payload.get("scope") or "").strip()
        scope = "all"
        if asked == "all":
            ids = None

        if ids:
            ids = [int(i) for i in ids]
            accounts = store.load_for_export(ids=ids)
            scope = "selected"
            # 分版本导出对「已选中的这批」照样有效：拆包只看 realm，不看范围。
            realm_mode = "split" if realm_mode == "split" else "all"
        elif realm_mode in ("cn", "intl"):
            accounts = store.load_for_export(realm=realm_mode)
        else:
            accounts = store.load_for_export()

        if not accounts:
            raise ValueError("本地库里没有可导出的账号")

        outputs = self._build_exports(accounts, profile_id, realm_mode)
        self._respond_files(outputs, profile_id, {"scope": scope},
                            realm_mode, len(accounts))

    # ---- 在线升级 -------------------------------------------------
    def _site(self) -> tuple[int, str]:
        """当前监听的 (端口, 地址)。重启要把这两个原样交代给监督进程。"""
        host, port = self.server.server_address[:2]
        return int(port), str(host or "127.0.0.1")

    def _update_check(self) -> None:
        try:
            result = updater.check()
        except ValueError as exc:
            self._json(200, {"ok": False, "error": str(exc),
                             "current": version.version()})
            return
        self._json(200, {"ok": True, **result})

    def _update_apply(self) -> None:
        """只替换代码，不重启。留给命令行 / 脚本用。"""
        try:
            result = updater.apply()
        except ValueError as exc:
            self._error(400, str(exc))
            return
        self._json(200, result)

    def _exit_after_restart(self, result: dict) -> None:
        """响应已经发出去了，现在决定这一进程怎么退。

        正常情况：监督进程在另一个 cgroup / 独立 session 里接着干，这里直接
        退出，把端口让出来。

        降级情况（systemd 部署但拿不到 systemd-run）：没有独立 cgroup 可用，
        监督进程会被 systemd 随服务一起停掉。那就由本进程在退出前按一下重启键
        —— `--no-block` 让它立即返回，真正的停和起交给 systemd。
        """
        from wk import relaunch

        spawn = result.get("restart") if isinstance(result.get("restart"), dict) else {}
        if spawn.get("degraded"):
            unit = str(spawn.get("unit") or "")
            def kick() -> None:
                if unit:
                    relaunch.kick_systemd(unit)
                os._exit(0)
            # 0.5 秒：够浏览器把响应体收完，又不至于让页面干等
            threading.Timer(0.5, kick).start()
            return
        threading.Timer(0.5, lambda: os._exit(0)).start()

    def _update_upgrade(self) -> None:
        """一键升级：下载 → 替换 → 重启，一个请求做完。

        响应先把「已交棒」发出去，重启在监督进程里继续；页面拿到 200 就切到
        等待态轮询 /api/ping，不再让用户点第二次。
        """
        port, host = self._site()
        try:
            result = updater.upgrade(port, host)
        except ValueError as exc:
            self._error(400, str(exc))
            return
        result["old_pid"] = os.getpid()
        self._json(200, result)
        if result.get("restarting"):
            self._exit_after_restart(result)

    def _update_restart(self) -> None:
        port, host = self._site()
        result = updater.restart(port, host,
                                 target_version=version.version(),
                                 from_version=version.version())
        result["old_pid"] = os.getpid()
        self._json(200, result)
        if result.get("ok"):
            self._exit_after_restart(result)

    def _update_status(self) -> None:
        """重启进度。重启期间旧进程已退，只有新进程起来后才读得到。"""
        from wk import relaunch

        self._json(200, {"ok": True, "status": relaunch.status(),
                         "current": version.version()})

    def _update_rollback(self, payload: dict) -> None:
        """用备份回滚并重启。自动回滚失败时的手动兜底。"""
        port, host = self._site()
        prefer = str(payload.get("prefer_version") or "")
        exclude = str(payload.get("exclude_version") or version.version())
        try:
            result = updater.rollback_latest(port, host, exclude_version=exclude,
                                             prefer_version=prefer)
        except ValueError as exc:
            self._error(400, str(exc))
            return
        result["old_pid"] = os.getpid()
        self._json(200, result)
        if result.get("restarting"):
            self._exit_after_restart(result)

    def _update_local(self, payload: dict) -> None:
        """上传本地升级包，替换 + 重启一步到位。

        国内到 github.com 的 release 下载常被掐断，让用户自己在浏览器里
        下载 zip 再传回来是最省事的兜底路径——下载走浏览器自己的网络栈，
        和本服务无关。
        """
        name = str(payload.get("name") or "").strip()
        raw = payload.get("data") or ""
        if not raw:
            raise ValueError("没有收到文件内容")
        try:
            blob = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("文件内容不是合法的 base64") from exc
        if len(blob) > 64 * 1024 * 1024:
            raise ValueError("包太大了（上限 64MB）")
        port, host = self._site()
        result = updater.upgrade_local(port, host, blob, name)
        result["old_pid"] = os.getpid()
        self._json(200, result)
        if result.get("restarting"):
            self._exit_after_restart(result)

    def _update_config(self, payload: dict) -> None:
        patch = {k: payload[k] for k in ("repo", "index_url", "asset", "allow_prerelease", "proxy")
                 if k in payload}
        cfg = updater.save_config(patch)
        self._json(200, {"ok": True, "config": {k: cfg.get(k) for k in
                                                ("repo", "index_url", "asset", "allow_prerelease", "proxy")}})

    # ---- 备份 / 还原 -----------------------------------------------
    def _store_backup(self) -> None:
        """打包整库为 zip，返回一次性下载链接。

        备份包里带 token 明文——这是本地备份，脱敏就失去还原价值。
        链接和转换产物一样走 _outputs 的两小时 TTL，不落盘。
        """
        data, name = store.backup_bytes()
        file_id = _new_id()
        with _lock:
            _outputs[file_id] = {"name": name, "data": data, "created": time.time()}
        self._json(200, {
            "ok": True,
            "file": {"name": name, "size": len(data), "download": f"/api/download/{file_id}"},
            "stats": store.stats(),
        })

    def _store_restore(self, payload: dict) -> None:
        """从备份包还原。mode=merge 合并 / replace 覆盖。"""
        name = str(payload.get("name") or "backup.zip")
        encoded = payload.get("data") or ""
        mode = str(payload.get("mode") or "merge").lower()
        try:
            raw = base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"备份文件解码失败：{exc}") from exc
        if not raw:
            raise ValueError("备份文件是空的")
        if len(raw) > MAX_UPLOAD:
            raise ValueError(f"备份文件超过 {MAX_UPLOAD // 1024 // 1024} MB 上限")

        result = store.restore_bytes(raw, filename=name, mode=mode)
        self._json(200, {"ok": True, **result, "stats": store.stats()})

    # ---- 上游校验 -------------------------------------------------
    def _probe(self, payload: dict) -> None:
        """校验选中的若干账号。

        只读：探针只打计费查询接口，不刷新 token、不签到。刷新会让别处正在
        用的同一个账号掉线，见 wk/probe.py 顶部。
        """
        ids = payload.get("ids") or []
        if not isinstance(ids, list):
            raise ValueError("ids 必须是数组")
        if not ids:
            raise ValueError("没有选中账号")

        pairs = store.load_for_probe(ids=[int(i) for i in ids if str(i).isdigit()])
        if not pairs:
            raise ValueError("选中的账号在库里不存在")

        self._json(200, self._run_probe(pairs))

    def _probe_all(self, payload: dict) -> None:
        """整库校验，可按版本筛。账号多时慢，界面上有进度提示。"""
        realm = str(payload.get("realm") or "")
        pairs = store.load_for_probe(realm=realm if realm in ("cn", "intl") else "")
        if not pairs:
            raise ValueError("库里没有账号")
        self._json(200, self._run_probe(pairs))

    # ---- 封号复检 -------------------------------------------------
    def _recheck_config_get(self) -> None:
        """读设置。节点只列地址不探测——打开页面就发十个请求太重。"""
        cfg = recheck.load_config()
        self._json(200, {
            "ok": True,
            "config": cfg,
            "nodes": recheck.discover_nodes(cfg, probe_now=False),
            "exit_modes": [{"value": m, "label": recheck.EXIT_LABELS[m]}
                           for m in recheck.EXIT_MODES],
            "custom_urls": recheck.parse_urls(cfg.get("exit_custom")),
            "stats": store.recheck_stats(),
            "track_labels": store.RECHECK_LABELS,
        })

    def _recheck_list(self) -> None:
        """名单行。默认只出 banned——「封号复检」页要的就是这一批。"""
        from urllib.parse import parse_qs

        query = parse_qs(urlparse(self.path).query)
        realm = (query.get("realm") or [""])[0]
        keyword = (query.get("q") or [""])[0]
        track = (query.get("track") or ["banned"])[0]
        allowed = ("banned", "pending", "revived", "auth")
        rows = store.list_accounts(
            realm=realm if realm in ("cn", "intl") else "",
            query=keyword,
            track=track if track in allowed else "banned",
        )
        self._json(200, {
            "ok": True,
            "track": track if track in allowed else "banned",
            "rows": rows,
            "stats": store.recheck_stats(),
            "config": recheck.load_config(),
        })

    def _recheck_run(self, payload: dict) -> None:
        """跑一轮复检。

        没有 `ids` 就跑名单里那一档（默认 banned）；给了 `ids` 就只测这几个。
        `force` 跳过最小间隔——界面上「立即复检」用的就是它。
        """
        cfg = recheck.load_config()
        if not cfg["enabled"]:
            raise ValueError("封号复检没开启：先在设置里打开总开关")

        ids = payload.get("ids") or []
        if ids:
            pairs = store.load_for_probe(ids=[int(i) for i in ids if str(i).isdigit()])
        else:
            track = str(payload.get("track") or "banned")
            if track not in ("banned", "pending", "revived", "auth"):
                track = "banned"
            rows = store.list_accounts(track=track, limit=cfg["batch_limit"])
            pairs = store.load_for_probe(ids=[r["id"] for r in rows])

        if not pairs:
            raise ValueError("名单里没有账号，无需复检")

        runner = recheck.Runner(cfg)
        results = runner.run(pairs, force=bool(payload.get("force")))
        written = store.update_recheck(results)

        self._json(200, {
            "ok": True,
            "written": written,
            "summary": recheck.summarize(results),
            "results": [
                {
                    "id": item["id"],
                    "verdict": item["verdict"],
                    "detail": item["detail"],
                    "raw": item["raw"],
                    "status": item["status"],
                    "latency_ms": item["latency_ms"],
                    "proxy": item["proxy"],
                    "proxy_ip": item.get("proxy_ip", ""),
                    "exit_mode": item.get("exit_mode", ""),
                    "skipped": item.get("skipped", False),
                }
                for item in results
            ],
            "stats": store.recheck_stats(),
            "rows": store.list_accounts(track="banned"),
        })

    def _recheck_config_set(self, payload: dict) -> None:
        """保存设置。`config` 里可以只带要改的字段。"""
        patch = payload.get("config")
        if not isinstance(patch, dict):
            patch = payload
        cfg = recheck.save_config(patch)
        self._json(200, {
            "ok": True,
            "config": cfg,
            "nodes": recheck.discover_nodes(cfg, probe_now=False),
            "exit_modes": [{"value": m, "label": recheck.EXIT_LABELS[m]}
                           for m in recheck.EXIT_MODES],
            "custom_urls": recheck.parse_urls(cfg.get("exit_custom")),
            "stats": store.recheck_stats(),
        })

    def _recheck_nodes(self, payload: dict) -> None:
        """自动检测 mihomo 节点。

        允许在 body 里临时给 host/ports 试一下（不保存）：用户手上有两三套
        mihomo，得先确认哪套能用再决定写不写配置。
        """
        cfg = recheck.load_config()
        patch = payload.get("mihomo") if isinstance(payload.get("mihomo"), dict) else None
        if patch:
            cfg = dict(cfg)
            cfg["mihomo"] = {**cfg["mihomo"], **patch}
            cfg["mihomo"]["timeout"] = recheck._float(
                cfg["mihomo"].get("timeout"), 10.0, 3.0, 60.0)
            # api 只收 http/https（normalize_url 会把非法值洗成空串）；
            # secret 原样保留但去掉首尾空白——手输容易带空格
            cfg["mihomo"]["api"] = recheck.normalize_url(
                str(cfg["mihomo"].get("api") or "").strip())
            cfg["mihomo"]["secret"] = str(cfg["mihomo"].get("secret") or "").strip()
            # 池面板同 host 不同端口，地址同样只收 http/https
            cfg["mihomo"]["pool"] = recheck.normalize_url(
                str(cfg["mihomo"].get("pool") or "").strip())
            cfg["mihomo"]["pool_token"] = str(
                cfg["mihomo"].get("pool_token") or "").strip()
        data = recheck.discover_nodes(cfg, probe_now=True)
        # 自定义出口栏填着东西时一并测，页面上一次点击就能看到两边的出口 IP
        custom = payload.get("custom")
        if custom:
            urls = recheck.parse_urls(custom)
            nodes = recheck.probe_urls(urls, cfg["mihomo"]["probe_url"],
                                       cfg["mihomo"]["timeout"])
            data["custom_nodes"] = nodes
            data["custom_ok"] = sum(1 for n in nodes if n.get("ok"))
        self._json(200, {"ok": True, **data})

    def _recheck_track(self, payload: dict) -> None:
        """手工加进 / 移出名单。state 为空串表示移出。"""
        ids = payload.get("ids") or []
        if not isinstance(ids, list) or not ids:
            raise ValueError("没有选中账号")
        state = str(payload.get("state") or "")
        note = str(payload.get("note") or "")
        changed = store.set_track(
            [int(i) for i in ids if str(i).isdigit()],
            state if state in store.RECHECK_LABELS else "",
            note,
        )
        self._json(200, {
            "ok": True,
            "changed": changed,
            "stats": store.recheck_stats(),
            "rows": store.list_accounts(track="banned"),
        })

    def _run_probe(self, pairs: list) -> dict:
        """跑探针 + 回写 + 返回汇总。两条路由共用这一份。"""
        cfg = probe_config()
        runner = probe.Prober(
            concurrency=cfg["concurrency"],
            timeout=cfg["timeout"],
            min_gap=cfg["min_gap"],
            max_gap=cfg["max_gap"],
        )
        accounts = [acc for _, acc in pairs]
        results = runner.run(accounts)

        # 结果按输入顺序返回，pairs 也是同一个顺序，用下标对齐
        rows = []
        moved = 0
        for (row_id, acc), res in zip(pairs, results):
            restrict = bool(res.get("restrict"))
            if restrict:
                moved += 1
            rows.append({
                "id": row_id,
                "state": res.get("state"),
                "remain": res.get("remain"),
                "used": res.get("used"),
                "size": res.get("size"),
                "checked_at": res.get("checked_at"),
                "note": res.get("error") or "",
                "restrict": restrict,
            })

        written = store.update_credits(rows)
        summary = probe.summarize(results)
        return {
            "ok": True,
            "written": written,
            # 这次校验里有多少个被判定为风控、已经挪进风控区
            "restricted": moved,
            "summary": summary,
            "results": [
                {
                    "id": row_id,
                    "uid": acc.uid,
                    "name": acc.nickname or acc.uid,
                    "state": res.get("state"),
                    "remain": res.get("remain"),
                    "used": res.get("used"),
                    "size": res.get("size"),
                    "tier": res.get("tier") or "",
                    "error": res.get("error") or "",
                    "latency_ms": res.get("latency_ms") or 0,
                    "endpoint": res.get("endpoint") or "",
                }
                for (row_id, acc), res in zip(pairs, results)
            ],
            "accounts": store.list_accounts(limit=500),
            "stats": store.stats(),
        }


#: 探针默认参数。并发压到 4、请求间隔 200~500ms，是为了不把几十个账号
#: 同时怼到同一个上游——那是标准的风控触发姿势。
PROBE_DEFAULTS = {
    "concurrency": 4,
    "timeout": 12.0,
    "min_gap": 0.20,
    "max_gap": 0.50,
}

PROBE_CONFIG_PATH = ROOT / "config" / "probe.json"


def probe_config() -> dict:
    """读 config/probe.json，缺项用默认值补齐，坏文件不炸。"""
    cfg = dict(PROBE_DEFAULTS)
    try:
        raw = json.loads(PROBE_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for key, default in PROBE_DEFAULTS.items():
        value = raw.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            cfg[key] = float(value) if isinstance(default, float) else int(value)
        except (TypeError, ValueError):
            pass
    # 并发上下限：0 或负数会让线程池直接抛异常，开太大又违反风控口径
    cfg["concurrency"] = max(1, min(16, int(cfg["concurrency"])))
    cfg["timeout"] = max(3.0, min(60.0, float(cfg["timeout"])))
    cfg["min_gap"] = max(0.0, float(cfg["min_gap"]))
    cfg["max_gap"] = max(cfg["min_gap"], float(cfg["max_gap"]))
    return cfg


def _guess_type(path: Path) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8",
        ".webmanifest": "application/manifest+json; charset=utf-8",
    }.get(path.suffix.lower(), "application/octet-stream")


#: 自动复检上一次跑的时间戳。只在进程内记：重启后最多多跑一次，
#: 比往库里塞一张「调度表」简单，也不会跟用户手工点击抢锁。
_auto_state = {"last_day": ""}
_auto_lock = threading.Lock()


def _auto_recheck_once() -> None:
    """到点就跑一轮。由 `_auto_recheck_loop` 每分钟调一次。"""
    cfg = recheck.load_config()
    if not cfg["enabled"] or not cfg["auto"]:
        return

    # 北京时间：服务器在 UTC 上跑时，本地时间 23 点对应北京早上 7 点，
    # 「每天早上 8 点复检」会提前 8 小时触发，还会把「今天」算错一天。
    now = clock.dt()
    today = now.strftime("%Y-%m-%d")
    if now.hour < cfg["auto_hour"]:
        return
    with _auto_lock:
        if _auto_state["last_day"] == today:
            return
        _auto_state["last_day"] = today  # 先占位：跑挂了也不该每分钟重试

    try:
        rows = store.list_accounts(track="banned", limit=cfg["batch_limit"])
        if not rows:
            return
        pairs = store.load_for_probe(ids=[r["id"] for r in rows])
        results = recheck.Runner(cfg).run(pairs)
        written = store.update_recheck(results)
        stats = recheck.summarize(results)
        print(f"[auto-recheck] {today} 复检 {written} 个，"
              f"解封 {stats.get('revived', 0)}，仍封 {stats.get('banned', 0)}")
    except Exception as exc:  # noqa: BLE001 — 定时任务不能把服务带崩
        print(f"[auto-recheck] 失败：{type(exc).__name__}: {exc}")


def _auto_recheck_loop() -> None:
    """守护线程：每分钟看一眼该不该跑。

    为什么不用 Timer / 计划任务：这个服务在容器里跑，没有 cron，也不能
    假定 systemd；一个 daemon 线程是唯一在所有部署形态下都成立的做法。
    """
    while True:
        try:
            _auto_recheck_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)


def start_auto_recheck() -> None:
    """起自动复检线程。重复调用安全（只起一次）。"""
    if getattr(start_auto_recheck, "_started", False):
        return
    start_auto_recheck._started = True
    t = threading.Thread(target=_auto_recheck_loop, name="wk-auto-recheck", daemon=True)
    t.start()


def serve(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    start_auto_recheck()
    return httpd


if __name__ == "__main__":
    import sys

    p = int(os.environ.get("WK_PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8765))
    srv = serve(port=p)
    print(f"wk-switch 已启动：http://127.0.0.1:{p}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
