#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 Visual Studio Marketplace 查询并下载 VS Code 插件（.vsix）。

命令行与图形界面两种用法共用同一套查询、下载逻辑；只依赖 Python 标准库。
用法见仓库根目录 README.md。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import queue
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

__version__ = "1.0.0"

APP_TITLE = "VSIX 下载器"

MARKETPLACE_QUERY_URL = (
    "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery"
)
MARKETPLACE_ITEM_URL = "https://marketplace.visualstudio.com/items?itemName={0}"
VSIX_ASSET_TYPE = "Microsoft.VisualStudio.Services.VSIXPackage"
PRE_RELEASE_PROPERTY = "Microsoft.VisualStudio.Code.PreRelease"
VSIX_SHA256_PROPERTY = "Microsoft.VisualStudio.Services.VsixSha256"
USER_AGENT = "vsix-downloader/1.0"

# 查询标志位：版本列表 + 文件地址 + 版本属性 + AssetUri + 统计信息
QUERY_FLAGS = 0x1 | 0x2 | 0x10 | 0x80 | 0x100
# filterType: 7 = 全文搜索（publisher.name 可精确命中）
SEARCH_TEXT_FILTER = 7

DEFAULT_LIMIT = 30
EXAMPLES = """示例（在本脚本所在目录执行）:
  python vsix_downloader.py                      # 不带参数时打开图形界面
  python vsix_downloader.py ms-python.python
  python vsix_downloader.py ms-python.python --list
  python vsix_downloader.py ms-python.python --version 2025.6.1 -t win32-x64
  python vsix_downloader.py esbenp.prettier-vscode --info
"""


class CliError(Exception):
    """带退出码的命令行错误。"""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class ExtensionVersion:
    version: str
    target_platform: str  # 统一用 "universal" 表示无平台后缀的通用构建
    prerelease: bool
    last_updated: Optional[str]
    files: Dict[str, str] = field(default_factory=dict)
    properties: Dict[str, str] = field(default_factory=dict)

    @property
    def vsix_url(self) -> Optional[str]:
        return self.files.get(VSIX_ASSET_TYPE)


@dataclass
class Extension:
    publisher: str
    name: str
    display_name: str
    short_description: str
    last_updated: Optional[str]
    published_date: Optional[str]
    statistics: Dict[str, float]
    versions: List[ExtensionVersion]

    @property
    def extension_id(self) -> str:
        return "{0}.{1}".format(self.publisher, self.name)


# ---------------------------------------------------------------------------
# 网络访问
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict, timeout: float, attempts: int = 3) -> dict:
    """POST JSON 并在失败时做有限次重试。"""
    body = json.dumps(payload).encode("utf-8")
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("User-Agent", USER_AGENT)
        request.add_header("Content-Type", "application/json")
        request.add_header(
            "Accept", "application/json;api-version=7.2-preview.1;excludeUrls=true"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 4xx 通常重试也无意义（429 除外）
            if 400 <= exc.code < 500 and exc.code != 429:
                raise CliError(
                    "市场接口返回 HTTP {0} {1}".format(exc.code, exc.reason), 1
                ) from exc
            last_error = exc
        except (urllib.error.URLError, socket.timeout, ValueError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    raise CliError("访问市场接口失败: {0}".format(last_error), 1)


def _human_size(size: float) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return "{0:.0f} {1}".format(value, unit)
            return "{0:.1f} {1}".format(value, unit)
        value /= 1024.0
    return "{0:.1f} GB".format(value)


def _render_progress(received: int, total: int, elapsed: float, final: bool = False) -> None:
    speed = received / elapsed if elapsed > 0 else 0.0
    if total > 0:
        percent = received * 100.0 / total
        line = "\r下载中 {0:5.1f}%  {1}/{2}  {3}/s".format(
            percent, _human_size(received), _human_size(total), _human_size(speed)
        )
    else:
        line = "\r下载中 {0}  {1}/s".format(_human_size(received), _human_size(speed))
    sys.stderr.write(line + ("\n" if final else ""))
    sys.stderr.flush()


def download_file(
    url: str,
    destination: Path,
    timeout: float,
    show_progress: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """流式下载到 destination.part，返回临时文件路径。

    progress 不为空时按 (已下载字节, 总字节) 回调，供图形界面刷新进度。
    """
    part = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "*/*")
    request.add_header("Accept-Encoding", "identity")
    interactive = show_progress and progress is None and sys.stderr.isatty()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_length = response.headers.get("Content-Length")
            total = int(raw_length) if raw_length and raw_length.isdigit() else 0
            received = 0
            started = time.monotonic()
            last_report = 0.0
            with open(part, "wb") as handle:
                while True:
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress(received, total)
                    elif interactive:
                        now = time.monotonic()
                        if now - last_report >= 0.15:
                            last_report = now
                            _render_progress(received, total, now - started)
            if interactive:
                _render_progress(received, total, time.monotonic() - started, final=True)
    except urllib.error.HTTPError as exc:
        _remove_quietly(part)
        raise CliError("下载失败: HTTP {0} {1}".format(exc.code, exc.reason), 1) from exc
    except urllib.error.URLError as exc:
        _remove_quietly(part)
        raise CliError("下载失败: {0}".format(exc.reason), 1) from exc
    except socket.timeout as exc:
        _remove_quietly(part)
        raise CliError("下载超时({0} 秒)".format(timeout), 1) from exc
    except OSError as exc:
        _remove_quietly(part)
        raise CliError("写入文件失败: {0}".format(exc), 1) from exc
    return part


def _remove_quietly(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 标识解析
# ---------------------------------------------------------------------------

def parse_extension_id(raw: str) -> Tuple[str, str]:
    """接受 publisher.name 或市场页面链接，返回 (publisher, name)。"""
    text = (raw or "").strip()
    if not text:
        raise CliError("请提供插件标识，例如 ms-python.python", 2)

    query_string = ""
    if "://" in text:
        query_string = urllib.parse.urlparse(text).query
    elif "itemName=" in text:
        query_string = text.split("?", 1)[-1]

    if query_string:
        item = (urllib.parse.parse_qs(query_string).get("itemName") or [""])[0].strip()
        if not item:
            raise CliError(
                "链接中缺少 itemName 参数，示例: "
                "https://marketplace.visualstudio.com/items?itemName=ms-python.python",
                2,
            )
        text = item

    text = text.split("#", 1)[0].strip()
    if "." not in text:
        raise CliError(
            "无法识别的插件标识: {0}，应为 publisher.name 形式或市场页面链接".format(raw), 2
        )
    publisher, name = text.split(".", 1)
    publisher, name = publisher.strip(), name.strip()
    if not publisher or not name or "/" in name or any(ch.isspace() for ch in text):
        raise CliError(
            "无法识别的插件标识: {0}，应为 publisher.name 形式或市场页面链接".format(raw), 2
        )
    return publisher, name


# ---------------------------------------------------------------------------
# 市场查询与数据整理
# ---------------------------------------------------------------------------

def fetch_extension(publisher: str, name: str, timeout: float) -> Extension:
    extension_id = "{0}.{1}".format(publisher, name)
    payload = {
        "filters": [
            {
                "criteria": [{"filterType": SEARCH_TEXT_FILTER, "value": extension_id}],
                "pageNumber": 1,
                "pageSize": 10,
                "sortBy": 0,
                "sortOrder": 0,
            }
        ],
        "assetTypes": [],
        "flags": QUERY_FLAGS,
    }
    data = _post_json(MARKETPLACE_QUERY_URL, payload, timeout)

    extensions: List[dict] = []
    for result in data.get("results") or []:
        extensions.extend(result.get("extensions") or [])

    matched = None
    for item in extensions:
        item_publisher = ((item.get("publisher") or {}).get("publisherName") or "").lower()
        item_name = (item.get("extensionName") or "").lower()
        if item_publisher == publisher.lower() and item_name == name.lower():
            matched = item
            break
    if matched is None:
        suggestions = []
        for item in extensions[:5]:
            item_publisher = (item.get("publisher") or {}).get("publisherName") or ""
            item_name = item.get("extensionName") or ""
            if item_publisher and item_name:
                suggestions.append("{0}.{1}".format(item_publisher, item_name))
        hint = ""
        if suggestions:
            hint = "。相近结果: " + ", ".join(suggestions)
        raise CliError("市场中未找到插件 {0}{1}".format(extension_id, hint), 2)

    return _build_extension(matched)


def _build_extension(raw: dict) -> Extension:
    versions: List[ExtensionVersion] = []
    seen = set()
    for item in raw.get("versions") or []:
        version = str(item.get("version") or "").strip()
        if not version:
            continue
        platform_name = str(item.get("targetPlatform") or "universal").strip().lower()
        if platform_name in ("", "none", "any"):
            platform_name = "universal"
        key = (version, platform_name)
        if key in seen:
            continue
        seen.add(key)

        properties = {
            prop.get("key"): prop.get("value", "")
            for prop in item.get("properties") or []
            if prop.get("key")
        }
        files = {
            asset.get("assetType"): asset.get("source")
            for asset in item.get("files") or []
            if asset.get("assetType") and asset.get("source")
        }
        versions.append(
            ExtensionVersion(
                version=version,
                target_platform=platform_name,
                prerelease=str(properties.get(PRE_RELEASE_PROPERTY, "")).lower() == "true",
                last_updated=item.get("lastUpdated"),
                files=files,
                properties=properties,
            )
        )

    statistics = {
        stat.get("statisticName"): float(stat.get("value") or 0)
        for stat in raw.get("statistics") or []
        if stat.get("statisticName")
    }
    return Extension(
        publisher=(raw.get("publisher") or {}).get("publisherName") or "",
        name=raw.get("extensionName") or "",
        display_name=raw.get("displayName") or "",
        short_description=raw.get("shortDescription") or "",
        last_updated=raw.get("lastUpdated"),
        published_date=raw.get("publishedDate"),
        statistics=statistics,
        versions=versions,
    )


def unique_versions(extension: Extension) -> List[str]:
    """按接口返回顺序（新版本在前）去重后的版本号列表。"""
    ordered: List[str] = []
    seen = set()
    for item in extension.versions:
        if item.version not in seen:
            seen.add(item.version)
            ordered.append(item.version)
    return ordered


def version_is_prerelease(extension: Extension, version: str) -> bool:
    return any(v.prerelease for v in extension.versions if v.version == version)


def pick_version(extension: Extension, requested: Optional[str], allow_prerelease: bool) -> str:
    versions = unique_versions(extension)
    if not versions:
        raise CliError("该插件没有可下载的版本记录", 1)
    if requested:
        if requested not in versions:
            raise CliError(
                "未找到版本 {0}。可用版本请用 --list 查看。".format(requested), 2
            )
        return requested
    for version in versions:
        if allow_prerelease or not version_is_prerelease(extension, version):
            return version
    raise CliError("没有可用的正式版本，可加 --prerelease 选择预览版。", 2)


def normalize_platform(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip().lower()
    if value in ("universal", "any", "none", "noarch"):
        return "universal"
    return value


def host_target_platform() -> str:
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x64",
        "amd64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "armv7l": "armhf",
        "i686": "ia32",
        "i386": "ia32",
    }.get(machine, "x64")
    if sys.platform.startswith("win"):
        return "win32-" + arch
    if sys.platform == "darwin":
        return "darwin-" + arch
    return "linux-" + arch


def pick_build(
    extension: Extension, version: str, requested_platform: Optional[str]
) -> Tuple[ExtensionVersion, bool]:
    """返回 (版本构建, 是否为自动选择了非通用构建)。"""
    candidates = [v for v in extension.versions if v.version == version]
    if not candidates:
        raise CliError("版本 {0} 没有可用的构建".format(version), 1)

    requested = normalize_platform(requested_platform)
    if requested:
        for item in candidates:
            if item.target_platform == requested:
                return item, False
        available = ", ".join(sorted({v.target_platform for v in candidates}))
        raise CliError(
            "版本 {0} 没有 {1} 平台的构建。可用平台: {2}".format(version, requested, available),
            2,
        )

    for item in candidates:
        if item.target_platform == "universal":
            return item, False

    host = host_target_platform()
    for item in candidates:
        if item.target_platform == host:
            return item, True

    platforms = {v.target_platform for v in candidates}
    if len(platforms) == 1:
        return candidates[0], True
    raise CliError(
        "版本 {0} 没有通用构建，请用 --target-platform 指定平台。可用平台: {1}".format(
            version, ", ".join(sorted(platforms))
        ),
        2,
    )


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------

def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    # Python 3.9 的 fromisoformat 只接受 3 位或 6 位小数，这里统一补足
    text = re.sub(
        r"\.(\d+)",
        lambda match: "." + (match.group(1) + "000000")[:6],
        text,
        count=1,
    )
    # +0000 -> +00:00
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone()


def format_time(value: Optional[str], fmt: str = "%Y-%m-%d %H:%M") -> str:
    parsed = _parse_time(value)
    return parsed.strftime(fmt) if parsed else (value or "-")


def _format_int(value: Optional[float]) -> str:
    return "{0:,.0f}".format(value) if value else "-"


def _display_width(text: str) -> int:
    """按终端显示宽度计算（中日韩字符占两列）。"""
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text
    )


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def format_info(extension: Extension) -> str:
    versions = unique_versions(extension)
    stable = next(
        (v for v in versions if not version_is_prerelease(extension, v)), None
    )
    prerelease = next(
        (v for v in versions if version_is_prerelease(extension, v)), None
    )
    stats = extension.statistics
    platforms: List[str] = []
    if stable:
        platforms = sorted(
            {v.target_platform for v in extension.versions if v.version == stable}
        )

    lines = [
        ("插件 ID", extension.extension_id),
        ("名称", extension.display_name or "-"),
        ("简介", extension.short_description or "-"),
        ("最新正式版", stable or "-"),
        ("最新预览版", prerelease or "-"),
        ("平台构建", ", ".join(platforms) if platforms else "-"),
        ("版本数量", str(len(versions))),
        (
            "安装量",
            _format_int(stats.get("install") or stats.get("updateCount")),
        ),
    ]
    rating = stats.get("averagerating")
    rating_count = stats.get("ratingcount")
    if rating:
        lines.append(
            (
                "评分",
                "{0:.2f} ({1} 个评分)".format(rating, _format_int(rating_count)),
            )
        )
    lines.append(("更新时间", format_time(extension.last_updated)))
    lines.append(("市场页面", MARKETPLACE_ITEM_URL.format(extension.extension_id)))

    width = max(_display_width(label) for label, _ in lines)
    return "\n".join(
        "{0} : {1}".format(_pad(label, width), value) for label, value in lines
    )


def print_info(extension: Extension) -> None:
    print(format_info(extension))


def format_versions(extension: Extension, limit: int) -> str:
    versions = unique_versions(extension)
    shown = versions[: max(limit, 1)]
    latest_stable = next(
        (v for v in versions if not version_is_prerelease(extension, v)), None
    )
    lines = [
        "{0} 共 {1} 个版本（新版本在前，显示前 {2} 个）:".format(
            extension.extension_id, len(versions), len(shown)
        )
    ]
    for version in shown:
        entries = [v for v in extension.versions if v.version == version]
        tag = "预览" if any(v.prerelease for v in entries) else "正式"
        platforms = sorted({v.target_platform for v in entries})
        platform_text = ", ".join(platforms[:6])
        if len(platforms) > 6:
            platform_text += " 等 {0} 个".format(len(platforms))
        marker = "  (最新正式版)" if version == latest_stable else ""
        lines.append(
            "{0}  [{1}]  {2}  平台: {3}{4}".format(
                version, tag, format_time(entries[0].last_updated, "%Y-%m-%d"),
                platform_text or "-", marker,
            )
        )
    if len(versions) > len(shown):
        lines.append(
            "… 还有 {0} 个版本未显示，可用 --limit 调整。".format(
                len(versions) - len(shown)
            )
        )
    return "\n".join(lines)


def print_versions(extension: Extension, limit: int) -> None:
    print(format_versions(extension, limit))


# ---------------------------------------------------------------------------
# 文件校验
# ---------------------------------------------------------------------------

def _check_vsix(path: Path) -> None:
    with open(path, "rb") as handle:
        header = handle.read(4)
    if header != b"PK\x03\x04":
        raise CliError(
            "下载内容不是有效的 vsix(zip) 文件，可能被代理或登录页面拦截。", 1
        )


def _sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_matches(expected: Optional[str], actual_hex: str) -> Optional[bool]:
    """返回 True/False，格式无法识别时返回 None（跳过校验）。"""
    if not expected:
        return None
    expected = expected.strip()
    candidates = set()
    if re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        candidates.add(expected.lower())
    padded = expected + "=" * (-len(expected) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(padded)
        except (ValueError, TypeError):
            continue
        if len(raw) == 32:
            candidates.add(raw.hex())
    if not candidates:
        return None
    return actual_hex in candidates


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build_filename(extension: Extension, version: str, platform_name: str) -> str:
    name = "{0}-{1}".format(extension.extension_id, version)
    if platform_name != "universal":
        name += "@" + platform_name
    return name + ".vsix"


def resolve_download(
    extension: Extension,
    requested_version: Optional[str],
    allow_prerelease: bool,
    target_platform: Optional[str],
    output_dir: str,
) -> Tuple[str, ExtensionVersion, Path, bool]:
    """确定版本、构建与保存路径。

    返回 (版本号, 构建, 目标路径, 是否自动选择了非通用构建)。
    """
    version = pick_version(extension, requested_version, allow_prerelease)
    build, auto_selected = pick_build(extension, version, target_platform)
    if not build.vsix_url:
        raise CliError("服务器未返回 {0} 的 vsix 下载地址".format(version), 1)

    directory = Path(output_dir).expanduser().resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError("无法创建输出目录 {0}: {1}".format(directory, exc), 1) from exc
    destination = directory / build_filename(extension, version, build.target_platform)
    return version, build, destination, auto_selected


def verify_and_install(
    part: Path, destination: Path, expected_sha256: Optional[str]
) -> Tuple[str, Optional[bool]]:
    """校验临时文件并移动到目标路径，返回 (SHA256, 是否与市场记录一致)。"""
    try:
        _check_vsix(part)
        digest = _sha256_hex(part)
        matched = _hash_matches(expected_sha256, digest)
        if matched is False:
            raise CliError("SHA256 校验失败，文件可能已损坏，已删除。", 1)
        os.replace(str(part), str(destination))
    except BaseException:
        _remove_quietly(part)
        raise
    return digest, matched


def format_result(
    destination: Path,
    version: str,
    platform_name: str,
    digest: str,
    matched: Optional[bool],
) -> str:
    lines = [
        "已保存: {0}".format(destination),
        "版本  : {0} ({1})".format(version, platform_name),
        "大小  : {0}".format(_human_size(destination.stat().st_size)),
        "SHA256: {0}".format(digest),
    ]
    if matched:
        lines.append("校验  : SHA256 与市场记录一致")
    lines.append("安装  : code --install-extension \"{0}\"".format(destination))
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    publisher, name = parse_extension_id(args.extension)
    extension = fetch_extension(publisher, name, args.timeout)

    if args.info:
        print_info(extension)
        return 0
    if args.list:
        print_versions(extension, args.limit)
        return 0

    version, build, destination, auto_selected = resolve_download(
        extension, args.version, args.prerelease, args.target_platform, args.output
    )

    if destination.exists() and not args.force:
        print(
            "文件已存在: {0}\n如需覆盖请加 --force。".format(destination), file=sys.stderr
        )
        return 1

    if auto_selected and not args.quiet:
        print(
            "提示: 版本 {0} 没有通用构建，已自动选择 {1} 平台构建。".format(
                version, build.target_platform
            ),
            file=sys.stderr,
        )
    print(
        "正在下载 {0} {1} ({2}) ...".format(
            extension.extension_id, version, build.target_platform
        ),
        file=sys.stderr,
    )
    part = download_file(build.vsix_url, destination, args.timeout, not args.quiet)
    digest, matched = verify_and_install(
        part, destination, build.properties.get(VSIX_SHA256_PROPERTY)
    )
    print(format_result(destination, version, build.target_platform, digest, matched))
    return 0


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------

KNOWN_PLATFORMS = [
    "universal",
    "win32-x64",
    "win32-arm64",
    "linux-x64",
    "linux-arm64",
    "darwin-x64",
    "darwin-arm64",
    "alpine-x64",
    "alpine-arm64",
    "web",
]


def launch_gui(initial_extension: str = "") -> int:
    """启动 Tkinter 图形界面，返回进程退出码。"""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from tkinter.scrolledtext import ScrolledText
    except ImportError:
        print(
            "错误: 当前 Python 缺少 tkinter，无法启动图形界面，请改用命令行参数。",
            file=sys.stderr,
        )
        return 1

    class DownloaderApp:
        """查询信息、浏览版本、下载 vsix 的主窗口。"""

        def __init__(self, root: "tk.Tk") -> None:
            self.root = root
            self.events: "queue.Queue" = queue.Queue()
            self.timeout = 30.0
            root.title("{0} {1}".format(APP_TITLE, __version__))
            root.geometry("880x620")
            root.minsize(720, 480)
            self._build_widgets()
            self.var_extension.set(initial_extension)
            self._append(
                "填入插件标识（如 esbenp.prettier-vscode）后，可查询信息、查看版本或直接下载。"
            )
            self.root.after(100, self._pump)

        # -- 界面 -----------------------------------------------------------

        def _build_widgets(self) -> None:
            main = ttk.Frame(self.root, padding=12)
            main.pack(fill=tk.BOTH, expand=True)
            main.columnconfigure(1, weight=1)

            ttk.Label(main, text="插件标识").grid(row=0, column=0, sticky="w")
            self.var_extension = tk.StringVar()
            entry = ttk.Entry(main, textvariable=self.var_extension)
            entry.grid(row=0, column=1, sticky="ew", padx=8)
            entry.bind("<Return>", lambda event: self.query_info())

            buttons = ttk.Frame(main)
            buttons.grid(row=0, column=2, sticky="e")
            self.btn_info = ttk.Button(buttons, text="查询信息", command=self.query_info)
            self.btn_list = ttk.Button(buttons, text="版本列表", command=self.query_versions)
            self.btn_download = ttk.Button(buttons, text="下载", command=self.download)
            for button in (self.btn_info, self.btn_list, self.btn_download):
                button.pack(side=tk.LEFT, padx=(0, 6))

            options = ttk.LabelFrame(main, text="下载选项", padding=10)
            options.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(12, 0))

            line1 = ttk.Frame(options)
            line1.pack(fill=tk.X)
            ttk.Label(line1, text="版本").pack(side=tk.LEFT)
            self.var_version = tk.StringVar()
            self.cmb_version = ttk.Combobox(
                line1, textvariable=self.var_version, width=24
            )
            self.cmb_version.pack(side=tk.LEFT, padx=(6, 4))
            ttk.Label(line1, text="留空下载最新正式版", foreground="#666666").pack(
                side=tk.LEFT, padx=(0, 18)
            )
            ttk.Label(line1, text="平台").pack(side=tk.LEFT)
            self.var_platform = tk.StringVar()
            self.cmb_platform = ttk.Combobox(
                line1,
                textvariable=self.var_platform,
                values=KNOWN_PLATFORMS,
                width=16,
            )
            self.cmb_platform.pack(side=tk.LEFT, padx=(6, 4))
            ttk.Label(line1, text="留空自动选择", foreground="#666666").pack(side=tk.LEFT)

            line2 = ttk.Frame(options)
            line2.pack(fill=tk.X, pady=(10, 0))
            ttk.Label(line2, text="保存目录").pack(side=tk.LEFT)
            self.var_output = tk.StringVar(value=str(Path.cwd()))
            ttk.Entry(line2, textvariable=self.var_output).pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=6
            )
            ttk.Button(line2, text="浏览...", command=self._browse_directory).pack(
                side=tk.LEFT
            )

            line3 = ttk.Frame(options)
            line3.pack(fill=tk.X, pady=(10, 0))
            self.var_prerelease = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                line3, text="允许预览版", variable=self.var_prerelease
            ).pack(side=tk.LEFT)
            self.var_force = tk.BooleanVar(value=False)
            ttk.Checkbutton(
                line3, text="覆盖同名文件", variable=self.var_force
            ).pack(side=tk.LEFT, padx=(16, 0))

            progress_frame = ttk.Frame(main)
            progress_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12, 0))
            progress_frame.columnconfigure(0, weight=1)
            self.progress = ttk.Progressbar(
                progress_frame, mode="determinate", maximum=100
            )
            self.progress.grid(row=0, column=0, sticky="ew")
            self.var_status = tk.StringVar(value="就绪")
            ttk.Label(progress_frame, textvariable=self.var_status).grid(
                row=0, column=1, sticky="e", padx=(12, 0)
            )

            main.rowconfigure(3, weight=1)
            self.output = ScrolledText(
                main, height=16, wrap=tk.WORD, font=("Consolas", 9)
            )
            self.output.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(12, 0))
            self.output.configure(state=tk.DISABLED)

        def _append(self, text: str) -> None:
            self.output.configure(state=tk.NORMAL)
            self.output.insert(tk.END, text.rstrip("\n") + "\n")
            self.output.see(tk.END)
            self.output.configure(state=tk.DISABLED)

        def _set_busy(self, busy: bool) -> None:
            state = tk.DISABLED if busy else tk.NORMAL
            for button in (self.btn_info, self.btn_list, self.btn_download):
                button.configure(state=state)
            self.root.configure(cursor="watch" if busy else "")

        def _browse_directory(self) -> None:
            chosen = filedialog.askdirectory(
                parent=self.root, initialdir=self.var_output.get() or "."
            )
            if chosen:
                self.var_output.set(chosen)

        # -- 后台任务与事件回传 ---------------------------------------------

        def _pump(self) -> None:
            try:
                while True:
                    self._handle_event(self.events.get_nowait())
            except queue.Empty:
                pass
            self.root.after(100, self._pump)

        def _handle_event(self, event: tuple) -> None:
            kind = event[0]
            if kind == "log":
                self._append(event[1])
            elif kind == "status":
                self.var_status.set(event[1])
            elif kind == "progress":
                received, total = event[1], event[2]
                if total > 0:
                    self.progress["value"] = received * 100.0 / total
                    self.var_status.set(
                        "{0} / {1}".format(_human_size(received), _human_size(total))
                    )
                else:
                    self.var_status.set(_human_size(received))
            elif kind == "extension":
                self._show_extension(event[1], event[2])
            elif kind == "error":
                self._append("错误: {0}".format(event[1]))
                self.var_status.set("出错")
                messagebox.showerror(APP_TITLE, event[1], parent=self.root)
            elif kind == "busy":
                self._set_busy(event[1])

        def _require_extension(self) -> Optional[str]:
            raw = self.var_extension.get().strip()
            if not raw:
                messagebox.showwarning(
                    APP_TITLE, "请先填写插件标识。", parent=self.root
                )
                return None
            return raw

        def _start_query(self, mode: str) -> None:
            raw = self._require_extension()
            if raw is None:
                return
            self._set_busy(True)
            self.var_status.set("正在查询...")
            self._append("正在查询 {0} ...".format(raw))

            def work() -> None:
                try:
                    publisher, name = parse_extension_id(raw)
                    extension = fetch_extension(publisher, name, self.timeout)
                    self.events.put(("extension", extension, mode))
                except CliError as exc:
                    self.events.put(("error", str(exc)))
                finally:
                    self.events.put(("busy", False))

            threading.Thread(target=work, daemon=True).start()

        def _show_extension(self, extension: Extension, mode: str) -> None:
            versions = unique_versions(extension)
            self.cmb_version["values"] = versions
            stable = next(
                (v for v in versions if not version_is_prerelease(extension, v)), None
            )
            self.var_version.set(stable or (versions[0] if versions else ""))

            platforms: List[str] = []
            for item in (
                ["universal"]
                + sorted({v.target_platform for v in extension.versions})
                + KNOWN_PLATFORMS
            ):
                if item not in platforms:
                    platforms.append(item)
            self.cmb_platform["values"] = platforms

            if mode == "list":
                self._append(format_versions(extension, DEFAULT_LIMIT))
            else:
                self._append(format_info(extension))
            self.var_status.set("查询完成")

        # -- 按钮动作 -------------------------------------------------------

        def query_info(self) -> None:
            self._start_query("info")

        def query_versions(self) -> None:
            self._start_query("list")

        def download(self) -> None:
            raw = self._require_extension()
            if raw is None:
                return
            version_request = self.var_version.get().strip() or None
            platform_request = self.var_platform.get().strip() or None
            output_dir = self.var_output.get().strip() or "."
            allow_prerelease = bool(self.var_prerelease.get())
            force = bool(self.var_force.get())

            self._set_busy(True)
            self.progress["value"] = 0
            self.var_status.set("正在查询...")
            self._append("正在查询 {0} ...".format(raw))

            def work() -> None:
                try:
                    publisher, name = parse_extension_id(raw)
                    extension = fetch_extension(publisher, name, self.timeout)
                    version, build, destination, auto_selected = resolve_download(
                        extension,
                        version_request,
                        allow_prerelease,
                        platform_request,
                        output_dir,
                    )
                    if destination.exists() and not force:
                        raise CliError(
                            "文件已存在: {0}\n如需覆盖请勾选「覆盖同名文件」。".format(
                                destination
                            ),
                            1,
                        )
                    if auto_selected:
                        self.events.put(
                            (
                                "log",
                                "提示: 版本 {0} 没有通用构建，已自动选择 {1} 平台构建。".format(
                                    version, build.target_platform
                                ),
                            )
                        )
                    self.events.put(
                        (
                            "log",
                            "正在下载 {0} {1} ({2}) ...".format(
                                extension.extension_id, version, build.target_platform
                            ),
                        )
                    )
                    self.events.put(("status", "正在下载..."))
                    part = download_file(
                        build.vsix_url,
                        destination,
                        self.timeout,
                        show_progress=False,
                        progress=lambda received, total: self.events.put(
                            ("progress", received, total)
                        ),
                    )
                    digest, matched = verify_and_install(
                        part,
                        destination,
                        build.properties.get(VSIX_SHA256_PROPERTY),
                    )
                    self.events.put(("progress", 1, 1))
                    self.events.put(
                        (
                            "log",
                            format_result(
                                destination,
                                version,
                                build.target_platform,
                                digest,
                                matched,
                            ),
                        )
                    )
                    self.events.put(("status", "下载完成"))
                except CliError as exc:
                    self.events.put(("error", str(exc)))
                except OSError as exc:
                    self.events.put(("error", "写入文件失败: {0}".format(exc)))
                finally:
                    self.events.put(("busy", False))

            threading.Thread(target=work, daemon=True).start()

    root = tk.Tk()
    DownloaderApp(root)
    root.mainloop()
    return 0


def _attach_console(argv: List[str]) -> None:
    """打包成 GUI 程序后，从终端带参数运行时把输出接回父控制台。"""
    if not argv or os.name != "nt" or not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        import ctypes

        if not ctypes.windll.kernel32.AttachConsole(-1):
            return
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except (AttributeError, OSError):
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vsix_downloader.py",
        description="从 marketplace.visualstudio.com 查询并下载 VS Code 插件（.vsix）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )
    parser.add_argument(
        "extension",
        nargs="?",
        help="插件标识 publisher.name，或市场页面链接；留空则打开图形界面",
    )
    parser.add_argument(
        "-g", "--gui", action="store_true", help="打开图形界面（不带任何参数时默认打开）"
    )
    parser.add_argument(
        "--version", metavar="X.Y.Z", help="指定版本；默认最新正式版"
    )
    parser.add_argument(
        "-p", "--prerelease", "--pre", action="store_true",
        help="未指定版本时允许选择最新的预览版",
    )
    parser.add_argument(
        "-t", "--target-platform", metavar="PLATFORM",
        help="目标平台，如 win32-x64、darwin-arm64、universal；默认优先 universal",
    )
    parser.add_argument(
        "-o", "--output", metavar="DIR", default=".", help="保存目录，默认当前目录"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-l", "--list", action="store_true", help="只列出可用版本，不下载")
    mode.add_argument("-i", "--info", action="store_true", help="只显示插件信息，不下载")
    parser.add_argument(
        "-n", "--limit", type=int, default=DEFAULT_LIMIT, metavar="N",
        help="--list 显示的最大版本数，默认 {0}".format(DEFAULT_LIMIT),
    )
    parser.add_argument("-f", "--force", action="store_true", help="同名文件存在时覆盖")
    parser.add_argument(
        "--timeout", type=float, default=30.0, metavar="SEC",
        help="网络超时秒数，默认 30",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="不显示下载进度和提示")
    parser.add_argument(
        "-V", "--tool-version", action="version",
        version="vsix-downloader {0}".format(__version__),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    _attach_console(arguments)
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.gui or not arguments:
        return launch_gui(args.extension or "")
    try:
        return run(args)
    except CliError as exc:
        print("错误: {0}".format(exc), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
