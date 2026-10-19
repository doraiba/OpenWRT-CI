#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 VIKINGYFY
"""
把固件上传到 Gitea 的 Release（对应 GitHub 版里 gh release create/upload 那一段）。

为什么不用 gh CLI：gh 只认 GitHub API，Gitea 的 REST 路径是 /api/v1 且上传字段
不同（multipart 字段名是 attachment）。这里直接用标准库，不依赖任何 pip 包，
runner 上只要 python3 就能跑。

行为与原版 gh 逻辑一一对应：
  - Release 已存在（同组同日多机型追加）→ 复用，不重建
  - 同名资产 → 先删后传，等价于 gh 的 --clobber
  - 单文件串行上传 + 失败重试 5 次，规避服务端偶发断连拖垮整步
  - 最后用本次的 body 覆盖 Release 说明（与原版行为一致）

环境变量：GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_TOKEN
用法：
  python3 Scripts/gitea_release.py --tag 自用-26.10.19 \
      --body-file ./release_body.md --assets-dir ./wrt/upload
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def die(msg: str) -> "None":
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def request(url: str, token: str, method: str = "GET",
            data: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        return 0, str(exc).encode()


def multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----gitea-release-%d" % int(time.time() * 1000)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(),
        content,
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    return body, f"multipart/form-data; boundary={boundary}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--assets-dir", required=True)
    args = parser.parse_args()

    server = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not server or not repo or not token:
        die("缺少 GITHUB_SERVER_URL / GITHUB_REPOSITORY / GITHUB_TOKEN 环境变量")

    api = f"{server}/api/v1/repos/{repo}"
    tag = args.tag
    body = open(args.body_file, encoding="utf-8").read()

    # 1) 取或建 Release
    code, raw = request(f"{api}/releases/tags/{urllib.parse.quote(tag, safe='')}", token)
    if code == 200:
        rel = json.loads(raw)
        release_id = rel["id"]
        print(f"Release 已存在（{tag}, id={release_id}），追加产物")
    elif code == 404:
        payload = json.dumps({
            "tag_name": tag,
            "name": tag,
            "body": body,
            "draft": False,
            "prerelease": False,
        }).encode()
        code, raw = request(f"{api}/releases", token, "POST", payload,
                            {"Content-Type": "application/json"})
        if code not in (201, 200):
            die(f"创建 Release 失败 HTTP {code}: {raw[:400].decode('utf-8', 'replace')}")
        release_id = json.loads(raw)["id"]
        print(f"已创建 Release（{tag}, id={release_id}）")
    else:
        die(f"查询 Release 失败 HTTP {code}: {raw[:400].decode('utf-8', 'replace')}")

    # 2) 清掉同名旧资产（等价 gh upload --clobber）
    existing: dict[str, int] = {}
    code, raw = request(f"{api}/releases/{release_id}/assets", token)
    if code == 200:
        for asset in json.loads(raw):
            existing[asset["name"]] = asset["id"]

    # 3) 单文件串行上传 + 重试
    files = sorted(
        os.path.join(args.assets_dir, name)
        for name in os.listdir(args.assets_dir)
        if os.path.isfile(os.path.join(args.assets_dir, name))
    )
    if not files:
        die(f"{args.assets_dir} 下没有任何文件可上传")

    failed = 0
    for path in files:
        name = os.path.basename(path)
        size = os.path.getsize(path)

        if name in existing:
            code, raw = request(f"{api}/releases/{release_id}/assets/{existing[name]}",
                                token, "DELETE")
            if code not in (204, 200):
                print(f"::warning::删除旧资产 {name} 失败 HTTP {code}", file=sys.stderr)
            else:
                print(f"已删除同名旧资产 {name}")

        data, ctype = multipart("attachment", name, open(path, "rb").read())
        url = f"{api}/releases/{release_id}/assets?name={urllib.parse.quote(name)}"

        for attempt in range(1, 6):
            code, raw = request(url, token, "POST", data, {"Content-Type": ctype})
            if code in (201, 200):
                print(f"已上传 {name}（{size / 1048576:.1f} MB）")
                break
            print(f"上传 {name} 失败（第 {attempt} 次，HTTP {code}），5s 后重试")
            time.sleep(5)
        else:
            print(f"::error::上传失败（已重试 5 次）：{name}", file=sys.stderr)
            failed += 1

    # 4) 说明以最后完成的机型为准（与原版 gh release edit 行为一致）
    payload = json.dumps({"body": body}).encode()
    code, raw = request(f"{api}/releases/{release_id}", token, "PATCH", payload,
                        {"Content-Type": "application/json"})
    if code not in (200, 201):
        print(f"::warning::更新 Release 说明失败 HTTP {code}", file=sys.stderr)

    if failed:
        die(f"{failed} 个文件上传失败")
    print(f"发布完成：{server}/{repo}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
