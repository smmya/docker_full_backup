# Docker Full Backup

面向 Debian/Ubuntu Linux 的 Docker 容器完整备份与恢复工具。

当前文档对应脚本版本：**1.2.1**。

该工具将所选容器的镜像、容器可写层、Docker 配置、bind mount、Docker volume、自定义网络、可发现的 Docker Compose 配置文件及历史日志打包为一个 `.tar.xz` 文件，并可在同架构的另一台 Linux 服务器上重建容器。

> 这里的“完整恢复”是指数据和配置层面的冷迁移：容器会在目标服务器重新创建并从正常启动流程启动，不恢复 RAM、PID、TCP 连接或进程执行位置。

---

## 1. 适用范围

### 1.1 优先支持

- Ubuntu 20.04.6 LTS x86_64
- Ubuntu 22.04 / 24.04 / 26.04 x86_64
- Debian 11 / 12 / 13 x86_64
- Python 3.8 及以上版本
- Docker Engine 使用本地 Linux 存储

脚本仅依赖：

- Python 3 标准库
- Docker CLI 和 Docker Engine
- GNU tar
- xz-utils

不需要安装第三方 Python 包。

### 1.2 常见适用场景

- 将 Docker 服务迁移到另一台服务器
- 为 VPS 上的全部容器制作单文件归档
- 在升级系统、重装服务器前保存 Docker 环境
- 保存停止状态或运行状态容器的配置和数据
- 对 Docker Compose 项目进行整组冷备份
- 在保留原目录的情况下测试恢复是否可用

### 1.3 不属于本工具目标的场景

- CRIU 热迁移
- 恢复运行中的内存状态
- 恢复原 PID
- 恢复已经建立的 TCP/UDP 会话
- Docker Swarm 服务级迁移
- Kubernetes 工作负载迁移
- 跨 CPU 架构透明运行，例如 amd64 镜像直接恢复到 arm64
- 非本地 volume driver 的通用备份，例如某些 NFS、CIFS、云存储插件卷

---

## 2. 备份内容

对于选中的容器，脚本会保存以下内容。

### 2.1 镜像和容器可写层

- 容器原始基础镜像
- 原始镜像已有的 RepoTags
- 每个容器通过 `docker commit` 生成的快照镜像
- 容器未挂载路径中的文件变化
- 容器内部安装的软件和修改过的文件

快照镜像建立在原始镜像层之上，因此多个容器共享的镜像层只保存一次。

### 2.2 容器创建配置

脚本保存完整的 `docker inspect`，恢复时根据它重建容器，包括但不限于：

- 容器名称
- Hostname 和 Domainname
- 环境变量
- Entrypoint
- Cmd
- WorkingDir
- User
- Labels
- Healthcheck
- ExposedPorts
- 端口映射
- 重启策略
- CPU 和内存限制
- Capability
- Privileged
- Devices
- DNS
- ExtraHosts
- SecurityOpt
- ShmSize
- ReadonlyRootfs
- IPC、PID、UTS、User namespace 模式
- bind mount 和 volume 关系
- Docker 网络和网络别名
- `VolumesFrom`、传统 Links、`container:<id>` 网络模式引用

恢复后的容器使用归档中的快照镜像，而不是仅依赖远程仓库重新拉取镜像。

### 2.3 bind mount

保存容器使用的宿主机目录、普通文件或符号链接，例如：

```text
-v /srv/mysql:/var/lib/mysql
-v /opt/app/.env:/app/.env
```

同一个宿主机源路径被多个容器共享时，只归档一次。

父子嵌套挂载也会分别记录，例如：

```text
/srv/app
/srv/app/data/redis
```

恢复时会先恢复父目录，再让子挂载跟随父目录的新映射位置，并用子挂载自己的归档覆盖对应子目录。

### 2.4 Docker volume

支持：

- named volume
- anonymous volume
- Docker `local` driver 且具有可访问 Mountpoint 的卷

对于非本地驱动，脚本会中止备份，而不是生成一份表面成功但缺失 volume 数据的归档。

### 2.5 Docker 网络

保存所选容器使用的网络：

- 内置 `bridge`、`host`、`none` 网络只记录引用
- 用户自定义网络保存完整 Inspect 配置
- 恢复网络驱动、IPAM、Subnet、Gateway、Options、Labels、IPv6 等配置
- 恢复网络别名和显式 IPAM 地址

若目标服务器已经存在同名网络，脚本会比较关键配置。配置兼容时复用，不兼容时停止恢复。

### 2.6 Docker Compose 配置

脚本根据以下 Compose Labels 查找配置：

```text
com.docker.compose.project.config_files
com.docker.compose.project.working_dir
```

可以保存：

- `compose.yaml`
- `docker-compose.yaml`
- 工作目录下的 `.env`

这里只能保存从容器 Labels 中可以定位到、并且实际存在的文件。未被 Labels 引用的额外配置文件不会自动扫描整个项目目录。

### 2.7 容器历史日志

默认归档 Docker `LogPath` 以及相关轮转文件。

恢复时这些日志会解压到普通目录，但 Docker 不支持把历史日志重新注入当前容器的活动日志驱动。因此：

- 日志内容不会丢失
- `docker logs` 只显示恢复后新产生的日志
- 旧日志保存在单独目录供查阅

### 2.8 宿主机元数据

归档中还会记录：

- Docker version
- Docker info
- `/etc/os-release`
- 内核版本
- Python 版本
- CPU 架构
- `/etc/docker/daemon.json`，若该文件存在

这些信息用于排查和对照，不会自动覆盖目标服务器的 Docker daemon 配置。

---

## 3. 工作原理

## 3.1 为什么不使用 `docker export`

`docker export` 会把单个容器根文件系统展开为 tar，但存在几个问题：

- 不包含 bind mount 和 volume 数据
- 不保留镜像分层
- 不保留原始镜像历史和标签
- 不保留环境变量、端口、重启策略、网络等容器创建配置
- 多个容器共享的基础文件会被重复导出

本工具使用以下组合：

```text
原始镜像层
+ 每个容器的快照增量层
+ docker inspect 配置
+ bind mount / volume 数据归档
+ 网络和 Compose 配置
```

这样既保留 Docker 原生镜像结构，又减少重复数据。

## 3.2 镜像快照流程

对每个容器执行等效操作：

```bash
docker commit <container> docker-full-backup/snapshot:<backup-id>
```

`docker commit` 只捕获容器可写层，不包含挂载目录，因此挂载数据必须另外归档。

原始镜像和全部快照镜像通过一次 `docker image save` 保存：

```bash
docker image save <original-tags> <snapshot-tags...>
```

Docker 镜像层按内容存储，共享层只在镜像 tar 中出现一次。

## 3.3 挂载数据归档流程

每个唯一 bind mount 或 volume 独立形成一个 `.tar.xz`：

```text
mounts/bind/<id>.tar.xz
mounts/volume/<id>.tar.xz
```

归档时使用 GNU tar 保存：

- 数字 UID/GID
- 文件权限
- ACL
- xattrs
- sparse file
- 符号链接

需要 root 权限才能尽可能完整地恢复这些元数据。

## 3.4 配置和校验

所有对象由 `manifest.json` 建立索引，并生成：

```text
checksums.sha256
```

恢复前会验证归档内部所有文件的 SHA-256。任何文件缺失或哈希不匹配都会停止恢复。

## 3.5 单一归档

内部所有内容最终再次打包为一个文件：

```text
docker-backup.tar.xz
```

因此传输和长期保存时只需要管理一个文件。

## 3.6 恢复顺序

恢复流程大致如下：

1. 检查外层 `.tar.xz` 是否有效
2. 拒绝绝对路径和 `..` 路径穿越条目
3. 解压到临时目录
4. 校验备份格式和版本
5. 检查 CPU 架构是否兼容
6. 验证 `checksums.sha256`
7. 加载原始镜像和快照镜像
8. 恢复目标机原本已有的同名镜像标签指向
9. 检查或创建 Docker 网络
10. 处理 bind mount 和 volume 冲突
11. 恢复 Compose 配置
12. 按原始创建时间排序并重建容器
13. 根据原状态或 `--start-all` 启动容器
14. 导出历史日志
15. 清理临时目录

---

## 4. 归档内部结构

典型归档解压后如下：

```text
.
├── manifest.json
├── host.json
├── checksums.sha256
├── containers/
│   ├── 0001-container-a/
│   │   ├── inspect.json
│   │   └── original-state.json
│   └── 0002-container-b/
│       ├── inspect.json
│       └── original-state.json
├── images/
│   └── docker-images.tar.xz
├── mounts/
│   ├── bind/
│   │   └── <mount-id>.tar.xz
│   └── volume/
│       └── <mount-id>.tar.xz
├── networks/
│   └── <network-name>-<hash>.json
├── configs/
│   └── <config-id>-compose.yaml
└── logs/
    └── <log-id>.tar.xz
```

### 4.1 `manifest.json`

记录：

- 归档格式和格式版本
- 工具版本
- 备份 ID
- 创建时间
- CPU 架构
- 一致性模式
- XZ 压缩级别
- 容器清单
- 镜像标签映射
- mount 清单
- 网络清单
- Compose 配置清单
- 日志清单

### 4.2 `inspect.json`

保存容器完整的 `docker inspect` 结果。

### 4.3 `original-state.json`

保存容器备份前是否运行、是否暂停等状态，用于决定恢复后默认是否启动。

---

## 5. 系统要求与安装

## 5.1 安装依赖

Debian/Ubuntu：

```bash
sudo apt-get update
sudo apt-get install -y python3 tar xz-utils curl
```

Docker 尚未安装时，请先安装 Docker Engine。发行版自带包示例：

```bash
sudo apt-get install -y docker.io
sudo systemctl enable --now docker
```

检查：

```bash
python3 --version
tar --version
xz --version
docker version
docker info
```

## 5.2 从 GitHub 快速安装

仓库：

```text
https://github.com/smmya/docker_full_backup
```

安装脚本：

```bash
curl -fsSL \
  https://raw.githubusercontent.com/smmya/docker_full_backup/main/docker_full_backup_reviewed.py \
  -o /tmp/docker-full-backup

sudo python3 -m py_compile /tmp/docker-full-backup
sudo install -m 0755 /tmp/docker-full-backup /usr/local/sbin/docker-full-backup
rm -f /tmp/docker-full-backup

docker-full-backup --version
```

## 5.3 Git Clone 安装

```bash
git clone https://github.com/smmya/docker_full_backup.git
cd docker_full_backup

sudo python3 -m py_compile docker_full_backup_reviewed.py
sudo install -m 0755 docker_full_backup_reviewed.py \
  /usr/local/sbin/docker-full-backup

docker-full-backup --version
```

## 5.4 更新现有安装

```bash
curl -fsSL \
  https://raw.githubusercontent.com/smmya/docker_full_backup/main/docker_full_backup_reviewed.py \
  -o /tmp/docker-full-backup

sudo python3 -m py_compile /tmp/docker-full-backup &&
sudo install -m 0755 /tmp/docker-full-backup \
  /usr/local/sbin/docker-full-backup &&
rm -f /tmp/docker-full-backup &&
sudo docker-full-backup --version
```

只有 Python 语法检查通过后才会覆盖当前版本。

---

## 6. 命令总览

```text
docker-full-backup [--version] <command> [options]
```

支持的子命令：

| 子命令 | 作用 |
|---|---|
| `list` | 列出本机 Docker 容器 |
| `backup` | 备份指定或全部容器 |
| `restore` | 从归档恢复容器 |
| `archive-list` | 查看归档中保存的容器 |

查看帮助：

```bash
docker-full-backup --help
docker-full-backup backup --help
docker-full-backup restore --help
docker-full-backup archive-list --help
```

查看版本：

```bash
docker-full-backup --version
```

---

## 7. `list`：列出容器

```bash
sudo docker-full-backup list
```

显示：

- 容器 ID
- 容器名称
- 镜像
- 当前状态

`list` 只读取 Docker 信息，不修改容器。

---

## 8. `backup`：备份容器

基本语法：

```bash
sudo docker-full-backup backup [容器名称或ID ...] [参数]
```

## 8.1 交互选择

不指定容器，也不使用 `--all`：

```bash
sudo docker-full-backup backup \
  -o /backup/docker-backup.tar.xz
```

脚本会显示容器编号，可以输入：

```text
1,2,4
```

或：

```text
a
```

选择全部。

## 8.2 备份全部容器

```bash
sudo mkdir -p /backup

sudo docker-full-backup backup \
  --all \
  -o /backup/docker-backup.tar.xz
```

`-a` 是 `--all` 的短写：

```bash
sudo docker-full-backup backup -a \
  -o /backup/docker-backup.tar.xz
```

## 8.3 指定容器

```bash
sudo docker-full-backup backup \
  nginx mysql redis \
  -o /backup/web-stack.tar.xz
```

可以使用完整容器 ID、容器 ID 前缀或容器名称。

## 8.4 自动添加时间

```bash
sudo docker-full-backup backup \
  --all \
  -o "/backup/docker-backup-$(date +%Y%m%d-%H%M%S).tar.xz"
```

若不指定 `-o`，默认在当前目录生成：

```text
docker-full-backup-YYYYMMDD-HHMMSS.tar.xz
```

---

## 9. `backup` 全部参数

## 9.1 `containers`

位置参数，指定一个或多个容器：

```bash
sudo docker-full-backup backup app db cache
```

## 9.2 `-a, --all`

选择当前 Docker daemon 中的全部容器，包括运行中和已停止容器：

```bash
sudo docker-full-backup backup --all
```

不会自动备份没有被所选容器使用的孤立镜像。

## 9.3 `-o, --output`

指定最终归档路径：

```bash
-o /backup/docker-backup.tar.xz
```

父目录不存在时会自动创建。

## 9.4 `--consistency`

控制备份期间容器的一致性策略。

### `--consistency stop`

默认值：

```bash
--consistency stop
```

行为：

1. 对备份前正在运行的容器执行正常 `docker stop`
2. 使用容器自身 `StopTimeout`，没有有效值时使用 30 秒
3. 备份完成后重新启动原本运行的容器
4. 原本停止的容器保持停止
5. 原本已经暂停的容器保持暂停

这是数据库和重要业务最推荐的模式。

带 `--rm` / AutoRemove 的运行容器不会被停止，以免容器被自动删除；脚本会改为暂停它。

### `--consistency pause`

```bash
--consistency pause
```

行为：

- 对运行中的容器执行 `docker pause`
- 完成后执行 `docker unpause`
- 不触发应用正常关机流程

优点是停顿时间和应用重启开销较小；缺点是数据库缓存未必以应用级一致方式落盘。

### `--consistency live`

```bash
--consistency live
```

不统一停止或暂停容器，直接读取正在变化的数据。

适合：

- 主要是只读文件
- 可以接受时间点不一致
- 应用自身具备快照机制

不建议直接用于正在写入的 MySQL、PostgreSQL、SQLite、Redis 持久化目录等关键数据。

## 9.5 `--xz-threads`

控制 XZ 压缩线程：

```bash
--xz-threads auto
--xz-threads 0
--xz-threads 1
--xz-threads 2
--xz-threads 4
```

默认：

```text
auto
```

### 自动模式

`auto` 和 `0` 等价。脚本会：

1. 使用 `sched_getaffinity()` 获取当前进程实际可用 CPU 数
2. 无法读取 affinity 时使用 `os.cpu_count()`
3. 从 `/proc/meminfo` 读取 `MemAvailable`
4. 尽量为系统保留至少 512 MiB
5. 最多将当前可用内存约 75% 提供给 XZ
6. 运行 `xz -T0 --memlimit-compress=<限制>`
7. 由 XZ 根据压缩级别和内存预算决定实际线程数

运行时会显示类似：

```text
[+] XZ auto tuning: CPUs=4, MemAvailable=6144 MiB, compression memory limit=4608 MiB
```

### 手动模式

```bash
--xz-threads 2
```

会直接使用：

```text
xz -T2
```

手动模式不会自动追加内存限制。设置较高线程数时需自行确认内存足够。

## 9.6 `--max-compress`

默认所有内部归档和最终总归档使用：

```text
xz -6
```

启用：

```bash
--max-compress
```

后，统一改为：

```text
xz -9e
```

示例：

```bash
sudo docker-full-backup backup \
  --all \
  --max-compress \
  -o /backup/docker-backup-max.tar.xz
```

`-9e` 可能明显增加 CPU、内存和压缩时间，通常只适合：

- 长期冷存储
- 网络传输成本高
- 磁盘空间非常紧张
- 可以接受较长备份时间

日常备份建议使用默认 `-6`。

## 9.7 `--no-logs`

不归档历史 Docker 日志：

```bash
--no-logs
```

可减少归档大小和备份时间，特别适合日志很大的容器。

这不会影响容器业务数据，但恢复后无法查看归档前的历史日志副本。

## 9.8 `--non-interactive`

禁用容器选择交互：

```bash
--non-interactive
```

备份时必须同时：

- 指定容器名称/ID；或
- 使用 `--all`

否则脚本会报错。

适合 cron、自动化脚本和无终端环境。

## 9.9 `--work-dir`

指定临时工作目录的父目录：

```bash
--work-dir /mnt/large-disk/tmp
```

默认：

```text
/var/tmp
```

工作目录中会暂时存放：

- 独立 mount 归档
- 镜像归档
- Inspect JSON
- Compose 配置
- 日志归档
- manifest 和校验文件

临时空间不足时应将其指向更大的磁盘。

## 9.10 `--keep-workdir`

备份完成或失败后保留临时目录：

```bash
--keep-workdir
```

适合调试。正常使用不建议开启，否则会占用额外磁盘空间。

## 9.11 `--force`

允许覆盖已存在的输出归档：

```bash
--force
```

没有该参数时，如果输出文件已经存在，脚本会停止。

---

## 10. 推荐备份命令

## 10.1 日常完整备份

```bash
sudo mkdir -p /backup

sudo docker-full-backup backup \
  --all \
  --consistency stop \
  -o "/backup/docker-backup-$(date +%Y%m%d-%H%M%S).tar.xz"
```

默认使用：

- XZ `-6`
- 自动 CPU/内存调优
- 归档历史日志
- 正常停止并恢复运行状态

## 10.2 不备份日志

```bash
sudo docker-full-backup backup \
  --all \
  --consistency stop \
  --no-logs \
  -o "/backup/docker-backup-$(date +%Y%m%d-%H%M%S).tar.xz"
```

## 10.3 极限压缩

```bash
sudo docker-full-backup backup \
  --all \
  --consistency stop \
  --max-compress \
  -o "/backup/docker-backup-max-$(date +%Y%m%d-%H%M%S).tar.xz"
```

## 10.4 固定双线程

```bash
sudo docker-full-backup backup \
  --all \
  --xz-threads 2 \
  -o /backup/docker-backup.tar.xz
```

## 10.5 指定大容量临时盘

```bash
sudo docker-full-backup backup \
  --all \
  --work-dir /mnt/backup-work \
  -o /backup/docker-backup.tar.xz
```

---

## 11. 备份选择和共享数据注意事项

若只选择部分容器，而某个 mount 同时被未选择容器使用，脚本会警告：

```text
Mount ... is also used by unselected containers. Consistency is not guaranteed.
```

原因是：

- 被选中的容器可能已经停止
- 未选择的容器仍可能继续写入同一个目录或 volume

解决方式：

- 把共享该 mount 的所有容器一起备份；或
- 手动停止未选择容器；或
- 使用应用原生快照/导出机制

---

## 12. 校验归档

## 12.1 检查外层 XZ

```bash
xz -t /backup/docker-backup.tar.xz
```

没有输出且退出码为 0，表示外层 XZ 流有效。

查看退出码：

```bash
xz -t /backup/docker-backup.tar.xz && echo OK
```

## 12.2 查看归档容器清单

```bash
sudo docker-full-backup archive-list \
  /backup/docker-backup.tar.xz
```

会显示：

- 归档路径
- 创建时间
- CPU 架构
- 容器名称和 ID

## 12.3 手工验证内部 SHA-256

```bash
sudo rm -rf /tmp/docker-backup-verify
sudo mkdir -p /tmp/docker-backup-verify

sudo tar -xJf /backup/docker-backup.tar.xz \
  -C /tmp/docker-backup-verify

cd /tmp/docker-backup-verify
sudo sha256sum -c checksums.sha256
```

恢复命令会自动完成这一步，因此正式恢复前通常不需要手工执行。

## 12.4 校验所有内部 XZ

```bash
sudo find /tmp/docker-backup-verify \
  -type f -name '*.xz' -print0 |
  sudo xargs -0 -r -n1 xz -t
```

---

## 13. 传输归档到另一台服务器

## 13.1 SCP 推送

在源服务器执行：

```bash
scp /backup/docker-backup.tar.xz \
  root@目标服务器IP:/backup/
```

自定义 SSH 端口：

```bash
scp -P 2222 \
  /backup/docker-backup.tar.xz \
  root@目标服务器IP:/backup/
```

使用私钥：

```bash
scp -i /root/.ssh/id_ed25519 \
  -P 2222 \
  /backup/docker-backup.tar.xz \
  root@目标服务器IP:/backup/
```

## 13.2 目标目录不存在

```bash
ssh root@目标服务器IP 'mkdir -p /backup' &&
scp /backup/docker-backup.tar.xz \
  root@目标服务器IP:/backup/
```

## 13.3 传输后比较 SHA-256

源服务器：

```bash
sha256sum /backup/docker-backup.tar.xz
```

目标服务器：

```bash
sha256sum /backup/docker-backup.tar.xz
```

两边哈希必须完全一致。

对于大文件和不稳定链路，推荐使用支持续传的 rsync：

```bash
rsync -ah --partial --append-verify --progress \
  /backup/docker-backup.tar.xz \
  root@目标服务器IP:/backup/
```

归档已经压缩，不必再启用 SSH 压缩。

---

## 14. `archive-list`：查看归档内容

```bash
sudo docker-full-backup archive-list \
  /backup/docker-backup.tar.xz
```

该命令只查看 manifest，不加载镜像、不创建容器、不恢复数据。

---

## 15. `restore`：恢复容器

基本语法：

```bash
sudo docker-full-backup restore \
  <归档文件> \
  [归档中的容器名称或ID ...] \
  [参数]
```

## 15.1 交互选择恢复

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz
```

会列出归档中的容器并询问选择。

## 15.2 恢复全部容器

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  --all
```

## 15.3 恢复指定容器

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  nginx mysql
```

可以使用归档中记录的：

- 容器名称
- 完整容器 ID
- 容器 ID 前缀

只恢复部分容器时，需要注意它可能依赖归档中未选择的数据库、Redis、网络别名或 `VolumesFrom` 容器。

---

## 16. `restore` 全部参数

## 16.1 `archive`

必需的位置参数：

```bash
/backup/docker-backup.tar.xz
```

## 16.2 `containers`

可选位置参数，指定从归档恢复的容器。

## 16.3 `-a, --all`

恢复归档中的全部容器：

```bash
--all
```

## 16.4 `--conflict`

控制以下对象发生冲突时的处理方式：

- bind mount 路径
- Docker volume 名称
- Compose 配置文件路径

可选值：

```text
overwrite
alternate
existing
fail
```

默认值：

```text
alternate
```

该参数主要在 `--non-interactive` 模式下决定自动行为；交互模式会显示选择菜单。

### `--conflict overwrite`

删除现有路径或清空现有 volume，然后恢复归档数据到原位置。

优点：

- 保持原 mount 路径
- 方便继续使用原 Compose 文件

风险：

- 现有数据会被删除
- 恢复过程不是完整事务
- 若中途磁盘写满或断电，不能自动恢复旧目录

### `--conflict alternate`

保留现有数据，将归档恢复到新路径或新 volume，并改写容器挂载。

bind mount 示例：

```text
/srv/app/data
→ /srv/app/data.restored-20260803-030000
```

volume 示例：

```text
mysql_data
→ mysql_data-restored-20260803-030000
```

这是恢复测试和保留旧数据时最安全的选择。

### `--conflict existing`

保留并直接使用目标服务器已经存在的目录或 volume，不解压该对象的归档数据。

适合：

- 数据已通过其他方式同步完成
- 只想恢复镜像和容器配置

不适合用于验证归档中的数据是否可恢复。

### `--conflict fail`

发现任何路径或 volume 冲突立即终止。

最适合：

- 全新服务器
- 原始路径应当不存在
- 不允许脚本自动覆盖或改路径

## 16.5 `--container-conflict`

控制目标服务器已经存在同名容器时的处理方式。

可选值：

```text
overwrite
alternate
existing
fail
```

默认：

```text
alternate
```

### `--container-conflict overwrite`

在镜像、网络和 mount 前置检查完成后，紧邻重建操作前删除同名容器，然后使用原名称创建恢复容器。

仍然不是完整事务；删除旧容器后若新容器创建失败，旧容器不会自动重建。

### `--container-conflict alternate`

保留现有容器，并为恢复容器自动生成名称，例如：

```text
app-restored-20260803-030000
```

注意：即使容器名称不同，端口仍可能冲突。

### `--container-conflict existing`

跳过该容器，不恢复。其他恢复容器引用它时，会尽量继续使用现有同名容器。

### `--container-conflict fail`

发现同名容器立即终止，不修改现有容器。

## 16.6 `--non-interactive`

禁止全部交互提示：

```bash
--non-interactive
```

必须配合：

- 容器名称/ID；或
- `--all`

冲突处理完全由：

```text
--conflict
--container-conflict
```

决定。

## 16.7 `--no-start`

只创建容器，不启动：

```bash
--no-start
```

适合：

- 先检查 mount 和配置
- 需要手工调整防火墙或端口
- 需要按自己的顺序启动

即使同时指定 `--start-all`，`--no-start` 仍会跳过启动阶段。

## 16.8 `--start-all`

无论容器备份前是否运行，恢复后都尝试启动：

```bash
--start-all
```

不使用该参数时：

- 原本运行的容器会启动
- 原本停止的容器只创建，不启动
- 原本暂停的容器会启动后重新暂停

若归档中的容器备份时全部为 `Exited`，但希望迁移后立即运行，就需要加 `--start-all`。

## 16.9 `--no-logs`

不解压历史日志归档：

```bash
--no-logs
```

不影响容器创建和业务数据。

## 16.10 `--logs-dir`

指定历史日志解压目录：

```bash
--logs-dir /backup/restored-logs
```

默认目录位于归档旁边：

```text
<archive-stem>-restored-logs
```

例如：

```text
/backup/docker-backup.tar-restored-logs
```

## 16.11 `--docker-socket`

指定 Docker Engine Unix Socket：

```bash
--docker-socket /var/run/docker.sock
```

默认：

```text
/var/run/docker.sock
```

仅影响恢复阶段通过 Docker Engine API 创建网络和容器的连接。

## 16.12 `--work-dir`

指定恢复临时目录的父目录：

```bash
--work-dir /mnt/large-disk/tmp
```

默认：

```text
/var/tmp
```

恢复需要临时解压外层归档，因此可用空间至少要容纳归档解压后的内部文件。

## 16.13 `--keep-workdir`

恢复结束或失败后保留临时工作目录：

```bash
--keep-workdir
```

适合调试 manifest、Inspect、网络和 mount 归档。

---

## 17. 两类冲突参数的区别

| 参数 | 处理对象 |
|---|---|
| `--conflict` | bind 路径、Docker volume、Compose 配置文件 |
| `--container-conflict` | Docker 容器名称 |

例如：

```text
/srv/mysql 已存在
```

由 `--conflict` 决定。

```text
mysql 容器已存在
```

由 `--container-conflict` 决定。

宿主机端口被占用不属于以上两类冲突。端口冲突通常在启动容器时被 Docker 报出，脚本最终会以失败状态退出并列出未启动容器。

---

## 18. 推荐恢复方式

## 18.1 全新服务器，恢复原路径

确认目标服务器：

- CPU 架构一致
- Docker 已安装
- 原 mount 路径不存在
- 不存在同名容器
- 不存在不兼容的同名自定义网络

执行：

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  --all \
  --start-all \
  --non-interactive \
  --conflict fail \
  --container-conflict fail
```

这是正式迁移到空服务器的推荐模式。

## 18.2 保留目标服务器已有目录，安全测试恢复

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  --all \
  --start-all \
  --non-interactive \
  --conflict alternate \
  --container-conflict fail
```

归档数据会恢复到 `.restored-时间` 路径，原数据不被覆盖。

## 18.3 只创建，不启动

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  --all \
  --no-start \
  --non-interactive \
  --conflict fail \
  --container-conflict fail
```

检查完成后手工启动：

```bash
docker start container-a container-b
```

## 18.4 覆盖原路径和同名容器

高风险模式：

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  --all \
  --start-all \
  --non-interactive \
  --conflict overwrite \
  --container-conflict overwrite
```

正式使用前应另外备份目标服务器现有目录和容器配置。

---

## 19. 从 `alternate` 切换回原 mount 路径

Docker 不支持修改已创建容器的 mount 来源，因此必须重建容器。

安全思路：

1. 删除当前使用 `.restored-*` 路径的恢复容器
2. 将原路径改名保留作为回滚副本
3. 确保原路径不存在
4. 使用 `--conflict fail` 重新恢复

示例：

```bash
mv /srv/app/data /srv/app/data.before-restore

sudo docker rm -f app

sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  app \
  --start-all \
  --non-interactive \
  --conflict fail \
  --container-conflict fail
```

对于父子嵌套目录，只移动父目录，不要再单独移动父目录内部的子挂载路径。

---

## 20. 恢复后的检查

## 20.1 容器状态

```bash
docker ps -a --no-trunc
```

## 20.2 查看镜像和运行状态

```bash
docker inspect \
  --format '{{.Name}} status={{.State.Status}} image={{.Config.Image}} restart={{.RestartCount}} error={{json .State.Error}}' \
  container-a container-b
```

恢复容器通常使用：

```text
docker-full-backup/snapshot:<backup-id>-<index>
```

## 20.3 检查 mount

```bash
docker inspect \
  --format '{{.Name}}{{range .Mounts}}{{println}}{{.Source}} -> {{.Destination}}{{end}}{{println}}' \
  container-a container-b
```

## 20.4 检查端口

```bash
docker port container-a
ss -lntp
```

## 20.5 检查网络

```bash
docker network ls
docker network inspect <network-name>
```

## 20.6 检查日志

```bash
docker logs --tail 100 container-a
```

批量检查：

```bash
for c in container-a container-b container-c; do
  echo "========== $c =========="
  docker logs --tail 50 "$c" 2>&1
done
```

重点注意：

```text
panic
fatal
permission denied
database locked
connection refused
no such file
```

---

## 21. 容器启动状态规则

| 备份前状态 | 默认恢复行为 | 使用 `--start-all` | 使用 `--no-start` |
|---|---|---|---|
| Running | 创建并启动 | 创建并启动 | 只创建 |
| Exited | 只创建 | 创建并启动 | 只创建 |
| Paused | 启动后重新暂停 | 启动后重新暂停 | 只创建 |

若某个容器创建成功但启动失败：

- 已创建容器会保留，便于排查
- 脚本会列出失败原因
- 最终退出码为 1
- 其他成功启动的容器不会自动删除

---

## 22. Compose 使用注意事项

恢复工具直接调用 Docker Engine API 创建容器，不依赖目标服务器执行 `docker compose up`。

使用 `--conflict alternate` 时：

- 容器 mount 会指向 `.restored-*` 路径
- Compose Labels 中可定位的配置路径会尽量改写
- 恢复出来的 Compose 文件正文中的 `volumes:` 路径不会自动全面重写

因此，在检查 Compose 文件前，不要直接运行：

```bash
docker compose up -d
docker compose down
```

否则 Compose 可能按照文件里的旧路径重新创建容器。

正式恢复到原路径并确认 Compose 文件正常后，才建议重新交给 Compose 管理。

---

## 23. 镜像标签处理

恢复时 `docker image load` 可能加载与目标服务器同名的原始镜像标签。

脚本会：

1. 在加载前记录目标服务器已有标签当前指向
2. 加载归档镜像和快照镜像
3. 将目标服务器原本存在的标签重新指回原镜像
4. 恢复容器使用归档中的专用快照标签

因此不会为了恢复容器而永久抢占目标服务器已有镜像标签。

不要在恢复容器仍在使用快照镜像时执行：

```bash
docker image prune -a
docker system prune -a
```

虽然 Docker 通常不会删除正在被容器引用的镜像，但正式确认业务前不建议进行大范围清理。

---

## 24. 数据一致性建议

### 24.1 数据库容器

推荐：

```bash
--consistency stop
```

此外，重要数据库最好同时保留原生逻辑备份：

- MySQL/MariaDB：`mysqldump`
- PostgreSQL：`pg_dump` / `pg_dumpall`
- Redis：确认 RDB/AOF 状态
- SQLite：应用停止后复制，或使用 SQLite backup API

完整 Docker 归档用于快速恢复，数据库逻辑备份作为第二层保险。

### 24.2 大量小文件

默认 `-6` 在速度和体积之间更合理。只有明确需要极限节省空间时使用：

```bash
--max-compress
```

### 24.3 正在使用的共享目录

必须把共享同一 mount 的容器作为一个整体停止和备份，否则无法保证同一时间点一致性。

---

## 25. 安全注意事项

归档中可能包含：

- 数据库文件
- API Key
- Docker 环境变量
- `.env`
- Token
- 私钥
- 用户上传文件
- 内部网络和端口信息
- Docker daemon 配置

因此归档应按敏感数据处理：

```bash
chmod 600 /backup/docker-backup.tar.xz
chown root:root /backup/docker-backup.tar.xz
```

通过公网传输时使用 SSH/SCP/rsync，不要上传到公开可访问的位置。

本工具本身不对最终归档进行加密。需要加密时，可以在备份完成后使用 age、GPG 或其他专用加密工具另行处理。

---

## 26. 已知限制

1. 不恢复 RAM、PID、TCP 会话或进程执行位置。
2. 历史日志只能解压为普通文件，不能注入新的 Docker 日志驱动。
3. 只通用支持 `local` Docker volume driver。
4. bind source 在备份时不存在，会记录警告，但没有数据可归档。
5. 只备份被所选容器实际使用的原始镜像，不备份全部孤立镜像。
6. Compose 配置只从 Compose Labels 中发现，不扫描未知项目文件。
7. 外部数据库、远程 API、NFS、DNS、固定公网 IP 等依赖不会随归档自动迁移。
8. `/dev/net/tun`、GPU、USB、`/dev/dri` 等设备必须在目标服务器真实存在。
9. 目标服务器必须具备容器所需内核模块和安全能力。
10. 恢复目录覆盖不是完整事务；中途失败不能自动回滚被覆盖的数据。
11. 容器创建顺序按原始创建时间排序，不实现完整 Compose dependency/healthcheck 调度。
12. 静态 IP 恢复要求目标网络子网和地址不冲突。
13. amd64 与 arm64 默认拒绝互相恢复。
14. 不专门迁移 Docker Swarm secret、config、service 和 stack。
15. 不自动调整宿主机防火墙、反向代理、DNS 或系统服务。

---

## 27. 常见问题

### 27.1 为什么几个几百 MB 的镜像，归档只有一两百 MB？

原因包括：

- `docker images` 显示的是逻辑累计大小
- 多个镜像可能共享基础层
- 多个容器可能使用同一个镜像
- 快照只增加容器可写层
- `docker image save` 对共享层去重
- XZ 会继续压缩镜像 tar
- 同一个 mount 源只保存一次

恢复后 Docker 实际占用会大于压缩归档文件。

### 27.2 `-p` 对应的数据会不会备份？

`-p` 是端口映射，不是目录数据。

- 端口映射保存在 `docker inspect` 中并恢复
- 宿主机数据目录由 `-v` 或 `--mount` 定义并归档

### 27.3 为什么恢复容器使用 `docker-full-backup/snapshot:*`？

该镜像包含：

```text
原始镜像层 + 容器可写层
```

这样容器内部未挂载路径的修改不会丢失。

### 27.4 恢复后原本停止的容器为什么没有运行？

默认尊重备份前状态。使用：

```bash
--start-all
```

强制启动全部恢复容器。

### 27.5 为什么产生 `.restored-*` 目录？

因为使用了：

```bash
--conflict alternate
```

这是为了不覆盖目标服务器已有数据。

### 27.6 如何恢复到原路径？

确保原路径不存在，然后使用：

```bash
--conflict fail
```

或者高风险地使用：

```bash
--conflict overwrite
```

### 27.7 可以跨 Ubuntu 和 Debian 恢复吗？

通常可以。容器使用自己的镜像用户空间，宿主机发行版不必完全一致。

重点是：

- CPU 架构一致
- Docker Engine 版本不要明显低于源服务器
- 目标内核具备所需能力
- 宿主机设备和外部依赖存在

### 27.8 中断备份会怎样？

脚本在 `finally` 中尽量：

- 恢复被停止的容器
- 解除脚本暂停的容器
- 删除临时镜像标签
- 删除工作目录，除非使用 `--keep-workdir`

未完成的输出文件可能无效，应删除后重新备份。

### 27.9 为什么压缩 CPU 没有全部跑满？

自动模式受以下因素限制：

- 实际 CPU affinity
- 当前 `MemAvailable`
- XZ 压缩级别内存需求
- XZ 自己选择的有效线程数
- 上游 `docker image save` 或 tar 读取速度
- 磁盘 I/O

手动增加线程：

```bash
--xz-threads 4
```

但需自行承担内存风险。

---

## 28. 故障排查

## 28.1 `Docker daemon is not available`

检查：

```bash
systemctl status docker
docker info
```

启动：

```bash
sudo systemctl start docker
```

## 28.2 输出文件已经存在

错误示例：

```text
Output already exists
```

解决：

- 更换输出文件名；或
- 使用 `--force`

```bash
sudo docker-full-backup backup \
  --all \
  --force \
  -o /backup/docker-backup.tar.xz
```

## 28.3 临时磁盘空间不足

查看：

```bash
df -h /var/tmp /backup
```

改用更大临时盘：

```bash
--work-dir /mnt/large-disk/tmp
```

## 28.4 XZ 压缩太慢

优先确认没有使用：

```bash
--max-compress
```

使用默认 `-6` 和自动线程：

```bash
--xz-threads auto
```

或者明确指定线程：

```bash
--xz-threads 2
```

## 28.5 恢复时端口冲突

查看：

```bash
ss -lntp
docker ps --format 'table {{.Names}}\t{{.Ports}}'
```

可以先：

```bash
--no-start
```

创建容器后手工处理冲突。

## 28.6 同名网络配置不兼容

查看：

```bash
docker network inspect <network-name>
```

确认没有其他容器使用后，再决定是否删除目标服务器的同名网络：

```bash
docker network rm <network-name>
```

不要在不了解用途时强制删除网络。

## 28.7 架构不匹配

查看两端：

```bash
uname -m
docker info --format '{{.Architecture}}'
```

典型等价关系：

```text
x86_64 = amd64
aarch64 = arm64
```

不同架构需要重新构建或拉取对应架构镜像，而不是直接恢复当前镜像归档。

## 28.8 容器创建成功但启动失败

查看：

```bash
docker inspect <container>
docker logs <container>
docker start <container>
```

常见原因：

- 端口被占用
- mount 权限不正确
- 目标设备不存在
- 外部数据库不可达
- 静态 IP 冲突
- 内核模块缺失
- 环境变量中的地址仍指向旧服务器

---

## 29. 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 成功 |
| `1` | 参数、校验、Docker、恢复或启动失败 |
| `130` | 用户通过 Ctrl+C 中断 |

自动化任务应检查退出码：

```bash
if sudo docker-full-backup backup --all -o /backup/all.tar.xz; then
  echo "backup success"
else
  echo "backup failed"
fi
```

---

## 30. 定时备份示例

脚本本身不创建定时任务，可以通过 cron 调用。

例如每天 03:30 备份全部容器：

```cron
30 3 * * * /usr/local/sbin/docker-full-backup backup --all --consistency stop --non-interactive -o "/backup/docker-$(date +\%Y\%m\%d-\%H\%M\%S).tar.xz" >> /var/log/docker-full-backup.log 2>&1
```

注意 cron 中 `%` 必须写成 `\%`。

该示例不会自动删除旧备份。应根据容量另行制定保留策略，并在删除前确认新归档已通过校验。

---

## 31. 正式使用建议

推荐至少遵循以下流程：

1. 使用 `--consistency stop`
2. 使用默认 `-6`，不要默认开启 `--max-compress`
3. 输出文件名带时间
4. 备份完成后运行 `xz -t`
5. 使用 `archive-list` 检查容器清单
6. 将归档复制到另一台服务器或异地存储
7. 比较传输前后的 SHA-256
8. 定期在测试环境执行真实恢复
9. 数据库额外保留原生逻辑备份
10. 对归档设置严格文件权限

一份未经过实际恢复测试的备份，不能视为已经完全可靠。

---

## 32. 快速命令表

### 安装

```bash
curl -fsSL https://raw.githubusercontent.com/smmya/docker_full_backup/main/docker_full_backup_reviewed.py \
  -o /tmp/docker-full-backup &&
sudo python3 -m py_compile /tmp/docker-full-backup &&
sudo install -m 0755 /tmp/docker-full-backup /usr/local/sbin/docker-full-backup
```

### 列出容器

```bash
sudo docker-full-backup list
```

### 备份全部容器

```bash
sudo docker-full-backup backup \
  --all \
  --consistency stop \
  -o "/backup/docker-$(date +%Y%m%d-%H%M%S).tar.xz"
```

### 极限压缩

```bash
sudo docker-full-backup backup \
  --all \
  --max-compress \
  -o /backup/docker-max.tar.xz
```

### 查看归档

```bash
sudo docker-full-backup archive-list /backup/docker-backup.tar.xz
```

### 空服务器原路径恢复

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  --all \
  --start-all \
  --non-interactive \
  --conflict fail \
  --container-conflict fail
```

### 保留旧目录的测试恢复

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  --all \
  --start-all \
  --non-interactive \
  --conflict alternate \
  --container-conflict fail
```

### 只创建不启动

```bash
sudo docker-full-backup restore \
  /backup/docker-backup.tar.xz \
  --all \
  --no-start \
  --non-interactive \
  --conflict fail \
  --container-conflict fail
```

---

## 33. License

仓库使用 MIT License。具体内容以仓库中的 `LICENSE` 文件为准。
