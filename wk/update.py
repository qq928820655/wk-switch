"""在线升级：查最新制品 → 下载 → 校验 → 备份 → 覆盖代码 → 提示重启。

制品源支持两种：
1. GitHub Releases（默认）：repo 从 config/update.json 或 .git/config 里取；
2. 任意制品库索引：index_url 返回 {"version": "...", "url": "包地址", "notes": "..."}。

只覆盖代码（main.py / server.py / wk / web / deploy / 文档），
data/ 与 config/ 一律不碰——升级把账号库或口令覆盖掉，比不升级糟糕得多。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from . import version

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "update.json")
BACKUP_DIR = os.path.join(ROOT, "backups")

#: 允许被升级覆盖的顶层条目。没列进来的一律不动（data/ config/ .git/）。
#: deploy/ 故意不在里面：那是安装器，装完之后用户可能改过端口、路径、
#: 服务名，被升级覆盖会直接把人家的部署配置冲掉；要换安装器就重跑一次
#: 部署脚本。docs/ 同理，属于说明文档，和运行无关。
REPLACEABLE = (
    "main.py", "server.py", "wk", "web",
    "README.md", "requirements.txt", "VERSION", "BUILD", "LICENSE",
    "start.sh", "stop.sh", "start.bat", "stop.bat",
)

DEFAULT_CONFIG = {
    "repo": "",
    "api": "https://api.github.com",
    "asset": r"^wk-switch-[0-9.]+zip$",
    "index_url": "",
    "proxy": "",
    "timeout": 30,
    "allow_prerelease": False,
}


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _repo_from_git() -> str:
    """从 .git/config 里读 origin 地址，避免配置里再写一遍仓库名。"""
    # .git/config 是 ini 不是 json，直接扫文本更省事
    path = os.path.join(ROOT, ".git", "config")
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    match = re.search(r"url\s*=\s*(\S+)", text)
    if not match:
        return ""
    url = match.group(1).strip()
    url = re.sub(r"^git@github\.com:", "https://github.com/", url)
    url = re.sub(r"\.git$", "", url)
    match = re.search(r"github\.com[/:]([^/]+/[^/]+)$", url)
    return match.group(1) if match else ""


def config() -> dict:
    data = dict(DEFAULT_CONFIG)
    data.update(_read_json(CONFIG_PATH))
    if not data.get("repo"):
        data["repo"] = _repo_from_git()
    if not data.get("proxy"):
        # 配置没写就吃环境变量，服务从带代理的 shell 起来时不用再配一遍
        data["proxy"] = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                         or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
    return data


def save_config(patch: dict) -> dict:
    """只接受已知字段，避免页面把任意键写进配置。"""
    data = config()
    for key in DEFAULT_CONFIG:
        if key in patch:
            data[key] = patch[key]
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)
    return data


def _opener(cfg: dict | None = None):
    """带代理的 opener。

    代理优先级：config/update.json 的 proxy > 环境变量 HTTPS_PROXY/HTTP_PROXY。
    国内直连 github.com 下载 release 包基本会超时，而 api.github.com 常常
    能通——所以「检查更新成功、下载卡死」是常态，必须让代理可配。
    """
    proxy = str((cfg or {}).get("proxy") or "").strip()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _request(url: str, timeout: int = 30, accept: str = "application/vnd.github+json",
             cfg: dict | None = None):
    req = urllib.request.Request(url, headers={
        "User-Agent": "wk-switch/" + version.version(),
        "Accept": accept,
    })
    return _opener(cfg).open(req, timeout=timeout)


def _github_latest(cfg: dict) -> dict:
    repo = str(cfg.get("repo") or "").strip()
    if not repo:
        raise ValueError("还没有配置制品仓库（config/update.json 的 repo，或 .git 的 origin）")
    url = f"{cfg['api'].rstrip('/')}/repos/{repo}/releases"
    try:
        with _request(url, int(cfg.get("timeout") or 30), cfg=cfg) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"制品库返回 {exc.code}（{repo} 可能还没有发布版本）") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"连不上制品库：{exc.reason}") from exc

    if not isinstance(releases, list) or not releases:
        raise ValueError(f"{repo} 还没有任何 release")

    allow_pre = bool(cfg.get("allow_prerelease"))
    pattern = re.compile(str(cfg.get("asset") or DEFAULT_CONFIG["asset"]))
    for rel in releases:
        if rel.get("draft"):
            continue
        if rel.get("prerelease") and not allow_pre:
            continue
        assets = [a for a in rel.get("assets") or [] if pattern.search(a.get("name") or "")]
        if not assets:
            continue
        ver = str(rel.get("tag_name") or rel.get("name") or "").lstrip("vV")
        exact = f"wk-switch-{ver}.zip"
        asset = next((a for a in assets if a.get("name") == exact), assets[0])
        return {
            "version": str(rel.get("tag_name") or rel.get("name") or "").lstrip("vV"),
            "url": asset.get("browser_download_url"),
            "asset": asset.get("name"),
            "size": asset.get("size"),
            "notes": (rel.get("body") or "").strip()[:2000],
            "published_at": rel.get("published_at") or "",
            "page": rel.get("html_url") or "",
            "source": f"github:{repo}",
        }
    raise ValueError("制品库里没有匹配的发布包（检查 asset 正则）")


def _index_latest(cfg: dict) -> dict:
    url = str(cfg.get("index_url") or "").strip()
    if not url:
        raise ValueError("index_url 为空")
    try:
        with _request(url, int(cfg.get("timeout") or 30), accept="application/json", cfg=cfg) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ValueError(f"连不上制品索引：{exc.reason}") from exc
    if not isinstance(data, dict) or not data.get("version") or not data.get("url"):
        raise ValueError("制品索引格式不对，需要 {version, url}")
    return {
        "version": str(data["version"]).lstrip("vV"),
        "url": data["url"],
        "asset": data.get("asset") or os.path.basename(str(data["url"])),
        "size": data.get("size") or 0,
        "notes": str(data.get("notes") or "")[:2000],
        "published_at": data.get("published_at") or "",
        "page": data.get("page") or "",
        "source": "index:" + url,
    }


def check() -> dict:
    cfg = config()
    latest = _index_latest(cfg) if cfg.get("index_url") else _github_latest(cfg)
    newer = version.is_newer(latest["version"], version.version())
    return {
        "current": version.version(),
        "latest": latest["version"],
        "newer": newer,
        "up_to_date": not newer,
        "release": latest,
        "repo": cfg.get("repo") or "",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _safe_members(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """返回 [(zip 内路径, 落地相对路径)]，只保留白名单内的条目。

    去掉包顶层目录（wk-switch-1.0.0/xxx → xxx），并把绝对路径、..
    跳过去——升级包是网络来的，不能直接信。
    """
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if not names:
        raise ValueError("升级包是空的")

    tops = {n.split("/")[0] for n in names}
    strip = ""
    if len(tops) == 1:
        only = next(iter(tops))
        if only not in REPLACEABLE and any(n.startswith(only + "/") for n in names):
            strip = only + "/"

    picked = []
    for name in names:
        rel = name[len(strip):] if strip and name.startswith(strip) else name
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        top = rel.split("/")[0]
        if top not in REPLACEABLE:
            continue
        picked.append((name, rel))
    if not any(rel == "server.py" or rel.startswith("wk/") for _, rel in picked):
        raise ValueError("升级包里没有 server.py / wk/，不像本程序的包")
    return picked


def _download(url: str, cfg: dict, attempts: int = 3, say=None) -> bytes:
    """下载制品，失败重试。

    release 资产挂在 github.com（会 302 到 objects.githubusercontent.com），
    国内链路上这条域名比 api.github.com 更容易被掐断，单次失败很常见，
    重试两轮基本能过；仍失败再让用户配代理或用本地包。
    """
    last = None
    for i in range(attempts):
        try:
            with _request(url, 120, accept="*/*", cfg=cfg) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = getattr(exc, "reason", exc)
            if i + 1 < attempts:
                wait = 2 * (i + 1)
                if say:
                    say(f"下载失败（{last}），{wait}s 后重试 {i + 2}/{attempts}")
                time.sleep(wait)
    raise ValueError(
        f"下载失败：{last}。制品在 github.com 上，国内直连常被掐断；"
        f"可在页面「下载代理」里填本地代理，或用下方「本地包升级」直接上传 zip"
    )


def _cleanup_stale_tmp() -> None:
    """清掉上次升级中途留下的 .wk-upgrade-* 目录。

    临时目录现在放在 ROOT 下（同盘才能用 os.replace），进程若在替换
    途中被杀，残留目录会一直躺在项目根目录里。
    """
    try:
        for name in os.listdir(ROOT):
            if name.startswith(".wk-upgrade-"):
                shutil.rmtree(os.path.join(ROOT, name), ignore_errors=True)
    except OSError:
        pass


def _install_zip(blob: bytes, target_version: str, say) -> dict:
    """把 zip 里的白名单文件覆盖到当前目录。调用方负责重启。"""
    if not blob.startswith(b"PK"):
        raise ValueError("不是 zip 包（文件头不对）")

    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"升级包损坏：{exc}") from exc

    members = _safe_members(zf)

    # 先备份当前代码：升级失败要能原样退回去
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    old_version = version.version()
    backup = os.path.join(BACKUP_DIR, f"code-{old_version}-{stamp}.zip")
    with zipfile.ZipFile(backup, "w", compression=zipfile.ZIP_DEFLATED) as bz:
        for top in ("main.py", "server.py", "wk", "web", "deploy"):
            path = os.path.join(ROOT, top)
            if os.path.isfile(path):
                bz.write(path, top)
            elif os.path.isdir(path):
                for base, _dirs, files in os.walk(path):
                    for name in files:
                        full = os.path.join(base, name)
                        bz.write(full, os.path.relpath(full, ROOT))
    say(f"已备份到 {os.path.basename(backup)}")

    # 落盘：先解到临时目录再逐个替换，避免解压中途失败留下半个程序。
    # 临时目录必须放在 ROOT 下：跨盘符（C: → E:）时 os.replace 会报
    # WinError 17「无法将文件移到不同的磁盘驱动器」，而 os.replace 是
    # 唯一能保证「要么旧文件、要么新文件」的原子操作，不能放弃。
    _cleanup_stale_tmp()
    tmpdir = tempfile.mkdtemp(prefix=".wk-upgrade-", dir=ROOT)
    try:
        for arc, rel in members:
            target = os.path.join(tmpdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(target) or tmpdir, exist_ok=True)
            with zf.open(arc) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

        for arc, rel in members:
            src = os.path.join(tmpdir, rel.replace("/", os.sep))
            dst = os.path.join(ROOT, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst) or ROOT, exist_ok=True)
            if os.path.exists(dst):
                try:
                    os.replace(src, dst)
                except OSError:
                    # 极少数情况（目标被占用/权限）退回覆盖写，仍保证内容完整
                    shutil.copyfile(src, dst)
                    os.remove(src)
            else:
                shutil.move(src, dst)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 记录构建时间与版本号文件（页面读 BUILD / VERSION 显示）
    with open(os.path.join(ROOT, "BUILD"), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    with open(os.path.join(ROOT, "VERSION"), "w", encoding="utf-8") as f:
        f.write(target_version + "\n")

    downgraded = version.parse_semver(target_version) < version.parse_semver(old_version)
    say("代码已替换，重启后生效")
    return {
        "ok": True,
        "upgraded": True,
        "downgraded": downgraded,
        "from": old_version,
        "to": target_version,
        "files": len(members),
        "backup": os.path.basename(backup),
        "restart_required": True,
        "message": (f"已回滚到 {target_version}，重启服务后生效" if downgraded
                    else f"已升级到 {target_version}，重启服务后生效"),
    }


def apply(progress=None) -> dict:
    """从制品库下载最新包并覆盖代码。调用方负责重启。"""
    def say(text: str) -> None:
        if progress:
            progress(text)

    info = check()
    if not info["newer"]:
        return {"ok": True, "upgraded": False, "current": version.version(),
                "latest": info["latest"], "message": "已经是最新版本"}

    rel = info["release"]
    say(f"下载 {rel['asset'] or rel['url']}")
    blob = _download(rel["url"], config(), say=say)
    return _install_zip(blob, rel["version"], say)


def apply_local(blob: bytes, filename: str = "", progress=None) -> dict:
    """用上传的 zip 直接升级，不走网络。

    版本号优先取包内 VERSION 文件，没有就看文件名里的 x.y.z，
    都取不到就沿用当前版本——宁可版本号不动，也不要写个假版本进去。
    包版本低于当前版本时按「回滚」处理并在结果里标出来。
    """
    def say(text: str) -> None:
        if progress:
            progress(text)

    target = ""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                if name == "VERSION" or name.endswith("/VERSION"):
                    target = zf.read(name).decode("utf-8", "replace").strip()
                    break
    except zipfile.BadZipFile as exc:
        raise ValueError(f"升级包损坏：{exc}") from exc

    if not target:
        match = re.search(r"(\d+\.\d+\.\d+)", filename or "")
        target = match.group(1) if match else version.version()

    say(f"使用本地包 {filename or '(未命名)'}，目标版本 {target}")
    result = _install_zip(blob, target, say)
    result["source"] = "local:" + (filename or "")
    return result


def restart(port: int) -> dict:
    """拉起一个新进程再退出当前进程。

    新进程带 --wait-port，会等端口释放后自己绑上；这里直接退出，
    否则旧进程占着端口，新进程只能失败退出。
    """
    python = sys.executable
    args = [python, os.path.join(ROOT, "main.py"), str(port), "--no-browser", "--wait-port", "25"]
    kwargs = {
        "cwd": ROOT,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)
    return {"ok": True, "message": "新进程已拉起，正在重启", "port": port}
