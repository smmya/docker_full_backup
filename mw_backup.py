#!/usr/bin/env python3
"""
mdserver-web (MW 面板) 关键数据备份工具。

面向 Linux，基线兼容 Python 3.8+，仅依赖标准库；外部只需要 `tar` 与 `xz`。
  本工具**只做备份，不做还原**：产出单一 `.tar.xz` 归档，内含 `manifest.json`
与 `checksums.sha256`，用于事后核对完整性与人工按需取用。

默认使用 xz -6 压缩（速度与体积平衡），`--max-compress` 切换为 xz -9e 极限压缩。

备份内容：
  * 自动探测面板已安装插件（`<fatherDir>/server/<plugin>`），采集其配置与数据
  * 数据库一律使用命令导出（mysqldump / mariadb-dump / pg_dumpall / redis-cli
    --rdb / mongodump），不直接拷贝数据目录
  * 面板自身关键状态：`panelDir/data`（panel.db、*.pl 等）与 `panelDir/ssl`
  * 面板计划任务脚本：`<fatherDir>/server/cron/`（排除其执行日志）
  * 系统目录：`/root`、`/opt`（排除二进制与中间产物）、`/etc`（白名单）、
    `/home`（默认备份，`--no-home` 关闭）
  * 业务站点数据：默认开启，站点根目录按面板 `site_path` 选项
    动态解析（默认 `<fatherDir>/wwwroot`），也可 `--wwwroot <绝对路径>` 直接指定；`--no-wwwroot` 关闭

归档内部目录结构：
  * mw-server/panel/       — 面板自身状态（data/、ssl/）
  * mw-server/panel/server/— serverDir 级共享配置（web_conf、元数据）
  * mw-server/cron/        — 面板计划任务脚本
  * mw-server/plugins/     — 已安装插件配置与数据
  * databases/             — 数据库命令导出结果
  * file/root/ file/opt/ file/home/ file/etc/ — 系统目录
  * wwwroot/               — 业务站点数据

明确不备份：
  * 二进制包与中间文件（缓存、构建产物、node_modules、*.pyc、.git、版本 tar 包…）
  * 数据库的原始数据目录（已由导出命令覆盖）
  * 网站根目录 `wwwroot` 默认采集（使用 `--no-wwwroot` 关闭）；体量较大时建议按需关闭
"""

import argparse
import dataclasses
import datetime as dt
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any, Dict, FrozenSet, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

VERSION = "1.0.0"

#: 默认压缩档位；-6 在速度与体积之间取平衡，适合日常备份。
#: 使用 --max-compress 可切回 -9e 极限压缩。
XZ_LEVEL = "-6"
#: xz -9 每个线程大约需要的内存（MiB），用于按内存反推安全线程数。
XZ_MEM_PER_THREAD_MIB = 700
#: 单文件体积上限，超过则跳过并记入 manifest（避免误打包巨型二进制/镜像）。
DEFAULT_MAX_FILE_BYTES = 256 * 1024 * 1024

#: 捕获强度。strict 用于用户目录/插件目录，config 用于 /etc 与面板配置。
PROFILE_STRICT = "strict"
PROFILE_CONFIG = "config"

# 确保 stdout/stderr 在非 UTF-8 locale（如 C/POSIX）下不因中文提示语抛
# UnicodeEncodeError；改用 backslashreplace 兜底（Python 3.7+ 的 reconfigure 特性）。
for _fh in (sys.stdout, sys.stderr):
    if hasattr(_fh, "reconfigure"):
        try:
            _fh.reconfigure(errors="backslashreplace")
        except Exception:
            pass


class BackupError(RuntimeError):
    """备份流程中的可预期错误，统一由 main() 转成非零退出码。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def now_stamp() -> str:
    """返回 `YYYYMMDD-HHMMSS` 形式的本地时间戳。"""
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def info(msg: str) -> None:
    print(f"[+] {msg}", flush=True)


def warn(msg: str) -> None:
    eprint(f"[!] {msg}")


def die(msg: str) -> None:
    """抛出 BackupError；返回值标注为 None 只是为了让调用处读起来自然。"""
    raise BackupError(msg)


def human_bytes(value: int) -> str:
    """把字节数格式化成便于阅读的字符串。"""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TiB"


def safe_slug(value: str, max_len: int = 80) -> str:
    """把任意字符串规整成可安全用于文件名的短标识。"""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    value = value.strip("-._") or "item"
    return value[:max_len]


def json_dump(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
    text: bool = True,
    cwd: Optional[pathlib.Path] = None,
) -> subprocess.CompletedProcess:
    """执行外部命令；`check=True` 时失败直接抛 BackupError。"""
    try:
        return subprocess.run(
            list(cmd),
            check=check,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=text,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        die(f"命令不存在: {cmd[0]}")
    except subprocess.CalledProcessError as exc:
        detail = ""
        if capture:
            detail = ((exc.stderr or "") + (exc.stdout or "")).strip()
        die(f"命令执行失败 ({exc.returncode}): {shlex.join(list(cmd))}\n{detail}".rstrip())
    raise AssertionError("unreachable")


def current_uid() -> int:
    """返回当前有效 UID；在没有 geteuid 的平台（如 Windows）返回 -1。"""
    getter = getattr(os, "geteuid", None)
    return int(getter()) if getter is not None else -1


def require_root() -> None:
    uid = current_uid()
    if uid != 0:
        die("请以 root 身份运行（sudo）。备份需要读取 /etc、/root 与各插件数据并保留属主信息。")


def require_commands(*commands: str) -> None:
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        die(
            "缺少必要命令: " + ", ".join(missing) + "\n"
            "Debian/Ubuntu 安装示例: apt-get update && apt-get install -y tar xz-utils"
        )


def which_first(candidates: Sequence[str]) -> Optional[str]:
    """按顺序返回第一个可用的可执行文件（绝对路径优先按存在性判断）。"""
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.sep in candidate:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- #
# 压缩线程数：根据 CPU 与内存自动推算
# --------------------------------------------------------------------------- #


def read_mem_total_mib(meminfo_path: str = "/proc/meminfo") -> Optional[int]:
    """从 /proc/meminfo 读取物理内存总量（MiB）；读不到返回 None。"""
    try:
        with open(meminfo_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1]) // 1024
    except OSError:
        return None
    return None


def compute_threads(
    requested: Optional[int] = None,
    cpu_count: Optional[int] = None,
    mem_total_mib: Optional[int] = None,
) -> int:
    """
    计算 xz 压缩线程数。

    显式指定则以指定值为准；否则取 CPU 核心数，并按 `-9e` 单线程约
    700 MiB 的内存需求做上限收敛，避免在小内存机器上触发 OOM。
    """
    if requested is not None:
        if requested < 1:
            die(f"--threads 必须 >= 1，当前为 {requested}")
        return requested
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    threads = max(1, int(cpus))
    memory = mem_total_mib if mem_total_mib is not None else read_mem_total_mib()
    if memory:
        # 预留一半内存给系统与 tar，其余按每线程 700 MiB 折算。
        affordable = max(1, (memory // 2) // XZ_MEM_PER_THREAD_MIB)
        threads = min(threads, affordable)
    return max(1, threads)


# --------------------------------------------------------------------------- #
# 排除规则（纯函数，便于单测）
# --------------------------------------------------------------------------- #

#: 版本控制与缓存类垃圾目录，任何 profile 都排除。
JUNK_DIR_NAMES: FrozenSet[str] = frozenset({
    ".git", ".svn", ".hg", ".bzr", "CVS",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".cache", ".npm", ".yarn", ".pnpm-store", ".sass-cache",
    "node_modules", ".Trash", ".Trash-0", "lost+found",
})

#: 二进制/构建产物/运行期中间目录，仅 strict profile 排除。
BINARY_DIR_NAMES: FrozenSet[str] = frozenset({
    "bin", "sbin", "lib", "lib64", "libexec", "include", "share", "man",
    "doc", "docs", "build", "dist", "target", "obj", "vendor",
    "venv", ".venv", "virtualenv", "site-packages", "dist-packages",
    ".gradle", ".m2", ".cargo", ".rustup", ".nvm", ".pyenv", ".rbenv",
    ".terraform", ".next", ".nuxt", ".parcel-cache", "coverage", "htmlcov",
    ".eggs", "tmp", "temp", "logs", "log", "run", "cache", "caches",
})

#: strict profile 下按文件名匹配排除的模式（二进制、压缩包、日志、运行期文件）。
BINARY_FILE_GLOBS: Tuple[str, ...] = (
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.so.*", "*.a", "*.o", "*.obj", "*.ko",
    "*.dll", "*.dylib", "*.exe", "*.class", "*.jar", "*.war", "*.whl", "*.egg",
    "*.deb", "*.rpm", "*.apk", "*.msi", "*.appimage",
    "*.img", "*.iso", "*.qcow2", "*.vmdk", "*.vdi", "*.swap",
    "*.tar", "*.tar.*", "*.tgz", "*.tbz2", "*.txz", "*.zip", "*.7z", "*.rar",
    "*.gz", "*.bz2", "*.xz", "*.zst", "*.lz4",
    "*.log", "*.log.*", "*.out", "*.err",
    "*.pid", "*.sock", "*.core", "core.[0-9]*", "*.dmp", "*.stackdump",
    "*.swp", "*.swo", "*~",
)

#: 任何 profile 都排除的编辑器/包管理器残留。
JUNK_FILE_GLOBS: Tuple[str, ...] = (
    "*~", "*.bak", "*.old", "*.orig", "*.swp", "*.swo", "*.tmp",
    "*.dpkg-old", "*.dpkg-new", "*.dpkg-dist", "*.ucf-old", "*.ucf-dist",
    "*.rpmsave", "*.rpmnew", "*.rpmorig", ".DS_Store",
)


def _matches_any(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def is_excluded_dir(name: str, profile: str = PROFILE_STRICT) -> bool:
    """判断目录名是否应当整棵跳过。"""
    if name in JUNK_DIR_NAMES:
        return True
    if profile == PROFILE_STRICT and name in BINARY_DIR_NAMES:
        return True
    return False


def is_excluded_file(name: str, profile: str = PROFILE_STRICT) -> bool:
    """判断文件名是否应当跳过。"""
    if _matches_any(name, JUNK_FILE_GLOBS):
        return True
    if profile == PROFILE_STRICT and _matches_any(name, BINARY_FILE_GLOBS):
        return True
    return False


# --------------------------------------------------------------------------- #
# 文件树遍历与暂存
# --------------------------------------------------------------------------- #


class CaptureEntry:
    """一次遍历命中的条目；`skip_reason` 非空表示被排除。"""

    __slots__ = ("source", "relative", "kind", "size", "skip_reason")

    def __init__(
        self,
        source: pathlib.Path,
        relative: pathlib.PurePosixPath,
        kind: str,
        size: int = 0,
        skip_reason: Optional[str] = None,
    ) -> None:
        self.source = source
        self.relative = relative
        self.kind = kind
        self.size = size
        self.skip_reason = skip_reason

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"CaptureEntry({self.source!s}, {self.relative!s}, {self.kind}, {self.size}, {self.skip_reason})"


def entry_kind(path: pathlib.Path) -> str:
    """区分 symlink / dir / file / other，不跟随符号链接。"""
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "dir"
    if path.is_file():
        return "file"
    return "other"


def _lstat_size(path: pathlib.Path) -> int:
    try:
        return int(path.lstat().st_size)
    except OSError:
        return 0


def iter_capture(
    source: pathlib.Path,
    rel_base: str,
    profile: str = PROFILE_STRICT,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    excluded_paths: Optional[Iterable[pathlib.Path]] = None,
) -> Iterator[CaptureEntry]:
    """
    遍历 `source`，产出应当纳入归档的条目（以及被跳过的条目，带 skip_reason）。

    * 不跟随符号链接，符号链接原样保留。
    * `source` 自身是显式指定的路径，不做名字级排除。
    * `excluded_paths` 中的绝对路径整棵跳过（用于排除数据库原始数据目录）。
    """
    excluded: Set[str] = {os.path.normpath(str(p)) for p in (excluded_paths or ())}
    src = pathlib.Path(source)
    base = pathlib.PurePosixPath(rel_base)

    if not os.path.lexists(str(src)):
        return
    if os.path.normpath(str(src)) in excluded:
        return

    kind = entry_kind(src)
    if kind != "dir":
        size = _lstat_size(src)
        reason = None
        if kind == "other":
            reason = "unsupported-file-type"
        elif kind == "file" and max_file_bytes > 0 and size > max_file_bytes:
            reason = f"file-too-large(>{max_file_bytes})"
        yield CaptureEntry(src, base, kind, size, reason)
        return

    stack: List[Tuple[pathlib.Path, pathlib.PurePosixPath]] = [(src, base)]
    yield CaptureEntry(src, base, "dir", 0, None)

    while stack:
        current_dir, current_rel = stack.pop()
        try:
            with os.scandir(str(current_dir)) as scanner:
                children = sorted(scanner, key=lambda e: e.name)
        except OSError as exc:
            yield CaptureEntry(current_dir, current_rel, "dir", 0, f"unreadable:{exc.strerror or exc}")
            continue

        for child in children:
            child_path = pathlib.Path(child.path)
            child_rel = current_rel / child.name
            if os.path.normpath(str(child_path)) in excluded:
                yield CaptureEntry(child_path, child_rel, "dir", 0, "explicitly-excluded")
                continue

            if child.is_symlink():
                yield CaptureEntry(child_path, child_rel, "symlink", _lstat_size(child_path), None)
                continue

            try:
                is_dir = child.is_dir(follow_symlinks=False)
                is_file = child.is_file(follow_symlinks=False)
            except OSError:
                is_dir = False
                is_file = False

            if is_dir:
                if is_excluded_dir(child.name, profile):
                    yield CaptureEntry(child_path, child_rel, "dir", 0, "excluded-dir-name")
                    continue
                yield CaptureEntry(child_path, child_rel, "dir", 0, None)
                stack.append((child_path, child_rel))
                continue

            if not is_file:
                yield CaptureEntry(child_path, child_rel, "other", 0, "unsupported-file-type")
                continue

            if is_excluded_file(child.name, profile):
                yield CaptureEntry(child_path, child_rel, "file", 0, "excluded-file-name")
                continue

            size = _lstat_size(child_path)
            if max_file_bytes > 0 and size > max_file_bytes:
                yield CaptureEntry(child_path, child_rel, "file", size, f"file-too-large(>{max_file_bytes})")
                continue
            yield CaptureEntry(child_path, child_rel, "file", size, None)


@dataclasses.dataclass
class StageStats:
    """一个备份单元的暂存统计。"""

    files: int = 0
    dirs: int = 0
    symlinks: int = 0
    bytes: int = 0
    skipped: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    errors: List[str] = dataclasses.field(default_factory=list)

    #: skipped 列表最多保留的条目数，避免 manifest 膨胀。
    MAX_SKIPPED_RECORDED = 200

    def record_skip(self, entry: CaptureEntry) -> None:
        if len(self.skipped) < self.MAX_SKIPPED_RECORDED:
            self.skipped.append({
                "path": str(entry.source),
                "reason": entry.skip_reason,
                "size_bytes": entry.size,
            })

    def as_dict(self) -> Dict[str, Any]:
        return {
            "files": self.files,
            "dirs": self.dirs,
            "symlinks": self.symlinks,
            "bytes": self.bytes,
            "skipped_sample": self.skipped,
            "errors": self.errors,
        }


def _copy_metadata(source: pathlib.Path, dest: pathlib.Path) -> None:
    """尽力还原属主/权限/时间戳；非 root 或不支持时静默降级。"""
    try:
        st = source.lstat()
    except OSError:
        return
    chown = getattr(os, "lchown", None)
    if chown is not None and current_uid() == 0:
        try:
            chown(str(dest), st.st_uid, st.st_gid)
        except OSError:
            pass
    if not dest.is_symlink():
        try:
            os.chmod(str(dest), st.st_mode & 0o7777)
            os.utime(str(dest), ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass


def _place_file(source: pathlib.Path, dest: pathlib.Path) -> None:
    """
    把文件放入暂存目录：优先硬链接（零拷贝、同时保留属主与权限），
    跨设备或不支持硬链接时回退为 copy2。
    """
    try:
        os.link(str(source), str(dest))
        return
    except OSError:
        pass
    shutil.copy2(str(source), str(dest), follow_symlinks=False)
    _copy_metadata(source, dest)


def stage_entries(entries: Iterable[CaptureEntry], stage_root: pathlib.Path) -> StageStats:
    """把遍历结果落到暂存目录 `stage_root` 下（按 relative 路径）。"""
    stats = StageStats()
    for entry in entries:
        if entry.skip_reason:
            stats.record_skip(entry)
            continue
        target = stage_root / pathlib.Path(str(entry.relative))
        try:
            if entry.kind == "dir":
                target.mkdir(parents=True, exist_ok=True)
                _copy_metadata(entry.source, target)
                stats.dirs += 1
            elif entry.kind == "symlink":
                target.parent.mkdir(parents=True, exist_ok=True)
                if os.path.lexists(str(target)):
                    os.unlink(str(target))
                os.symlink(os.readlink(str(entry.source)), str(target))
                _copy_metadata(entry.source, target)
                stats.symlinks += 1
            elif entry.kind == "file":
                target.parent.mkdir(parents=True, exist_ok=True)
                if os.path.lexists(str(target)):
                    os.unlink(str(target))
                _place_file(entry.source, target)
                stats.files += 1
                stats.bytes += entry.size
        except OSError as exc:
            stats.errors.append(f"{entry.source}: {exc}")
    return stats


def summarize_entries(entries: Iterable[CaptureEntry]) -> StageStats:
    """只统计不落盘，供 dry-run/list 使用。"""
    stats = StageStats()
    for entry in entries:
        if entry.skip_reason:
            stats.record_skip(entry)
            continue
        if entry.kind == "dir":
            stats.dirs += 1
        elif entry.kind == "symlink":
            stats.symlinks += 1
        elif entry.kind == "file":
            stats.files += 1
            stats.bytes += entry.size
    return stats


# --------------------------------------------------------------------------- #
# 面板探测
# --------------------------------------------------------------------------- #

#: 常见安装位置，按优先级排列。
PANEL_ROOT_CANDIDATES: Tuple[str, ...] = (
    "/www/server/mdserver-web",
    "/mdserver/mdserver-web",
    "/opt/mdserver-web",
    "/usr/local/mdserver-web",
    "/usr/local/lib/mdserver-web",
)

#: 上次探测使用的方法（用于 manifest 记录，便于排查）。
_panel_discovery_method: Optional[str] = None

#: 动态扫描的目标根目录。
_DYNAMIC_SCAN_ROOTS: Tuple[str, ...] = ("/www", "/opt", "/home", "/root", "/usr/local")

#: 动态扫描中匹配的目录名（小写比对）。
_DYNAMIC_SCAN_NAMES: Tuple[str, ...] = ("mdserver-web", "panel", "mdserver-web-main")


def is_panel_root(path: pathlib.Path) -> bool:
    """判定是否为 mdserver-web 安装根。

    安装后的面板是 flat 结构（无 web/ 子目录），用以下标志判定：
    - app.py（Flask 入口）
    - data/（运行时数据目录）
    - plugins/（插件源码目录）"""
    path = pathlib.Path(path)
    return (path / "app.py").is_file() and (path / "data").is_dir() and (path / "plugins").is_dir()


def _dynamic_scan_panel() -> Optional[pathlib.Path]:
    """第 2/3 级动态探测：cwd 向上回溯 + 顶级目录 glob 扫描。"""
    global _panel_discovery_method
    # --- 第 2 级：从当前工作目录向上回溯 -------------------------------- #
    try:
        cwd = pathlib.Path(os.getcwd()).resolve()
    except (OSError, FileNotFoundError):
        cwd = pathlib.Path("/")
    p: pathlib.Path = cwd
    while True:
        if is_panel_root(p):
            _panel_discovery_method = "cwd_parents"
            return p
        parent = p.parent
        if parent == p:  # 已到达根目录 /
            break
        p = parent

    # --- 第 3 级：顶级目录下 glob 扫描 --------------------------------- #
    for search_root_str in _DYNAMIC_SCAN_ROOTS:
        search_root = pathlib.Path(search_root_str)
        if not search_root.is_dir():
            continue
        try:
            for d1 in search_root.iterdir():
                if not d1.is_dir():
                    continue
                # 第 1 层：search_root/<d1>/<target-name>
                for d2 in d1.iterdir():
                    if not d2.is_dir():
                        continue
                    if d2.name.lower() in _DYNAMIC_SCAN_NAMES and is_panel_root(d2):
                        _panel_discovery_method = "glob_scan"
                        return d2.resolve()
        except PermissionError:
            continue
    return None


def discover_panel_root(
    explicit: Optional[str] = None,
    candidates: Optional[Sequence[str]] = None,
    dynamic_scan: bool = True,
) -> Optional[pathlib.Path]:
    """
    定位面板根目录（三级探测）。

    1. 显式 --panel-root：校验后直接返回
    2. 硬编码候选列表（PANEL_ROOT_CANDIDATES）
    3. 从当前工作目录向上回溯到根 /
    4. 在 /www /opt /home /root /usr/local 下 glob 扫描

    探测不到返回 None（调用方降级为仅备份系统目录）。
    通过 _panel_discovery_method 记录本次探测手段。
    """
    global _panel_discovery_method
    _panel_discovery_method = None

    if candidates is None:
        candidates = PANEL_ROOT_CANDIDATES

    if explicit:
        path = pathlib.Path(explicit).expanduser().resolve()
        if not path.is_dir():
            die(f"--panel-root 指向的目录不存在: {path}")
        if not is_panel_root(path):
            die(f"--panel-root 不像 mdserver-web 安装根（缺少 web/core/mw.py 或 plugins/）: {path}")
        _panel_discovery_method = "explicit"
        return path

    # --- 第 1 级：硬编码候选列表 ----------------------------------------- #
    for candidate in candidates:
        path = pathlib.Path(candidate)
        if path.is_dir() and is_panel_root(path):
            _panel_discovery_method = "candidates"
            return path.resolve()

    # --- 第 2/3 级：动态扫描 --------------------------------------------- #
    if dynamic_scan:
        result = _dynamic_scan_panel()
        if result is not None:
            return result

    return None


class PanelLayout:
    """由 panelDir 推导出的目录布局，对齐 mw.py 中的 getXxxDir()。"""

    __slots__ = ("panel_dir", "father_dir", "server_dir", "plugin_dir", "data_dir", "ssl_dir")

    def __init__(self, panel_dir: pathlib.Path) -> None:
        self.panel_dir = pathlib.Path(panel_dir)
        # mw.getFatherDir() == dirname(dirname(panelDir))
        self.father_dir = self.panel_dir.parent.parent
        self.server_dir = self.father_dir / "server"
        self.plugin_dir = self.panel_dir / "plugins"
        self.data_dir = self.panel_dir / "data"
        self.ssl_dir = self.panel_dir / "ssl"

    def as_dict(self) -> Dict[str, str]:
        return {
            "panel_dir": str(self.panel_dir),
            "father_dir": str(self.father_dir),
            "server_dir": str(self.server_dir),
            "plugin_dir": str(self.plugin_dir),
            "data_dir": str(self.data_dir),
            "ssl_dir": str(self.ssl_dir),
        }


#: serverDir 下不是插件的共享目录（面板自身、日志、回收站等）。
#: 注意 `cron` 仍留在这里——它不是插件，不应出现在 detected_plugins；
#: 其内容由专用单元 `panel-cron`（归档内 `mw-server/cron/`）单独采集。
SERVER_SHARED_DIR_NAMES: FrozenSet[str] = frozenset({
    "web_conf", "wwwlogs", "wwwroot", "recycle_bin", "backup", "cron",
    "panel", "tmp", "mdserver-web",
})

#: serverDir 下的计划任务脚本目录（面板「计划任务」写入的 shell 脚本）。
SERVER_CRON_DIR_NAME = "cron"


def discover_installed_plugins(
    server_dir: pathlib.Path,
    plugin_dir: Optional[pathlib.Path] = None,
    exclude_names: Optional[Iterable[str]] = None,
) -> List[str]:
    """
    列出已安装插件：`serverDir` 下的每个子目录即一个已安装插件。

    `web_conf`、面板自身安装目录等共享目录不是插件，需要排除；若提供
    `plugin_dir`，则把面板插件仓库中存在的名字排在前面（仅影响顺序，
    不做过滤，因为用户可能安装了仓库之外的插件）。
    """
    server_dir = pathlib.Path(server_dir)
    if not server_dir.is_dir():
        return []
    shared: Set[str] = set(SERVER_SHARED_DIR_NAMES)
    shared.update(exclude_names or ())
    names: List[str] = []
    try:
        with os.scandir(str(server_dir)) as scanner:
            children = sorted(scanner, key=lambda e: e.name)
    except OSError:
        return []
    for child in children:
        if child.name.startswith("."):
            continue
        if child.name in shared:
            continue
        try:
            if not child.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        names.append(child.name)
    if plugin_dir is not None and pathlib.Path(plugin_dir).is_dir():
        known = {p.name for p in pathlib.Path(plugin_dir).iterdir() if p.is_dir()}
        names.sort(key=lambda n: (n not in known, n))
    return names


# --------------------------------------------------------------------------- #
# 插件采集计划
# --------------------------------------------------------------------------- #

#: 插件根目录下直接命中的配置文件模式。
PLUGIN_ROOT_CONFIG_GLOBS: Tuple[str, ...] = (
    "*.conf", "*.cnf", "*.ini", "*.pl", "*.json", "*.yaml", "*.yml",
    "*.toml", "*.db", "*.env", "*.properties",
)
#: 插件配置目录（存在才采集）。
PLUGIN_CONFIG_SUBDIRS: Tuple[str, ...] = ("etc", "conf", "config", "conf.d", "init.d")
#: 插件数据目录（DB 类插件除外，见 DB_PLUGINS）。
PLUGIN_DATA_SUBDIRS: Tuple[str, ...] = ("data",)


@dataclasses.dataclass(frozen=True)
class DbSpec:
    """数据库插件的导出规格。"""

    engine: str
    dump_relative: str
    #: 原始数据目录一律不打包，改由导出命令覆盖。
    skip_raw_data_dir: bool = True
    #: 位于原始数据目录中、但仍需单独保留的配置文件。
    config_files_in_data: Tuple[str, ...] = ()


DB_PLUGINS: Dict[str, DbSpec] = {
    "mysql": DbSpec("mysql", "databases/mysql-all.sql"),
    "mysql-community": DbSpec("mysql", "databases/mysql-all.sql"),
    "mysql-apt": DbSpec("mysql", "databases/mysql-all.sql"),
    "mysql-yum": DbSpec("mysql", "databases/mysql-all.sql"),
    "mariadb": DbSpec("mariadb", "databases/mysql-all.sql"),
    "postgresql": DbSpec(
        "postgresql",
        "databases/postgresql-all.sql",
        config_files_in_data=("postgresql.conf", "pg_hba.conf", "pg_ident.conf"),
    ),
    "pgsql": DbSpec(
        "postgresql",
        "databases/postgresql-all.sql",
        config_files_in_data=("postgresql.conf", "pg_hba.conf", "pg_ident.conf"),
    ),
    "redis": DbSpec("redis", "databases/redis.rdb"),
    "valkey": DbSpec("redis", "databases/valkey.rdb"),
    "mongodb": DbSpec("mongodb", "databases/mongodb.archive"),
}


@dataclasses.dataclass
class PluginPlan:
    """单个插件的采集计划。"""

    name: str
    base_dir: pathlib.Path
    config_paths: List[pathlib.Path] = dataclasses.field(default_factory=list)
    data_paths: List[pathlib.Path] = dataclasses.field(default_factory=list)
    excluded_paths: List[pathlib.Path] = dataclasses.field(default_factory=list)
    db_engine: Optional[str] = None

    def is_empty(self) -> bool:
        return not self.config_paths and not self.data_paths

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "base_dir": str(self.base_dir),
            "config_paths": [str(p) for p in self.config_paths],
            "data_paths": [str(p) for p in self.data_paths],
            "excluded_paths": [str(p) for p in self.excluded_paths],
            "db_engine": self.db_engine,
        }


def _sorted_glob(base: pathlib.Path, pattern: str) -> List[pathlib.Path]:
    try:
        return sorted(p for p in base.glob(pattern) if p.is_file() or p.is_dir())
    except OSError:
        return []


def plugin_capture_plan(server_dir: pathlib.Path, name: str) -> PluginPlan:
    """
    针对单个已安装插件生成采集计划。

    通用规则：插件根下的配置文件 + `etc/ conf/ config/ conf.d/ init.d/` +
    `data/`。DB 类插件跳过原始数据目录（改用导出命令），并单独保留其
    位于数据目录中的配置文件。openresty / php / apache 有专门的布局特例。
    """
    server_dir = pathlib.Path(server_dir)
    base = server_dir / name
    plan = PluginPlan(name=name, base_dir=base)
    if not base.is_dir():
        return plan

    for pattern in PLUGIN_ROOT_CONFIG_GLOBS:
        for path in _sorted_glob(base, pattern):
            if path.is_file() and path not in plan.config_paths:
                plan.config_paths.append(path)

    for sub in PLUGIN_CONFIG_SUBDIRS:
        candidate = base / sub
        if candidate.is_dir() and candidate not in plan.config_paths:
            plan.config_paths.append(candidate)

    # --- 布局特例 --------------------------------------------------------- #
    if name == "openresty":
        # 主配置在 nginx/conf；站点 vhost 与 php 处理器在共享的 web_conf 下。
        # 源码中 web_conf 位于 serverDir 级（mw.getServerDir() + '/web_conf'），
        # 部分老版本/自定义部署会放在插件目录内，这里两处都兼容。
        for extra in (base / "nginx" / "conf", base / "web_conf"):
            if extra.is_dir() and extra not in plan.config_paths:
                plan.config_paths.append(extra)
    elif name == "apache":
        for extra in (base / "httpd" / "conf", base / "httpd" / "conf.d", base / "httpd" / "conf.modules.d"):
            if extra.is_dir() and extra not in plan.config_paths:
                plan.config_paths.append(extra)
    elif name.startswith("php"):
        # php 目录按版本分层：<server>/php/<version>/etc/{php.ini,php-fpm.conf,php-fpm.d/}
        for version_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            if is_excluded_dir(version_dir.name, PROFILE_STRICT):
                continue
            etc_dir = version_dir / "etc"
            if etc_dir.is_dir() and etc_dir not in plan.config_paths:
                plan.config_paths.append(etc_dir)
            for pattern in ("*.ini", "*.conf", "*.pl"):
                for path in _sorted_glob(version_dir, pattern):
                    if path.is_file() and path not in plan.config_paths:
                        plan.config_paths.append(path)

    # --- 数据目录 --------------------------------------------------------- #
    spec = DB_PLUGINS.get(name)
    if spec is not None:
        plan.db_engine = spec.engine
        for sub in PLUGIN_DATA_SUBDIRS:
            data_dir = base / sub
            if not data_dir.is_dir():
                continue
            if spec.skip_raw_data_dir:
                plan.excluded_paths.append(data_dir)
                for filename in spec.config_files_in_data:
                    conf = data_dir / filename
                    if conf.is_file() and conf not in plan.config_paths:
                        plan.config_paths.append(conf)
            else:
                plan.data_paths.append(data_dir)
    else:
        for sub in PLUGIN_DATA_SUBDIRS:
            data_dir = base / sub
            if data_dir.is_dir():
                plan.data_paths.append(data_dir)

    return plan


def _relative_under(base: pathlib.Path, path: pathlib.Path) -> str:
    """把绝对路径转成相对 base 的 POSIX 相对路径；无法相对化时退化为去掉根的完整路径。"""
    try:
        return pathlib.PurePosixPath(*pathlib.Path(path).relative_to(base).parts).as_posix()
    except ValueError:
        parts = [p for p in pathlib.Path(path).parts if p not in ("/", "\\")]
        parts = [p.replace(":", "") for p in parts]
        return pathlib.PurePosixPath(*parts).as_posix()


# --------------------------------------------------------------------------- #
# 计划任务脚本（serverDir/cron）
# --------------------------------------------------------------------------- #

#: 计划任务目录中的执行日志（运行期产物，不入归档）。
#: `**/` 在 pathlib.glob 中匹配零级或多级目录，因此也覆盖 cron 根下的日志。
CRON_LOG_GLOBS: Tuple[str, ...] = ("**/*.log", "**/*.log.*")


def cron_log_paths(cron_dir: pathlib.Path) -> List[pathlib.Path]:
    """
    列出计划任务目录下的执行日志文件。

    面板把任务脚本与其执行日志放在同一个目录（`<echo>` 与 `<echo>.log`），
    脚本必须备份、日志属于运行期产物。cron 单元用 PROFILE_CONFIG（否则
    脚本本身可能被 strict 的文件名规则误伤），因此日志需要显式排除。
    """
    cron_dir = pathlib.Path(cron_dir)
    if not cron_dir.is_dir():
        return []
    found: List[pathlib.Path] = []
    seen: Set[str] = set()
    for pattern in CRON_LOG_GLOBS:
        for path in _sorted_glob(cron_dir, pattern):
            if not path.is_file():
                continue
            key = os.path.normpath(str(path))
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
    return found


# --------------------------------------------------------------------------- #
# 业务站点根目录（--wwwroot）
# --------------------------------------------------------------------------- #

#: 面板站点根目录对应的 option 键；见 mw.getWwwDir()：
#: `thisdb.getOption('site_path', default=getFatherDir() + '/wwwroot')`。
PANEL_SITE_PATH_OPTION = "site_path"
#: option 表的默认分类（thisdb.getOption 的 type 参数默认值）。
PANEL_OPTION_TYPE = "common"


def read_panel_option(
    panel_db: pathlib.Path,
    name: str,
    option_type: str = PANEL_OPTION_TYPE,
) -> Optional[str]:
    """
    以只读方式从面板主库 `panel.db` 的 `option` 表读取一个配置项。

    等价于面板侧的 `thisdb.getOption(name, type)`。任何异常（没有 sqlite3、
    库文件损坏、表不存在、被独占锁定…）都返回 None，由调用方回退默认值——
    备份工具不能因为读不到一个可选配置就整体失败。
    """
    panel_db = pathlib.Path(panel_db)
    if not panel_db.is_file():
        return None
    try:
        import sqlite3
    except ImportError:
        warn("当前 Python 缺少 sqlite3 模块，无法读取面板 site_path 选项，将使用默认站点目录。")
        return None

    value: Optional[str] = None
    conn = None
    try:
        # mode=ro 只读打开，绝不写入面板主库（面板可能正在运行）。
        uri = panel_db.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        cursor = conn.execute(
            "SELECT value FROM option WHERE name=? AND type=? LIMIT 1",
            (name, option_type),
        )
        row = cursor.fetchone()
        if row is not None and row[0] is not None:
            value = str(row[0]).strip()
    except (sqlite3.Error, OSError, ValueError) as exc:
        warn(f"读取面板选项 {name} 失败（将使用默认值）: {exc}")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return value or None


def resolve_site_root(
    layout: Optional[PanelLayout],
    explicit: Optional[str] = None,
) -> Optional[pathlib.Path]:
    """
    解析业务站点根目录（`--wwwroot`）。

    优先级（对齐 mw.getWwwDir()，但站点路径**动态解析**而非硬编码 wwwroot）：
      1. `--wwwroot <path>` 显式给出的路径（不存在直接报错，避免静默备份空目录）
      2. 面板 `option` 表中的 `site_path`（面板设置页可改）
      3. `<fatherDir>/wwwroot`（面板默认值）

    未探测到面板且未显式指定时返回 None，由调用方给出可操作的提示。
    """
    if explicit:
        path = pathlib.Path(explicit).expanduser()
        if not path.is_dir():
            die(f"--wwwroot 指向的目录不存在: {path}")
        return path.resolve()

    if layout is None:
        return None

    configured = read_panel_option(layout.data_dir / "panel.db", PANEL_SITE_PATH_OPTION)
    if configured:
        candidate = pathlib.Path(configured).expanduser()
        if candidate.is_dir():
            info(f"站点根目录来自面板 site_path 选项: {candidate}")
            return candidate.resolve()
        warn(f"面板 site_path 选项指向的目录不存在，回退到默认站点目录: {candidate}")

    default_root = layout.father_dir / "wwwroot"
    if default_root.is_dir():
        return default_root.resolve()
    return None


# --------------------------------------------------------------------------- #
# /etc 白名单
# --------------------------------------------------------------------------- #

#: /etc 关键路径白名单：网络配置、用户/账户配置、以及常见的用户自建服务配置。
#: 采用白名单策略——只备份这里列出的路径，其余一律不进归档。
ETC_ALLOWLIST: Tuple[str, ...] = (
    # --- 主机与网络 ---
    "etc/hostname",
    "etc/hosts",
    "etc/hosts.allow",
    "etc/hosts.deny",
    "etc/resolv.conf",
    "etc/nsswitch.conf",
    "etc/network",
    "etc/netplan",
    "etc/NetworkManager/system-connections",
    "etc/NetworkManager/conf.d",
    "etc/systemd/network",
    "etc/systemd/resolved.conf",
    "etc/systemd/resolved.conf.d",
    "etc/systemd/timesyncd.conf",
    "etc/sysconfig/network",
    "etc/sysconfig/network-scripts",
    "etc/dhcp",
    "etc/iproute2",
    "etc/wpa_supplicant",
    "etc/wireguard",
    "etc/hosts.equiv",
    # --- 防火墙与内核参数 ---
    "etc/firewalld",
    "etc/ufw",
    "etc/iptables",
    "etc/nftables.conf",
    "etc/sysconfig/iptables",
    "etc/sysconfig/ip6tables",
    "etc/sysctl.conf",
    "etc/sysctl.d",
    "etc/security/limits.conf",
    "etc/security/limits.d",
    # --- 用户 / 账户 / 认证 ---
    "etc/passwd",
    "etc/shadow",
    "etc/group",
    "etc/gshadow",
    "etc/subuid",
    "etc/subgid",
    "etc/login.defs",
    "etc/sudoers",
    "etc/sudoers.d",
    "etc/pam.d",
    "etc/skel",
    "etc/ssh",
    # --- 系统基础配置 ---
    "etc/fstab",
    "etc/crypttab",
    "etc/machine-id",
    "etc/timezone",
    "etc/localtime",
    "etc/locale.conf",
    "etc/locale.gen",
    "etc/default",
    "etc/environment",
    "etc/profile",
    "etc/profile.d",
    "etc/bash.bashrc",
    "etc/bashrc",
    "etc/os-release",
    "etc/selinux/config",
    "etc/rc.local",
    "etc/init.d",
    "etc/systemd/system",
    # --- 计划任务与日志 ---
    "etc/crontab",
    "etc/cron.d",
    "etc/cron.daily",
    "etc/cron.hourly",
    "etc/cron.weekly",
    "etc/cron.monthly",
    "etc/cron.allow",
    "etc/cron.deny",
    "etc/logrotate.conf",
    "etc/logrotate.d",
    "etc/rsyslog.conf",
    "etc/rsyslog.d",
    # --- 软件源（重装时需要复现） ---
    "etc/apt/sources.list",
    "etc/apt/sources.list.d",
    "etc/apt/preferences.d",
    "etc/apt/apt.conf.d",
    "etc/yum.repos.d",
    "etc/yum.conf",
    "etc/dnf/dnf.conf",
    # --- 用户自建 / 常见服务配置 ---
    "etc/nginx",
    "etc/apache2",
    "etc/httpd",
    "etc/php",
    "etc/my.cnf",
    "etc/my.cnf.d",
    "etc/mysql",
    "etc/postgresql",
    "etc/redis",
    "etc/mongod.conf",
    "etc/supervisor",
    "etc/supervisord.conf",
    "etc/supervisord.d",
    "etc/docker/daemon.json",
    "etc/containerd/config.toml",
    "etc/fail2ban",
    "etc/keepalived",
    "etc/haproxy",
    "etc/samba/smb.conf",
    "etc/exports",
    "etc/chrony",
    "etc/chrony.conf",
    "etc/ntp.conf",
    "etc/vsftpd.conf",
    "etc/pure-ftpd",
    "etc/letsencrypt",
    "etc/ssl/private",
    "etc/ssl/openssl.cnf",
    "etc/rsyncd.conf",
    "etc/rsyncd.secrets",
)

#: 从 systemd unit 中抽取配置路径时，只接受这些前缀（避免把 /usr 里的东西拉进来）。
_SYSTEMD_CONF_PREFIXES: Tuple[str, ...] = ("/etc/",)
#: unit 文件里可能引用配置路径的指令。
_SYSTEMD_CONF_DIRECTIVES = re.compile(
    r"^\s*(?:ExecStart|ExecStartPre|ExecStartPost|ExecReload|EnvironmentFile|Environment)\s*=\s*(.+)$",
    re.IGNORECASE,
)


def detect_user_service_conf_paths(
    systemd_dir: pathlib.Path,
    root: pathlib.Path = pathlib.Path("/"),
) -> List[pathlib.Path]:
    """
    扫描 `/etc/systemd/system/*.service`（用户自建单元），从中抽取被引用的
    `/etc/...` 配置文件或目录，作为白名单的自动补充。

    这一步让「非系统自带的服务配置目录」不必完全依赖硬编码列表。
    """
    systemd_dir = pathlib.Path(systemd_dir)
    root = pathlib.Path(root)
    found: List[pathlib.Path] = []
    seen: Set[str] = set()
    if not systemd_dir.is_dir():
        return found
    try:
        # 只扫用户自建单元：厂商 unit 常以软链形式存在于此目录，
        # 跟随符号链接会把它也算进来，与「用户自建」语义不符，也更易踩雷。
        units = sorted(p for p in systemd_dir.glob("*.service") if p.is_file() and not p.is_symlink())
    except OSError:
        return found
    for unit in units:
        try:
            text = unit.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # 单个 unit 解析失败（如畸形引号、编码问题）只告警跳过，
        # best-effort，不影响其它单元与整体备份流程。
        try:
            lines = text.splitlines()
        except Exception as exc:  # pragma: no cover - 理论上 read_text(errors=replace) 不会触发
            warn(f"跳过无法解析的 systemd 单元 {unit}: {exc}")
            continue
        for line in lines:
            match = _SYSTEMD_CONF_DIRECTIVES.match(line)
            if not match:
                continue
            try:
                tokens = shlex.split(match.group(1).replace("-/etc/", "/etc/"), posix=True)
            except ValueError as exc:
                # 未闭合引号 / 单个撇号等畸形写法：跳过该行，继续其余指令。
                warn(f"systemd 单元 {unit} 的指令无法分词（已跳过）: {exc}")
                continue
            for token in tokens:
                token = token.strip().lstrip("-@+!")
                if not token.startswith(_SYSTEMD_CONF_PREFIXES):
                    continue
                candidate = root / token.lstrip("/")
                # 只收配置文件本身或其所在目录，且必须真实存在。
                if candidate.is_file() or candidate.is_dir():
                    key = os.path.normpath(str(candidate))
                    if key not in seen:
                        seen.add(key)
                        found.append(candidate)
    return found


def build_etc_allowlist(
    extra: Optional[Sequence[str]] = None,
    root: pathlib.Path = pathlib.Path("/"),
    allowlist: Sequence[str] = ETC_ALLOWLIST,
    autodetect_services: bool = True,
) -> List[pathlib.Path]:
    """
    构造最终的 /etc 采集路径列表（只返回真实存在的路径）。

    `root` 参数用于测试：把假的文件树根目录传进来即可在非 Linux 环境验证逻辑。
    """
    root = pathlib.Path(root)
    result: List[pathlib.Path] = []
    seen: Set[str] = set()

    def push(path: pathlib.Path) -> None:
        if not os.path.lexists(str(path)):
            return
        key = os.path.normpath(str(path))
        if key in seen:
            return
        seen.add(key)
        result.append(path)

    for rel in allowlist:
        push(root / rel)

    if autodetect_services:
        for path in detect_user_service_conf_paths(root / "etc" / "systemd" / "system", root):
            push(path)

    for item in extra or ():
        raw = str(item).strip()
        if not raw:
            continue
        candidate = pathlib.Path(raw)
        if not candidate.is_absolute():
            candidate = root / raw.lstrip("/")
        if not os.path.lexists(str(candidate)):
            warn(f"--etc-allowlist-extra 指向的路径不存在，已忽略: {candidate}")
            continue
        push(candidate)

    return result


# --------------------------------------------------------------------------- #
# 数据库导出
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class DumpResult:
    """一次数据库导出的结果记录。"""

    plugin: str
    engine: str
    relative: str
    ok: bool
    command: str = ""
    message: str = ""
    bytes: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _read_text_safe(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_conf_value(text: str, key: str) -> Optional[str]:
    """
    从 ini/conf 风格文本里取出 `key = value` 或 `key value` 的值。

    用于从 my.cnf / redis.conf / postgresql.conf 中提取 socket、port、密码等。
    """
    pattern = re.compile(
        r"^[ \t]*" + re.escape(key) + r"[ \t]*(?:=|[ \t])[ \t]*(.+?)[ \t]*(?:#.*)?$",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in pattern.finditer(text):
        value = match.group(1).strip().strip('"').strip("'")
        if value:
            return value
    return None


def _run_dump_to_file(
    cmd: Sequence[str],
    out_path: pathlib.Path,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 3600,
) -> Tuple[bool, str]:
    """执行导出命令并把标准输出写入文件；返回 (是否成功, 错误信息)。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    try:
        with out_path.open("wb") as fh:
            proc = subprocess.Popen(
                list(cmd),
                stdout=fh,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=merged_env,
            )
            _, stderr = proc.communicate(timeout=timeout)
        if proc.returncode != 0:
            out_path.unlink(missing_ok=True)
            return False, (stderr or b"").decode("utf-8", "replace").strip()
        if out_path.stat().st_size == 0:
            out_path.unlink(missing_ok=True)
            return False, "导出结果为空文件"
        return True, ""
    except FileNotFoundError:
        out_path.unlink(missing_ok=True)
        return False, f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        proc.kill()
        out_path.unlink(missing_ok=True)
        return False, f"导出超时（{timeout}s）"
    except OSError as exc:
        out_path.unlink(missing_ok=True)
        return False, str(exc)


def _run_side_effect_dump(
    cmd: Sequence[str],
    out_path: pathlib.Path,
    timeout: int = 3600,
) -> Tuple[bool, str]:
    """执行「自己写文件」的导出命令（如 redis-cli --rdb / mongodump --archive=）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"导出超时（{timeout}s）"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode != 0:
        out_path.unlink(missing_ok=True)
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return False, detail or f"退出码 {proc.returncode}"
    if not out_path.is_file() or out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        return False, "导出结果为空文件"
    return True, ""


def _derive_mysql_client(mysqldump_path: str) -> str:
    """从 mysqldump/mariadb-dump 路径推导 mysql/mariadb CLI 路径。"""
    dirname = os.path.dirname(mysqldump_path)
    if "mariadb-dump" in mysqldump_path:
        candidates = [
            os.path.join(dirname, "mariadb"),
            os.path.join(dirname, "mysql"),
            "mariadb",
            "mysql",
        ]
    else:
        candidates = [
            os.path.join(dirname, "mysql"),
            os.path.join(dirname, "mariadb"),
            "mysql",
            "mariadb",
        ]
    resolved = which_first(candidates)
    return resolved if resolved else "mysql"


def dump_mysql(plugin_dir: pathlib.Path, dest_dir: pathlib.Path, plugin: str, engine: str) -> List[DumpResult]:
    """
    使用 mysqldump / mariadb-dump 按库拆分导出。

    流程：
      1. 确定可用二进制与连接参数（同旧逻辑）
      2. 用已验证的连接执行 SHOW DATABASES，获取用户数据库列表
      3. 对每个用户库单独导出到 ``<dest_dir>/mysql-<db>.sql``
      4. 若 SHOW DATABASES 失败，回退为 ``--all-databases`` 行为（兼容性）
      5. 返回 ``List[DumpResult]``，每个库一个

    边界处理：
      - 无用户数据库 → 返回空列表
      - 单个库导出失败 → 记入对应 DumpResult.message，继续下一个库
    """
    SYSTEM_DBS: FrozenSet[str] = frozenset({"information_schema", "performance_schema", "mysql", "sys"})
    my_cnf = plugin_dir / "etc" / "my.cnf"
    socket_path = parse_conf_value(_read_text_safe(my_cnf), "socket") if my_cnf.is_file() else None

    mysql_db = plugin_dir / "mysql.db"
    mysql_pwd: Optional[str] = None
    if mysql_db.is_file():
        try:
            import sqlite3 as _sql
            _db = _sql.connect(str(mysql_db))
            _row = _db.execute("SELECT mysql_root FROM config WHERE id=1").fetchone()
            if _row and _row[0]:
                mysql_pwd = _row[0]
            _db.close()
        except Exception:
            pass

    binaries: List[str] = []
    if engine == "mariadb":
        binaries = [
            str(plugin_dir / "bin" / "mariadb-dump"),
            str(plugin_dir / "bin" / "mysqldump"),
            "mariadb-dump",
            "mysqldump",
        ]
    else:
        binaries = [
            str(plugin_dir / "bin" / "mysqldump"),
            str(plugin_dir / "bin" / "mariadb-dump"),
            "mysqldump",
            "mariadb-dump",
        ]

    # 构建不带导出标志的连接尝试列表（先只测连通性 + 获取库列表）
    all_attempts: List[List[str]] = []
    for binary in binaries:
        resolved = which_first([binary])
        if not resolved:
            continue
        if my_cnf.is_file():
            all_attempts.append([resolved, f"--defaults-file={my_cnf}", "-uroot"])
        base = [resolved, "-uroot"]
        if socket_path:
            base += [f"--socket={socket_path}"]
        all_attempts.append(list(base))
        all_attempts.append([resolved, "-uroot", "-h", "127.0.0.1"])
        if mysql_pwd:
            all_attempts.append([resolved, "-uroot", f"-p{mysql_pwd}"])

    if not all_attempts:
        result = DumpResult(
            plugin=plugin, engine=engine, relative="databases/mysql",
            ok=False, message="未找到 mysqldump / mariadb-dump 可执行文件",
        )
        return [result]

    # ── 步骤 1：尝试获取数据库列表 ───────────────────────────────────── #
    dbs: List[str] = []
    working_base: Optional[List[str]] = None
    errors: List[str] = []

    for base_cmd in all_attempts:
        mysql_cli = _derive_mysql_client(base_cmd[0])
        db_cmd = [mysql_cli] + base_cmd[1:] + ["-ss", "-e", "SHOW DATABASES"]
        try:
            proc = subprocess.run(
                db_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                timeout=30,
            )
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            continue
        except OSError:
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            errors.append(f"{shlex.join(db_cmd)} -> 退出码 {proc.returncode}")
            continue
        # 成功：解析数据库名，过滤系统库
        raw_names = [line.strip() for line in proc.stdout.strip().splitlines() if line.strip()]
        dbs = [name for name in raw_names if name.lower() not in SYSTEM_DBS]
        working_base = base_cmd
        break

    # ── 步骤 2：回退为 --all-databases（兼容性） ─────────────────────── #
    if working_base is None:
        common = [
            "--all-databases",
            "--single-transaction",
            "--routines",
            "--events",
            "--triggers",
            "--default-character-set=utf8mb4",
        ]
        out_path = dest_dir / "mysql-all.sql"
        attempt_errors: List[str] = []
        for base_cmd in all_attempts:
            cmd = base_cmd + common
            ok, message = _run_dump_to_file(cmd, out_path)
            if ok:
                result = DumpResult(
                    plugin=plugin, engine=engine,
                    relative="databases/mysql-all.sql", ok=True,
                    command=shlex.join(cmd), bytes=out_path.stat().st_size,
                )
                return [result]
            attempt_errors.append(f"{shlex.join(cmd)} -> {message}")
        result = DumpResult(
            plugin=plugin, engine=engine,
            relative="databases/mysql-all.sql", ok=False,
            message="; ".join(attempt_errors[-3:] or errors[-3:]),
        )
        return [result]

    # ── 步骤 3：无用户数据库，直接返回空列表 ─────────────────────────── #
    if not dbs:
        return []

    # ── 步骤 4：按库导出 ─────────────────────────────────────────────── #
    common_flags = [
        "--single-transaction",
        "--routines",
        "--events",
        "--triggers",
        "--default-character-set=utf8mb4",
    ]
    results: List[DumpResult] = []
    for db in dbs:
        out_path = dest_dir / f"mysql-{db}.sql"
        relative = f"databases/mysql-{db}.sql"
        result = DumpResult(plugin=plugin, engine=engine, relative=relative, ok=False)
        cmd = working_base + ["--databases", db] + common_flags
        ok, message = _run_dump_to_file(cmd, out_path)
        if ok:
            result.ok = True
            result.command = shlex.join(cmd)
            result.bytes = out_path.stat().st_size
        else:
            result.message = message
        results.append(result)
    return results


def dump_postgresql(plugin_dir: pathlib.Path, out_path: pathlib.Path, plugin: str) -> DumpResult:
    """使用 pg_dumpall 导出全部数据库；以 root 运行时切换到 postgres 用户。"""
    result = DumpResult(plugin=plugin, engine="postgresql", relative=str(out_path.name), ok=False)
    binary = which_first([str(plugin_dir / "bin" / "pg_dumpall"), "pg_dumpall"])
    if not binary:
        result.message = "未找到 pg_dumpall 可执行文件"
        return result

    port = parse_conf_value(_read_text_safe(plugin_dir / "data" / "postgresql.conf"), "port") or "5432"

    attempts: List[List[str]] = []
    if current_uid() == 0 and shutil.which("su"):
        inner = f"{shlex.quote(binary)} -p {shlex.quote(port)}"
        attempts.append(["su", "-", "postgres", "-c", inner])
    attempts.append([binary, "-p", port])
    attempts.append([binary, "-U", "postgres", "-p", port])

    errors: List[str] = []
    for cmd in attempts:
        ok, message = _run_dump_to_file(cmd, out_path)
        if ok:
            result.ok = True
            result.command = shlex.join(cmd)
            result.bytes = out_path.stat().st_size
            return result
        errors.append(f"{shlex.join(cmd)} -> {message}")
    result.message = "; ".join(errors[-3:])
    return result


def dump_redis(plugin_dir: pathlib.Path, out_path: pathlib.Path, plugin: str) -> DumpResult:
    """使用 `redis-cli --rdb` 拉取一份一致性 RDB 快照。"""
    result = DumpResult(plugin=plugin, engine="redis", relative=str(out_path.name), ok=False)
    conf_names = ("redis.conf", "valkey.conf")
    conf_text = ""
    for name in conf_names:
        conf = plugin_dir / name
        if conf.is_file():
            conf_text = _read_text_safe(conf)
            break
    if not conf_text:
        conf = plugin_dir / "etc" / "redis.conf"
        if conf.is_file():
            conf_text = _read_text_safe(conf)

    port = parse_conf_value(conf_text, "port") or "6379"
    password = parse_conf_value(conf_text, "requirepass")

    cli_names = ["redis-cli", "valkey-cli"] if plugin != "valkey" else ["valkey-cli", "redis-cli"]
    binary = which_first([str(plugin_dir / "bin" / n) for n in cli_names] + cli_names)
    if not binary:
        result.message = "未找到 redis-cli / valkey-cli 可执行文件"
        return result

    cmd = [binary, "-h", "127.0.0.1", "-p", port, "--no-auth-warning", "--rdb", str(out_path)]

    # 密码通过 REDISCLI_AUTH 环境变量传递，避免出现在命令行参数里（ps 对同机其他用户可见）。
    # 顺序导出，临时注入并在调用后恢复，不改写 _run_side_effect_dump 签名。
    if password:
        os.environ["REDISCLI_AUTH"] = password
    try:
        ok, message = _run_side_effect_dump(cmd, out_path)
    finally:
        if password:
            os.environ.pop("REDISCLI_AUTH", None)
    # 命令行本身不含明文密码；记录时亦不写密码。
    result.command = shlex.join(cmd)
    if ok:
        result.ok = True
        result.bytes = out_path.stat().st_size
    else:
        result.message = message
    return result


def dump_mongodb(plugin_dir: pathlib.Path, out_path: pathlib.Path, plugin: str) -> DumpResult:
    """使用 mongodump 导出为单一 archive 文件（尽力而为）。"""
    result = DumpResult(plugin=plugin, engine="mongodb", relative=str(out_path.name), ok=False)
    binary = which_first([str(plugin_dir / "bin" / "mongodump"), "mongodump"])
    if not binary:
        result.message = "未找到 mongodump 可执行文件"
        return result
    port = parse_conf_value(_read_text_safe(plugin_dir / "mongodb.conf"), "port") or "27017"
    cmd = [binary, "--host", "127.0.0.1", "--port", port, f"--archive={out_path}"]
    ok, message = _run_side_effect_dump(cmd, out_path)
    result.command = shlex.join(cmd)
    if ok:
        result.ok = True
        result.bytes = out_path.stat().st_size
    else:
        result.message = message
    return result


def run_db_dumps(
    plans: Sequence[PluginPlan],
    stage_root: pathlib.Path,
) -> List[DumpResult]:
    """
    对所有 DB 类插件执行导出，结果写入 `<stage_root>/databases/`。

    采用 best-effort 策略：单个导出失败只 warn 并记入 manifest，不中断整体备份。
    """
    results: List[DumpResult] = []
    handled_relatives: Set[str] = set()
    for plan in plans:
        spec = DB_PLUGINS.get(plan.name)
        if spec is None:
            continue
        if spec.dump_relative in handled_relatives:
            # 例如 mysql 与 mysql-community 同时"存在"，只导一次，避免互相覆盖。
            info(f"跳过重复的数据库导出目标: {plan.name} -> {spec.dump_relative}")
            continue
        info(f"导出数据库: {plan.name} -> {spec.dump_relative}")
        if spec.engine in ("mysql", "mariadb"):
            dest_dir = stage_root / "databases"
            batch = dump_mysql(plan.base_dir, dest_dir, plan.name, spec.engine)
            for result in batch:
                if result.ok:
                    info(f"  完成: {result.relative} ({human_bytes(result.bytes)})")
                else:
                    warn(f"数据库导出失败（已记入 manifest，继续备份）: {plan.name}: {result.message}")
                results.append(result)
            handled_relatives.add(spec.dump_relative)
            continue
        out_path = stage_root / spec.dump_relative
        if spec.engine == "postgresql":
            result = dump_postgresql(plan.base_dir, out_path, plan.name)
        elif spec.engine == "redis":
            result = dump_redis(plan.base_dir, out_path, plan.name)
        elif spec.engine == "mongodb":
            result = dump_mongodb(plan.base_dir, out_path, plan.name)
        else:
            result = DumpResult(
                plugin=plan.name,
                engine=spec.engine,
                relative=spec.dump_relative,
                ok=False,
                message="暂不支持该数据库引擎的命令导出",
            )
        result.relative = spec.dump_relative
        if result.ok:
            handled_relatives.add(spec.dump_relative)
            info(f"  完成: {spec.dump_relative} ({human_bytes(result.bytes)})")
        else:
            warn(f"数据库导出失败（已记入 manifest，继续备份）: {plan.name}: {result.message}")
        results.append(result)
    return results


# --------------------------------------------------------------------------- #
# 归档与校验
# --------------------------------------------------------------------------- #


def tar_flags_for_create() -> List[str]:
    """GNU tar 创建参数；保留数值属主、扩展属性与稀疏文件。"""
    return ["--numeric-owner", "--xattrs", "--acls", "--sparse"]


def xz_compress_pipeline(
    producer_cmd: Sequence[str],
    output_path: pathlib.Path,
    level: str,
    threads: int,
) -> None:
    """`producer | xz <level> -T<threads>` 管道压缩到 output_path。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    info(f"正在写入 {output_path}")
    with output_path.open("wb") as out:
        producer = subprocess.Popen(list(producer_cmd), stdout=subprocess.PIPE)
        assert producer.stdout is not None
        compressor = subprocess.Popen(
            ["xz", level, f"-T{threads}", "-c"], stdin=producer.stdout, stdout=out
        )
        producer.stdout.close()
        rc_comp = compressor.wait()
        rc_prod = producer.wait()
    if rc_prod != 0 or rc_comp != 0:
        output_path.unlink(missing_ok=True)
        die(f"压缩管道失败: {shlex.join(list(producer_cmd))} | xz {level} -T{threads}")


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(root: pathlib.Path) -> int:
    """为暂存目录下所有普通文件生成 checksums.sha256，返回文件数量。"""
    entries: List[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name == "checksums.sha256":
            continue
        rel = path.relative_to(root).as_posix()
        entries.append(f"{sha256_file(path)}  {rel}")
    (root / "checksums.sha256").write_text("\n".join(entries) + "\n", encoding="utf-8")
    return len(entries)


def verify_checksums(root: pathlib.Path) -> None:
    """按 checksums.sha256 校验目录内容，失败即报错。

    对校验条目做路径穿越防护：拒绝空值、绝对路径、含反斜杠、含 ``..``
    的条目（纵深防御，避免误把归档根之外的文件当成「校验通过」）。
    """
    checksum_path = root / "checksums.sha256"
    if not checksum_path.is_file():
        die("归档中缺少 checksums.sha256。")
    failures: List[str] = []
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, _, rel = line.partition("  ")
        if (
            not rel
            or rel.startswith("/")
            or "\\" in rel
            or os.path.isabs(rel)
            or ".." in pathlib.PurePosixPath(rel).parts
        ):
            failures.append(rel)
            continue
        path = root / rel
        if not path.is_file() or sha256_file(path) != expected:
            failures.append(rel)
    if failures:
        die("校验和不匹配:\n  " + "\n  ".join(failures[:50]))
    info("校验和验证通过。")


def extract_archive(archive: pathlib.Path, destination: pathlib.Path) -> None:
    """`xz -dc | tar -x` 解开归档（仅用于 --verify 的自检）。"""
    destination.mkdir(parents=True, exist_ok=True)
    decomp = subprocess.Popen(["xz", "-dc", str(archive)], stdout=subprocess.PIPE)
    assert decomp.stdout is not None
    tarproc = subprocess.Popen(
        ["tar", "--numeric-owner", "-C", str(destination), "-xpf", "-"], stdin=decomp.stdout
    )
    decomp.stdout.close()
    rc_tar = tarproc.wait()
    rc_xz = decomp.wait()
    if rc_tar != 0 or rc_xz != 0:
        die(f"解压归档失败: {archive}")


def test_archive_integrity(archive: pathlib.Path) -> None:
    """用 `xz -t` 做一次快速的压缩流完整性检查。"""
    cp = run(["xz", "-t", str(archive)], check=False, capture=True)
    if cp.returncode != 0:
        die(f"归档完整性检查失败: {archive}\n{(cp.stderr or cp.stdout).strip()}")
    info("归档压缩流完整性检查通过。")


# --------------------------------------------------------------------------- #
# 备份计划
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class CaptureUnit:
    """一个备份单元：一组源路径 -> 归档内某个前缀。"""

    unit_id: str
    category: str                       # plugin | panel | system | server | site
    archive_prefix: str                 # 归档内相对路径前缀
    sources: List[Tuple[pathlib.Path, str]]  # (源路径, 相对 archive_prefix 的子路径)
    profile: str = PROFILE_STRICT
    excluded_paths: List[pathlib.Path] = dataclasses.field(default_factory=list)
    note: str = ""
    #: 细分语义标签，写入 manifest.units[].kind；留空时回落为 category。
    #: 目前使用的取值：wwwroot / cron-scripts。
    kind: str = ""
    #: 该单元的主源路径（manifest.units[].source_path），留空时取第一个源。
    source_path: str = ""


@dataclasses.dataclass
class BackupPlan:
    """完整备份计划，list 与 backup 共用。"""

    panel_root: Optional[pathlib.Path] = None
    layout: Optional[PanelLayout] = None
    plugins: List[PluginPlan] = dataclasses.field(default_factory=list)
    units: List[CaptureUnit] = dataclasses.field(default_factory=list)
    system_dirs: List[str] = dataclasses.field(default_factory=list)
    etc_allowlist: List[pathlib.Path] = dataclasses.field(default_factory=list)
    db_targets: List[str] = dataclasses.field(default_factory=list)
    threads: int = 1
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    include_home: bool = False
    #: --wwwroot 是否启用（启用但未能定位站点目录时 site_root 仍为 None）。
    include_wwwroot: bool = False
    #: 实际采集的业务站点根目录。
    site_root: Optional[pathlib.Path] = None
    #: 实际采集的计划任务脚本目录（serverDir/cron）。
    cron_dir: Optional[pathlib.Path] = None
    #: 面板根目录探测手段（candidates / cwd_parents / glob_scan / explicit）。
    panel_discovery_method: Optional[str] = None
    #: 是否使用 xz -9e 极限压缩（--max-compress）。
    max_compress: bool = False


#: 面板 data 目录中的关键文件（用于 manifest 记录其是否存在）。
PANEL_DATA_KEY_FILES: Tuple[str, ...] = (
    "panel.db", "default.pl", "port.pl", "ipv6.pl", "language.pl",
    "restart.pl", "panel_speed.pl", "system.db", "iplist.txt",
)
#: serverDir 根目录下的共享配置/元数据文件模式（如 mysql.db）。
SERVER_ROOT_META_GLOBS: Tuple[str, ...] = ("*.db", "*.pl", "*.json", "*.conf")

#: 面板探测手段的中文标签映射。
_METHOD_LABELS: Dict[str, str] = {
    "explicit": "显式指定",
    "candidates": "候选列表命中",
    "cwd_parents": "从当前目录向上回溯",
    "glob_scan": "自动扫描发现",
}


def build_backup_plan(args: argparse.Namespace) -> BackupPlan:
    """把命令行参数解析成一份完整的、可打印也可执行的备份计划。"""
    plan = BackupPlan()
    plan.threads = compute_threads(args.threads)
    plan.max_compress = getattr(args, "max_compress", False)
    plan.max_file_bytes = int(args.max_file_size) * 1024 * 1024 if args.max_file_size else 0
    plan.include_home = not args.no_home
    # --wwwroot 支持三态：未给出 -> None（关闭）；给出但无值 -> ""（自动解析）；
    # 给出绝对路径 -> 直接使用。用 getattr 兼容不带该字段的调用方（如既有测试桩）。
    wwwroot_arg = getattr(args, "wwwroot", None)
    plan.include_wwwroot = wwwroot_arg is not None

    # --- 面板 ------------------------------------------------------------- #
    panel_root = discover_panel_root(args.panel_root)
    plan.panel_discovery_method = _panel_discovery_method
    if panel_root is None:
        warn("未探测到 mdserver-web 面板安装目录，将仅备份系统目录。可用 --panel-root 显式指定。")
    else:
        # 根据探测方式定制日志
        label = _METHOD_LABELS.get(_panel_discovery_method, "")
        if label:
            info(f"面板根目录（{label}）: {panel_root}")
        else:
            info(f"面板根目录: {panel_root}")
        layout = PanelLayout(panel_root)
        plan.panel_root = panel_root
        plan.layout = layout

        # 面板自身状态：data/ 与 ssl/
        panel_sources: List[Tuple[pathlib.Path, str]] = []
        if layout.data_dir.is_dir():
            panel_sources.append((layout.data_dir, "data"))
        if layout.ssl_dir.is_dir():
            panel_sources.append((layout.ssl_dir, "ssl"))
        if panel_sources:
            plan.units.append(CaptureUnit(
                unit_id="panel-state",
                category="panel",
                archive_prefix="mw-server/panel",
                sources=panel_sources,
                profile=PROFILE_CONFIG,
                note="面板主库、运行参数与 SSL 证书",
            ))

        # serverDir 级共享内容：web_conf 与根目录下的元数据文件
        server_sources: List[Tuple[pathlib.Path, str]] = []
        web_conf = layout.server_dir / "web_conf"
        if web_conf.is_dir():
            server_sources.append((web_conf, "web_conf"))
        if layout.server_dir.is_dir():
            for pattern in SERVER_ROOT_META_GLOBS:
                for path in _sorted_glob(layout.server_dir, pattern):
                    if path.is_file():
                        server_sources.append((path, path.name))
        if server_sources:
            plan.units.append(CaptureUnit(
                unit_id="panel-server-shared",
                category="server",
                archive_prefix="mw-server/panel/server",
                sources=server_sources,
                profile=PROFILE_CONFIG,
                note="serverDir 级共享配置（nginx vhost / php 处理器 / 插件元数据库）",
            ))

        # 计划任务脚本：serverDir/cron（不是插件，由专用单元采集）
        cron_dir = layout.server_dir / SERVER_CRON_DIR_NAME
        if cron_dir.is_dir():
            plan.cron_dir = cron_dir
            plan.units.append(CaptureUnit(
                unit_id="panel-cron",
                category="server",
                kind="cron-scripts",
                archive_prefix="mw-server/cron",
                source_path=str(cron_dir),
                sources=[(cron_dir, "")],
                # 脚本多为无扩展名/.sh 文本，用 config profile 避免被 strict 的
                # 文件名规则误伤；执行日志单独排除。
                profile=PROFILE_CONFIG,
                excluded_paths=cron_log_paths(cron_dir),
                note="面板计划任务脚本（serverDir/cron，已排除执行日志）",
            ))
        else:
            info(f"未发现计划任务目录，跳过: {cron_dir}")

        # 已安装插件
        names = discover_installed_plugins(
            layout.server_dir,
            layout.plugin_dir,
            exclude_names={layout.panel_dir.name},
        )
        if not names:
            warn(f"在 {layout.server_dir} 下没有发现任何已安装插件。")
        for name in names:
            plugin_plan = plugin_capture_plan(layout.server_dir, name)
            plan.plugins.append(plugin_plan)
            if plugin_plan.db_engine:
                plan.db_targets.append(name)
            if plugin_plan.is_empty():
                continue
            db_note = f"（数据库经命令导出: {plugin_plan.db_engine}）" if plugin_plan.db_engine else ""
            # 配置路径是显式挑选出来的，用宽松的 config profile，避免误伤；
            # 数据目录用 strict profile，剔除缓存与构建中间物。
            if plugin_plan.config_paths:
                plan.units.append(CaptureUnit(
                    unit_id=f"plugin:{name}:config",
                    category="plugin",
                    archive_prefix=f"mw-server/plugins/{safe_slug(name)}",
                    sources=[
                        (path, "config/" + _relative_under(plugin_plan.base_dir, path))
                        for path in plugin_plan.config_paths
                    ],
                    profile=PROFILE_CONFIG,
                    excluded_paths=list(plugin_plan.excluded_paths),
                    note=f"插件 {name} 配置{db_note}",
                ))
            if plugin_plan.data_paths:
                plan.units.append(CaptureUnit(
                    unit_id=f"plugin:{name}:data",
                    category="plugin",
                    archive_prefix=f"mw-server/plugins/{safe_slug(name)}",
                    sources=[
                        # data_paths 的相对路径本身就以 data/ 开头，无需再加前缀。
                        (path, _relative_under(plugin_plan.base_dir, path))
                        for path in plugin_plan.data_paths
                    ],
                    profile=PROFILE_STRICT,
                    excluded_paths=list(plugin_plan.excluded_paths),
                    note=f"插件 {name} 数据",
                ))

    # --- 业务站点数据（--wwwroot，默认开启） ------------------------------- #
    # 放在面板块之外：即使没探测到面板，只要 --wwwroot 带了绝对路径也能采集。
    if plan.include_wwwroot:
        site_root = resolve_site_root(plan.layout, wwwroot_arg)
        if site_root is None:
            warn(
                "--wwwroot 已启用，但未能确定站点根目录。"
                "请用 --panel-root 指定面板安装根，或用 --wwwroot <绝对路径> 直接指定站点目录。"
            )
        else:
            plan.site_root = site_root
            info(f"业务站点根目录: {site_root}")
            plan.units.append(CaptureUnit(
                unit_id="wwwroot",
                category="site",
                kind="wwwroot",
                archive_prefix="wwwroot",
                source_path=str(site_root),
                sources=[(site_root, "")],
                # 与插件 data 采集一致：保留普通文件，剔除缓存/构建产物/
                # node_modules/.git 等二进制与中间物。
                profile=PROFILE_STRICT,
                note=f"业务站点数据 {site_root}（已排除缓存/构建产物/node_modules/.git）",
            ))

    # --- 系统目录 --------------------------------------------------------- #
    for name in ("root", "opt"):
        path = pathlib.Path("/") / name
        if not path.is_dir():
            warn(f"系统目录不存在，跳过: {path}")
            continue
        plan.system_dirs.append(str(path))
        plan.units.append(CaptureUnit(
            unit_id=f"system:{name}",
            category="system",
            archive_prefix=f"file/{name}",
            sources=[(path, "")],
            profile=PROFILE_STRICT,
            note=f"系统目录 {path}（已排除二进制与中间文件）",
        ))

    if plan.include_home:
        home = pathlib.Path("/home")
        if home.is_dir():
            plan.system_dirs.append(str(home))
            plan.units.append(CaptureUnit(
                unit_id="system:home",
                category="system",
                archive_prefix="file/home",
                sources=[(home, "")],
                profile=PROFILE_STRICT,
                note="系统目录 /home（默认备份；--no-home 可关闭）",
            ))
        else:
            warn("/home 在本机不存在，跳过。")

    # /etc 采用白名单
    plan.etc_allowlist = build_etc_allowlist(
        args.etc_allowlist_extra,
        autodetect_services=not getattr(args, "no_service_autodetect", False),
    )
    if plan.etc_allowlist:
        plan.system_dirs.append("/etc")
        etc_sources = [(path, _relative_under(pathlib.Path("/etc"), path)) for path in plan.etc_allowlist]
        plan.units.append(CaptureUnit(
            unit_id="system:etc",
            category="system",
            archive_prefix="file/etc",
            sources=etc_sources,
            profile=PROFILE_CONFIG,
            note="/etc 白名单：网络、用户/账户、用户自建服务配置",
        ))
    else:
        warn("/etc 白名单为空（当前环境下没有命中任何路径）。")

    return plan


def unit_entries(
    unit: CaptureUnit,
    max_file_bytes: int,
    extra_excluded: Sequence[pathlib.Path] = (),
) -> Iterator[CaptureEntry]:
    """
    把一个备份单元的所有源路径展开成归档条目。

    `extra_excluded` 用于传入暂存目录与输出归档路径，防止备份把自己
    （位于 /opt、/root 等被采集目录下时）递归打包进去。
    """
    prefix = pathlib.PurePosixPath(unit.archive_prefix)
    excluded = list(unit.excluded_paths) + list(extra_excluded)
    for source, sub in unit.sources:
        rel = prefix / sub if sub else prefix
        for entry in iter_capture(
            source,
            str(rel),
            profile=unit.profile,
            max_file_bytes=max_file_bytes,
            excluded_paths=excluded,
        ):
            yield entry


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #


def host_metadata() -> Dict[str, Any]:
    """收集主机侧的环境信息，便于事后追溯。"""
    meta: Dict[str, Any] = {
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
    }
    uname = getattr(os, "uname", None)
    if uname is not None:
        info_tuple = uname()
        meta["machine"] = info_tuple.machine
        meta["kernel"] = info_tuple.release
        meta["system"] = info_tuple.sysname
    os_release = pathlib.Path("/etc/os-release")
    if os_release.is_file():
        meta["os_release"] = _read_text_safe(os_release).strip()
    meta["cpu_count"] = os.cpu_count() or 1
    mem = read_mem_total_mib()
    if mem:
        meta["mem_total_mib"] = mem
    return meta


def build_manifest(
    plan: BackupPlan,
    unit_stats: Dict[str, StageStats],
    dumps: Sequence[DumpResult],
    created_at: str,
    file_count: int,
    threads: int,
) -> Dict[str, Any]:
    """
    组装 manifest.json。

    纯数据组装，不触碰文件系统（除 host_metadata 外），便于单测。
    """
    total_bytes = sum(stats.bytes for stats in unit_stats.values())
    manifest: Dict[str, Any] = {
        "format": "mw-backup",
        "format_version": 1,
        "tool_version": VERSION,
        "created_at": created_at,
        "timestamp": created_at,
        "host": host_metadata(),
        "hostname": socket.gethostname(),
        "panel_root": str(plan.panel_root) if plan.panel_root else None,
        "panel_discovery_method": plan.panel_discovery_method,
        "panel_layout": plan.layout.as_dict() if plan.layout else None,
        "panel_data_key_files": {},
        "detected_plugins": [],
        "db_list": [],
        "system_dirs": list(plan.system_dirs),
        "etc_allowlist": [str(p) for p in plan.etc_allowlist],
        "include_home": plan.include_home,
        "include_wwwroot": plan.include_wwwroot,
        "site_root": str(plan.site_root) if plan.site_root else None,
        "cron_dir": str(plan.cron_dir) if plan.cron_dir else None,
        "compression": {
            "algorithm": "xz",
            "level": "-9e" if plan.max_compress else XZ_LEVEL,
            "threads": threads,
            "tar_flags": tar_flags_for_create(),
        },
        "limits": {
            "max_file_bytes": plan.max_file_bytes,
        },
        "units": [],
        "totals": {
            "file_count": file_count,
            "bytes_uncompressed": total_bytes,
        },
        "excluded_by_design": [
            "二进制文件与安装包（bin/lib/include/share、*.so、*.tar.*、*.deb 等）",
            "构建与缓存中间产物（node_modules、__pycache__、.git、build、dist、logs、cache 等）",
            "数据库原始数据目录（改用 mysqldump / pg_dumpall / redis-cli --rdb 导出）",
            "计划任务执行日志（serverDir/cron/*.log，脚本本身已备份）",
            (
                "业务站点数据 wwwroot（本次已采集）"
                if plan.site_root
                else "业务站点数据 wwwroot（本次未采集；使用 --no-wwwroot 关闭，或站点目录无法定位）"
            ),
        ],
    }

    if plan.layout is not None:
        for name in PANEL_DATA_KEY_FILES:
            manifest["panel_data_key_files"][name] = (plan.layout.data_dir / name).is_file()

    dumps_by_plugin: Dict[str, List[DumpResult]] = {}
    for d in dumps:
        dumps_by_plugin.setdefault(d.plugin, []).append(d)
    for plugin_plan in plan.plugins:
        plugin_dumps = dumps_by_plugin.get(plugin_plan.name, [])
        record = plugin_plan.as_dict()
        record["db_dumped"] = any(d.ok for d in plugin_dumps)
        manifest["detected_plugins"].append(record)

    for dump in dumps:
        manifest["db_list"].append(dump.as_dict())

    for unit in plan.units:
        stats = unit_stats.get(unit.unit_id, StageStats())
        manifest["units"].append({
            "id": unit.unit_id,
            "category": unit.category,
            "kind": unit.kind or unit.category,
            "archive_prefix": unit.archive_prefix,
            "profile": unit.profile,
            "note": unit.note,
            "source_path": unit.source_path or (str(unit.sources[0][0]) if unit.sources else ""),
            "sources": [str(src) for src, _sub in unit.sources],
            "stats": stats.as_dict(),
        })

    return manifest


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #


def print_plan(plan: BackupPlan) -> None:
    """打印 dry-run 预览。"""
    print("=" * 72)
    print(f"mw_backup {VERSION} — 备份内容预览（dry-run，不会写任何归档）")
    print("=" * 72)
    print(f"主机名          : {socket.gethostname()}")
    print(f"面板根目录      : {plan.panel_root or '(未探测到)'}")
    if plan.layout is not None:
        print(f"fatherDir       : {plan.layout.father_dir}")
        print(f"serverDir       : {plan.layout.server_dir}")
    print(f"计划任务脚本    : {plan.cron_dir or '(未发现 serverDir/cron)'}")
    if plan.include_wwwroot:
        print(f"业务站点根目录  : {plan.site_root or '(未能确定，见上方告警)'}")
    else:
        print("业务站点根目录  : (已关闭；去掉 --no-wwwroot 可重新采集)")
    print(f"压缩            : xz {'-9e' if plan.max_compress else XZ_LEVEL} -T{plan.threads}")
    print(f"单文件上限      : {human_bytes(plan.max_file_bytes) if plan.max_file_bytes else '不限制'}")
    print(f"包含 /home      : {'是' if plan.include_home else '否'}")
    print()

    if plan.plugins:
        print(f"已探测到 {len(plan.plugins)} 个已安装插件:")
        for plugin_plan in plan.plugins:
            marker = f"  [DB:{plugin_plan.db_engine}]" if plugin_plan.db_engine else ""
            print(f"  - {plugin_plan.name}{marker}")
        print()

    if plan.db_targets:
        print("将通过命令导出的数据库插件: " + ", ".join(plan.db_targets))
        print()

    total_files = 0
    total_bytes = 0
    print(f"{'归档内路径':<34} {'文件数':>8} {'原始大小':>12}  说明")
    print("-" * 72)
    for unit in plan.units:
        stats = summarize_entries(unit_entries(unit, plan.max_file_bytes))
        total_files += stats.files
        total_bytes += stats.bytes
        print(f"{unit.archive_prefix + '/':<34} {stats.files:>8} {human_bytes(stats.bytes):>12}  {unit.note}")
    print("-" * 72)
    print(f"{'合计':<34} {total_files:>8} {human_bytes(total_bytes):>12}")
    print()
    print(f"/etc 白名单命中 {len(plan.etc_allowlist)} 条路径:")
    for path in plan.etc_allowlist:
        print(f"  - {path}")
    if not plan.etc_allowlist:
        print("  (无)")
    print()
    structure = ["manifest.json", "checksums.sha256", "mw-server/plugins/", "databases/", "file/", "mw-server/panel/"]
    if plan.cron_dir is not None:
        structure.append("mw-server/cron/")
    if plan.site_root is not None:
        structure.append("wwwroot/")
    print("归档内部结构: " + " / ".join(structure))


def list_command(args: argparse.Namespace) -> None:
    """预览将要备份的内容，不产生任何归档。"""
    if current_uid() not in (0, -1):
        warn("当前不是 root，部分路径可能读不到，预览结果会偏小。")
    plan = build_backup_plan(args)
    print_plan(plan)


def backup_command(args: argparse.Namespace) -> None:
    """执行备份，产出单一 .tar.xz 归档。"""
    if args.dry_run:
        list_command(args)
        return

    require_root()
    require_commands("tar", "xz")

    plan = build_backup_plan(args)

    xz_level = XZ_LEVEL
    if plan.max_compress:
        xz_level = "-9e"
        info(f"压缩参数: xz {xz_level} -T{plan.threads}（极限压缩，较慢）")
    else:
        info(f"压缩参数: xz {xz_level} -T{plan.threads}")

    stamp = now_stamp()
    hostname = socket.gethostname()
    default_name = f"mw-backup-{safe_slug(hostname)}-{stamp}.tar.xz"
    output = pathlib.Path(args.output or default_name).expanduser()
    if output.is_dir():
        output = output / default_name
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not args.force:
        die(f"输出文件已存在: {output}（使用 --force 覆盖）")

    # 终端交互环境下给用户确认机会（非交互/shell 重定向时跳过）
    if sys.stdin.isatty():
        print()
        print("=== 备份确认 ===")
        print()
        # 面板路径与探测方式
        if plan.panel_root is not None:
            method_label = _METHOD_LABELS.get(plan.panel_discovery_method or "", "")
            suffix = f" ({method_label})" if method_label else ""
            print(f"面板: {plan.panel_root}{suffix}")
        else:
            print("面板: (未探测到)")
        # 已安装插件
        if plan.plugins:
            print(f"已安装插件 ({len(plan.plugins)} 个):")
            for plugin_plan in plan.plugins:
                marker = f" [DB:{plugin_plan.db_engine}]" if plugin_plan.db_engine else ""
                print(f"  - {plugin_plan.name}{marker}")
        else:
            print("已安装插件: (无)")
        print()
        # 采集单元
        if plan.units:
            print(f"采集单元 ({len(plan.units)} 个):")
            for unit in plan.units:
                print(f"  {unit.archive_prefix:<30} {unit.note}")
        else:
            print("采集单元: (无)")
        print()
        # wwwroot / home / 输出 / 压缩
        if plan.include_wwwroot:
            wwwroot_label = str(plan.site_root) if plan.site_root else "(未能确定)"
            print(f"/wwwroot: {wwwroot_label}")
        else:
            print("/wwwroot: 否")
        print(f"/home: {'是' if plan.include_home else '否'}")
        print(f"输出: {output}")
        print(f"压缩: xz {xz_level} -T{plan.threads}")
        print()
        try:
            input("按回车键开始备份（Ctrl+C 取消）... ")
        except (EOFError, KeyboardInterrupt):
            print()
            info("已取消。")
            return

    work_parent = pathlib.Path(args.work_dir).expanduser() if args.work_dir else pathlib.Path("/var/tmp")
    work_parent.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"mw-backup-{stamp}-", dir=str(work_parent)))
    # 归一化为规范绝对路径，与 output.resolve() 对称：当 --work-dir 指向站点内
    # 某软链接路径时，self_paths 排除才能精确命中（iter_capture 用 normpath 精确匹配）。
    work = work.resolve()
    info(f"暂存目录: {work}")

    try:
        # 1) 数据库导出（优先于文件采集，保证 dump 与配置尽量同一时刻）
        dumps = run_db_dumps(plan.plugins, work)

        # 2) 逐单元采集文件（排除暂存目录与输出归档自身，避免自包含）
        self_paths = [work, output]
        unit_stats: Dict[str, StageStats] = {}
        for unit in plan.units:
            info(f"采集 {unit.archive_prefix}/ — {unit.note}")
            stats = stage_entries(unit_entries(unit, plan.max_file_bytes, self_paths), work)
            unit_stats[unit.unit_id] = stats
            if stats.errors:
                warn(f"  {len(stats.errors)} 个路径读取失败（已记入 manifest），示例: {stats.errors[0]}")
            info(f"  {stats.files} 个文件 / {human_bytes(stats.bytes)}")

        # 3) manifest + 校验和
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        total_files = sum(s.files for s in unit_stats.values()) + sum(1 for d in dumps if d.ok)
        manifest = build_manifest(plan, unit_stats, dumps, created_at, total_files, plan.threads)
        json_dump(work / "manifest.json", manifest)
        info("生成 checksums.sha256 …")
        checksum_count = write_checksums(work)
        info(f"已为 {checksum_count} 个文件生成校验和。")

        # 4) 打包
        if output.exists():
            output.unlink()
        info(f"创建最终归档: {output}")
        xz_compress_pipeline(
            ["tar", *tar_flags_for_create(), "-C", str(work), "-cpf", "-", "."],
            output,
            xz_level,
            plan.threads,
        )
        test_archive_integrity(output)

        if args.verify:
            info("执行完整校验（解包并逐文件比对 sha256）…")
            verify_dir = pathlib.Path(tempfile.mkdtemp(prefix="mw-backup-verify-", dir=str(work_parent)))
            try:
                extract_archive(output, verify_dir)
                verify_checksums(verify_dir)
            finally:
                shutil.rmtree(str(verify_dir), ignore_errors=True)

        size = output.stat().st_size
        info(f"备份完成: {output}")
        info(f"归档大小: {human_bytes(size)}（原始 {human_bytes(manifest['totals']['bytes_uncompressed'])}）")
        failed = [d for d in dumps if not d.ok]
        if failed:
            warn("以下数据库导出未成功，请人工确认: " + ", ".join(f"{d.plugin}({d.engine})" for d in failed))
    finally:
        if args.keep_workdir:
            warn(f"按要求保留暂存目录: {work}")
        else:
            shutil.rmtree(str(work), ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """backup 与 list 共享的参数。"""
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="交互模式：通过问答式提示完成备份配置，无需手搓参数")
    parser.add_argument("--panel-root", help="mdserver-web 安装根目录；省略则自动探测")
    parser.add_argument("--no-home", action="store_true", dest="no_home", default=False,
                        help="不备份 /home（默认备份）")
    parser.add_argument(
        "--wwwroot",
        nargs="?",
        const="",
        default="",
        metavar="PATH",
        help=(
            "备份业务站点数据目录（默认开启，按面板 site_path 选项自动解析站点根，"
            "默认 <fatherDir>/wwwroot）；需要时可 --wwwroot <绝对路径> 直接指定站点目录"
        ),
    )
    parser.add_argument(
        "--no-wwwroot",
        dest="wwwroot",
        action="store_const",
        const=None,
        default="",
        help="不备份业务站点数据",
    )
    parser.add_argument(
        "--etc-allowlist-extra",
        action="append",
        default=[],
        metavar="PATH",
        help="追加 /etc 白名单路径，可重复指定",
    )
    parser.add_argument("--threads", type=int, default=None, help="xz 压缩线程数；默认按 CPU 与内存自动计算")
    parser.add_argument("--max-compress", action="store_true", help="使用 xz -9e 极限压缩（更小但更慢，适合长期归档）")
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES // (1024 * 1024),
        metavar="MiB",
        help="单文件体积上限（MiB），超过则跳过并记入 manifest；0 表示不限制",
    )
    parser.add_argument(
        "--no-service-autodetect",
        action="store_true",
        help="关闭 /etc 白名单的 systemd 自建服务自动探测（仅用硬编码白名单 + --etc-allowlist-extra）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mw_backup.py",
        description="mdserver-web 面板与系统关键配置/数据备份工具（仅备份，不含还原）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 预览将要备份的内容（不写归档）
  sudo python3 mw_backup.py list

  # 执行备份，输出到当前目录（默认全量：面板 + 系统 + 站点 + /home）
  sudo python3 mw_backup.py backup

  # 不备份 /home
  sudo python3 mw_backup.py backup --no-home

  # 指定面板根目录、输出到指定文件
  sudo python3 mw_backup.py backup --panel-root /www/server/mdserver-web -o /backup/mw.tar.xz

  # 追加 /etc 白名单路径并限制压缩线程
  sudo python3 mw_backup.py backup --etc-allowlist-extra /etc/myapp --threads 4

  # 使用极限压缩（xz -9e，更小但更慢，适合长期归档）
  sudo python3 mw_backup.py backup --max-compress

  # 业务站点数据默认备份；关闭则用 --no-wwwroot
  sudo python3 mw_backup.py backup --no-wwwroot

  # 站点目录不在默认位置、或没装面板时，直接指定绝对路径
  sudo python3 mw_backup.py backup --wwwroot /data/wwwroot

说明:
  * 数据库一律使用命令导出（mysqldump / pg_dumpall / redis-cli --rdb），不拷原始数据目录
  * 二进制包与中间文件（缓存、构建产物、node_modules、*.pyc、.git、版本 tar 包等）不打包
  * /etc 采用白名单策略，只备份网络、用户/账户与用户自建服务配置
  * 面板计划任务脚本（serverDir/cron）默认备份，其执行日志 *.log 不备份
  * 业务站点数据默认备份，不需要时用 --no-wwwroot 关闭
  * /home 默认备份，不需要时用 --no-home 关闭
  * 默认使用 xz -6 压缩（速度与体积平衡），--max-compress 切换为 xz -9e 极限压缩
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="执行备份，产出单一 .tar.xz 归档")
    add_common_arguments(p_backup)
    p_backup.add_argument("-o", "--output", help="输出归档路径（可为目录）；默认 ./mw-backup-<host>-<时间戳>.tar.xz")
    p_backup.add_argument("--work-dir", help="暂存父目录，默认 /var/tmp")
    p_backup.add_argument("--keep-workdir", action="store_true", help="保留暂存目录以便排查")
    p_backup.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    p_backup.add_argument("--verify", action="store_true", help="备份后解包并逐文件校验 sha256（较慢）")
    p_backup.add_argument("--dry-run", action="store_true", help="等同于 list：只预览不写归档")
    p_backup.set_defaults(func=backup_command)

    p_list = sub.add_parser("list", help="预览将要备份的内容（dry-run）")
    add_common_arguments(p_list)
    p_list.set_defaults(func=list_command)

    return parser


# --------------------------------------------------------------------------- #
# 交互模式
# --------------------------------------------------------------------------- #


def _yn_input(prompt_text: str, default: bool) -> bool:
    """Y/n 问答，返回 bool；空输入 = 接受默认值。Ctrl+C 直传。"""
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            raw = input(f"{prompt_text} {hint}: ").strip()
        except EOFError:
            # stdin closed（如管道），当做取消。
            return default
        if not raw:
            return default
        first = raw[0].lower()
        if first in ("y", "是"):
            return True
        if first in ("n", "否"):
            return False
        eprint(f"请输入 y/yes/是 或 n/no/否，直接回车接受默认值。")


def _path_input(
    prompt_text: str,
    default: str,
    must_exist: bool = True,
    check_is_dir: bool = True,
    max_retries: int = 3,
) -> str:
    """路径输入；可选的 3 次重试存在性校验，仍失败则回退默认。"""
    for attempt in range(1, max_retries + 1):
        raw = input(f"{prompt_text}: ").strip()
        if not raw:
            return default
        expanded = os.path.expanduser(raw)
        path = pathlib.Path(expanded)
        exists = path.is_dir() if check_is_dir else (path.is_file() or path.is_dir())
        if not must_exist or exists:
            return expanded
        if attempt < max_retries:
            eprint(f"  路径不存在，请重新输入（{attempt}/{max_retries}）: {path}")
        else:
            warn(f"  路径不存在，已回退为默认值: {default}")
            return default
    return default


def _int_input(prompt_text: str, default: Optional[int]) -> Optional[int]:
    """数字输入；转换失败时重试，空输入接受默认。"""
    default_display = str(default) if default is not None else "自动"
    hint = f"[{default_display}]"
    while True:
        raw = input(f"{prompt_text} {hint}: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
            if value < 1:
                eprint("  线程数必须 >= 1，请重新输入。")
                continue
            return value
        except ValueError:
            eprint("  请输入一个整数。")


def interactive_config(args: argparse.Namespace) -> Optional[argparse.Namespace]:
    """
    交互式问答收集备份配置。

    按顺序逐项提示，每项显示当前默认值（方括号标注），
    直接回车 = 接受默认，输入新值 = 覆盖。
    最后展示汇总并请用户确认；确认后返回修改后的 args，取消返回 None。
    """
    if not sys.stdin.isatty():
        die("当前环境不支持交互输入（stdin 不是 TTY），交互模式需要终端。")

    is_backup = getattr(args, "command", "") == "backup"

    print("=== mw_backup 交互配置 ===")
    print("（直接回车接受方括号内的默认值）")
    print()

    # --- 1. 面板根目录 ---------------------------------------------------- #
    default_panel = args.panel_root or "自动探测"
    panel_root = _path_input(
        f"  1. 面板安装根目录 [{default_panel}]",
        args.panel_root or "",
        must_exist=True,
        check_is_dir=True,
    )
    if panel_root:
        args.panel_root = panel_root

    # --- 2. 是否备份站点数据 ---------------------------------------------- #
    wwwroot_on = getattr(args, "wwwroot", "") is not None
    include_wwwroot = _yn_input("  2. 是否备份业务站点数据 (wwwroot)?", wwwroot_on)

    # --- 3. 站点根目录（仅选了 Y 才问） ----------------------------------- #
    if include_wwwroot:
        default_site = getattr(args, "wwwroot", "") or "自动按面板 site_path 解析"
        if default_site == "自动按面板 site_path 解析":
            # 当前 args.wwwroot 为空字符串 → 自动解析
            site_path = _path_input(
                f"  3. 站点根目录路径 [{default_site}]",
                "",
                must_exist=False,
                check_is_dir=True,
            )
            args.wwwroot = site_path if site_path else ""
        else:
            site_path = _path_input(
                f"  3. 站点根目录路径 [{default_site}]",
                default_site,
                must_exist=True,
                check_is_dir=True,
            )
            args.wwwroot = site_path if site_path else default_site
    else:
        args.wwwroot = None

    # --- 4. 是否备份 /home ------------------------------------------------ #
    include_home = _yn_input("  4. 是否备份 /home?", not args.no_home)
    args.no_home = not include_home

    # --- 5. 输出归档路径 -------------------------------------------------- #
    if is_backup:
        hostname = socket.gethostname()
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        default_output = args.output or f"./mw-backup-{safe_slug(hostname)}-{stamp}.tar.xz"
        output_path = _path_input(
            f"  5. 输出归档路径 [{default_output}]",
            default_output,
            must_exist=False,
            check_is_dir=False,
        )
        args.output = output_path

    # --- 6. 压缩线程数 ---------------------------------------------------- #
    threads_default: Optional[int] = args.threads
    threads = _int_input(f"  6. 压缩线程数", threads_default)
    args.threads = threads

    # --- 7. 校验 ---------------------------------------------------------- #
    verify_on = getattr(args, "verify", False)
    do_verify = _yn_input("  7. 备份后校验完整性?", verify_on)
    if hasattr(args, "verify"):
        args.verify = do_verify

    # --- 8. /etc 额外白名单 ----------------------------------------------- #
    existing_extra: List[str] = list(getattr(args, "etc_allowlist_extra", []) or [])
    default_extra = " ".join(existing_extra) if existing_extra else "(无)"
    raw = input(f"  8. 追加 /etc 白名单路径（多个用空格分隔，无则回车） [{default_extra}]: ").strip()
    if raw:
        args.etc_allowlist_extra = raw.split()
    else:
        args.etc_allowlist_extra = existing_extra

    # --- 汇总确认 --------------------------------------------------------- #
    print()
    print("=== 配置汇总 ===")
    print(f"   面板根目录: {args.panel_root or '自动探测'}")
    if include_wwwroot:
        site_display = args.wwwroot if args.wwwroot else "自动解析"
        print(f"   备份站点数据: 是 ({site_display})")
    else:
        print("   备份站点数据: 否")
    print(f"   备份 /home: {'是' if not args.no_home else '否'}")
    if is_backup:
        print(f"   输出归档: {args.output}")
    print(f"   压缩线程: {args.threads if args.threads else '自动'}")
    print(f"   校验: {'是' if getattr(args, 'verify', False) else '否'}")
    etc_display = " ".join(args.etc_allowlist_extra) if args.etc_allowlist_extra else "(无)"
    print(f"   /etc 额外白名单: {etc_display}")
    print()

    confirmed = _yn_input("确认开始?", True)
    if not confirmed:
        print("已取消。")
        return None

    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if getattr(args, 'interactive', False):
            args = interactive_config(args)
            if args is None:
                return 0
        args.func(args)
        return 0
    except KeyboardInterrupt:
        eprint("\n已取消。")
        return 130
    except BackupError as exc:
        eprint(f"错误: {exc}")
        return 1
    except Exception as exc:
        # 兜底：任何未预期异常都转为受控退出，避免裸 traceback 让整个备份中止。
        eprint(f"未预期错误: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
