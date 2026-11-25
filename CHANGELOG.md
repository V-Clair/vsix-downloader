# 更新日志

本文件记录版本变化与发布规则。版本号在以下四处保持一致：脚本内 `__version__`、发布资产文件名、GitHub Release 的 tag、本文件。

## 发布规则

- 每次发布同时上传两个文件，版本号相同：`vsix-downloader-<版本>.exe`（PyInstaller 打包）与 `vsix-downloader-<版本>.py`（源码）。
- 发布前把 `software/vsix-downloader/vsix_downloader.py` 中的 `__version__` 改为本次版本号，并在本文件补一条记录。
- GitHub tag 使用 `v<版本>`，例如 `v1.0.0`。

## [1.0.0] - 2026-11-25

首次发布。

### 新增

- 图形界面（tkinter）：查询插件信息、浏览版本列表、选择版本/平台/保存目录并下载，显示进度与结果。
- 不带参数运行即打开图形界面；命令行用法保持不变（`python vsix_downloader.py esbenp.prettier-vscode --info` 等）。
- 发布 Windows 单文件 exe（PyInstaller `--onefile --windowed`）与同版本号源码 `.py`。

### 说明

- 命令行与图形界面共用同一套查询、选版本、选平台、下载与校验逻辑。
- 仅依赖 Python 标准库，无需 `pip install`。
