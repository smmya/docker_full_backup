# mw_backup — mdserver-web 面板全量备份工具

> Python 3.8+ · 纯标准库 · 仅依赖 `tar` + `xz` · 单文件 0 依赖

## 一、它做什么

一键备份 mdserver-web 面板服务器的**所有关键数据**，打包为单个 `.tar.xz` 归档：

- 面板自身状态（SQLite 数据库、SSL 证书、运行参数）
- 已安装插件的配置与数据
- 数据库（MySQL/MariaDB/PostgreSQL/Redis/MongoDB）——**命令导出**，不拷原始数据目录
- 计划任务脚本
- 业务站点文件（wwwroot）
- 系统关键目录（`/etc` 白名单、`/root`、`/opt`、`/home`）

明确**不含**：二进制包、缓存、构建产物、node_modules、`*.pyc`、`.git`、版本 tar 包等中间文件。

**只做备份，不做还原。** 归档内含 `manifest.json`（元数据）和 `checksums.sha256`（逐文件校验和），用于事后核对完整性。

---

## 二、工作原理

### 2.1 面板自动探测（三级）

| 级别 | 方式 | 说明 |
|------|------|------|
| 1 | 候选列表 | `/www/server/mdserver-web` 等 5 个常见安装路径 |
| 2 | 当前目录回溯 | 从 `cwd` 向上逐级查找面板标志 |
| 3 | 动态扫描 | `/www` `/opt` `/home` `/root` `/usr/local` 下 2 层深度搜索 |

判定条件：目录下同时存在 `app.py` + `data/` + `plugins/`。

探测失败时降级为仅备份系统目录，不会报错退出。也可用 `--panel-root` 显式指定。

### 2.2 目录布局推导

找到面板根后，按 mdserver-web 约定推导其余路径：

```
panelDir:         /www/server/mdserver-web
  fatherDir:      /www
  serverDir:      /www/server          ← 插件运行目录
  wwwroot:        /www/wwwroot         ← 站点根（可被面板选项覆盖）
```

### 2.3 插件采集

扫描 `serverDir/` 下的子目录作为已安装插件（排除 `web_conf`、`wwwlogs`、`recycle_bin`、`cron` 等共享目录），每个插件采集两类内容：

| 采集内容 | 排除规则 |
|----------|----------|
| 配置（`etc/`） | 保留 `.conf`/`.cnf`/`.ini`，排除 `*.tar.gz` |
| 数据（`data/`） | 严格排除二进制、缓存、node_modules、`.git` 等 |

DB 类插件（MySQL/PostgreSQL/Redis）**不拷原始数据目录**，改用命令导出。

### 2.4 数据库导出

| 数据库 | 导出方式 | 输出文件 |
|--------|----------|----------|
| MySQL / MariaDB | `mysqldump --databases <db>` 按库拆分 | `databases/mysql-<dbname>.sql` |
| PostgreSQL | `pg_dumpall` | `databases/postgresql-all.sql` |
| Redis | `redis-cli --rdb` | `databases/redis.rdb` |
| MongoDB | `mongodump --archive` | `databases/mongodb.archive` |

**密码自动读取**：从 `/www/server/mysql/mysql.db`（panel 内部 SQLite）读取 `mysql_root` 密码。
**连接策略**：socket → my.cnf → TCP 127.0.0.1 → 带密码。
**容错**：某库导出失败时记入 manifest，继续下一个；所有导出均失败时回退为 `--all-databases`。

### 2.5 系统目录备份

| 目录 | 策略 | 说明 |
|------|------|------|
| `/root` | 排除二进制/中间文件 | 保留脚本、配置、密钥 |
| `/opt` | 排除二进制/中间文件 | 保留自定义应用的配置 |
| `/etc` | **白名单** | 仅备份：网络配置、用户/账户、systemd 自建服务 |
| `/home` | 默认备份 | `--no-home` 关闭 |

`/etc` 白名单自动探测 systemd 用户自建服务引用的配置文件路径。

### 2.6 压缩

默认 `xz -6`（速度优先），`--max-compress` 切到 `xz -9e`（体积优先）。
线程数按 `CPU 核心数 × 内存/700MiB` 自动收敛，`--threads` 可覆盖。

---

## 三、用法

### 3.1 一键全量备份

```bash
sudo python3 mw_backup.py backup
```

备份面板 + 系统 + 站点 + /home。唯一需要的命令。

### 3.2 交互式

```bash
sudo python3 mw_backup.py backup -i
```

逐项提示确认，回车接受默认值，改为输入新值覆盖。适合首次使用或不记参数。

### 3.3 预览

```bash
python3 mw_backup.py list
```

显示所有将要备份的内容（插件列表、采集单元、文件统计），不写入任何文件。

### 3.4 排除内容

```bash
sudo python3 mw_backup.py backup --no-wwwroot    # 不备份站点数据
sudo python3 mw_backup.py backup --no-home        # 不备份 /home
sudo python3 mw_backup.py backup --no-wwwroot --no-home
```

### 3.5 压缩控制

```bash
sudo python3 mw_backup.py backup --max-compress   # xz -9e 极限压缩
sudo python3 mw_backup.py backup --threads 4      # 手动指定线程数
```

### 3.6 面板不在标准位置

```bash
sudo python3 mw_backup.py backup --panel-root /custom/path/mdserver-web
```

### 3.7 输出控制

```bash
sudo python3 mw_backup.py backup -o /backup/weekly.tar.xz   # 指定输出路径
sudo python3 mw_backup.py backup --force                     # 覆盖已存在归档
sudo python3 mw_backup.py backup --verify                    # 备份后校验完整性
```

### 3.8 /etc 白名单扩展

```bash
sudo python3 mw_backup.py backup --etc-allowlist-extra /etc/myapp \
                                  --etc-allowlist-extra /etc/another
sudo python3 mw_backup.py backup --no-service-autodetect    # 关闭 systemd 自动探测
```

### 3.9 调试

```bash
sudo python3 mw_backup.py backup --keep-workdir   # 保留暂存目录
sudo python3 mw_backup.py backup --dry-run         # 等同于 list
sudo python3 mw_backup.py backup --max-file-size 0 # 不限制单文件大小
```

---

## 四、全部参数

| 参数 | 说明 |
|------|------|
| `--panel-root PATH` | 面板安装根目录（默认自动探测） |
| `--no-wwwroot` | 不备份业务站点数据 |
| `--no-home` | 不备份 /home |
| `--max-compress` | 使用 xz -9e 极限压缩 |
| `--threads N` | 压缩线程数（默认按 CPU 和内存自动） |
| `--max-file-size MiB` | 单文件上限（0=不限制，默认 256） |
| `-o, --output PATH` | 输出路径（可为文件或目录） |
| `--work-dir PATH` | 暂存目录（默认 /var/tmp） |
| `--keep-workdir` | 保留暂存目录 |
| `--force` | 覆盖已存在输出文件 |
| `--verify` | 备份后解包逐文件校验 SHA-256 |
| `--dry-run` | 等同于 list，只预览 |
| `--etc-allowlist-extra PATH` | 追加 /etc 白名单路径（可重复） |
| `--no-service-autodetect` | 关闭 systemd 服务配置自动探测 |
| `-i, --interactive` | 交互式问答模式 |

---

## 五、归档内部结构

```
mw-backup-<hostname>-<YYYYMMDD-HHMMSS>.tar.xz
├── manifest.json          # 版本、主机名、插件列表、文件统计、DB导出状态
├── checksums.sha256       # 逐文件 SHA-256 校验和
├── databases/             # 数据库导出（按库拆分）
│   ├── mysql-<db1>.sql
│   ├── mysql-<db2>.sql
│   ├── postgresql-all.sql
│   └── redis.rdb
├── mw-server/             # 面板一切
│   ├── panel/             # panel.db、*.pl、SSL 证书
│   ├── panel/server/      # nginx vhost、php 处理器配置、插件元数据库
│   ├── cron/              # 计划任务脚本（不含 *.log）
│   └── plugins/<name>/    # 各插件配置与数据
├── wwwroot/               # 业务站点文件
└── file/                  # 系统目录
    ├── etc/               # /etc 白名单
    ├── root/              # /root
    ├── opt/               # /opt
    └── home/              # /home
```

---

## 六、典型场景

```bash
# 每日自动备份（crontab）
0 3 * * * root /usr/local/bin/mw_backup.py backup -o /backup/daily-$(date +\%Y\%m\%d).tar.xz

# 迁移前完整备份
sudo mw_backup.py backup --max-compress --verify -o /backup/migrate.tar.xz

# 排查问题：先看要备什么
python3 mw_backup.py list --panel-root /www/server/mdserver-web

# 交互式（不记参数）
sudo mw_backup.py backup -i
```

## 七、环境要求

- Linux（root 执行）
- Python 3.8+
- `tar` + `xz`（系统预装）
- 数据库导出额外需要 `mysqldump` / `mariadb-dump` / `pg_dumpall` / `redis-cli` / `mongodump`（缺失则 skip 并 warn）
