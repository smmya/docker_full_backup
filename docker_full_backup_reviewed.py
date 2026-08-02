#!/usr/bin/env python3
"""
Portable Docker container backup/restore for Linux.

Designed for Debian/Ubuntu and Python 3.8+, with Ubuntu 20.04.6 LTS x86_64
as the compatibility baseline. No third-party Python packages are required.

What is backed up:
  * original images and per-container snapshot images (writable layer included)
  * docker inspect metadata and original running state
  * bind mounts, named volumes, and anonymous volumes
  * user-defined network definitions
  * Docker Compose config files discoverable from Compose labels
  * container log files (optional, enabled by default; archived only)

Important limitations:
  * Running process/RAM/checkpoint state is not portable and is not restored.
  * Historical Docker logs are archived for reference but cannot be injected back
    into Docker's active log driver on restore.
  * Non-local volume drivers may require driver-specific backup procedures.
"""

import argparse
import copy
import datetime as dt
import errno
import hashlib
import http.client
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
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

VERSION = "1.2.0"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
DEFAULT_FINAL_XZ_LEVEL = "-6"
MAX_DATA_XZ_LEVEL = "-9e"
DEFAULT_XZ_THREADS = "auto"
XZ_AUTO_RESERVE_MIB = 512
XZ_AUTO_MIN_LIMIT_MIB = 256


class BackupError(RuntimeError):
    pass


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)


def info(msg: str) -> None:
    print(f"[+] {msg}", flush=True)


def warn(msg: str) -> None:
    eprint(f"[!] {msg}")


def die(msg: str, code: int = 1) -> None:
    raise BackupError(msg)


def safe_slug(value: str, max_len: int = 80) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "-", value)
    value = value.strip("-._") or "item"
    return value[:max_len]


def json_dump(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def json_load(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def run(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture: bool = False,
    text: bool = True,
    input_data: Optional[Any] = None,
    cwd: Optional[pathlib.Path] = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(cmd),
            check=check,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=text,
            input=input_data,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        die(f"Command not found: {cmd[0]}")
    except subprocess.CalledProcessError as exc:
        if capture:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout
            die(f"Command failed ({exc.returncode}): {shlex.join(list(cmd))}\n{detail}")
        die(f"Command failed ({exc.returncode}): {shlex.join(list(cmd))}")
    raise AssertionError("unreachable")


def run_json(cmd: Sequence[str]) -> Any:
    cp = run(cmd, capture=True)
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as exc:
        die(f"Invalid JSON from command: {shlex.join(list(cmd))}: {exc}")


def require_root() -> None:
    if os.geteuid() != 0:
        die("Please run as root (sudo). Root is required to preserve owners, ACLs, xattrs, and volume data.")


def require_commands(*commands: str) -> None:
    missing = [c for c in commands if shutil.which(c) is None]
    if missing:
        hint = "sudo apt-get update && sudo apt-get install -y docker.io xz-utils tar"
        die(f"Missing required commands: {', '.join(missing)}\nUbuntu/Debian install example: {hint}")


def docker_ready() -> None:
    cp = run(["docker", "info"], check=False, capture=True)
    if cp.returncode != 0:
        die(f"Docker daemon is not available:\n{(cp.stderr or cp.stdout).strip()}")


def list_containers() -> List[Dict[str, str]]:
    fmt = "{{json .}}"
    cp = run(["docker", "ps", "-a", "--no-trunc", "--format", fmt], capture=True)
    rows: List[Dict[str, str]] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def resolve_containers(identifiers: Sequence[str], all_flag: bool, interactive: bool = True) -> List[Dict[str, Any]]:
    rows = list_containers()
    if not rows:
        die("No Docker containers found.")

    selected_ids: List[str] = []
    if all_flag:
        selected_ids = [r["ID"] for r in rows]
    elif identifiers:
        for ident in identifiers:
            data = run_json(["docker", "inspect", "--type", "container", ident])
            if not data:
                die(f"Container not found: {ident}")
            selected_ids.append(data[0]["Id"])
    elif interactive:
        print("\nAvailable containers:")
        for idx, row in enumerate(rows, 1):
            print(f"  {idx:>3}. {row.get('Names','')}  {row.get('Image','')}  {row.get('Status','')}")
        print("    a. all containers")
        raw = input("Select numbers separated by commas, or 'a' [a]: ").strip().lower() or "a"
        if raw in ("a", "all", "*"):
            selected_ids = [r["ID"] for r in rows]
        else:
            indices: Set[int] = set()
            for token in re.split(r"[ ,]+", raw):
                if not token:
                    continue
                if "-" in token:
                    start_s, end_s = token.split("-", 1)
                    start, end = int(start_s), int(end_s)
                    indices.update(range(start, end + 1))
                else:
                    indices.add(int(token))
            for idx in sorted(indices):
                if idx < 1 or idx > len(rows):
                    die(f"Invalid selection: {idx}")
                selected_ids.append(rows[idx - 1]["ID"])
    else:
        die("Specify container names/IDs or use --all.")

    seen: Set[str] = set()
    result: List[Dict[str, Any]] = []
    for cid in selected_ids:
        data = run_json(["docker", "inspect", "--type", "container", cid])
        if not data:
            continue
        item = data[0]
        if item["Id"] not in seen:
            result.append(item)
            seen.add(item["Id"])
    return result


_XZ_AUTO_PROFILE: Optional[Tuple[int, int, int]] = None
_XZ_AUTO_PROFILE_LOGGED = False


def available_cpu_count() -> int:
    """Return CPUs available to this process, respecting CPU affinity when possible."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def linux_mem_available_mib() -> int:
    """Read MemAvailable from /proc/meminfo, falling back to MemFree."""
    values: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="ascii") as fh:
            for line in fh:
                key, sep, rest = line.partition(":")
                if not sep:
                    continue
                fields = rest.strip().split()
                if fields and fields[0].isdigit():
                    values[key] = int(fields[0])  # Linux reports these values in KiB.
    except OSError:
        return 0
    kib = values.get("MemAvailable") or values.get("MemFree") or 0
    return max(0, kib // 1024)


def xz_auto_profile() -> Tuple[int, int, int]:
    """Return (CPU count, available MiB, safe xz compression limit MiB)."""
    global _XZ_AUTO_PROFILE
    if _XZ_AUTO_PROFILE is not None:
        return _XZ_AUTO_PROFILE

    cpus = available_cpu_count()
    available_mib = linux_mem_available_mib()
    if available_mib <= 0:
        # Let xz use its own system-specific soft limit if MemAvailable cannot be read.
        limit_mib = 0
    else:
        # Keep at least 512 MiB free where possible, and never hand xz more than
        # 75% of currently available RAM. xz may reduce its worker count further
        # for memory-heavy presets such as -9e.
        by_percentage = max(XZ_AUTO_MIN_LIMIT_MIB, available_mib * 3 // 4)
        by_reserve = max(XZ_AUTO_MIN_LIMIT_MIB, available_mib - XZ_AUTO_RESERVE_MIB)
        limit_mib = min(by_percentage, by_reserve)
        limit_mib = min(limit_mib, available_mib)

    _XZ_AUTO_PROFILE = (cpus, available_mib, limit_mib)
    return _XZ_AUTO_PROFILE


def build_xz_command(level: str, threads: str) -> List[str]:
    global _XZ_AUTO_PROFILE_LOGGED
    value = str(threads).strip().lower()
    if value in ("auto", "0"):
        cpus, available_mib, limit_mib = xz_auto_profile()
        if not _XZ_AUTO_PROFILE_LOGGED:
            if limit_mib:
                info(
                    f"XZ auto tuning: CPUs={cpus}, MemAvailable={available_mib} MiB, "
                    f"compression memory limit={limit_mib} MiB"
                )
            else:
                info(f"XZ auto tuning: CPUs={cpus}, memory limit delegated to xz")
            _XZ_AUTO_PROFILE_LOGGED = True
        cmd = ["xz", level, "-T0"]
        if limit_mib:
            cmd.append(f"--memlimit-compress={limit_mib}MiB")
        cmd.append("-c")
        return cmd

    try:
        count = int(value)
    except ValueError:
        die("--xz-threads must be 'auto', 0, or a positive integer.")
    if count < 1:
        die("--xz-threads must be 'auto', 0, or a positive integer.")
    return ["xz", level, f"-T{count}", "-c"]


def xz_compress_pipeline(producer_cmd: Sequence[str], output_path: pathlib.Path, level: str, threads: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    info(f"Writing {output_path}")
    xz_cmd = build_xz_command(level, threads)
    with output_path.open("wb") as out:
        producer = subprocess.Popen(list(producer_cmd), stdout=subprocess.PIPE)
        assert producer.stdout is not None
        compressor = subprocess.Popen(xz_cmd, stdin=producer.stdout, stdout=out)
        producer.stdout.close()
        rc_comp = compressor.wait()
        rc_prod = producer.wait()
    if rc_prod != 0 or rc_comp != 0:
        output_path.unlink(missing_ok=True)
        die(
            f"Compression pipeline failed: {shlex.join(list(producer_cmd))} | "
            f"{shlex.join(xz_cmd)}"
        )


def tar_flags_for_create() -> List[str]:
    # GNU tar on Ubuntu 20.04 supports these. --selinux is intentionally omitted
    # because it can emit noisy warnings on hosts without SELinux labels.
    return ["--numeric-owner", "--xattrs", "--acls", "--sparse"]


def tar_flags_for_extract() -> List[str]:
    return ["--numeric-owner", "--xattrs", "--acls", "--same-owner", "--same-permissions", "--sparse"]


def archive_directory_contents(source: pathlib.Path, output: pathlib.Path, threads: str) -> None:
    if not source.is_dir():
        die(f"Expected directory: {source}")
    cmd = ["tar", *tar_flags_for_create(), "-C", str(source), "-cpf", "-", "."]
    xz_compress_pipeline(cmd, output, MAX_DATA_XZ_LEVEL, threads)


def archive_single_path(source: pathlib.Path, output: pathlib.Path, threads: str) -> None:
    parent = source.parent
    name = source.name
    cmd = ["tar", *tar_flags_for_create(), "-C", str(parent), "-cpf", "-", "--", name]
    xz_compress_pipeline(cmd, output, MAX_DATA_XZ_LEVEL, threads)


def extract_xz_tar(archive: pathlib.Path, destination: pathlib.Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    decomp = subprocess.Popen(["xz", "-dc", str(archive)], stdout=subprocess.PIPE)
    assert decomp.stdout is not None
    cmd = ["tar", *tar_flags_for_extract(), "-C", str(destination), "-xpf", "-"]
    tarproc = subprocess.Popen(cmd, stdin=decomp.stdout)
    decomp.stdout.close()
    rc_tar = tarproc.wait()
    rc_xz = decomp.wait()
    if rc_tar != 0 or rc_xz != 0:
        die(f"Failed to extract {archive} into {destination}")


def remove_path(path: pathlib.Path) -> None:
    if not os.path.lexists(str(path)):
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        if path == pathlib.Path("/"):
            die("Refusing to delete the root directory.")
        shutil.rmtree(str(path))
    else:
        path.unlink()


def path_kind(path: pathlib.Path) -> str:
    if path.is_symlink():
        return "symlink"
    if path.is_dir():
        return "directory"
    if path.is_file():
        return "file"
    return "other"


def size_bytes(path: pathlib.Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.lstat().st_size
    except OSError:
        return 0
    total = 0
    for root, dirs, files in os.walk(str(path), followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(root: pathlib.Path) -> None:
    entries: List[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "checksums.sha256":
            rel = path.relative_to(root).as_posix()
            entries.append(f"{sha256_file(path)}  {rel}")
    (root / "checksums.sha256").write_text("\n".join(entries) + "\n", encoding="utf-8")


def verify_checksums(root: pathlib.Path) -> None:
    checksum_path = root / "checksums.sha256"
    if not checksum_path.exists():
        die("Archive has no checksums.sha256 file.")
    failures: List[str] = []
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, rel = line.split("  ", 1)
        path = root / rel
        if not path.is_file() or sha256_file(path) != expected:
            failures.append(rel)
    if failures:
        die("Checksum verification failed for:\n  " + "\n  ".join(failures))
    info("Checksum verification passed.")


def normalized_arch(value: str) -> str:
    aliases = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    return aliases.get((value or "").strip().lower(), (value or "").strip().lower())


def validate_restore_host(manifest: Dict[str, Any]) -> None:
    source_arch = normalized_arch(str(manifest.get("architecture") or ""))
    target_arch = normalized_arch(os.uname().machine)
    if source_arch and target_arch and source_arch != target_arch:
        die(
            f"Architecture mismatch: backup is {source_arch}, target host is {target_arch}. "
            "Refusing to restore images that may not run on this host."
        )


def compose_config_candidates(inspect_data: Dict[str, Any]) -> List[pathlib.Path]:
    labels = (inspect_data.get("Config") or {}).get("Labels") or {}
    candidates: List[pathlib.Path] = []
    config_files = labels.get("com.docker.compose.project.config_files")
    working_dir = labels.get("com.docker.compose.project.working_dir")
    if config_files:
        for value in config_files.split(","):
            value = value.strip()
            if value:
                p = pathlib.Path(value)
                if not p.is_absolute() and working_dir:
                    p = pathlib.Path(working_dir) / p
                candidates.append(p)
    if working_dir:
        env_file = pathlib.Path(working_dir) / ".env"
        candidates.append(env_file)
    return candidates


def network_is_builtin(name: str, inspect_data: Dict[str, Any]) -> bool:
    return name in {"bridge", "host", "none"} and not inspect_data.get("Internal", False)


def host_metadata() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tool_version": VERSION,
        "platform": sys.platform,
        "machine": os.uname().machine,
        "kernel": os.uname().release,
        "python": sys.version,
    }
    for cmd_name, cmd in {
        "docker_version": ["docker", "version"],
        "docker_info": ["docker", "info"],
        "os_release": ["cat", "/etc/os-release"],
    }.items():
        cp = run(cmd, check=False, capture=True)
        result[cmd_name] = cp.stdout if cp.returncode == 0 else cp.stderr
    daemon_json = pathlib.Path("/etc/docker/daemon.json")
    if daemon_json.exists():
        try:
            result["daemon_json"] = daemon_json.read_text(encoding="utf-8")
        except Exception as exc:
            result["daemon_json_error"] = str(exc)
    return result


def mount_source_key(mount: Dict[str, Any]) -> Optional[str]:
    if mount.get("Type") == "volume":
        return mount.get("Name") or mount.get("Source")
    return mount.get("Source")


def containers_using_mount(mount_type: str, source: str, selected_ids: Set[str]) -> List[str]:
    users: List[str] = []
    for row in list_containers():
        data = run_json(["docker", "inspect", "--type", "container", row["ID"]])[0]
        if data["Id"] in selected_ids:
            continue
        for mount in data.get("Mounts") or []:
            if mount.get("Type") == mount_type and mount_source_key(mount) == source:
                users.append(data.get("Name", "").lstrip("/"))
                break
    return users


def stop_or_pause_for_backup(
    containers: List[Dict[str, Any]],
    consistency: str,
    stopped: List[str],
    paused: List[str],
) -> None:
    running = [c for c in containers if (c.get("State") or {}).get("Running")]
    if consistency == "live" or not running:
        return

    for c in running:
        cid = c["Id"]
        name = c.get("Name", "").lstrip("/")
        already_paused = bool((c.get("State") or {}).get("Paused"))
        if already_paused:
            info(f"Container was already paused; leaving it paused: {name}")
            continue
        auto_remove = bool((c.get("HostConfig") or {}).get("AutoRemove"))
        if consistency == "pause" or auto_remove:
            if auto_remove and consistency == "stop":
                warn(f"{name} uses --rm; pausing it instead of stopping to avoid deletion.")
            info(f"Pausing container: {name}")
            run(["docker", "pause", cid])
            paused.append(cid)
        else:
            timeout = (c.get("Config") or {}).get("StopTimeout")
            timeout = str(timeout if isinstance(timeout, int) and timeout >= 0 else 30)
            info(f"Stopping container cleanly: {name}")
            run(["docker", "stop", "-t", timeout, cid])
            stopped.append(cid)


def resume_after_backup(stopped: List[str], paused: List[str]) -> None:
    for cid in paused:
        cp = run(["docker", "unpause", cid], check=False, capture=True)
        if cp.returncode != 0:
            warn(f"Failed to unpause {cid[:12]}: {(cp.stderr or cp.stdout).strip()}")
    for cid in stopped:
        cp = run(["docker", "start", cid], check=False, capture=True)
        if cp.returncode != 0:
            warn(f"Failed to restart {cid[:12]}: {(cp.stderr or cp.stdout).strip()}")


def backup_command(args: argparse.Namespace) -> None:
    require_root()
    require_commands("docker", "tar", "xz")
    docker_ready()

    containers = resolve_containers(args.containers, args.all, interactive=not args.non_interactive)
    backup_id = f"dfb-{now_stamp()}-{os.getpid()}"
    output = pathlib.Path(args.output or f"docker-full-backup-{now_stamp()}.tar.xz").expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and not args.force:
        die(f"Output already exists: {output}. Use --force to replace it.")

    work_parent = pathlib.Path(args.work_dir).expanduser().resolve() if args.work_dir else pathlib.Path("/var/tmp")
    work_parent.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix=f"{backup_id}-", dir=str(work_parent)))
    info(f"Working directory: {work}")

    manifest: Dict[str, Any] = {
        "format": "docker-full-backup",
        "format_version": 1,
        "tool_version": VERSION,
        "backup_id": backup_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "architecture": os.uname().machine,
        "containers": [],
        "mounts": {},
        "networks": {},
        "configs": [],
        "logs": [],
        "images_archive": "images/docker-images.tar.xz",
        "consistency": args.consistency,
    }

    temp_image_tags: List[str] = []
    stopped: List[str] = []
    paused: List[str] = []
    selected_ids = {c["Id"] for c in containers}

    try:
        json_dump(work / "host.json", host_metadata())

        # Warn about shared mounts used by unselected containers.
        seen_sources: Set[Tuple[str, str]] = set()
        for c in containers:
            for m in c.get("Mounts") or []:
                src = mount_source_key(m)
                mtype = m.get("Type") or ""
                marker = (mtype, src or "")
                if not src or marker in seen_sources:
                    continue
                seen_sources.add(marker)
                users = containers_using_mount(mtype, src, selected_ids)
                if users:
                    warn(f"Mount {src} is also used by unselected containers: {', '.join(users)}. Consistency is not guaranteed.")

        stop_or_pause_for_backup(containers, args.consistency, stopped, paused)

        original_image_tags: Dict[str, str] = {}
        original_repo_tags: Dict[str, List[str]] = {}
        image_save_tags: List[str] = []

        for index, original_inspect in enumerate(containers, 1):
            # Re-inspect after stop/pause so state metadata stays current, while preserving original running state separately.
            current = run_json(["docker", "inspect", "--type", "container", original_inspect["Id"]])[0]
            name = current.get("Name", "").lstrip("/")
            safe_name = safe_slug(name)
            cdir = work / "containers" / f"{index:04d}-{safe_name}"
            cdir.mkdir(parents=True, exist_ok=True)
            json_dump(cdir / "inspect.json", current)

            original_state = {
                "was_running": bool((original_inspect.get("State") or {}).get("Running")),
                "was_paused": bool((original_inspect.get("State") or {}).get("Paused")),
                "name": name,
                "id": original_inspect["Id"],
            }
            json_dump(cdir / "original-state.json", original_state)

            image_id = current.get("Image")
            if not image_id:
                die(f"Container {name} has no image ID in inspect output.")
            if image_id not in original_image_tags:
                original_tag = f"docker-full-backup/original:{backup_id}-{len(original_image_tags)+1}"
                run(["docker", "image", "tag", image_id, original_tag])
                original_image_tags[image_id] = original_tag
                image_inspect = run_json(["docker", "image", "inspect", image_id])[0]
                repo_tags = [x for x in (image_inspect.get("RepoTags") or []) if x and x != "<none>:<none>"]
                original_repo_tags[image_id] = repo_tags
                temp_image_tags.append(original_tag)
                image_save_tags.append(original_tag)
                image_save_tags.extend(repo_tags)
            else:
                original_tag = original_image_tags[image_id]

            snapshot_tag = f"docker-full-backup/snapshot:{backup_id}-{index}"
            info(f"Creating snapshot image for container: {name}")
            commit_pause = "true" if ((current.get("State") or {}).get("Running") and not (current.get("State") or {}).get("Paused")) else "false"
            run([
                "docker", "commit", f"--pause={commit_pause}",
                "--message", f"docker-full-backup {backup_id} container {name}",
                current["Id"], snapshot_tag,
            ])
            temp_image_tags.append(snapshot_tag)
            image_save_tags.append(snapshot_tag)

            mounts_for_container: List[str] = []
            for mount in current.get("Mounts") or []:
                mtype = mount.get("Type")
                source = mount_source_key(mount)
                if not source or mtype not in {"bind", "volume"}:
                    continue
                key_seed = f"{mtype}\0{source}".encode("utf-8", "surrogateescape")
                mount_id = hashlib.sha256(key_seed).hexdigest()[:24]
                mounts_for_container.append(mount_id)
                if mount_id in manifest["mounts"]:
                    continue

                users = []
                for cc in containers:
                    if any(mm.get("Type") == mtype and mount_source_key(mm) == source for mm in (cc.get("Mounts") or [])):
                        users.append(cc.get("Name", "").lstrip("/"))

                if mtype == "bind":
                    src_path = pathlib.Path(source)
                    if not os.path.lexists(str(src_path)):
                        warn(f"Bind source does not exist and cannot be archived: {source}")
                        record = {
                            "id": mount_id, "type": "bind", "source": source,
                            "archive": None, "kind": "missing", "containers": users,
                        }
                    else:
                        kind = path_kind(src_path)
                        archive_rel = f"mounts/bind/{mount_id}.tar.xz"
                        archive_abs = work / archive_rel
                        info(f"Archiving bind mount ({kind}): {source}")
                        if kind == "directory":
                            archive_directory_contents(src_path, archive_abs, args.xz_threads)
                            archive_layout = "contents"
                        else:
                            archive_single_path(src_path, archive_abs, args.xz_threads)
                            archive_layout = "single-path"
                        record = {
                            "id": mount_id,
                            "type": "bind",
                            "source": source,
                            "archive": archive_rel,
                            "archive_layout": archive_layout,
                            "kind": kind,
                            "size_bytes_uncompressed_estimate": size_bytes(src_path),
                            "containers": users,
                        }
                else:
                    vol_data = run_json(["docker", "volume", "inspect", source])[0]
                    driver = vol_data.get("Driver")
                    mountpoint = vol_data.get("Mountpoint")
                    if driver != "local" or not mountpoint or not pathlib.Path(mountpoint).is_dir():
                        die(
                            f"Volume {source} uses driver={driver!r} or has no accessible local Mountpoint. "
                            "A driver-specific backup method is required; refusing to create an incomplete backup."
                        )
                    archive_rel = f"mounts/volume/{mount_id}.tar.xz"
                    archive_abs = work / archive_rel
                    info(f"Archiving Docker volume: {source}")
                    archive_directory_contents(pathlib.Path(mountpoint), archive_abs, args.xz_threads)
                    record = {
                        "id": mount_id,
                        "type": "volume",
                        "source": source,
                        "archive": archive_rel,
                        "archive_layout": "contents",
                        "kind": "directory",
                        "driver": driver,
                        "volume_inspect": vol_data,
                        "size_bytes_uncompressed_estimate": size_bytes(pathlib.Path(mountpoint)),
                        "containers": users,
                    }
                manifest["mounts"][mount_id] = record

            networks_for_container: List[str] = []
            for net_name in ((current.get("NetworkSettings") or {}).get("Networks") or {}).keys():
                networks_for_container.append(net_name)
                if net_name in manifest["networks"]:
                    continue
                net_inspect = run_json(["docker", "network", "inspect", net_name])[0]
                if network_is_builtin(net_name, net_inspect):
                    manifest["networks"][net_name] = {"builtin": True, "inspect": net_inspect}
                else:
                    rel = f"networks/{safe_slug(net_name)}-{hashlib.sha256(net_name.encode()).hexdigest()[:8]}.json"
                    json_dump(work / rel, net_inspect)
                    manifest["networks"][net_name] = {"builtin": False, "inspect_file": rel}

            config_refs: List[str] = []
            for config_path in compose_config_candidates(current):
                try:
                    resolved = config_path.expanduser().resolve()
                except OSError:
                    resolved = config_path.expanduser().absolute()
                if not resolved.is_file():
                    continue
                already = next((x for x in manifest["configs"] if x["source"] == str(resolved)), None)
                if already:
                    config_refs.append(already["id"])
                    continue
                cfg_id = hashlib.sha256(str(resolved).encode()).hexdigest()[:24]
                rel = f"configs/{cfg_id}-{safe_slug(resolved.name)}"
                config_archive_path = work / rel
                config_archive_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(resolved), str(config_archive_path))
                st = resolved.stat()
                cfg_record = {
                    "id": cfg_id,
                    "source": str(resolved),
                    "archive_file": rel,
                    "mode": st.st_mode & 0o7777,
                    "uid": st.st_uid,
                    "gid": st.st_gid,
                    "mtime_ns": st.st_mtime_ns,
                }
                manifest["configs"].append(cfg_record)
                config_refs.append(cfg_id)

            log_ref: Optional[str] = None
            if not args.no_logs:
                log_path_raw = current.get("LogPath")
                if log_path_raw:
                    log_path = pathlib.Path(log_path_raw)
                    log_files = sorted(log_path.parent.glob(log_path.name + "*")) if log_path.parent.exists() else []
                    log_files = [p for p in log_files if p.is_file()]
                    if log_files:
                        log_id = hashlib.sha256(current["Id"].encode()).hexdigest()[:24]
                        rel = f"logs/{log_id}.tar.xz"
                        cmd = ["tar", *tar_flags_for_create(), "-C", str(log_path.parent), "-cpf", "-", "--"] + [p.name for p in log_files]
                        xz_compress_pipeline(cmd, work / rel, MAX_DATA_XZ_LEVEL, args.xz_threads)
                        log_record = {
                            "id": log_id,
                            "container": name,
                            "original_log_path": log_path_raw,
                            "archive": rel,
                            "files": [p.name for p in log_files],
                            "restorable_to_docker": False,
                        }
                        manifest["logs"].append(log_record)
                        log_ref = log_id

            manifest["containers"].append({
                "index": index,
                "name": name,
                "id": current["Id"],
                "inspect_file": str((cdir / "inspect.json").relative_to(work)),
                "state_file": str((cdir / "original-state.json").relative_to(work)),
                "original_image_id": image_id,
                "original_image_reference": (current.get("Config") or {}).get("Image"),
                "original_image_tag": original_tag,
                "original_repo_tags": original_repo_tags.get(image_id, []),
                "snapshot_image_tag": snapshot_tag,
                "mount_ids": mounts_for_container,
                "networks": networks_for_container,
                "config_ids": config_refs,
                "log_id": log_ref,
            })

        info("Saving original and snapshot images into one deduplicated image archive.")
        images_path = work / manifest["images_archive"]
        image_save_tags = list(dict.fromkeys(image_save_tags))
        xz_compress_pipeline(
            ["docker", "image", "save", *image_save_tags],
            images_path,
            DEFAULT_FINAL_XZ_LEVEL,
            args.xz_threads,
        )

        json_dump(work / "manifest.json", manifest)
        write_checksums(work)

        if output.exists():
            output.unlink()
        info(f"Creating final single archive: {output}")
        xz_compress_pipeline(
            ["tar", "--numeric-owner", "-C", str(work), "-cpf", "-", "."],
            output,
            DEFAULT_FINAL_XZ_LEVEL,
            args.xz_threads,
        )
        info(f"Backup completed: {output}")
        info(f"Archive size: {output.stat().st_size / (1024 ** 3):.2f} GiB")
    finally:
        resume_after_backup(stopped, paused)
        for tag in reversed(temp_image_tags):
            run(["docker", "image", "rm", tag], check=False, capture=True)
        if args.keep_workdir:
            warn(f"Keeping working directory: {work}")
        else:
            shutil.rmtree(str(work), ignore_errors=True)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


class DockerAPI:
    def __init__(self, socket_path: str = DEFAULT_DOCKER_SOCKET):
        self.socket_path = socket_path
        version = self.request("GET", "/version")
        self.api_version = version.get("ApiVersion")
        if not self.api_version:
            die("Could not determine Docker Engine API version.")

    def request(self, method: str, path: str, body: Optional[Any] = None, expected: Iterable[int] = (200, 201, 204)) -> Any:
        conn = UnixHTTPConnection(self.socket_path)
        headers = {"Content-Type": "application/json"}
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        conn.request(method, path, body=encoded, headers=headers)
        response = conn.getresponse()
        data = response.read()
        status = response.status
        ctype = response.getheader("Content-Type", "")
        conn.close()
        if status not in set(expected):
            try:
                detail = json.loads(data.decode("utf-8", "replace")).get("message")
            except Exception:
                detail = data.decode("utf-8", "replace")
            die(f"Docker API {method} {path} failed with HTTP {status}: {detail}")
        if not data:
            return None
        if "json" in ctype or data[:1] in (b"{", b"["):
            return json.loads(data.decode("utf-8"))
        return data

    def create_network(self, inspect_data: Dict[str, Any]) -> None:
        payload = {
            "Name": inspect_data.get("Name"),
            "CheckDuplicate": True,
            "Driver": inspect_data.get("Driver") or "bridge",
            "Internal": bool(inspect_data.get("Internal")),
            "Attachable": bool(inspect_data.get("Attachable")),
            "Ingress": bool(inspect_data.get("Ingress")),
            "EnableIPv6": bool(inspect_data.get("EnableIPv6")),
            "IPAM": inspect_data.get("IPAM") or {},
            "Options": inspect_data.get("Options") or {},
            "Labels": inspect_data.get("Labels") or {},
        }
        self.request("POST", f"/v{self.api_version}/networks/create", payload, expected=(201,))

    def create_container(self, name: str, payload: Dict[str, Any]) -> str:
        quoted = urllib.parse.quote(name, safe="")
        result = self.request(
            "POST",
            f"/v{self.api_version}/containers/create?name={quoted}",
            payload,
            expected=(201,),
        )
        return result["Id"]


def validate_final_archive(archive: pathlib.Path) -> None:
    if not archive.is_file():
        die(f"Backup archive not found: {archive}")
    # Reject absolute paths and parent traversal before extraction.
    decomp = subprocess.Popen(["xz", "-dc", str(archive)], stdout=subprocess.PIPE)
    assert decomp.stdout is not None
    listing = subprocess.Popen(["tar", "-tf", "-"], stdin=decomp.stdout, stdout=subprocess.PIPE, text=True)
    decomp.stdout.close()
    stdout, _ = listing.communicate()
    rc_xz = decomp.wait()
    if listing.returncode != 0 or rc_xz != 0:
        die("The archive is not a valid .tar.xz backup.")
    for name in stdout.splitlines():
        normalized = pathlib.PurePosixPath(name)
        if normalized.is_absolute() or ".." in normalized.parts:
            die(f"Unsafe path in archive: {name}")


def extract_final_archive(archive: pathlib.Path, destination: pathlib.Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    decomp = subprocess.Popen(["xz", "-dc", str(archive)], stdout=subprocess.PIPE)
    assert decomp.stdout is not None
    tarproc = subprocess.Popen(["tar", "--numeric-owner", "-C", str(destination), "-xpf", "-"], stdin=decomp.stdout)
    decomp.stdout.close()
    rc_tar = tarproc.wait()
    rc_xz = decomp.wait()
    if rc_tar != 0 or rc_xz != 0:
        die(f"Failed to extract backup archive: {archive}")


def choose_from_archive(manifest: Dict[str, Any], identifiers: Sequence[str], all_flag: bool, non_interactive: bool) -> List[Dict[str, Any]]:
    entries = manifest.get("containers") or []
    if not entries:
        die("No containers found in backup manifest.")
    if all_flag:
        return entries
    if identifiers:
        result: List[Dict[str, Any]] = []
        for ident in identifiers:
            matches = [x for x in entries if x["name"] == ident or x["id"] == ident or x["id"].startswith(ident)]
            if not matches:
                die(f"Container is not present in backup: {ident}")
            result.append(matches[0])
        seen: Set[str] = set()
        return [x for x in result if not (x["id"] in seen or seen.add(x["id"]))]
    if non_interactive:
        die("Specify container names/IDs or use --all in non-interactive mode.")

    print("\nContainers in backup:")
    for idx, entry in enumerate(entries, 1):
        print(f"  {idx:>3}. {entry['name']}  {entry['id'][:12]}")
    print("    a. all containers")
    raw = input("Select numbers separated by commas, or 'a' [a]: ").strip().lower() or "a"
    if raw in ("a", "all", "*"):
        return entries
    chosen: List[Dict[str, Any]] = []
    for token in re.split(r"[ ,]+", raw):
        if not token:
            continue
        idx = int(token)
        if idx < 1 or idx > len(entries):
            die(f"Invalid selection: {idx}")
        chosen.append(entries[idx - 1])
    return chosen


def prompt_choice(prompt: str, choices: Dict[str, str], default: str, non_interactive: bool, policy: str) -> str:
    if non_interactive:
        if policy not in choices:
            die(f"Conflict encountered but policy {policy!r} is not valid here.")
        return policy
    print(prompt)
    for key, description in choices.items():
        print(f"  [{key}] {description}")
    raw = input(f"Choice [{default}]: ").strip().lower() or default
    if raw not in choices:
        warn(f"Invalid choice {raw!r}; using {default!r}.")
        raw = default
    return raw


def default_alternate_path(path: pathlib.Path, stamp: str) -> pathlib.Path:
    return path.with_name(path.name + f".restored-{stamp}") if path.name else pathlib.Path(f"/restored-{stamp}")


def resolve_bind_target(original: pathlib.Path, stamp: str, args: argparse.Namespace) -> Tuple[pathlib.Path, str]:
    if not os.path.lexists(str(original)):
        return original, "restore"
    choice = prompt_choice(
        f"Bind path already exists: {original}",
        {
            "overwrite": "delete the existing path, then restore backup data",
            "alternate": "restore into another path and rewrite the container mount",
            "existing": "keep and use the existing path without restoring backup data",
            "fail": "abort restore",
        },
        "alternate",
        args.non_interactive,
        args.conflict,
    )
    if choice == "fail":
        die(f"Conflict at bind path: {original}")
    if choice == "existing":
        return original, "existing"
    if choice == "overwrite":
        if original == pathlib.Path("/"):
            die("Refusing to overwrite '/'. Use alternate or existing.")
        return original, "overwrite"

    suggested = default_alternate_path(original, stamp)
    if args.non_interactive:
        target = suggested
        counter = 1
        while os.path.lexists(str(target)):
            target = pathlib.Path(str(suggested) + f"-{counter}")
            counter += 1
        return target, "restore"
    raw = input(f"Alternate path [{suggested}]: ").strip()
    target = pathlib.Path(raw).expanduser().resolve() if raw else suggested
    if os.path.lexists(str(target)):
        die(f"Alternate path also exists: {target}")
    return target, "restore"


def volume_exists(name: str) -> bool:
    return run(["docker", "volume", "inspect", name], check=False, capture=True).returncode == 0


def resolve_volume_name(original: str, stamp: str, args: argparse.Namespace) -> Tuple[str, str]:
    if not volume_exists(original):
        return original, "restore"
    choice = prompt_choice(
        f"Docker volume already exists: {original}",
        {
            "overwrite": "delete existing volume data and restore backup data",
            "alternate": "create another volume and rewrite container mounts",
            "existing": "keep and use the existing volume without restoring backup data",
            "fail": "abort restore",
        },
        "alternate",
        args.non_interactive,
        args.conflict,
    )
    if choice == "fail":
        die(f"Conflict at Docker volume: {original}")
    if choice == "existing":
        return original, "existing"
    if choice == "overwrite":
        return original, "overwrite"
    base = safe_slug(original) + f"-restored-{stamp}"
    candidate = base
    counter = 1
    while volume_exists(candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    if not args.non_interactive:
        raw = input(f"Alternate volume name [{candidate}]: ").strip()
        if raw:
            candidate = raw
            if volume_exists(candidate):
                die(f"Alternate volume already exists: {candidate}")
    return candidate, "restore"


def clear_directory_contents(path: pathlib.Path) -> None:
    if not path.is_dir():
        die(f"Expected directory: {path}")
    for entry in path.iterdir():
        remove_path(entry)


def restore_bind_mount(record: Dict[str, Any], root: pathlib.Path, target: pathlib.Path, action: str) -> None:
    if action == "existing" or not record.get("archive"):
        return
    if action == "overwrite":
        remove_path(target)
    archive = root / record["archive"]
    kind = record.get("kind")
    layout = record.get("archive_layout")
    if layout == "contents" and kind == "directory":
        target.mkdir(parents=True, exist_ok=True)
        extract_xz_tar(archive, target)
    elif layout == "single-path":
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="dfb-bind-", dir=str(target.parent)))
        try:
            extract_xz_tar(archive, temp_dir)
            items = list(temp_dir.iterdir())
            if len(items) != 1:
                die(f"Unexpected single-path archive layout in {archive}")
            shutil.move(str(items[0]), str(target))
        finally:
            shutil.rmtree(str(temp_dir), ignore_errors=True)
    else:
        die(f"Unsupported bind archive layout for {record.get('source')}")


def create_or_reuse_volume(record: Dict[str, Any], target_name: str) -> Dict[str, Any]:
    if not volume_exists(target_name):
        inspect_data = record.get("volume_inspect") or {}
        cmd = ["docker", "volume", "create", "--driver", inspect_data.get("Driver") or "local"]
        for key, value in (inspect_data.get("Labels") or {}).items():
            cmd.extend(["--label", f"{key}={value}"])
        for key, value in (inspect_data.get("Options") or {}).items():
            cmd.extend(["--opt", f"{key}={value}"])
        cmd.append(target_name)
        run(cmd)
    return run_json(["docker", "volume", "inspect", target_name])[0]


def restore_volume_mount(record: Dict[str, Any], root: pathlib.Path, target_name: str, action: str) -> None:
    volume_data = create_or_reuse_volume(record, target_name)
    if action == "existing" or not record.get("archive"):
        return
    mountpoint = volume_data.get("Mountpoint")
    if volume_data.get("Driver") != "local" or not mountpoint:
        die(f"Restored volume {target_name} is not a local volume with an accessible Mountpoint.")
    mp = pathlib.Path(mountpoint)
    if action == "overwrite":
        clear_directory_contents(mp)
    elif any(mp.iterdir()):
        die(f"New volume {target_name} is unexpectedly non-empty: {mp}")
    extract_xz_tar(root / record["archive"], mp)


def restore_config_file(record: Dict[str, Any], root: pathlib.Path, target: pathlib.Path, action: str) -> None:
    if action == "existing":
        return
    if action == "overwrite":
        remove_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(root / record["archive_file"]), str(target))
    try:
        os.chmod(str(target), int(record.get("mode", 0o644)))
        os.chown(str(target), int(record.get("uid", 0)), int(record.get("gid", 0)))
        if record.get("mtime_ns") is not None:
            os.utime(str(target), ns=(int(record["mtime_ns"]), int(record["mtime_ns"])))
    except OSError as exc:
        warn(f"Could not fully restore metadata for config file {target}: {exc}")


def mapped_path_if_covered_by_bind(
    config_source: pathlib.Path,
    source_map: Dict[Tuple[str, str], str],
) -> Optional[pathlib.Path]:
    best: Optional[Tuple[pathlib.Path, pathlib.Path]] = None
    for (kind, original), mapped in source_map.items():
        if kind != "bind":
            continue
        original_path = pathlib.Path(original)
        try:
            relative = config_source.relative_to(original_path)
        except ValueError:
            continue
        if best is None or len(original_path.parts) > len(best[0].parts):
            best = (original_path, pathlib.Path(mapped) / relative)
    return best[1] if best else None


def patch_bind_spec(spec: str, source_map: Dict[Tuple[str, str], str]) -> str:
    # Docker bind syntax is source:destination[:mode]. Linux source paths can
    # technically contain ':', but Docker's short syntax cannot unambiguously
    # represent that either. Split from the right to preserve common paths.
    parts = spec.rsplit(":", 2)
    if len(parts) < 2:
        return spec
    if len(parts) == 2:
        source, dest = parts
        mode = None
    else:
        source, dest, mode = parts
        # If the middle token doesn't look like an absolute container path,
        # the original likely had only source:destination with a colon in source.
        if not dest.startswith("/"):
            source, dest = spec.rsplit(":", 1)
            mode = None
    kind = "bind" if source.startswith("/") or source.startswith(".") else "volume"
    mapped = source_map.get((kind, source), source)
    return f"{mapped}:{dest}" + (f":{mode}" if mode is not None else "")


def build_container_payload(
    inspect_data: Dict[str, Any],
    snapshot_tag: str,
    source_map: Dict[Tuple[str, str], str],
    id_to_new_name: Dict[str, str],
    config_path_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    config = copy.deepcopy(inspect_data.get("Config") or {})
    host_config = copy.deepcopy(inspect_data.get("HostConfig") or {})
    config["Image"] = snapshot_tag

    labels = config.get("Labels") or {}
    config_path_map = config_path_map or {}
    config_files_label = labels.get("com.docker.compose.project.config_files")
    if config_files_label:
        patched_files: List[str] = []
        for raw in config_files_label.split(","):
            value = raw.strip()
            patched_files.append(config_path_map.get(value, value))
        labels["com.docker.compose.project.config_files"] = ",".join(patched_files)
    working_dir_label = labels.get("com.docker.compose.project.working_dir")
    if working_dir_label:
        mapped_workdir = mapped_path_if_covered_by_bind(pathlib.Path(working_dir_label), source_map)
        if mapped_workdir is not None:
            labels["com.docker.compose.project.working_dir"] = str(mapped_workdir)
    config["Labels"] = labels

    if not isinstance(host_config.get("Mounts"), list):
        host_config["Mounts"] = []

    if host_config.get("Binds"):
        host_config["Binds"] = [patch_bind_spec(x, source_map) for x in host_config["Binds"]]

    if host_config.get("Mounts"):
        for mount in host_config["Mounts"]:
            mtype = mount.get("Type")
            source = mount.get("Source")
            if mtype in {"bind", "volume"} and source:
                mount["Source"] = source_map.get((mtype, source), source)

    # Ensure anonymous/image-declared volume mounts are explicitly reattached.
    long_mounts = host_config.setdefault("Mounts", [])
    existing_targets = {m.get("Target") for m in long_mounts}
    bind_targets: Set[str] = set()
    for spec in host_config.get("Binds") or []:
        parts = spec.rsplit(":", 2)
        if len(parts) == 2:
            bind_targets.add(parts[1])
        elif len(parts) == 3:
            bind_targets.add(parts[1])

    for mount in inspect_data.get("Mounts") or []:
        mtype = mount.get("Type")
        source = mount_source_key(mount)
        target = mount.get("Destination")
        if mtype not in {"bind", "volume"} or not source or not target:
            continue
        if target in existing_targets or target in bind_targets:
            continue
        mapped_source = source_map.get((mtype, source), source)
        entry: Dict[str, Any] = {
            "Type": mtype,
            "Source": mapped_source,
            "Target": target,
            "ReadOnly": not bool(mount.get("RW", True)),
        }
        if mtype == "bind" and mount.get("Propagation"):
            entry["BindOptions"] = {"Propagation": mount["Propagation"]}
        long_mounts.append(entry)
        existing_targets.add(target)

    # Patch container:<id> network namespace references and volumes-from IDs.
    network_mode = host_config.get("NetworkMode") or ""
    if isinstance(network_mode, str) and network_mode.startswith("container:"):
        old = network_mode.split(":", 1)[1]
        replacement = id_to_new_name.get(old)
        if replacement:
            host_config["NetworkMode"] = f"container:{replacement}"

    if host_config.get("VolumesFrom"):
        patched_vf: List[str] = []
        for item in host_config["VolumesFrom"]:
            old, sep, mode = item.partition(":")
            new = id_to_new_name.get(old, old)
            patched_vf.append(new + (sep + mode if sep else ""))
        host_config["VolumesFrom"] = patched_vf

    if host_config.get("Links"):
        patched_links: List[str] = []
        for item in host_config["Links"]:
            left, sep, right = item.partition(":")
            original_target = left.lstrip("/")
            replacement = id_to_new_name.get(original_target, original_target)
            new_left = "/" + replacement
            if sep:
                old_prefix = "/" + original_target + "/"
                new_prefix = "/" + replacement + "/"
                right = right.replace(old_prefix, new_prefix, 1)
            patched_links.append(new_left + (sep + right if sep else ""))
        host_config["Links"] = patched_links

    endpoints: Dict[str, Any] = {}
    network_mode = str(host_config.get("NetworkMode") or "")
    if network_mode not in {"host", "none"} and not network_mode.startswith("container:"):
        old_name = str(inspect_data.get("Name") or "").lstrip("/")
        new_name = id_to_new_name.get(old_name, old_name)
        old_short_id = str(inspect_data.get("Id") or "")[:12]
        for name, endpoint in (((inspect_data.get("NetworkSettings") or {}).get("Networks")) or {}).items():
            clean: Dict[str, Any] = {}
            for key in ("Links", "DriverOpts"):
                value = endpoint.get(key)
                if value not in (None, [], {}, ""):
                    clean[key] = value

            aliases: List[str] = []
            for alias in endpoint.get("Aliases") or []:
                if alias == old_short_id:
                    continue
                if alias == old_name:
                    alias = new_name
                if alias and alias not in aliases:
                    aliases.append(alias)
            if aliases:
                clean["Aliases"] = aliases

            # NetworkSettings.MacAddress is normally a runtime-assigned value;
            # do not force it on a second restore. Explicit Config.MacAddress is
            # still preserved in the container Config above.
            raw_ipam = endpoint.get("IPAMConfig") or {}
            ipam: Dict[str, Any] = {}
            for key in ("IPv4Address", "IPv6Address", "LinkLocalIPs"):
                value = raw_ipam.get(key)
                if value not in (None, "", []):
                    ipam[key] = value
            if ipam:
                clean["IPAMConfig"] = ipam
            endpoints[name] = clean

    payload = {
        **config,
        "HostConfig": host_config,
        "NetworkingConfig": {"EndpointsConfig": endpoints},
    }
    return payload


def container_exists(name: str) -> Optional[Dict[str, Any]]:
    cp = run(["docker", "inspect", "--type", "container", name], check=False, capture=True)
    if cp.returncode != 0:
        return None
    data = json.loads(cp.stdout)
    return data[0] if data else None


def resolve_container_name(original: str, stamp: str, args: argparse.Namespace) -> Tuple[str, str]:
    existing = container_exists(original)
    if not existing:
        return original, "create"
    choice = prompt_choice(
        f"Container name already exists: {original}",
        {
            "overwrite": "stop and remove the existing container, then restore",
            "alternate": "restore with another container name",
            "existing": "skip restoring this container",
            "fail": "abort restore",
        },
        "alternate",
        args.non_interactive,
        args.container_conflict,
    )
    if choice == "fail":
        die(f"Container name conflict: {original}")
    if choice == "existing":
        return original, "skip"
    if choice == "overwrite":
        # Defer removal until immediately before recreation, after archive, image,
        # network, and mount validation have completed.
        return original, "replace"
    base = safe_slug(original) + f"-restored-{stamp}"
    candidate = base
    counter = 1
    while container_exists(candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    if not args.non_interactive:
        raw = input(f"Alternate container name [{candidate}]: ").strip()
        if raw:
            candidate = raw
            if container_exists(candidate):
                die(f"Alternate container name already exists: {candidate}")
    return candidate, "create"


def network_compatibility_signature(data: Dict[str, Any]) -> Dict[str, Any]:
    ipam = data.get("IPAM") or {}
    configs = []
    for cfg in ipam.get("Config") or []:
        configs.append({
            "Subnet": cfg.get("Subnet") or "",
            "Gateway": cfg.get("Gateway") or "",
            "IPRange": cfg.get("IPRange") or "",
            "AuxiliaryAddresses": cfg.get("AuxiliaryAddresses") or {},
        })
    configs.sort(key=lambda x: (x["Subnet"], x["Gateway"], x["IPRange"], json.dumps(x["AuxiliaryAddresses"], sort_keys=True)))
    return {
        "Driver": data.get("Driver") or "bridge",
        "Internal": bool(data.get("Internal")),
        "Attachable": bool(data.get("Attachable")),
        "EnableIPv6": bool(data.get("EnableIPv6")),
        "IPAMDriver": ipam.get("Driver") or "default",
        "IPAMConfig": configs,
        "Options": data.get("Options") or {},
    }


def restore_networks(root: pathlib.Path, manifest: Dict[str, Any], selected: List[Dict[str, Any]], api: DockerAPI) -> None:
    needed: Set[str] = set()
    for entry in selected:
        needed.update(entry.get("networks") or [])
    for name in sorted(needed):
        record = (manifest.get("networks") or {}).get(name)
        if not record or record.get("builtin"):
            continue
        existing_cp = run(["docker", "network", "inspect", name], check=False, capture=True)
        inspect_data = json_load(root / record["inspect_file"])
        if existing_cp.returncode == 0:
            try:
                existing_data = json.loads(existing_cp.stdout)[0]
            except Exception as exc:
                die(f"Could not parse existing Docker network {name}: {exc}")
            if network_compatibility_signature(existing_data) != network_compatibility_signature(inspect_data):
                die(
                    f"Docker network {name} already exists but its driver/IPAM/options differ from the backup. "
                    "Rename or remove the conflicting network before restore; it will not be reused silently."
                )
            info(f"Using compatible existing Docker network: {name}")
            continue
        if inspect_data.get("Scope") == "swarm" or inspect_data.get("Ingress"):
            die(f"Network {name} is a swarm/ingress network and cannot be recreated as a standalone local network.")
        info(f"Creating Docker network: {name}")
        api.create_network(inspect_data)


def capture_existing_image_tags(manifest: Dict[str, Any]) -> Dict[str, str]:
    existing: Dict[str, str] = {}
    tags: Set[str] = set()
    for entry in manifest.get("containers") or []:
        for tag in entry.get("original_repo_tags") or []:
            if tag and not tag.startswith("docker-full-backup/") and tag != "<none>:<none>":
                tags.add(tag)
    for tag in sorted(tags):
        cp = run(["docker", "image", "inspect", "--format", "{{.Id}}", tag], check=False, capture=True)
        if cp.returncode == 0 and cp.stdout.strip():
            existing[tag] = cp.stdout.strip()
    return existing


def restore_preexisting_image_tags(existing: Dict[str, str]) -> None:
    for tag, image_id in sorted(existing.items()):
        cp = run(["docker", "image", "tag", image_id, tag], check=False, capture=True)
        if cp.returncode == 0:
            warn(f"Preserved pre-existing image tag instead of replacing it: {tag}")
        else:
            warn(f"Could not restore pre-existing image tag {tag}: {(cp.stderr or cp.stdout).strip()}")


def load_images(root: pathlib.Path, manifest: Dict[str, Any]) -> None:
    archive = root / manifest["images_archive"]
    info(f"Loading Docker images from {archive}")
    decomp = subprocess.Popen(["xz", "-dc", str(archive)], stdout=subprocess.PIPE)
    assert decomp.stdout is not None
    loader = subprocess.Popen(["docker", "image", "load"], stdin=decomp.stdout)
    decomp.stdout.close()
    rc_load = loader.wait()
    rc_xz = decomp.wait()
    if rc_load != 0 or rc_xz != 0:
        die("Failed to load Docker images.")


def restore_archived_logs(root: pathlib.Path, manifest: Dict[str, Any], output_dir: pathlib.Path, selected_names: Set[str]) -> None:
    logs = [x for x in (manifest.get("logs") or []) if x.get("container") in selected_names]
    if not logs:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    for record in logs:
        target = output_dir / safe_slug(record["container"])
        target.mkdir(parents=True, exist_ok=True)
        extract_xz_tar(root / record["archive"], target)
    warn(f"Historical logs were extracted to {output_dir}; Docker does not support reattaching them to the active log driver.")


def restore_command(args: argparse.Namespace) -> None:
    require_root()
    require_commands("docker", "tar", "xz")
    docker_ready()

    archive = pathlib.Path(args.archive).expanduser().resolve()
    validate_final_archive(archive)
    work_parent = pathlib.Path(args.work_dir).expanduser().resolve() if args.work_dir else pathlib.Path("/var/tmp")
    work_parent.mkdir(parents=True, exist_ok=True)
    work = pathlib.Path(tempfile.mkdtemp(prefix="docker-full-restore-", dir=str(work_parent)))
    info(f"Extracting backup into: {work}")
    extract_final_archive(archive, work)

    # The final tar stores entries as ./manifest.json.
    root = work
    if not (root / "manifest.json").exists() and (root / "." / "manifest.json").exists():
        root = root / "."

    try:
        manifest = json_load(root / "manifest.json")
        if manifest.get("format") != "docker-full-backup":
            die("Unsupported backup format.")
        if manifest.get("format_version") != 1:
            die(f"Unsupported backup format version: {manifest.get('format_version')!r}")
        validate_restore_host(manifest)
        verify_checksums(root)
        selected = choose_from_archive(manifest, args.containers, args.all, args.non_interactive)
        stamp = now_stamp()

        preexisting_image_tags = capture_existing_image_tags(manifest)
        load_images(root, manifest)
        restore_preexisting_image_tags(preexisting_image_tags)
        api = DockerAPI(args.docker_socket)

        id_to_new_name: Dict[str, str] = {}
        restore_plan: List[Tuple[Dict[str, Any], str, str]] = []
        for entry in selected:
            new_name, action = resolve_container_name(entry["name"], stamp, args)
            restore_plan.append((entry, new_name, action))
            if action == "skip":
                # Dependencies can use the existing same-name container.
                id_to_new_name[entry["id"]] = entry["name"]
                id_to_new_name[entry["id"][:12]] = entry["name"]
                id_to_new_name[entry["name"]] = entry["name"]
            else:
                id_to_new_name[entry["id"]] = new_name
                id_to_new_name[entry["id"][:12]] = new_name
                id_to_new_name[entry["name"]] = new_name

        active_entries = [entry for entry, _name, action in restore_plan if action != "skip"]
        if not active_entries:
            warn("All selected containers were skipped; nothing to restore.")
            return

        restore_networks(root, manifest, active_entries, api)

        needed_mount_ids: Set[str] = set()
        for entry in active_entries:
            needed_mount_ids.update(entry.get("mount_ids") or [])

        source_map: Dict[Tuple[str, str], str] = {}
        restored_mounts: Set[str] = set()
        mount_records: List[Tuple[str, Dict[str, Any]]] = []
        for mount_id in sorted(needed_mount_ids):
            record = manifest["mounts"].get(mount_id)
            if not record:
                die(f"Missing mount record in manifest: {mount_id}")
            mount_records.append((mount_id, record))

        bind_records = [(mid, rec) for mid, rec in mount_records if rec.get("type") == "bind"]
        volume_records = [(mid, rec) for mid, rec in mount_records if rec.get("type") == "volume"]
        unsupported = [(mid, rec) for mid, rec in mount_records if rec.get("type") not in {"bind", "volume"}]
        if unsupported:
            die(f"Unsupported mount type: {unsupported[0][1].get('type')}")

        # Restore parent bind paths before nested child bind paths. A child path
        # created by restoring its parent is not a user conflict; the child's own
        # archive should replace that subtree at the corresponding mapped path.
        bind_records.sort(key=lambda item: len(pathlib.Path(item[1]["source"]).parts))
        processed_binds: List[Tuple[pathlib.Path, pathlib.Path, str]] = []
        for mount_id, record in bind_records:
            original_path = pathlib.Path(record["source"])
            inherited: Optional[Tuple[pathlib.Path, pathlib.Path, str]] = None
            for parent_original, parent_target, parent_action in processed_binds:
                if parent_action == "existing":
                    continue
                try:
                    original_path.relative_to(parent_original)
                except ValueError:
                    continue
                if inherited is None or len(parent_original.parts) > len(inherited[0].parts):
                    inherited = (parent_original, parent_target, parent_action)

            if inherited is not None:
                relative = original_path.relative_to(inherited[0])
                target = inherited[1] / relative
                action = "overwrite" if os.path.lexists(str(target)) else "restore"
                info(f"Nested bind mount follows restored parent: {original_path} -> {target}")
            else:
                target, action = resolve_bind_target(original_path, stamp, args)

            if action == "existing" and os.path.lexists(str(target)):
                expected_kind = record.get("kind")
                actual_kind = path_kind(target)
                if expected_kind in {"file", "directory", "symlink"} and actual_kind != expected_kind:
                    die(f"Existing bind path has wrong type: {target} is {actual_kind}, backup expects {expected_kind}")
            info(f"Restoring bind mount {original_path} -> {target} ({action})")
            restore_bind_mount(record, root, target, action)
            source_map[("bind", str(original_path))] = str(target)
            processed_binds.append((original_path, target, action))
            restored_mounts.add(mount_id)

        for mount_id, record in volume_records:
            original = record["source"]
            target_name, action = resolve_volume_name(original, stamp, args)
            info(f"Restoring volume {original} -> {target_name} ({action})")
            restore_volume_mount(record, root, target_name, action)
            source_map[("volume", original)] = target_name
            restored_mounts.add(mount_id)

        needed_config_ids: Set[str] = set()
        for entry in active_entries:
            needed_config_ids.update(entry.get("config_ids") or [])
        configs_by_id = {x["id"]: x for x in (manifest.get("configs") or [])}
        config_path_map: Dict[str, str] = {}
        for config_id in sorted(needed_config_ids):
            record = configs_by_id.get(config_id)
            if not record:
                warn(f"Config record is missing from manifest: {config_id}")
                continue
            original = pathlib.Path(record["source"])
            covered = mapped_path_if_covered_by_bind(original, source_map)
            if covered is not None:
                info(f"Compose config restored through bind archive: {original} -> {covered}")
                config_path_map[str(original)] = str(covered)
                continue
            target, action = resolve_bind_target(original, stamp, args)
            info(f"Restoring Compose config {original} -> {target} ({action})")
            restore_config_file(record, root, target, action)
            config_path_map[str(original)] = str(target)

        created: List[Dict[str, Any]] = []
        # Sort by original creation time, which improves legacy link/volumes-from behavior.
        def created_at(item: Tuple[Dict[str, Any], str, str]) -> str:
            inspect_data = json_load(root / item[0]["inspect_file"])
            return inspect_data.get("Created", "")

        restore_plan.sort(key=created_at)
        for entry, new_name, action in restore_plan:
            if action == "skip":
                warn(f"Skipping container due to name conflict policy: {entry['name']}")
                continue
            inspect_data = json_load(root / entry["inspect_file"])
            state_data = json_load(root / entry["state_file"])
            payload = build_container_payload(
                inspect_data, entry["snapshot_image_tag"], source_map, id_to_new_name, config_path_map
            )
            if action == "replace":
                existing = container_exists(new_name)
                if existing:
                    info(f"Removing existing container immediately before replacement: {new_name}")
                    run(["docker", "rm", "-f", existing["Id"]])
            info(f"Creating container: {entry['name']} -> {new_name}")
            cid = api.create_container(new_name, payload)
            created.append({
                "name": new_name,
                "id": cid,
                "was_running": bool(state_data.get("was_running")),
                "was_paused": bool(state_data.get("was_paused")),
                "started": False,
                "paused": False,
            })

        start_failures: List[str] = []
        if not args.no_start:
            for item in created:
                if item["was_running"] or args.start_all:
                    info(f"Starting container: {item['name']}")
                    cp = run(["docker", "start", item["id"]], check=False, capture=True)
                    if cp.returncode != 0:
                        detail = (cp.stderr or cp.stdout).strip()
                        warn(f"Container {item['name']} was created but failed to start:\n{detail}")
                        start_failures.append(f"{item['name']}: {detail}")
                        continue
                    item["started"] = True
                    if item["was_paused"]:
                        cp_pause = run(["docker", "pause", item["id"]], check=False, capture=True)
                        if cp_pause.returncode == 0:
                            item["paused"] = True
                        else:
                            warn(f"Container {item['name']} started but could not be returned to paused state: {(cp_pause.stderr or cp_pause.stdout).strip()}")

        if not args.no_logs:
            log_dir = pathlib.Path(args.logs_dir).expanduser().resolve() if args.logs_dir else archive.parent / f"{archive.stem}-restored-logs"
            restore_archived_logs(root, manifest, log_dir, {e["name"] for e in selected})

        for item in created:
            status = "paused" if item["paused"] else ("started" if item["started"] else "created")
            print(f"  {item['name']}: {item['id'][:12]}  {status}")
        if start_failures:
            die("Restore created containers, but one or more failed to start:\n  " + "\n  ".join(start_failures))
        info("Restore completed.")
    finally:
        if args.keep_workdir:
            warn(f"Keeping restore working directory: {work}")
        else:
            shutil.rmtree(str(work), ignore_errors=True)


def list_command(args: argparse.Namespace) -> None:
    require_commands("docker")
    docker_ready()
    rows = list_containers()
    if not rows:
        print("No containers found.")
        return
    print(f"{'ID':<14} {'NAME':<28} {'IMAGE':<36} STATUS")
    for row in rows:
        print(f"{row.get('ID','')[:12]:<14} {row.get('Names','')[:27]:<28} {row.get('Image','')[:35]:<36} {row.get('Status','')}")


def archive_list_command(args: argparse.Namespace) -> None:
    require_commands("tar", "xz")
    archive = pathlib.Path(args.archive).expanduser().resolve()
    validate_final_archive(archive)
    work = pathlib.Path(tempfile.mkdtemp(prefix="docker-full-list-", dir="/var/tmp"))
    try:
        extract_final_archive(archive, work)
        manifest = json_load(work / "manifest.json")
        print(f"Archive: {archive}")
        print(f"Created: {manifest.get('created_at')}")
        print(f"Architecture: {manifest.get('architecture')}")
        print("Containers:")
        for entry in manifest.get("containers") or []:
            print(f"  {entry['id'][:12]}  {entry['name']}")
    finally:
        shutil.rmtree(str(work), ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Complete, portable Docker container backup and restore for Debian/Ubuntu Linux.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  sudo python3 docker_full_backup.py list
  sudo python3 docker_full_backup.py backup --all -o /backup/all-containers.tar.xz
  sudo python3 docker_full_backup.py backup nginx mysql -o /backup/web-stack.tar.xz
  sudo python3 docker_full_backup.py archive-list /backup/web-stack.tar.xz
  sudo python3 docker_full_backup.py restore /backup/web-stack.tar.xz --all
  sudo python3 docker_full_backup.py restore /backup/web-stack.tar.xz nginx mysql

Default behavior:
  * backup with no container arguments opens a CLI selection menu
  * restore with no container arguments opens a CLI selection menu
  * bind/volume/container conflicts are asked interactively
  * selected running containers are stopped cleanly and restarted after backup
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list local Docker containers")
    p_list.set_defaults(func=list_command)

    p_backup = sub.add_parser("backup", help="backup selected containers")
    p_backup.add_argument("containers", nargs="*", help="container names or IDs")
    p_backup.add_argument("-a", "--all", action="store_true", help="select all containers")
    p_backup.add_argument("-o", "--output", help="final .tar.xz output path")
    p_backup.add_argument("--consistency", choices=("stop", "pause", "live"), default="stop",
                          help="stop: graceful stop (default); pause: freeze; live: no global quiesce")
    p_backup.add_argument(
        "--xz-threads",
        default=DEFAULT_XZ_THREADS,
        help=(
            "xz worker threads: auto (default) uses all available CPUs with a "
            "dynamic RAM limit; 0 is an alias for auto; or specify a positive integer"
        ),
    )
    p_backup.add_argument("--no-logs", action="store_true", help="do not archive historical Docker log files")
    p_backup.add_argument("--non-interactive", action="store_true", help="disable selection prompts")
    p_backup.add_argument("--work-dir", help="temporary working parent directory (default /var/tmp)")
    p_backup.add_argument("--keep-workdir", action="store_true", help="keep temporary files for debugging")
    p_backup.add_argument("--force", action="store_true", help="replace an existing output archive")
    p_backup.set_defaults(func=backup_command)

    p_restore = sub.add_parser("restore", help="restore containers from a backup archive")
    p_restore.add_argument("archive", help="backup .tar.xz path")
    p_restore.add_argument("containers", nargs="*", help="container names or IDs from the archive")
    p_restore.add_argument("-a", "--all", action="store_true", help="restore all containers in the archive")
    p_restore.add_argument("--conflict", choices=("overwrite", "alternate", "existing", "fail"), default="alternate",
                           help="non-interactive bind/volume conflict policy")
    p_restore.add_argument("--container-conflict", choices=("overwrite", "alternate", "existing", "fail"), default="alternate",
                           help="non-interactive container-name conflict policy")
    p_restore.add_argument("--non-interactive", action="store_true", help="never prompt; use conflict policies")
    p_restore.add_argument("--no-start", action="store_true", help="create containers but do not start them")
    p_restore.add_argument("--start-all", action="store_true", help="start restored containers even if originally stopped")
    p_restore.add_argument("--no-logs", action="store_true", help="do not extract archived historical logs")
    p_restore.add_argument("--logs-dir", help="directory for archived historical logs")
    p_restore.add_argument("--docker-socket", default=DEFAULT_DOCKER_SOCKET, help="Docker Engine Unix socket")
    p_restore.add_argument("--work-dir", help="temporary working parent directory (default /var/tmp)")
    p_restore.add_argument("--keep-workdir", action="store_true", help="keep temporary files for debugging")
    p_restore.set_defaults(func=restore_command)

    p_alist = sub.add_parser("archive-list", help="list containers stored in a backup archive")
    p_alist.add_argument("archive", help="backup .tar.xz path")
    p_alist.set_defaults(func=archive_list_command)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except KeyboardInterrupt:
        eprint("\nCancelled.")
        return 130
    except BackupError as exc:
        eprint(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
