# AO3 书签备份器

定期备份 AO3 书签中的单篇作品。SQLite 保存作品资料、章节正文、作者附注、历史版本和阅读进度，也可选存 AO3 官方附件。Web 总览提供搜索、阅读、版本比较及 Markdown、JSON 导出。只需要 Python 标准库，支持 Docker。

## 总览与版本比较

- **作品总览**显示标题、作者和作品标签。点击标题打开最新存档正文。若当前版本无法阅读，显示最近一份有正文的留存版。点击一位作者或某个标签，可以查看本地归档中匹配的全部作品，再切换更新视图。
- **访问后更新**统计本次打开总览时，相对上次打开所增加的作品变更。同一批更新在 30 分钟内刷新时保留，筛选和搜索沿用当前范围。
- **未读更新**保留每篇作品自上次阅读后的变更。从总览点击标题会标记正文对应的更新。打开留存版时，更新版本仍会显示为未读。首次访问以现有备份为基线，版本页也可以手动标为已读。
- **版本与差异**在有未读更新时默认比较上次读到的版本与最新版，也可任选两版。差异按作品资料、章名、正文、章节简介、章首及章末作者附注展开。每个有正文的版本都可以阅读或导出 Markdown、JSON，已保存的官方附件也可下载。
- **抓取问题**显示尚无当前版正文、上次抓取失败或缺少已配置附件的作品。原有下载失败计数和重试状态会随旧库迁移。

阅读状态保存在 SQLite 中，浏览器用 Cookie 识别。同一浏览器重启后可接续，其他浏览器建立自己的访问和已读记录。

正文随窗口宽度排版。顶栏的「配色」提供跟随系统、白天、暖色、青绿、薰衣草、绯红及夜间、暮蓝、墨绿共 9 种主题；正文阅读工具栏支持调节字体（优雅衬线 / 现代黑体 / 温润楷体 / 系统默认）、字号和栏宽，浏览器会自动记住选择。

## 抓取管理

- **立即刷新**立即排队执行一次书签扫描，发现新增收藏、更新和需要补齐的作品。
- **重试失败或缺失项**按作品 ID 补抓归档里已有问题的作品。手动补抓允许再尝试已达连续失败上限的作品。
- **抓取设置**支持五种调度模式：**按固定间隔**（分钟 / 小时 / 天，可选对齐基准时刻不漂移或每次结束后顺延）、**每天**、**每周**（多选星期）、**每月**（1~31 日，遇小月自动在月末执行）以及**仅手动（关闭自动定时）**，并实时预览接下来 3 次执行时间；同时可调整请求间隔、尝试次数和连续失败上限。第一次使用时显示备份进程的环境配置，网页保存后将设置存入 SQLite，调度器在空闲时重新计算计划。
- **运行日志**显示备份进程输出的最近日志，抓取管理区会自动显示排队状态、上次运行结果和下次计划。点击「更新列表」查看本轮入库的作品。

总览的抓取操作由常驻备份进程领取。使用 Docker 时请同时运行 `ao3-backup` 和 `ao3-web`。局域网内能访问总览的设备也能提交抓取操作和更改抓取设置。

## Docker 启动

默认直接拉取 GitHub Container Registry 预构建的多架构镜像（支持 `linux/amd64` 与 `linux/arm64`）：`ghcr.io/cr4player/ao3-bookmark-backup:latest`。

在宿主机项目目录创建 `.env`，至少填 `BOOKMARK_USER`，然后运行：

~~~sh
docker compose pull
docker compose up -d
~~~

默认从 Docker 宿主机的 `http://127.0.0.1:8765/` 打开总览。备份进程与 Web 进程共享名为 `archive` 的 Compose 持久卷。SQLite 文件和事务日志应位于同一台宿主机的本地文件系统。NAS 可以用本机卷，运行中的库需要避开从其他机器映射的 SMB/NFS 路径。

在局域网内直接访问时，把 NAS 的局域网 IPv4 地址填入 `.env` 的 `WEB_BIND_IP`，例如 `WEB_BIND_IP=192.168.0.3`。在现有项目的目录中运行 `docker compose up -d --no-deps ao3-web` 应用端口配置。如果部署时用过 `-p`，沿用同一个项目名。然后打开 `http://192.168.0.3:8765/`。若用主机名访问，把该主机名加到 `WEB_ALLOWED_HOSTS`。HTTP 直连时使用 `WEB_COOKIE_SECURE=0`，让浏览器保留阅读状态。连得上该端口的设备可以直接读取归档。

保留默认监听地址时，从另一台电脑可用 SSH 转发 `ssh -L 8765:127.0.0.1:8765 用户@群晖地址`，或通过带认证的 HTTPS 反向代理访问。使用反向代理时，在 `.env` 设置 `WEB_ALLOWED_HOSTS` 为访问用域名，设置 `WEB_COOKIE_SECURE=1`，并让代理保留对应的 Host。

~~~sh
docker compose ps
docker compose logs -f ao3-backup
docker compose exec ao3-backup python /app/ao3_backup.py --once --dry-run
~~~

修改 `.env` 后，使用 `docker compose up -d` 重新创建配置有变更的容器。普通 `docker compose down` 保留持久卷。

### 更新现有项目

通过预构建镜像更新时，只需拉取最新镜像并重建容器（若部署时指定过 `-p`，请沿用同一个项目名）：

~~~sh
docker compose pull
docker compose up -d --force-recreate ao3-backup ao3-web
~~~

若在本地自行修改了源码并取消了 `docker-compose.yml` 中的 `# build: .` 注释，也可本地构建：

~~~sh
docker compose build ao3-backup
docker compose up -d --force-recreate ao3-backup ao3-web
~~~

现有 `archive` 卷会随原项目继续挂载，程序启动时会升级库结构。打开总览点击「重试失败或缺失项」补齐当前数据库中的正文。备份容器使用宿主机的日志驱动。群晖可在 Container Manager 的 `ao3-backup` 容器详情中查看「日志」。可同时通过总览的「运行日志」查看卷内的文件日志 `/data/logs/ao3_backup.log`。

## 从旧版迁移

在 Windows 上可先用不同的本地输出目录演练。

~~~powershell
py ao3_backup.py --import-legacy '<你的旧版数据目录>' --out .\data\sqlite-preview
py ao3_backup.py --serve --out .\data\sqlite-preview
~~~

导入读取源目录的 `state.json`、`works/` 和 `versions/`。原始文件按字节保存，已存在的变更记录一并保留。可从旧 HTML 重新计算文字差异。缺失的源文件会列在导入报告中。历史文件如果没有抓取记录，时间线会将抓取时间显示为未知。重复导入同一份旧库会校验源并返回原有迁移结果。

在群晖上切换时，使用新的 Compose 项目名与数据卷。将 `LEGACY_DATA` 设置为旧 `data` 目录在群晖上的实际绝对路径。等旧备份器空闲后暂停它，以固定迁移快照。保留旧项目目录和数据用于回滚。

~~~sh
docker compose -p ao3-sqlite pull
docker compose -p ao3-sqlite run --rm --no-deps \
  -v "$LEGACY_DATA:/legacy:ro" ao3-backup --import-legacy /legacy
docker compose -p ao3-sqlite up -d
~~~

切换后可先确认总览、旧版全文和版本比较。如需回滚，停止新 Compose 项目并重新启动旧容器。SQLite 中切换后的新变更会留在新卷中。

## 数据库备份与恢复

运行时使用 `--backup-db` 生成经过完整性检查的独立 SQLite 快照。

~~~sh
docker compose -p ao3-sqlite exec ao3-backup \
  python /app/ao3_backup.py --backup-db /data/archive-snapshot.sqlite3
docker compose -p ao3-sqlite cp ao3-backup:/data/archive-snapshot.sqlite3 \
  ./archive-snapshot.sqlite3
~~~

每次备份用新的文件名。恢复时将快照放进一个空数据目录或空数据卷，命名为 `archive.sqlite3`，让运行用户有读写权限。在该目录使用 `--serve --out <恢复目录>` 即可先验收历史和全文。登录凭据仍由环境变量提供，Cookie 缓存、运行日志和健康检查标记由各自的运行进程生成。

## 本地运行

~~~sh
python ao3_backup.py --once --bookmark-user YOUR_USERNAME --out ./data-sqlite
python ao3_backup.py --serve --out ./data-sqlite
~~~

省略 `--once` 会按计划常驻，也可由系统计划任务调用 `--once`。用 `--once --dry-run` 预览下载。原有文件模式可通过 `--legacy-files --out <旧目录>` 运行，保留静态 `index.html` 和 `manifest.csv`。

## 抓取与存储

抓取器根据书签页的作品 ID、更新时间和作品资料判断哪些作品需要核对，随后从 AO3 全文页提取标题、作者、分级、标签、简介、已发布章节及作品和章节的作者附注。校验章节是否齐全后，将文章、附件和变更记录写入同一笔 SQLite 事务。遇到需要登录、尚未公开的 Mystery Work 或正文不完整时，保留上次成功的归档并记录失败。AO3 账号隐藏警告或附加标签时，完整正文仍会入库。比较版本时会跳过隐藏的字段，并在可用时显示之前采集的标签。

`FORMATS=none` 只抓取和保存文章数据，旧备份中的附件仍可打开。选择 `html,epub` 等格式时，官方附件另外保存。变更比较以结构化文章为准，附件字节变化只标记为「备份文件变化」，同步个人书签备注也不会产生作品更新。正文中的图片会保留原地址和替代文字，图片文件由站点提供。

若 AO3 修改正文或附注时未更新书签页的作品资料或时间戳，可在本地运行 `python ao3_backup.py --once --refresh-content --out <数据目录>`，或在容器里运行 `docker compose exec ao3-backup python /app/ao3_backup.py --once --refresh-content`，主动核对所有扫描到的作品全文。

## 配置

| 变量 | 默认 | 用途 |
| --- | --- | --- |
| `BOOKMARK_USER` | 必填 | 要备份的 AO3 书签所属用户名 |
| `AO3_USER` / `AO3_PASSWORD` | 空 | 登录后访问私有书签及受限作品 |
| `RUN_AT` | `03:30` | 容器时区的每日运行时刻 |
| `INTERVAL` | 空 | 可设为 `6h` 等固定间隔，优先于 `RUN_AT` |
| `RUN_ON_START` | `1` | 启动即运行 |
| `DELAY` / `TIMEOUT` / `RETRIES` | `1.5` / `60` / `4` | 请求间隔、超时秒数、总尝试次数（含首次） |
| `PREFER_IPV4` | `1` | 优先 IPv4 |
| `HTTPS_PROXY` / `HTTP_PROXY` | 空 | 可选 HTTP 代理，比如 `http://host.docker.internal:7890` |
| `MAX_PAGES` / `MAX_ERRORS` | `200` / `5` | 翻页限制及连续失败暂停阈值 |
| `STOP_AFTER_IDLE_PAGES` | `0` | 非零时可提前结束无变化的扫描并按作品更新时间补扫 |
| `FORMATS` | `none` | 设为 `none` 只保存文章数据，也可填写以逗号分隔的 `html,epub,pdf,mobi,azw3`。旧文件模式默认 `html` |
| `KEEP_VERSIONS` | `1` | 旧版文件模式的历史归档开关 |
| `HEALTHCHECK_MAX_AGE_HOURS` | `48` | 距上次完整成功运行的健康检查上限 |
| `WEB_BIND_IP` | `127.0.0.1` | Compose 在宿主机上绑定的 IPv4 地址，直连时填 NAS 局域网 IP |
| `WEB_PORT` | `8765` | Compose 发布到宿主机的端口 |
| `TZ` | `Asia/Shanghai` | 容器时区 |

AO3 站点提供的私有或受限内容随登录权限读取。请勿将 `.env`、登录 Cookie 或个人书签归档上传到公共仓库。项目与 OTW / AO3 官方无关。
