#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AltStore 源聚合与 GitHub 加速处理脚本

功能：
1. 读取 sources.json 中配置的源列表
2. 拉取每个源的 JSON（AltStore 源格式）
3. 合并所有源的 apps 数组
4. 将其中所有 GitHub 网址统一添加代理前缀（默认 https://github.chenc.dev/）
5. 输出合并后的 all-apps.json

用法：
    python3 scripts/process.py [--config sources.json] [--output all-apps.json]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

GITHUB_HOSTS = (
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "codeload.github.com",
    "gist.githubusercontent.com",
    "api.github.com",
)


def log(msg):
    print(f"[process] {msg}", file=sys.stderr)


def fetch_json(url, timeout=60):
    """拉取远程 JSON，返回解析后的对象。

    优先使用 curl（对国内网络更稳定），失败时回退到 urllib。
    """
    # 方式一：curl
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sS",
                "-L",
                "-m",
                str(timeout),
                "-H",
                "User-Agent: AltSource-Aggregator/1.0",
                "-H",
                "Accept: application/json",
                url,
            ],
            capture_output=True,
            timeout=timeout + 10,
        )
        if proc.returncode == 0 and proc.stdout:
            return json.loads(proc.stdout.decode("utf-8"))
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        pass

    # 方式二：urllib 回退
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "AltSource-Aggregator/1.0 (+https://github.com)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def fetch_json_with_cache(url, cache_path, timeout=60):
    """拉取远程 JSON，成功后写入本地缓存；失败时回退到缓存。

    用于自动更新场景下增强健壮性，避免网络抖动导致产出空源。
    返回 (data, from_cache)。
    """
    try:
        data = fetch_json(url, timeout=timeout)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data, False
    except Exception as e:
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                log(f"  拉取失败({e})，使用缓存 {cache_path}")
                return cached, True
            except (OSError, json.JSONDecodeError):
                pass
        raise e


def should_proxy(url, proxy):
    """判断一个 URL 是否是需要代理的 GitHub 地址（且尚未加前缀）。"""
    if not isinstance(url, str) or not url:
        return False
    if url.startswith(proxy):
        return False
    low = url.lower()
    return any(host in low for host in GITHUB_HOSTS)


def proxy_url(url, proxy):
    """给单个 URL 加代理前缀。"""
    return proxy + url


def proxy_value(value, proxy):
    """递归处理任意 JSON 结构中的字符串，给 GitHub 网址加前缀。"""
    if isinstance(value, str):
        return proxy_url(value, proxy) if should_proxy(value, proxy) else value
    if isinstance(value, list):
        return [proxy_value(v, proxy) for v in value]
    if isinstance(value, dict):
        return {k: proxy_value(v, proxy) for k, v in value.items()}
    return value


def fetch_github_release(owner, repo):
    """通过 GitHub Releases API 获取最新 release 信息。

    返回 dict（含 tag_name、published_at、assets），失败抛异常。
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    return fetch_json(url)


def fetch_github_release_with_asset(owner, repo, asset_rule):
    """获取最新「包含匹配资产」的 release。

    当仓库 latest release 不含目标资产（例如最新 tag 只发了 APK、
    而 IPA 在更早的 release 里）时，遍历 release 列表找到最新一条
    含匹配资产的 release。返回 (release, asset)，找不到则抛异常。
    """
    # 先试 latest
    latest = fetch_github_release(owner, repo)
    asset = match_asset(latest.get("assets", []), asset_rule)
    if asset:
        return latest, asset

    # 回退：遍历 release 列表（按发布时间倒序）
    list_url = f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=100"
    releases = fetch_json(list_url)
    if not isinstance(releases, list):
        # GitHub API 限流等情况下会返回 {"message": ...}，抛异常交给上层回退
        raise ValueError(
            f"Releases 列表接口返回异常（可能被限流）: {releases}"
        )
    for rel in releases:
        asset = match_asset(rel.get("assets", []), asset_rule)
        if asset:
            return rel, asset

    raise ValueError(
        f"未在 {owner}/{repo} 的任何 release 中找到匹配资产（规则: {asset_rule}）"
    )


def match_asset(assets, asset_rule):
    """在 release 资产列表中匹配 IPA 文件。

    asset_rule 可以是：
      - 精确文件名，如 "bili-release-unsigned.ipa"
      - 正则表达式字符串，如 ".*\\.ipa$" 或 ".*release.*\\.ipa"
    返回匹配的 asset dict，找不到返回 None。
    """
    if not assets:
        return None

    # 先尝试精确匹配
    for a in assets:
        if a.get("name") == asset_rule:
            return a

    # 再尝试正则匹配
    try:
        pat = re.compile(asset_rule)
    except re.error:
        return None
    for a in assets:
        if pat.search(a.get("name", "")):
            return a
    return None


def build_versions_from_github(github_cfg):
    """根据 manual.github 配置探测最新 release，生成 versions 列表。

    返回 (versions, 日志信息)，失败抛异常由上层捕获。

    github_cfg 可选字段：
      - owner / repo：必填
      - asset：资产匹配规则（精确名或正则），默认匹配 .ipa 结尾
      - search_all_releases：为 true 时，若 latest 不含匹配资产，
        则遍历全部 release 找最新含匹配资产的 release（用于 IPA 与
        APK 混发、latest 是 APK 的仓库）。
    """
    owner = github_cfg["owner"]
    repo = github_cfg["repo"]
    asset_rule = github_cfg.get("asset", r".*\.ipa$")

    if github_cfg.get("search_all_releases"):
        release, asset = fetch_github_release_with_asset(owner, repo, asset_rule)
    else:
        release = fetch_github_release(owner, repo)
        asset = match_asset(release.get("assets", []), asset_rule)
        if not asset:
            raise ValueError(
                f"GitHub 最新 release ({release.get('tag_name')}) 中未找到匹配资产（规则: {asset_rule}）"
            )

    tag = release.get("tag_name", "")
    # 去掉可能的 "v" 前缀作为版本号
    version = tag[1:] if tag.startswith("v") else tag
    if not version:
        version = release.get("name", "unknown")

    versions = [
        {
            "version": version,
            "date": release.get("published_at")
            or release.get("created_at")
            or asset.get("updated_at"),
            "downloadURL": asset["browser_download_url"],
            "size": asset.get("size"),
        }
    ]
    return versions, f"{tag} ({asset['name']}, {asset.get('size')} bytes)"


def load_manual(entry):
    """加载手动配置的应用（用于没有现成 AltStore 源的仓库，如 cilicili）。

    若 manual 内含 github 配置，则自动探测最新 release 并动态生成 versions；
    否则使用 manual.versions 中的静态配置。
    """
    manual = entry.get("manual")
    if not manual:
        return []
    app = dict(manual)
    github_cfg = app.pop("github", None)

    if github_cfg:
        try:
            versions, info = build_versions_from_github(github_cfg)
            app["versions"] = versions
            log(f"  自动探测 [{app.get('name')}] 最新版: {info}")
        except Exception as e:
            log(f"  自动探测 [{app.get('name')}] 失败，回退到静态 versions: {e}")
            # versions 字段已由 manual 提供，保留静态兜底
    return [app]


def main():
    parser = argparse.ArgumentParser(description="聚合 AltStore 源并添加 GitHub 加速前缀")
    parser.add_argument("--config", default="sources.json")
    parser.add_argument("--output", default="all-apps.json")
    parser.add_argument("--no-cache", action="store_true", help="禁用源数据缓存")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, args.config)
    output_path = os.path.join(base_dir, args.output)
    cache_dir = os.path.join(base_dir, ".cache")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    proxy = config.get("proxy", "https://git.tangbai.cc/")
    if not proxy.endswith("/"):
        proxy += "/"

    merged_apps = []
    source_names = []
    errors = []

    for entry in config.get("sources", []):
        if not entry.get("enabled", True):
            continue

        name = entry.get("name", entry.get("id", "unknown"))
        url = entry.get("url")

        try:
            if url:
                sid = entry.get("id", name)
                cache_path = os.path.join(cache_dir, f"{sid}.json")
                if args.no_cache:
                    data = fetch_json(url)
                else:
                    data, from_cache = fetch_json_with_cache(url, cache_path)
                apps = data.get("apps", [])
                log(f"拉取源 [{name}] 成功，含 {len(apps)} 个应用")
                merged_apps.extend(apps)
            elif entry.get("manual"):
                apps = load_manual(entry)
                log(f"使用手动配置 [{name}]，含 {len(apps)} 个应用")
                merged_apps.extend(apps)
            else:
                raise ValueError("源既无 url 也无 manual 配置")
            source_names.append(name)
        except Exception as e:
            msg = f"源 [{name}] 处理失败: {e}"
            log(msg)
            errors.append(msg)

    # 去重（按 bundleIdentifier，后出现的保留）
    seen = {}
    for app in merged_apps:
        bid = app.get("bundleIdentifier")
        if bid:
            seen[bid] = app
        else:
            seen[f"__anon_{len(seen)}"] = app
    deduped_apps = list(seen.values())

    # 给所有 GitHub 网址加代理前缀
    proxied_apps = proxy_value(deduped_apps, proxy)

    # 构建输出（保留 AltGallery 源本身的元信息）
    output = {
        "name": config.get("output_name", "AltGallery 国内加速版"),
        "subtitle": config.get(
            "output_subtitle",
            "AltGallery 源聚合，GitHub 链接已加国内加速前缀",
        ),
        "description": "聚合多个 AltStore 源并统一添加 GitHub 加速前缀，方便国内访问。",
        "website": proxy + "https://github.com/bebound/AltGallery",
        "iconURL": proxy
        + "https://raw.githubusercontent.com/bebound/AltGallery/refs/heads/master/assets/icon.png",
        "tintColor": "#3B82F6",
        "apps": proxied_apps,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log(f"已输出 {output_path}，共 {len(proxied_apps)} 个应用")
    if errors:
        log(f"警告：{len(errors)} 个源处理失败")
        for e in errors:
            log("  - " + e)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
