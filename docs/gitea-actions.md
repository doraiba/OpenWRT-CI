# 在自建 Gitea 上跑 H5000M 自用编译

本目录（`.gitea/workflows/`）是 GitHub Actions 版本的 Gitea 移植版，只需自建
act_runner 即可运行，不需要 GitHub 托管。

## 文件

| 文件 | 作用 |
| :--- | :--- |
| `.gitea/workflows/h5000m-netmonitor.yml` | 完整流水线（单文件，替代原来的 `H5000M-NETMONITOR-AUTO.yml` + `WRT-CORE.yml`） |
| `Scripts/gitea_release.py` | 往 Gitea Release 上传产物（替代 `gh release`） |
| `Scripts/*.sh`、`Config/*.txt` | 与 GitHub 版完全共用，未做任何修改 |

## 与 GitHub 版的差异

| 项目 | GitHub 版 | Gitea 版 | 原因 |
| :--- | :--- | :--- | :--- |
| 工作流组织 | 入口 + `WRT-CORE.yml` 可复用工作流 | 合并成单个工作流 | Gitea Actions 不支持作业级 `uses: ./.github/workflows/xxx.yml` |
| 缓存 | `actions/cache`（10GB 配额，LRU 淘汰） | 软链到宿主持久目录 `WRT_CACHE_DIR` | 自建 runner 本地磁盘就是缓存，不会被配额挤掉，也省掉每轮 1.1GB 上下行 |
| 定时触发 | 依赖「自动清理 → `workflow_run`」串联 | 工作流自带 `schedule` | Gitea 不支持 `workflow_run` 事件 |
| 发布 | `gh release create/upload` | Gitea REST API（`gitea_release.py`） | `gh` 只认 GitHub API |
| 检出 | `actions/checkout@v7` | 手写 `git clone` | 内网 runner 常常拉不到 gitea.com 的 action 镜像 |
| 超时 | 345 分钟（GitHub 6h 硬上限） | 360 分钟 | 自建 runner 没有平台硬上限 |
| 插件清单 | `GITHUB_ENV` 多行写法 | 落盘成 `upload/plugins.md` 再拼 Markdown | act_runner 的多行 env 支持不稳定 |
| ccache 上限 | 5G | 20G | 本地盘空间充裕，命中率更高 |

## 一、准备 runner

### 1. 磁盘与内存

构建过程会产生：`build_dir` 约 40GB（每次结束 `make clean` 清掉）、
`staging_dir` 约 4GB（持久）、`dl` 约 1.6GB（持久）、ccache 数 GB（持久）。

- 磁盘：**预留 60GB 以上**
- 内存：**8GB 起步**，`-j4` 编 rust/llvm 时峰值较高，16GB 更稳
- CPU：4 核以上，核越多越快（8 核约为 4 核的一半耗时）

### 2. 注册 act_runner

```bash
# 0. token 从「仓库设置 → Actions → Runner」或「站点管理 → Actions → Runners」创建
docker run --rm \
  -v /srv/act-runner:/data -w /data \
  gitea/act_runner:latest register \
    --no-interactive \
    --instance https://git.example.com \
    --token <RUNNER_TOKEN> \
    --name wrt-builder \
    --labels ubuntu-latest:docker://catthehacker/ubuntu:act-22.04
```

两个容易漏的地方：

- **`-w /data` 不能省**。`register` 会把凭证写到当前工作目录的 `.runner` 文件，
  不指定 `-w /data` 就会写进容器临时目录，容器一退就没了，于是下次启动显示「未注册」。
- **`--labels` 里的 `ubuntu-latest` 才是标签名**，冒号后面那串是 job 容器镜像。
  `runs-on: ubuntu-latest` 匹配的是标签名，写错或没写都会出现
  「没有匹配 ubuntu-latest 标签的在线运行器」。

镜像用 `catthehacker/ubuntu:act-22.04` 而不是默认的 `node:16-bullseye`：
前者自带 `sudo`、`apt`、`gcc`、`python3`，流水线里的环境初始化步骤才跑得通。

只有在宿主机是 **Debian/Ubuntu** 时，才可以考虑 host 模式标签（省掉容器那层开销）：

```bash
  --labels ubuntu-latest:host
```

此时不需要挂载，`/srv/wrt-cache` 就是宿主机真实路径；前提是运行 act_runner 的用户有
**免密 sudo**，否则「初始化编译环境」装依赖那步会失败。
**Alpine 等 musl 发行版不要用 host 模式**，原因见文末 FAQ。

### 3. 启动 runner

先写配置文件 `/srv/act-runner/config.yaml`：

```yaml
log:
  level: info

runner:
  capacity: 1          # 一个 runner 同时只跑一个构建，避免两个 job 抢 CPU
  timeout: 6h
  fetch_timeout: 10m

cache:
  enabled: false       # 本方案用宿主持久目录，不依赖 Gitea 的 cache server

container:
  network: "host"      # 拉 GitHub 源码/feeds 更稳；也可换成自定义网络
  privileged: false
  # 关键：把持久缓存目录和大盘挂进 job 容器，路径要与工作流里的 WRT_CACHE_DIR 一致
  options: >-
    -v /srv/wrt-cache:/srv/wrt-cache
    -v /srv/wrt-build:/srv/wrt-build
```

然后启动（同样要 `-w /data`，否则读不到第 2 步生成的 `.runner`）：

```bash
docker run -d --name gitea-runner --restart always \
  -v /srv/act-runner:/data -w /data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  gitea/act_runner:latest

# 确认已连上 Gitea
docker logs -f gitea-runner        # 出现 "runner: xxx, with labels: [ubuntu-latest]" 即正常
```

改配置后 `docker restart gitea-runner` 生效。

### 4. 工作流里的两个路径

```yaml
env:
  WRT_CACHE_DIR: /srv/wrt-cache   # 持久缓存（dl / ccache / staging）
  WRT_BUILD_ROOT: ''              # 源码与 build_dir 的落脚点，留空=工作区
```

如果希望源码和 `build_dir` 也落在大盘上，把 `WRT_BUILD_ROOT` 改成
`/srv/wrt-build` 即可（该目录要存在于容器内，见上面的挂载）。

缓存目录不存在或不可写时，流水线会打一条 `::warning::` 然后以**无缓存**方式
继续跑（慢 2~3 小时，但不会失败），便于先验证流程再优化。

## 二、首次运行

1. 仓库 **设置 → 仓库 → 启用 Actions**（Gitea 1.21+ 默认开启，但 fork/迁移来的
   仓库需要确认一次）。
2. 手动触发一次，`TEST=true`：

   ```bash
   # 页面上：Actions → H5000M 自用编译 → Run workflow → TEST 填 true
   ```

   它只做「取源码 → 装插件 → 生成配置 → 断言」四件事，**5 分钟内结束**，
   用来验证 runner 环境、网络、token 是否正常，不会浪费三小时。
3. 通过后再跑 `TEST=false` 真编译。

## 三、定时

```yaml
on:
  schedule:
    - cron: '0 13 * * *'   # UTC 13:00 = 北京时间 21:00
```

Gitea 的 cron 按 **UTC** 解析。想改时间就改这行，注意换算。
定时任务只在**默认分支**上的工作流文件生效。

## 四、GITHUB_TOKEN 权限

流水线用 runner 自动注入的 `GITHUB_TOKEN` 上传 Release。若发布步骤报
`HTTP 403`：

- 站点管理 → Actions → **Token 权限**，确认默认 token 具备仓库读写；
- 或改为在仓库 **设置 → Actions → Secrets** 里建一个 `RELEASE_TOKEN`
  （个人访问令牌，勾选 `write:repository`），把工作流里的
  `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` 换成 `secrets.RELEASE_TOKEN`。

## 五、常见问题

**提示「没有匹配 ubuntu-latest 标签的在线运行器」（运行 0 秒、从未启动）**
说明 Gitea 找不到能接这个任务的 runner，任务只是排队后直接丢弃。按顺序查三处：

1. **runner 是否在跑**：`docker ps | grep -i runner`。在跑但后台显示离线，看
   `docker logs -f gitea-runner`，多半是 token 失效或连不上 `GITEA_INSTANCE_URL`。
2. **标签是否对得上**：后台看该 runner 的标签列表。`ubuntu-latest:docker://xxx`
   这种写法里，冒号前的 `ubuntu-latest` 才是标签名；如果只看到 `docker`、`linux`、
   `self-hosted`、`my-runner`、`act_runner` 之类，就是注册时没给 `--labels`，
   页面显示「空闲/在线」也接不了活。
3. **作用域对不对**：仓库级 runner 只服务注册它的那个仓库；组织级要显式授权，
   实例级才对全部仓库生效。

**runner 在线但标签不对，怎么修**

runner 列表最右侧有铅笔图标，点进去看能不能直接改 **标签**：能改就填
`ubuntu-latest:docker://catthehacker/ubuntu:act-22.04`（一行一个），保存后立刻生效，
不用重启。Gitea 较新版本支持这样改，老版本该页面只允许改描述和重置 token。

改不了就只能重来：

```bash
docker rm -f gitea-runner
rm -rf /srv/act-runner/.runner /srv/act-runner/config.yaml
# 然后按「一、准备 runner」第 2 步重新 register，再用第 3 步的配置启动
```

**服务器是 Alpine / 其它 musl 系统时**

别用 `ubuntu-latest:host`。OpenWrt buildroot 的宿主机工具链按 glibc 编写，
在 Alpine 上编不过去，而且流水线的依赖安装用的是 `apt-get`，Alpine 只有 `apk`。
正确做法是保持**容器模式**标签 `ubuntu-latest:docker://catthehacker/ubuntu:act-22.04`，
让任务跑在 Ubuntu 容器里，Alpine 只负责跑 Docker。工作流里已加守卫：万一配成
host 模式，会在「初始化编译环境」直接报「当前 runner 运行于 Alpine Linux，没有
apt-get」并给出这条建议，不会抛出难懂的 `apt-get: not found`。

**卡在「初始化编译环境」**
镜像里没有 `sudo`，或者 runner 不是 root。换 `catthehacker/ubuntu:act-22.04`
镜像，或用 `ubuntu-latest:host` 标签让它在宿主机上直接跑（此时需要 runner
用户有免密 sudo；宿主机须为 Debian/Ubuntu）。

**缓存一直提示不可写**
container 忘了挂载 `/srv/wrt-cache`，或宿主机目录属主不是容器内用户。执行
`chown -R 1000:1000 /srv/wrt-cache`（catthehacker 镜像默认用户是 1000）。

**拉 OpenWrt 源码很慢**
`WRT_REPO` 可以换成本地镜像地址（如自建的 GitHub 反代）。注意 `WRT_SOURCE`
要同步改成 `<owner>/<repo>` 形式，它参与缓存目录命名。

**想再跑其它机型**
复制本工作流，改四处即可：`WRT_CONFIG`、`WRT_REPO` / `WRT_BRANCH` /
`WRT_SOURCE`、`WRT_VARIANT`。FM350 / NetWiz / AP3000M 的专属步骤
（`Packages-FM350.sh`、`Packages-NetWiz.sh`、AirPi 预编译）在本文件里被裁掉了，
需要时从 `.github/workflows/WRT-CORE.yml` 对应段落搬过来。
