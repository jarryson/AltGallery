#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AltStore 源聚合与 GitHub 加速处理脚本

功能：
1. 读取 sources.json 中配置的源列表
2. 拉取每个源的 JSON（AltStore 源格式）
3. 合并所有源的 apps 数组
4. 将其中所有 GitHub 网址统一添加代理前缀（前缀来自 sources.json 的 proxy 字段）
5. 输出合并后的 all-apps.json

用法：
    python3 scripts/process.py [--config sources.json] [--output all-apps.json]
    python3 scripts/process.py --watch [--interval 3]   # 监听模式

监听模式（--watch）：
    - 监听 all-apps.json 是否被删除 / 不存在，若是则自动重新生成。
    - 监听 sources.json 中 proxy 地址是否变更，变更后立即用最新 proxy 重新生成。
    - 生成时覆盖旧文件，格式与生成规则一致。
    - 生成失败时打印明确错误信息。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
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


# 匹配文本中内嵌的完整 URL（避免匹配到标点、引号、尖括号等）
_EMBEDDED_URL_RE = re.compile(r"https?://[^\s\"'<>()\[\]{}]+")


def should_proxy(url, proxy):
    """判断一个字符串是否为需要代理的 GitHub URL。

    仅当整个字符串本身就是一个 http(s) URL 且指向 GitHub 时返回 True；
    普通文本（如版本更新说明）即使内含 GitHub 链接也不整体匹配，
    由 proxy_value 用正则单独提取内嵌 URL 处理。
    """
    if not isinstance(url, str) or not url:
        return False
    stripped = url.strip()
    low = stripped.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return False
    if stripped.startswith(proxy):
        return False
    return any(host in low for host in GITHUB_HOSTS)


def proxy_url(url, proxy):
    """给单个 URL 加代理前缀。"""
    return proxy + url


def proxy_value(value, proxy):
    """递归处理任意 JSON 结构中的字符串，给 GitHub 网址加前缀。

    - 纯 URL 字段（整体即 URL）→ 直接加前缀；
    - 长文本字段（如 localizedDescription / 版本更新说明）→ 用正则
      仅替换其中内嵌的 GitHub URL，避免把前缀错误拼到整个文本开头。
    """
    if isinstance(value, str):
        if should_proxy(value, proxy):
            return proxy_url(value, proxy)

        def _replace_embedded(match):
            u = match.group(0)
            return proxy_url(u, proxy) if should_proxy(u, proxy) else u

        return _EMBEDDED_URL_RE.sub(_replace_embedded, value)
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
    # 确保 app 始终有 versions 字段（即使 manual 未提供静态版本）
    app.setdefault("versions", [])
    return [app]


def is_altgallery_source(entry):
    """判断某个源是否为 altgallery 官方源（具有优先保留权）。

    判定标准：源的 id 为 "altgallery"，或 url 指向 bebound/AltGallery 仓库。
    """
    eid = str(entry.get("id", "")).lower()
    if eid == "altgallery":
        return True
    url = entry.get("url", "") or ""
    return "bebound/AltGallery" in url


def dedupe_apps(apps, preserve_bundle_ids):
    """对一组 apps 去重，同时剔除与指定 bundleIdentifier 集合重复的项。

    - preserve_bundle_ids：需保留的 bundleIdentifier 集合（来自更高优先级的源，
      例如 altgallery）。任何 bundleIdentifier 落入该集合的 app 会被丢弃，
      即「删除用户自定义添加的重复项」。
    - 返回 (去重后的 apps, 被剔除的重复项列表)。

    同组内的去重规则：按 bundleIdentifier，后出现的保留（与原逻辑一致），
    被替换掉的旧项也计入 removed。
    """
    result = []
    removed = []
    index_by_bid = {}
    for app in apps:
        bid = app.get("bundleIdentifier")
        if bid and bid in preserve_bundle_ids:
            # 与更高优先级源重复，剔除
            removed.append(app)
            continue
        if bid:
            if bid in index_by_bid:
                # 同组内重复：后出现保留，旧项移出 result 并计入 removed
                old_idx = index_by_bid[bid]
                removed.append(result[old_idx])
                result[old_idx] = app
                # index_by_bid[bid] 保持不变（仍指向该位置）
            else:
                index_by_bid[bid] = len(result)
                result.append(app)
        else:
            result.append(app)
    return result, removed


def generate(config_path, output_path, no_cache=False):
    """执行一次完整的聚合生成，返回退出码（0=成功，1=部分失败，2=完全失败）。

    将核心逻辑从 main 中抽出，供普通模式和 watch 模式复用。
    """
    cache_dir = os.path.join(os.path.dirname(config_path), ".cache")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # proxy 是唯一配置来源，缺失即为配置错误，不做任何硬编码兜底。
    proxy = config.get("proxy")
    if not proxy:
        log("错误：sources.json 缺少 proxy 字段，请在该文件中配置代理前缀后重试。")
        return 2
    if not isinstance(proxy, str):
        log(f"错误：proxy 字段类型错误（期望字符串，得到 {type(proxy).__name__}）。")
        return 2
    proxy = proxy.strip()
    if not proxy:
        log("错误：sources.json 的 proxy 字段为空字符串。")
        return 2
    if not proxy.endswith("/"):
        proxy += "/"

    merged_apps = []
    source_names = []
    errors = []
    enabled_sources = [
        e for e in config.get("sources", []) if e.get("enabled", True)
    ]

    # 分层去重：altgallery 源具有优先保留权，其收录的 bundleIdentifier
    # 会作为「保留集合」，后续处理其他源时，凡落入该集合的自定义项都会被剔除，
    # 避免自定义源与官方源重复导致误删 / 重复删除。
    altgallery_bids = set()
    altgallery_apps = []
    other_apps = []

    for entry in enabled_sources:
        name = entry.get("name", entry.get("id", "unknown"))
        url = entry.get("url")

        try:
            if url:
                sid = entry.get("id", name)
                cache_path = os.path.join(cache_dir, f"{sid}.json")
                if no_cache:
                    data = fetch_json(url)
                else:
                    data, from_cache = fetch_json_with_cache(url, cache_path)
                apps = data.get("apps", [])
                log(f"拉取源 [{name}] 成功，含 {len(apps)} 个应用")
                if is_altgallery_source(entry):
                    altgallery_apps.extend(apps)
                else:
                    other_apps.extend(apps)
            elif entry.get("manual"):
                apps = load_manual(entry)
                log(f"使用手动配置 [{name}]，含 {len(apps)} 个应用")
                other_apps.extend(apps)
            else:
                raise ValueError("源既无 url 也无 manual 配置")
            source_names.append(name)
        except Exception as e:
            msg = f"源 [{name}] 处理失败: {e}"
            log(msg)
            errors.append(msg)

    # 若所有源都失败（无任何可用应用），则不产出文件，直接报错退出。
    if not altgallery_apps and not other_apps:
        log("错误：所有源均处理失败，没有任何可用应用，未生成输出文件。")
        log(f"已启用源数量: {len(enabled_sources)}，失败数量: {len(errors)}")
        for e in errors:
            log("  - " + e)
        return 2

    # 1) 先对 altgallery 源自身去重（后出现保留），并收集其 bundleIdentifier 集合。
    altgallery_apps, _ = dedupe_apps(altgallery_apps, set())
    altgallery_bids = {
        a.get("bundleIdentifier")
        for a in altgallery_apps
        if a.get("bundleIdentifier")
    }

    # 2) 再处理其他（自定义）源：剔除与 altgallery 重复的项，并做同组去重。
    other_apps, removed = dedupe_apps(other_apps, altgallery_bids)
    for app in removed:
        log(
            f"去重：已删除自定义重复项 [{app.get('name')}]"
            f"（bundleIdentifier {app.get('bundleIdentifier')} 已被 altgallery 源收录）"
        )

    # 合并：altgallery 在前，其他源在后。
    merged_apps = altgallery_apps + other_apps

    # 给所有 GitHub 网址加代理前缀
    proxied_apps = proxy_value(merged_apps, proxy)

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
        log(f"警告：{len(errors)} 个源处理失败（已用剩余成功源产出文件）")
        for e in errors:
            log("  - " + e)
        log(f"成功源: {len(source_names)}/{len(enabled_sources)}，失败源: {len(errors)}")
        return 1
    return 0


def read_proxy(config_path):
    """读取 sources.json 中的 proxy 字段，返回 (proxy, mtime)。

    若文件不存在或解析失败，返回 (None, None)。
    """
    try:
        st = os.stat(config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("proxy"), st.st_mtime
    except (OSError, json.JSONDecodeError) as e:
        log(f"读取配置失败: {e}")
        return None, None


def watch(config_path, output_path, interval):
    """监听模式：当 all-apps.json 被删除/不存在，或 proxy 变更时自动重新生成。"""
    log(f"开始监听（间隔 {interval}s）")
    log(f"  配置文件: {config_path}")
    log(f"  输出文件: {output_path}")

    last_proxy, _ = read_proxy(config_path)
    log(f"  当前 proxy: {last_proxy}")

    def regenerate(reason):
        log(f"触发重新生成：{reason}")
        code = generate(config_path, output_path)
        if code == 0:
            log(f"生成成功：{output_path}")
        elif code == 1:
            log(f"警告：生成完成但有部分源失败（退出码 1），文件已产出：{output_path}")
        else:
            log(f"错误：生成失败（退出码 {code}）")
        return code in (0, 1)

    # 首次启动：若输出文件不存在，立即生成
    if not os.path.exists(output_path):
        regenerate("all-apps.json 不存在，启动时立即生成")
        last_proxy, _ = read_proxy(config_path)

    try:
        while True:
            time.sleep(interval)

            # 条件一：all-apps.json 被删除或不存在
            if not os.path.exists(output_path):
                if regenerate("检测到 all-apps.json 被删除/不存在"):
                    last_proxy, _ = read_proxy(config_path)
                continue

            # 条件二：sources.json 的 proxy 变更
            cur_proxy, _ = read_proxy(config_path)
            if cur_proxy is not None and cur_proxy != last_proxy:
                if regenerate(f"检测到 proxy 变更：{last_proxy} -> {cur_proxy}"):
                    last_proxy = cur_proxy
    except KeyboardInterrupt:
        log("已停止监听")


def main():
    parser = argparse.ArgumentParser(description="聚合 AltStore 源并添加 GitHub 加速前缀")
    parser.add_argument("--config", default="sources.json")
    parser.add_argument("--output", default="all-apps.json")
    parser.add_argument("--no-cache", action="store_true", help="禁用源数据缓存")
    parser.add_argument("--watch", action="store_true", help="监听模式：文件删除或 proxy 变更时自动重新生成")
    parser.add_argument("--interval", type=float, default=3.0, help="监听模式下的轮询间隔秒数，默认 3")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(base_dir, args.config)
    output_path = os.path.join(base_dir, args.output)

    if args.watch:
        if args.interval <= 0:
            log("错误：--interval 必须大于 0")
            sys.exit(2)
        watch(config_path, output_path, args.interval)
        return

    sys.exit(generate(config_path, output_path, no_cache=args.no_cache))


if __name__ == "__main__":
    main()
