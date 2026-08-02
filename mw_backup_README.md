# mw_backup

> mdserver-web 面板服务器**全量备份工具** — 一键打包面板、插件、数据库、站点、系统关键配置。

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![License MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

纯 Python 标准库实现，仅依赖系统预装的 `tar` 和 `xz`。**只做备份，不做还原**——归档内含完整清单与校验和，用于事后核对完整性或人工按需取用。

---

## 目录

- [设计原理](#设计原理)
- [归档结构](#归档结构)
- [安装](#安装)
- [快速开始](#快速开始)
- [子命令](#子命令)
- [全部参数](#全部参数)
- [典型迁移流程](#典型迁移流程)
- [校验与完整性](#校验与完整性)
- [故障排查](#故障排查)
- [已知限制](#已知限制)

---

## 设计原理

### 核心思路：发现 → 分类 → 导出 → 校验 → 打包

```
面板探测         插件采集           DB 导出          校验和          单归档
(三级探测)  →   (config+data)  →   (命令导出)  →   (SHA-256)  →   (tar.xz)
系统目录          白名单过滤         站点文件          manifest
(/etc/root/opt)  (关键配置)        (排除二进制)       (元数据)
```

### 为什么不用 `--all-databases`？

MySQL 按库拆分导出——每个数据库独立一个 `.sql` 文件。恢复时只倒需要的库，不用从一整坨 `all.sql` 里手割。系统库（`information_schema`、`performance_schema`、`mysql`、`sys`）自动过滤。

### 为什么命令导出而不是直接拷数据目录？

- 跨版本兼容性：`mysqldump` 可导入不同小版本的 MySQL/MariaDB
- 一致性：`--single-transaction` 保证 InnoDB 表一致性快照
- 安全：不需要停下来拷文件，生产环境无中断

### 面板自动探测（三级）

| 级别 | 方式 | 说明 |
|:---:|------|------|
| 1 | **候选列表** | `/www/server/mdserver-web` 等 5 个硬编码路径，直接匹配 |
| 2 | **CWD 回溯** | 从当前工作目录向上逐级查找面板标志 (`app.py` + `data/` + `plugins/`) |
| 3 | **动态扫描** | 在 `/www` `/opt` `/home` `/root` `/usr/local` 下 `iterdir` 2 层搜索 |

探测失败时降级为「仅备份系统目录」，不会中断。日志会标明探测方法：`候选列表命中 / CWD 回溯 / 自动扫描发现 / 显式指定`。

### 数据库密码自动读取

MySQL root 密码从 `/www/server/mysql/mysql.db`（面板内部 SQLite）的 `config` 表 `mysql_root` 列读取，连接尝试顺序：

```
socket → my.cnf → TCP 127.0.0.1 → 带密码
```

任意一步成功即停止。密码库读取失败（文件不存在/损坏）静默跳过，不影响其他尝试路径。

---

## 归档结构

```
mw-backup-<hostname>-<YYYYMMDD-HHMMSS>.tar.xz
│
├── manifest.json              # 工具版本、主机名、面板路径、插件列表、
│                              # 各单元文件数/字节数、DB导出状态、压缩参数
├── checksums.sha256           # 逐文件 SHA-256，可用于 --verify 校验
│
├── databases/                 # 数据库导出（按库拆分）
│   ├── mysql-<dbname>.sql
│   ├── postgresql-all.sql
│   ├── redis.rdb
│   └── mongodb.archive
│
├── mw-server/                 # 面板一切
│   ├── panel/                 #   panel.db / system.db / *.pl 运行参数
│   │   └── ssl/               #   面板 HTTPS 证书（cert.pem + private.pem）
│   ├── panel/server/          #   nginx vhost / php 处理器 / 插件元数据库
│   ├── cron/                  #   面板计划任务脚本（排除 *.log 执行日志）
│   └── plugins/<name>/        #   各插件配置（etc/）与非 DB 数据（data/）
│       ├── config/
│       └── data/
│
├── wwwroot/                   # 业务站点文件（默认备份，--no-wwwroot 关闭）
│
└── file/                      # 系统目录
    ├── etc/                   #   /etc 白名单（网络/账户/自建服务配置）
    ├── root/                  #   /root（已排除二进制与中间文件）
    ├── opt/                   #   /opt（已排除二进制与中间文件）
    └── home/                  #   /home（默认备份，--no-home 关闭）
```

### 各单元排除策略

| 单元 | Profile | 排除内容 |
|------|---------|----------|
| 面板数据 / 站点 / 系统目录 | `strict` | `node_modules` `.git` `__pycache__` `*.pyc` `*.log` `*.pid` `*.sock` 版本 tar 包、二进制 `*.so` 等 |
| 插件配置 / 面板配置 / cron 脚本 | `config` | 仅排除 `*.log` `*.pid` `*.sock`，保留 `.conf` `.cnf` `.ini` `.pem` |

---

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/smmya/docker_full_backup/main/mw_backup.py \
  -o /usr/local/bin/mw_backup.py
chmod +x /usr/local/bin/mw_backup.py
```

### 依赖

| 依赖 | 来源 | 说明 |
|------|------|------|
| Python 3.8+ | 系统 | 脚本解释器 |
| `tar` | 系统 | 归档打包 |
| `xz` | 系统 | 压缩 |
| `mysqldump` / `mariadb-dump` | 可选 | MySQL 导出，缺失则 skip |
| `pg_dumpall` | 可选 | PostgreSQL 导出，缺失则 skip |
| `redis-cli` | 可选 | Redis RDB 导出，缺失则 skip |
| `mongodump` | 可选 | MongoDB 归档导出，缺失则 skip |

---

## 快速开始

```bash
# 预览将要备份的内容（dry-run，不写任何文件）
python3 mw_backup.py list

# 一键全量备份（面板 + 系统 + 站点 + 数据库 + /home）
sudo python3 mw_backup.py backup

# 交互式（逐项确认，不记参数）
sudo python3 mw_backup.py backup -i

# 面板不在标准位置
sudo python3 mw_backup.py backup --panel-root /custom/path/mdserver-web

# 每日定时（crontab）
0 3 * * * root /usr/local/bin/mw_backup.py backup -o /backup/daily-$(date +\%Y\%m\%d).tar.xz
```

---

## 子命令

### `list` — 预览

```bash
python3 mw_backup.py list
python3 mw_backup.py list --panel-root /www/server/mdserver-web
python3 mw_backup.py list --no-wwwroot --no-home
```

显示：面板路径、探测方式、已安装插件列表（含 DB 引擎标注）、每个采集单元的文件数/体积、`/etc` 白名单命中详情。**不写任何文件**，非 root 也能跑（部分路径可能读不到，预览结果偏小）。

### `backup` — 执行备份

```bash
sudo python3 mw_backup.py backup
```

完整流程：

1. 面板探测 → 构建采集计划
2. **确认提示**：列出所有插件、采集单元、输出路径，等待回车（非 TTY 环境跳过）
3. 数据库导出（优先，保证 dump 与配置尽量同一时刻）
4. 逐单元采集文件至暂存目录
5. 生成 `manifest.json` + `checksums.sha256`
6. 整体打包为 `.tar.xz`
7. 可选 `--verify`：解包逐文件校验
8. 清理暂存目录（`--keep-workdir` 保留）

---

## 全部参数

### 通用参数（`backup` 和 `list` 共享）

| 参数 | 类型 | 默认 | 说明 |
|------|:--:|------|------|
| `--panel-root PATH` | string | 自动探测 | 面板安装根目录 |
| `--no-wwwroot` | flag | false | 不备份业务站点数据 |
| `--no-home` | flag | false | 不备份 `/home` |
| `--max-compress` | flag | false | 使用 `xz -9e` 极限压缩（体积更小，速度慢 3-5 倍） |
| `--threads N` | int | 按 CPU+内存自动收敛 | xz 压缩线程数 |
| `--max-file-size MiB` | int | 256 | 单文件体积上限，超过则跳过并记入 manifest（0=不限制） |
| `--etc-allowlist-extra PATH` | string | - | 追加 `/etc` 白名单路径，可重复指定 |
| `--no-service-autodetect` | flag | false | 关闭 systemd 自建服务自动探测 |
| `-i` `--interactive` | flag | false | 交互式问答模式（逐项确认） |

### `backup` 专属参数

| 参数 | 类型 | 默认 | 说明 |
|------|:--:|------|------|
| `-o` `--output PATH` | string | `./mw-backup-<host>-<ts>.tar.xz` | 输出路径（可为文件或目录） |
| `--work-dir PATH` | string | `/var/tmp` | 暂存父目录 |
| `--keep-workdir` | flag | false | 保留暂存目录以便排查 |
| `--force` | flag | false | 覆盖已存在的输出文件 |
| `--verify` | flag | false | 备份后解包逐文件校验 SHA-256（较慢） |
| `--dry-run` | flag | false | 等同于 `list`，只预览不写归档 |

### 压缩档位对比

| 档位 | 触发方式 | 速度 | 体积 | 适用 |
|------|----------|------|------|------|
| `xz -6` | **默认** | 快 | 适中 | 每日 cron |
| `xz -9e` | `--max-compress` | 慢 3-5 倍 | 最小 | 长期归档、传输 |

---

## 典型迁移流程

### 从 A 服务器迁移到 B 服务器

```bash
# === A 服务器：完整备份 ===
sudo mw_backup.py backup --max-compress --verify -o /tmp/migrate.tar.xz
scp /tmp/migrate.tar.xz user@B:/tmp/

# === B 服务器：查看归档内容 ===
xz -dc /tmp/migrate.tar.xz | tar tvf - | head -50

# === B 服务器：按需恢复 ===
# 解压全部
mkdir -p /tmp/restore && cd /tmp/restore
tar -I'xz -d' -xvf /tmp/migrate.tar.xz

# 面板状态
cp -a mw-server/panel/* /www/server/mdserver-web/data/
cp -a mw-server/panel/ssl/* /www/server/mdserver-web/ssl/

# 数据库
mysql -u root -p < databases/mysql-wordpress.sql

# 站点文件
cp -a wwwroot/* /www/wwwroot/

# 系统配置
cp -a file/etc/nginx /etc/
```

### 快速验证归档完整性

```bash
# 校验清单统计
xz -dc backup.tar.xz | tar tvf - | wc -l                    # 总条目数
xz -dc backup.tar.xz | tar tvf - | grep "./databases/" | wc -l  # SQL 文件数
xz -dc backup.tar.xz | tar tvf - | grep "./mw-server/" | wc -l  # 面板条目

# 读取 manifest 摘要
xz -dc backup.tar.xz | tar xOf - manifest.json | python3 -c \
"import json,sys;m=json.load(sys.stdin);print(f'文件:{m[\"totals\"][\"file_count\"]} 原始大小:{m[\"totals\"][\"bytes_uncompressed\"]}')"
```

---

## 校验与完整性

### 备份时在线校验

```bash
sudo mw_backup.py backup --verify
```

备份完成后自动解包到临时目录，逐文件比对 `checksums.sha256` 中的 SHA-256。校验通过则删除临时目录，失败则报错并保留。

### 事后手动校验

```bash
mkdir /tmp/verify && cd /tmp/verify
tar -I'xz -d' -xvf /backup/mw-backup-xxx.tar.xz
sha256sum -c checksums.sha256 | grep -v "OK$"
```

如果输出为空，表示所有文件校验通过。有输出表示该文件损坏。

### manifest.json 关键字段

```json
{
  "version": "1.0.0",
  "hostname": "racknerd-xxx",
  "panel_root": "/www/server/mdserver-web",
  "panel_discovery_method": "candidates",
  "panel_layout": { "panel_dir": "...", "server_dir": "..." },
  "detected_plugins": ["mysql", "openresty", "php", "redis"],
  "include_wwwroot": true,
  "include_home": true,
  "db_list": [ { "plugin": "mysql", "engine": "mysql", "dbs": ["qdshangfeiji"], "ok": true } ],
  "totals": { "file_count": 4555, "bytes_uncompressed": 193000000 },
  "compression": { "program": "xz", "level": "-6", "threads": 2 },
  "units": [ { "id": "...", "archive_prefix": "mw-server/panel", "stats": {...} } ]
}
```

---

## 故障排查

### 面板探测失败

```
[!] 未探测到 mdserver-web 面板安装目录
```

**原因**：面板不在标准路径。**解决**：手动指定 `--panel-root /实际路径`。新版已升级为三级探测，大部分场景自动发现。

### MySQL 导出失败：Access denied

```
[!] 数据库导出失败: mysql: Access denied for user 'root'@'localhost'
```

**原因**：MySQL root 有密码但密码库不存在/损坏。**解决**：确认 `/www/server/mysql/mysql.db` 中存在 `config` 表且 `mysql_root` 有值。新版已自动从该库读取密码。

### 压缩时间过长

```bash
# 默认 -6，已经很快。如果仍嫌慢：
sudo mw_backup.py backup --threads 1 --no-wwwroot
```

`-9e` 是 `-6` 的 3-5 倍耗时。不需要极限压缩时不要加 `--max-compress`。

### 暂存目录空间不足

```bash
sudo mw_backup.py backup --work-dir /mnt/large-disk
```

暂存目录需要约 **2 倍** 原始数据大小的可用空间（暂存文件 + 最终归档）。默认 `/var/tmp`。

### 非 TTY 环境（cron / CI）卡死

确认提示仅在有 `stdin` 是终端时显示。Cron 环境自动跳过，不会卡死。如果 cron 确实卡住了，检查 `sys.stdin.isatty()` 在你的 cron 环境中是否误判——极罕见。

### 交互模式在管道中报错

```
错误: 当前环境不支持交互输入
```

交互模式需要终端。在 cron/管道中使用非交互模式（不带 `-i`）。

---

## 已知限制

| 限制 | 说明 | 影响 |
|------|------|------|
| **仅备份不还原** | 设计意图，不是缺陷 | — |
| **MySQL/MariaDB 必须可连接** | `mysqldump` 需要服务运行且能认证 | DB 导出 skip，记入 manifest |
| **PostgreSQL/Redis/MongoDB 需客户端** | `pg_dumpall`/`redis-cli`/`mongodump` 缺失则 skip | 对应 DB 导出 skip |
| **单文件 256 MiB 上限（默认）** | `--max-file-size 0` 可关闭 | 超大文件跳过，记入 manifest |
| **平台限制** | 仅 Linux（依赖 `tar`/`xz`/`systemd`） | macOS/WSL 不可用 |
| **`/etc` 白名单为硬编码 + systemd 探测** | 未覆盖的自建服务配置需 `--etc-allowlist-extra` | 该服务配置不进入归档 |
| **Python 3.8+** | `TextIO.reconfigure()` 需要 3.7+ | 更低版本不支持 |
| **暂存目录空间** | 约需 2 倍原始数据大小的可用空间 | 磁盘满时报错 |
| **不备份业务站点日志** | `wwwlogs/` 不在采集范围内 | 需审计日志时另行处理 |
| **软链接** | `tar` 打包时保形存储（不跟随），但 `/root` 中的绝对路径软链接会被 tar 跟随 | 归档中可能出现多余系统目录，不影响数据完整性 |
