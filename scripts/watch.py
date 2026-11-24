#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AltStore 源聚合监听脚本（本地自愈守护进程）

功能：
1. 监听 all-apps.json 是否被删除 / 不存在，若是则自动重新生成。
2. 监听 sources.json 中 proxy 地址是否变更，变更后立即用最新 proxy 重新生成。
3. 生成时覆盖旧文件，格式与生成规则同 scripts/process.py 一致。
4. 生成失败时打印明确错误信息。

用法：
    python3 scripts/watch.py [--interval 3]

    --interval  轮询间隔（秒），默认 3 秒。
"""

import argparse
import json
import os
import subprocess
import sys
import time

DEFAULT_INTERVAL = 3


def log(msg):
    print(f"[watch] {msg}", file=sys.stderr, flush=True)


def resolve_path(base_dir, rel):
    """把相对路径解析为绝对路径。"""
    if os.path.isabs(rel):
        return rel
    return os.path.join(base_dir, rel)


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


def regenerate(base_dir, script_path, config_path, output_path, reason):
    """调用 process.py 重新生成 all-apps.json，返回是否成功。"""
    log(f"触发重新生成：{reason}")
    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=base_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        log("错误：生成超时（超过 300 秒）")
        return False
    except OSError as e:
        log(f"错误：无法启动生成脚本：{e}")
        return False

    # 打印 process.py 的日志（stderr）与标准输出
    if proc.stderr:
        for line in proc.stderr.strip().splitlines():
            log("  " + line)
    if proc.stdout:
        for line in proc.stdout.strip().splitlines():
            log("  " + line)

    if proc.returncode == 0:
        log(f"生成成功：{output_path}")
        return True
    elif proc.returncode == 1:
        log(f"警告：生成完成但有部分源失败（退出码 1），文件已产出：{output_path}")
        return True
    else:
        log(f"错误：生成失败（退出码 {proc.returncode}）")
        return False


def main():
    parser = argparse.ArgumentParser(description="监听并自动重新生成 all-apps.json")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="轮询间隔秒数，默认 3")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.dirname(script_dir)
    script_path = os.path.join(script_dir, "process.py")
    config_path = os.path.join(base_dir, "sources.json")
    output_path = os.path.join(base_dir, "all-apps.json")

    if args.interval <= 0:
        log("错误：--interval 必须大于 0")
        sys.exit(2)

    log(f"开始监听（间隔 {args.interval}s）")
    log(f"  配置文件: {config_path}")
    log(f"  输出文件: {output_path}")

    # 记录初始 proxy 值
    last_proxy, _ = read_proxy(config_path)
    log(f"  当前 proxy: {last_proxy}")

    # 首次启动：若输出文件不存在，立即生成
    if not os.path.exists(output_path):
        regenerate(base_dir, script_path, config_path, output_path,
                   "all-apps.json 不存在，启动时立即生成")
        # 生成后刷新 proxy 记录（避免后续误判）
        last_proxy, _ = read_proxy(config_path)

    try:
        while True:
            time.sleep(args.interval)

            # 条件一：all-apps.json 被删除或不存在
            if not os.path.exists(output_path):
                if regenerate(base_dir, script_path, config_path, output_path,
                              "检测到 all-apps.json 被删除/不存在"):
                    last_proxy, _ = read_proxy(config_path)
                continue

            # 条件二：sources.json 的 proxy 变更
            cur_proxy, cur_mtime = read_proxy(config_path)
            if cur_proxy is not None and cur_proxy != last_proxy:
                if regenerate(base_dir, script_path, config_path, output_path,
                              f"检测到 proxy 变更：{last_proxy} -> {cur_proxy}"):
                    last_proxy = cur_proxy
    except KeyboardInterrupt:
        log("已停止监听")


if __name__ == "__main__":
    main()
