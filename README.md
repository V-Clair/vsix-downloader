# VS Code 插件下载器（vsix-downloader）

一个零第三方依赖的 Python 工具：命令行与图形界面共用同一套逻辑，从 `marketplace.visualstudio.com` 查询 VS Code 插件，并把最新正式版（或指定版本）的 `.vsix` 安装包下载到本地。

当前版本：**1.0.0**（变更记录见 [CHANGELOG.md](CHANGELOG.md)）

## 目录结构

```text
vsix-downloader/
├── README.md                     # 仓库入口（本文件）
├── CHANGELOG.md                  # 版本记录与发布规则
├── .editorconfig
├── .gitattributes
├── .gitignore
└── software/
    └── vsix-downloader/          # 工具工程
        └── vsix_downloader.py
```

## 功能

- 图形界面：不带参数运行即打开窗口，可查询插件信息、浏览版本、选择版本/平台/保存目录并下载
- 命令行：`python vsix_downloader.py <标识> [参数]`，参数与行为保持不变
- 支持 `publisher.name` 形式标识，也支持直接粘贴市场页面链接
- 查询插件元信息：名称、简介、安装量、评分、更新时间、可用平台等
- 列出全部可用版本（含正式/预览标记），支持下载任意历史版本
- 未指定版本时下载最新正式版；`--prerelease` 可选择最新预览版
- 支持目标平台构建：默认优先 `universal`，无通用构建时自动回退到本机平台，也可用 `--target-platform` 显式指定
- 下载过程显示进度，完成后校验 ZIP 结构和 `VsixSha256`（服务器提供时）
- 仅使用 Python 标准库，无需 `pip install`

## 环境要求

- Python 3.8 及以上（在 Python 3.9.13 / Windows 上验证）
- 图形界面需要 Python 自带 tkinter（Windows 官方安装包默认包含）
- 可访问 `marketplace.visualstudio.com` 与 `*.gallerycdn.vsassets.io`
- 无第三方依赖

## 快速开始

```bash
cd software/vsix-downloader

# 打开图形界面（不带任何参数）
python vsix_downloader.py

# 查看帮助
python vsix_downloader.py -h

# 下载最新正式版到当前目录
python vsix_downloader.py ms-python.python

# 查看插件信息 / 版本列表
python vsix_downloader.py esbenp.prettier-vscode --info
python vsix_downloader.py esbenp.prettier-vscode --list

# 直接粘贴市场页面链接
python vsix_downloader.py "https://marketplace.visualstudio.com/items?itemName=esbenp.prettier-vscode"

# 指定版本和平台，保存到指定目录
python vsix_downloader.py ms-python.python --version 2026.10.0 --target-platform win32-x64 -o .\downloads

# 选择最新预览版
python vsix_downloader.py ms-python.python --prerelease
```

> 如果系统里没有 `python` 命令，可改用 `py`（Windows Python Launcher）或 `python3`。
> 也可以在仓库根目录直接用 `python software/vsix-downloader/vsix_downloader.py <参数>` 运行。

## 图形界面

界面上方填写插件标识（`publisher.name` 或市场链接），三个按钮分别对应查询信息、版本列表、下载；下方可设置版本、平台、保存目录，以及「允许预览版」「覆盖同名文件」。

- 版本、平台留空时：版本取最新正式版，平台优先 `universal`，无通用构建时自动回退本机平台。
- 查询成功后版本与平台下拉框会自动填入该插件可用的值。
- 下载进度显示在进度条，日志与结果（保存路径、大小、SHA256、安装命令）显示在下方文本区。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `extension`（位置参数） | 插件标识 `publisher.name`，或市场页面链接（含 `itemName=`）；留空则打开图形界面 |
| `-g`, `--gui` | 打开图形界面 |
| `--version X.Y.Z` | 指定要下载的版本，默认最新正式版 |
| `-p`, `--prerelease`, `--pre` | 未指定版本时允许选择最新预览版 |
| `-t`, `--target-platform` | 指定目标平台，如 `win32-x64`、`darwin-arm64`、`linux-x64`、`universal` |
| `-o`, `--output` | 保存目录，默认当前目录 |
| `-l`, `--list` | 只列出可用版本，不下载 |
| `-i`, `--info` | 只显示插件信息，不下载 |
| `-n`, `--limit` | `--list` 显示的最大版本数，默认 30 |
| `-f`, `--force` | 同名文件已存在时覆盖 |
| `--timeout` | 网络超时秒数，默认 30 |
| `-q`, `--quiet` | 不显示下载进度和提示 |
| `-V`, `--tool-version` | 显示工具版本 |

## 下载发布版

GitHub Releases 中每个版本提供两个文件，版本号一致：

| 文件 | 说明 |
| --- | --- |
| `vsix-downloader-1.0.0.exe` | PyInstaller 打包的 Windows 单文件程序，双击打开图形界面 |
| `vsix-downloader-1.0.0.py` | 单文件源码，用法与仓库内脚本完全相同 |

exe 也可以从终端带参数运行，例如 `vsix-downloader-1.0.0.exe esbenp.prettier-vscode --info`。

## 打包发布

```powershell
python -m pip install --user pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed `
  --name vsix-downloader-1.0.0 --distpath dist --workpath build --specpath build `
  software\vsix-downloader\vsix_downloader.py
Copy-Item software\vsix-downloader\vsix_downloader.py dist\vsix-downloader-1.0.0.py
```

发布时四处版本号保持一致：脚本内 `__version__`、`dist/` 下两个文件名、GitHub Release 的 tag（`v1.0.0`）、[CHANGELOG.md](CHANGELOG.md) 的记录。

## 输出文件

默认保存到当前目录，命名规则与 VS Code 一致：

- 通用构建：`publisher.name-version.vsix`，例如 `esbenp.prettier-vscode-12.4.0.vsix`
- 平台构建：`publisher.name-version@platform.vsix`，例如 `ms-python.python-2026.10.0@win32-x64.vsix`

下载完成后可直接安装：

```bash
code --install-extension ".\esbenp.prettier-vscode-12.4.0.vsix"
```

成功下载时会输出文件路径、版本、平台、大小、SHA256；若市场记录了 `VsixSha256` 并校验一致，会额外提示。

## 工作原理

1. 向 `POST https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery` 提交查询，`filterType: 7` 以 `publisher.name` 搜索，并在结果中做大小写不敏感的精确匹配。
2. 查询标志位 `0x1 | 0x2 | 0x10 | 0x80 | 0x100` 请求版本列表、文件地址、版本属性、AssetUri 与统计信息。
3. 版本按 `(version, targetPlatform)` 去重，`targetPlatform` 缺失视为 `universal`；预览版由版本属性 `Microsoft.VisualStudio.Code.PreRelease` 判断。
4. 选版本：默认取接口返回顺序中第一个非预览版本；`--version` 指定时按精确版本号匹配；`--prerelease` 时取最新版本（含预览）。
5. 选平台：先找 `universal`，再找本机平台（如 `win32-x64`），最后若该版本只有一个构建则直接使用，否则报错并列出可用平台。
6. 使用返回的 `Microsoft.VisualStudio.Services.VSIXPackage` CDN 地址流式下载到 `.part` 临时文件，校验 ZIP 文件头（`PK\x03\x04`）与 SHA256 后再重命名为最终文件。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 成功（含 `--list` / `--info`） |
| `1` | 网络错误、下载/校验失败，或目标文件已存在且未加 `--force` |
| `2` | 参数错误、插件或版本不存在、平台不匹配 |
| `130` | 用户中断 |

## 注意事项

- 该工具使用 Marketplace 的公开查询接口，属于非官方接口，接口行为可能随微软更新而变化。
- 请遵守 Visual Studio Marketplace 的使用条款，仅用于个人合理的插件获取需求。
- 如果处于代理网络，可通过环境变量 `HTTPS_PROXY` / `HTTP_PROXY` 指定代理，标准库会自动使用。
- 部分插件只提供平台专用构建（例如 `ms-python.python` 的正式版），默认会自动回退到本机平台，也可用 `--target-platform` 明确指定。
- 版本号写错时可先执行 `--list` 查看可用版本；插件标识写错时工具会提示相近的搜索结果。
