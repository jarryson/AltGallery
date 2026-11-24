# AltStore 源聚合 + 国内加速

将多个 AltStore 源（GitHub 仓库）聚合为一个源，并把其中所有 GitHub 链接自动添加 `https://github.chenc.dev/` 前缀，方便国内访问和下载 IPA。

## 快速开始

1. 把本目录推送到你自己的 GitHub 仓库
2. 仓库设置 → Actions → 确保 Workflow 权限已开启（Settings → Actions → General → 勾选 "Read and write permissions"）
3. GitHub Actions 会自动定时运行，产出 `all-apps.json`
4. 在 AltStore / SideStore 中添加源：

   ```
   https://raw.githubusercontent.com/<你的用户名>/<仓库名>/main/all-apps.json
   ```

   > 若希望国内也能稳定访问源文件本身，可开启 GitHub Pages 部署，用 `https://<用户名>.github.io/<仓库名>/all-apps.json`。

## 自动更新

工作流 `.github/workflows/update.yml` 每天 UTC 0:00 和 12:00（北京时间 08:00、20:00）自动运行，拉取最新数据并提交更新。也可在 Actions 页面手动点击 "Run workflow" 触发。

## 添加新源

编辑 `sources.json`：

### 方式一：添加现成的 AltStore 源（有公开 JSON）

在 `sources` 数组里加一项：

```json
{
  "id": "源id",
  "name": "源名称",
  "url": "https://xxx/apps.json",
  "enabled": true
}
```

### 方式二：GitHub 仓库没有 AltStore 源文件（手动配置 + 自动探测，如 cilicili）

该仓库只发布 IPA 到 GitHub Release，没有 `altstore.json`。手动写元数据，并加 `github` 字段让脚本**自动探测最新版本**：

```json
{
  "id": "cilicili",
  "name": "CiliCili",
  "enabled": true,
  "manual": {
    "name": "CiliCili",
    "bundleIdentifier": "com.cilicili.bili",
    "developerName": "Rone89",
    "subtitle": "SwiftUI 第三方 B 站客户端",
    "localizedDescription": "……",
    "iconURL": "https://raw.githubusercontent.com/Rone89/cilicili/refs/heads/main/.../icon.png",
    "tintColor": "#FB7299",
    "github": {
      "owner": "Rone89",
      "repo": "cilicili",
      "asset": "bili-release-unsigned.ipa"
    },
    "versions": [
      { "version": "兜底版本", "date": "...", "downloadURL": "...", "size": 0 }
    ]
  }
}
```

- **`github` 字段**（新增）：`owner` / `repo` 是仓库信息，`asset` 用于匹配 IPA 资产文件名——支持精确名（如 `"bili-release-unsigned.ipa"`）或正则（如 `".*\\.ipa$"`）。脚本会调用 GitHub Releases API 自动拉取最新 release，动态生成 `versions`（真实版本号、发布时间、下载地址、文件大小），无需手动更新版本。
- **`versions` 字段作为兜底**：当 GitHub API 探测失败（网络异常、未找到匹配资产）时，使用这里写死的静态版本，保证源不会变成空。
- 应用每次发新版后，`github` 探测会自动跟进，**完全无需人工维护**。

## 本地运行

```bash
python3 scripts/process.py            # 正常模式（带缓存）
python3 scripts/process.py --no-cache # 禁用缓存，强制重新拉取
```

产出 `all-apps.json`。脚本会自动：
- 拉取 `sources.json` 中所有 `url`
- 对 `manual.github` 源自动探测最新 GitHub Release 版本
- 合并所有源的 `apps`（按 `bundleIdentifier` 去重）
- 给所有 GitHub 网址（`github.com`、`raw.githubusercontent.com` 等）加代理前缀

### 退出码与错误信息

脚本用退出码区分不同结果，便于脚本化调用：

| 退出码 | 含义 |
| --- | --- |
| `0` | 全部源处理成功，已产出文件 |
| `1` | 部分源失败（已用剩余成功源产出文件），错误详情见 stderr |
| `2` | 所有源均失败，**未产出文件**（不会生成空文件） |

### 自动重新生成（watch 守护）

`scripts/watch.py` 会在本地持续监听，满足以下任一条件时自动调用 `process.py` 重新生成（覆盖旧文件）：

- `all-apps.json` 被删除或不存在；
- `sources.json` 中的 `proxy` 地址发生变更。

```bash
python3 scripts/watch.py              # 默认每 3 秒轮询一次
python3 scripts/watch.py --interval 5 # 自定义轮询间隔（秒）
```

启动时会先检查 `all-apps.json` 是否存在，不存在则立即生成；随后进入监听循环。生成失败时会打印明确的错误原因（超时、源全部失败等）。按 `Ctrl+C` 停止。

### 源数据缓存

脚本会把成功拉取的源数据缓存到 `.cache/` 目录。当某次网络抖动导致拉取失败时，自动回退到上次成功的缓存，避免产出空源。缓存目录已加入 `.gitignore`，不提交到仓库（GitHub Actions 每次运行会重新拉取并填充）。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `sources.json` | 源配置（代理前缀 + 源列表 + 手动应用） |
| `scripts/process.py` | 处理脚本（含 GitHub Release 自动探测、源缓存、失败分级退出码） |
| `scripts/watch.py` | 本地监听脚本（文件删除 / proxy 变更时自动重新生成） |
| `all-apps.json` | 最终产出的聚合源（自动生成） |
| `.github/workflows/update.yml` | 定时自动更新工作流 |
| `.cache/` | 源数据缓存（不提交，自动生成） |
