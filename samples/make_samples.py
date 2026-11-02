"""生成七个项目的样例账户文件，用于手工验证与单测。

跑法：python samples/make_samples.py
产物直接写在 samples/ 下，可拖进界面逐个试。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def fake_jwt(uid: str, iss: str, exp: int) -> str:
    """造一个结构合法（签名是假的）的 JWT，只为让解析路径走通。

    签名段不校验——本工具只读 payload，不做验签。
    """
    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = seg({"alg": "RS256", "typ": "JWT"})
    payload = seg({"uid": uid, "iss": iss, "exp": exp, "iat": exp - 86400})
    return f"{header}.{payload}.c2lnbmF0dXJl"


# 以生成时刻为基准：样例里的「过期账号」必须真的过期，否则演示看不到那条状态
NOW = int(time.time())
CN_ISS = "https://copilot.tencent.com"
INTL_ISS = "https://www.workbuddy.ai"

ACCOUNTS = [
    {
        "uid": "u-cn-1001",
        "nickname": "阿岩",
        "domain": "copilot.tencent.com",
        "realm": "cn",
        "at": fake_jwt("u-cn-1001", CN_ISS, NOW + 86400 * 20),
        "rt": "rt-cn-1001-xxxxxxxxxxxxxxxx",
        "email": "ayan@example.cn",
        "exp": NOW + 86400 * 20,
    },
    {
        "uid": "u-intl-2002",
        "nickname": "Avery",
        "domain": "www.workbuddy.ai",
        "realm": "intl",
        "at": fake_jwt("u-intl-2002", INTL_ISS, NOW + 86400 * 6),
        "rt": "rt-intl-2002-yyyyyyyyyyyyyyyy",
        "email": "avery@example.com",
        "exp": NOW + 86400 * 6,
    },
    {
        "uid": "u-cn-1003",
        "nickname": "测试号·过期",
        "domain": "codebuddy.cn",
        "realm": "cn",
        "at": fake_jwt("u-cn-1003", CN_ISS, NOW - 86400 * 30),
        "rt": "",
        "email": "",
        "exp": NOW - 86400 * 30,
    },
    {
        "uid": "u-cn-1001",  # 与第一条同 uid：用来验证去重
        "nickname": "阿岩（旧凭据）",
        "domain": "copilot.tencent.com",
        "realm": "cn",
        "at": fake_jwt("u-cn-1001", CN_ISS, NOW + 86400 * 2),
        "rt": "rt-cn-1001-old",
        "email": "ayan@example.cn",
        "exp": NOW + 86400 * 2,
    },
    {
        "uid": "apikey-9f2c",
        "nickname": "静态 Key 号",
        "domain": "copilot.tencent.com",
        "realm": "cn",
        "at": "ck_9f2c.ZmFrZS1zZWNyZXQtdmFsdWU=",
        "rt": "",
        "email": "",
        "exp": 0,
    },
]


def hub_flat(acc: dict) -> dict:
    return {
        "uid": acc["uid"],
        "nickname": acc["nickname"],
        "domain": acc["domain"],
        "realm": acc["realm"],
        "platform": "desktop",
        "product": "workbuddy",
        "enterpriseId": "",
        "accessToken": acc["at"],
        "refreshToken": acc["rt"],
        "expiresAt": acc["exp"],
        "addedAt": NOW - 86400,
        "source": "sample",
        "apiKey": acc["at"].startswith(("ck_", "pt_")),
        "proxySlot": "",
        "proxy": "",
        "enabled": True,
        "tasksEnabled": True,
        "weight": 1,
        "note": "",
        "disabledModels": [],
        "credits": 1200,
        "tier": "free",
    }


def nested(acc: dict) -> dict:
    doc = {
        "account": {
            "uid": acc["uid"],
            "enterpriseId": "",
            "nickname": acc["nickname"],
            "email": acc["email"],
        },
        "auth": {
            "accessToken": acc["at"],
            "refreshToken": acc["rt"],
            "expiresAt": acc["exp"],
            "domain": acc["domain"],
            "realm": acc["realm"],
        },
    }
    if acc["uid"] == "u-cn-1001":
        doc["device_token"] = "dt-sample-0001"
    return doc


def switch_flat(acc: dict) -> dict:
    return {
        "id": acc["uid"],
        "uid": acc["uid"],
        "nickname": acc["nickname"],
        "email": acc["email"],
        "access_token": acc["at"],
        "refresh_token": acc["rt"],
        "token_type": "Bearer",
        "domain": acc["domain"],
        "expiresAt": acc["exp"],
        "refreshExpiresAt": acc["exp"] + 86400 * 30 if acc["rt"] else 0,
        "createdAt": NOW - 86400 * 3,
    }


def daily_row(acc: dict) -> dict:
    phone = "1380000" + acc["uid"][-4:].replace("-", "")
    return {
        "phone": phone,
        "access_token": acc["at"],
        "refresh_token": acc["rt"],
        "env_line": f"{phone}:{acc['at']}:{acc['rt']}",
        "login_at": "2026-11-01 09:12:33",
    }


def write_json(name: str, payload) -> None:
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print("wrote", name, os.path.getsize(path), "bytes")


def write_zip(name: str, items: list[tuple[str, dict]]) -> None:
    """写 ZIP。同名条目自动加序号——样例里刻意留了同 uid 的两条，
    但 ZIP 内重名会让解压方行为不确定，真实项目导出时也会去重。"""
    path = os.path.join(HERE, name)
    seen: dict[str, int] = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, payload in items:
            count = seen.get(filename, 0) + 1
            seen[filename] = count
            if count > 1:
                head, ext = os.path.splitext(filename)
                filename = f"{head}-{count}{ext}"
            zf.writestr(filename, json.dumps(payload, ensure_ascii=False, indent=2))
    print("wrote", name, os.path.getsize(path), "bytes")


def main() -> int:
    # 1. Hub 导出文档
    write_json("hub-export.json", {
        "format": "workbuddy-accounts",
        "version": 1,
        "exportedAt": "2026-11-01 10:00:00",
        "count": len(ACCOUNTS),
        "accounts": [hub_flat(a) for a in ACCOUNTS],
    })

    # 2. Hub 账户目录（每账户一个文件）
    write_zip("hub-accounts.zip", [
        (f"accounts/{a['uid']}.json", hub_flat(a)) for a in ACCOUNTS
    ])

    # 3. Manager 账号包
    write_zip("manager-accounts.zip", [
        (f"workbuddy-{a['uid']}.json", nested(a)) for a in ACCOUNTS
    ])

    # 4. Panel 凭据目录（与 manager 同构，文件名同前缀）
    write_zip("panel-auths.zip", [
        (f"workbuddy-{a['uid']}.json", nested(a)) for a in ACCOUNTS
    ])

    # 5. Switch 账号库
    write_json("switch-accounts.json", [switch_flat(a) for a in ACCOUNTS])

    # 6. Qoder / Trae Switch 账号库（多一个 id 语义，形态相同）
    write_json("qoder-accounts.json", [switch_flat(a) for a in ACCOUNTS])

    # 7. Daily 登录结果
    write_json("daily-login.json", [daily_row(a) for a in ACCOUNTS])

    # 8. Daily 环境变量行
    lines = [f"{daily_row(a)['phone']}:{a['at']}:{a['rt']}" for a in ACCOUNTS if a["rt"]]
    path = os.path.join(HERE, "daily-env.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("wrote daily-env.txt", os.path.getsize(path), "bytes")

    # 9. 加密信封样例（新版客户端 AtRestEncryption 的形态）
    write_json("envelope-account.json", {
        "account": {"uid": "u-env-3001", "nickname": "信封号"},
        "auth": {
            "accessToken": json.dumps({"$wbEncrypted": 1, "envelope": "eyJzdWl0ZSI6MX0="}),
            "refreshToken": "",
            "expiresAt": NOW + 86400 * 10,
            "domain": "www.workbuddy.ai",
            "realm": "intl",
        },
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
