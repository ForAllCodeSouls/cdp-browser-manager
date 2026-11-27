# cdp-browser-manager

多 Chromium 实例管理器，每个实例独立 CDP 远程调试端口，可选 **VNC** 或 **CDP 抓屏** 查看画面。
适合配合任何基于 CDP 协议的自动化脚本或客户端使用。

> ⚠️ **免责声明**：本项目**仅限合法授权的自动化测试、开发调试与内网运维**。严禁用于未经授权的访问、爬取、绕过风控、批量注册、刷量或任何违法违规活动。下载、部署或使用本项目即表示你已阅读并同意文末 [免责声明](#免责声明)，并自行承担全部责任。

## 架构

采用**三份镜像独立构建**的设计，manager 与两种 browser 内核各自发布，改哪份只重新构建哪份：

| 镜像 | 大小 | 内容 | 更新频率 |
|---|---|---|---|
| `ghcr.io/forallcodesouls/cdp-browser-manager:latest` | 约 40 MB | FastAPI 管理面板，通过 Docker API 调度 browser 容器 | 代码改动时 |
| `ghcr.io/forallcodesouls/cdp-browser-full:latest` | 约 480 MB | 精简后的完整 `chrome` + Xvfb + x11vnc + noVNC + 中文字体 | 完整内核升级时 |
| `ghcr.io/forallcodesouls/cdp-browser-headless:latest` | 约 200 MB | 精简后的 `chrome-headless-shell` + 中文字体，无 VNC 栈 | 无头内核升级时 |

浏览器使用 **Chrome for Testing**：Google 官方为自动化测试发布的版本，无自动更新、版本可复现、不含推广内容。镜像各只装一份二进制，新建实例时选择启动方式，manager 按 `browser_mode` 决定用哪份镜像与启动参数：

| 启动方式 | 镜像 / 二进制 | 画面方式 | 说明 |
|---|---|---|---|
| 完整内核（full，默认） | `cdp-browser-full` / `chrome` | VNC + CDP 抓屏 | 有地址栏/标签栏，可看完整浏览器界面 |
| 老无头（headless） | `cdp-browser-headless` / `chrome-headless-shell` | 仅 CDP 抓屏 | 更省资源，无浏览器界面；身份由 manager 自动伪装 |
| 新无头（headless-new） | `cdp-browser-full` / `chrome --headless=new` | 仅 CDP 抓屏 | 复用完整 Chrome 的新无头模式，无浏览器界面；不额外新增镜像 |

> **关于浏览器身份**：实测 Chrome for Testing 158 的两种无头方式（`chrome-headless-shell` 与完整 Chrome 的 `--headless=new`）都会在 User-Agent 和 `Sec-CH-UA` Client Hints 里报 `HeadlessChrome`。两种无头实例启动后，manager 都会挂一条浏览器级 CDP 守护，自动给所有页面（含之后新建的标签）覆盖 UA 与 Client Hints，伪装成与有头实例一致的 `Chrome/主版本.0.0.0`；三种方式统一加启动参数让 `navigator.webdriver` 为 `false`。守护不改页面 DOM、不注入页面脚本。注意：`--remote-debugging-port` 是本项目工作的基础，无法隐藏，但普通网页 JS 读不到启动命令行。

三种方式的 CDP 端口都是容器内 9222，外部客户端的连接方式完全一致。版本号分别在 `Dockerfile.browser` 和 `Dockerfile.browser-headless` 顶部的 `CHROME_VERSION` 中锁定：需要单独升级某个内核就只改对应文件；想保持一致就两处一起改。CI 会分别构建推送，互不影响。

> **为什么容器里多了一层 socat 转发**：Chrome 136+ 起把 DevTools 强制绑在 `127.0.0.1`，`--remote-debugging-address=0.0.0.0` 在 148+ 已被完全忽略。若让 Chrome 直接监听 9222，manager 从容器外网桥 IP 访问会超时（表现为「CDP 未响应 / 画面无法连接 / targets 502」）。因此 `start.sh` 让 Chrome 只监听容器内 9223，再由 socat 把 `0.0.0.0:9222` 透明转发到 `127.0.0.1:9223`，对 manager 和外部客户端仍是 9222，完全无感。两份 browser 镜像都内置了 socat。

> **为什么浏览器容器要和 manager 同网**：`docker compose` 会为项目自动创建专属 bridge 网络，而 manager 通过 Docker API 创建的浏览器容器默认落在 `bridge`。两个不同 bridge 网络被 Docker 的隔离规则阻断，manager 按容器 IP 访问 CDP 会**超时**（不是拒绝连接），同样表现为 targets 502 / 抓屏不可用。现在创建实例时会把浏览器容器加入 manager 所在网络；manager 启动后还会自动把**历史遗留实例**补进该网络（每 30 秒检查一次，幂等），因此只需更新 manager 一次，旧实例也会自愈，不必删了重建。诊断接口 `/api/instance/{sid}/diag` 会返回 `manager_network` 与 `container_networks` 便于核对。

启动参数已加入去 Google 化开关（`--disable-background-networking`、`--disable-component-update`、`--disable-sync`、`--metrics-recording-only` 等），关闭后台联网与组件更新；官方 for Testing 包本身不含第三方渠道的广告内容。

> **浏览器顶部提示条已消除**：`--no-sandbox` 会触发 Chrome 76+ 的 "unsupported command-line flag" 安全警告（老参数 `--disable-infobars` 已失效），镜像内置管理策略 `CommandLineFlagSecurityWarningsEnabled: false` 关闭它；Chrome for Testing 的 "only for automated testing" 提示条则由 `--test-type=gpu` 跳过。完整内核 VNC 画面里不会再有这两条占用画面高度的横条。

### 镜像精简说明

Chrome 官方 zip 本身约 198 MB，但解压后是 412 MB，再加系统库和中文字体才堆到几百 MB。两份镜像做了以下精简（实测不影响 CDP、抓屏和 WebGL）：

| 精简项 | 节省 | 说明 |
|---|---|---|
| Noto CJK → 文泉驿微米黑 | 约 86 MB | 简体中文显示正常；繁体/日韩字形覆盖较弱 |
| `locales/` 220 个语言包 → 只留中英 | 约 46 MB | 仅影响 Chrome 界面语言，网页内容不受影响 |
| 删除 WidevineCdm（完整版） | 约 21 MB | DRM 视频播放组件，自动化用不到 |
| 清理 apt 文档 / 构建期工具 | 约 12 MB | 同一构建层内清除，分层删不会减小镜像 |

**两个绝对不能删的东西**（曾因误删导致容器反复重启，已修复）：

- `chrome_crashpad_handler`：Linux Chrome 启动时会 `exec` 它，文件缺失直接 FATAL 崩溃。
- 动态库 `libxkbcommon.so.0` / `libudev.so.1` / `libatspi.so.0`：它们在包里是传递依赖，若不在 Dockerfile 里**显式安装**，`apt autoremove` 会把它们当孤儿删掉，浏览器随即起不来。现在已显式列出，标记为 manual 不会被清理。

**刻意保留**：`libvk_swiftshader.so` / `libEGL.so` / `libGLESv2.so`。虽然启动参数有 `--disable-gpu`，实测删除后 WebGL 会变成 `NO_WEBGL`；它们合计仅 4 MB，不值得为省这点空间丢功能。

manager 启动后挂载宿主机 Docker socket，按需创建/销毁 browser 容器。完整内核容器内部固定使用 9222（CDP）、5900（VNC）、6080（noVNC），无头内核只使用 9222，再通过端口映射分配宿主机独立端口。`start.sh` 由两份镜像共用，改动它会同时重建两份 browser 镜像；只升级 manager 时大镜像层命中缓存，几乎零下载。

### 可配置环境变量

manager 支持以下环境变量（Compose 中已给出常用项）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BROWSER_IMAGE_FULL` / `BROWSER_IMAGE_HEADLESS` | 官方 GHCR 镜像 | 指定两份 browser 镜像；旧 `BROWSER_IMAGE` 仍作为 full 镜像兼容 |
| `AUTO_PULL_IMAGES` | `1` | 开启定时自动更新；设 `0` 关闭 |
| `AUTO_UPDATE_INTERVAL_HOURS` | `6` | 定时自动更新的检查间隔（小时，最小 1）：默认每 6 小时检查一次，仅当无运行实例时拉取 |
| `PUBLIC_HOST` | 空 | 反向代理时填对外地址，如 `https://cdp.example.com`；留空按请求 Host 生成链接 |
| `API_KEY` | 空 | 可选。设置后本机所有管理 API / CDP / WS 都需带同一密钥；留空则完全不启用鉴权，行为与单主机部署一致 |
| `REMOTE_HOSTS` | 空 | 可选。多主机聚合配置（JSON 数组或简易格式），详见「多主机统一操控」；也可免配置、直接在面板「远程」里互连 |
| `DATA_DIR` | `app/data` | 面板添加的远程主机与本机接入令牌的持久化目录；Docker 部署挂载到 `/app/data` |
| `MANAGER_ID` | `default` | 多个 manager 共用一台宿主机时，隔离容器标签与持久卷 |
| `BROWSER_SHM_SIZE` | `512m` | 每个 browser 容器的 `/dev/shm`；启动参数已带 `--disable-dev-shm-usage` |
| `CAST_MAX_WIDTH` / `CAST_MAX_HEIGHT` | `1920` / `1080` | 抓屏「高清」档的帧上限（默认与 start.sh 的 1920×1080 窗口一致），避免高分屏帧拖慢 WS 转发 |
| `CAST_KEEPALIVE_SEC` | `1.5` | 旧版兜底抓帧间隔（秒），仅当客户端未传 `snap` 参数时生效；新版面板始终传档位（默认 0.5 秒），此变量一般无需配置 |
| `LOG_LEVEL` | `INFO` | manager 日志级别 |

`GET /api/health` 是存活探针：同时检查面板服务与 Docker 守护进程是否可用。镜像内 `HEALTHCHECK` 和 Compose 健康检查都使用它。

## 功能
- 一键新建浏览器实例，可选完整内核 / 老无头 / 新无头三种方式，自动分配独立 CDP 端口（仅完整内核另分配 VNC、noVNC）
- **画面双模式，随时切换**：
  - **VNC**：完整桌面画面，可见地址栏/标签栏（仅完整内核）。画面上方的**导航条在 VNC 模式下同样可用**——双行布局：第一行是固定宽度的标签（每个右侧带 × 可单独关闭，标签多了横向滚动），第二行是后退/前进/地址栏（回车跳转）/刷新/恢复上次关闭标签。底层用同一实例的 CDP 接口读写标签页，切换标签时自动激活目标标签使 VNC 画面跟随，无需再退回 VNC 窗口手动点地址栏。
  - **CDP 抓屏**：画面与自动化共用同一 CDP 端口；导航条同 VNC 模式，支持标签切换/单独关闭、地址栏（回车跳转）、前进/后退、刷新、恢复上次关闭标签、鼠标键盘交互（三种启动方式通用，无头实例只能用它）。画面按原比例居中，两侧留白用面板色（不变形、不裁切）。刷新按钮真实下发 `Page.reload`，失败会明确提示而非假成功。
- **标签页行为（抓屏 / VNC 一致）**：标签按**打开顺序**排列——新标签（含页面脚本 `window.open` 弹出的）追加到最右并**自动切换过去**，与正常浏览器一致；切换标签不改变标签顺序，关闭后其余标签相对顺序保持不变，顺序持久化到 `DATA_DIR/tab_order.json`，manager 重启后仍保持。点标签右侧 × **直接关闭，不再弹确认框**；导航条上的**恢复按钮**可像浏览器 `Ctrl+Shift+T` 一样连续恢复最近关闭的多个标签（关闭栈保存在浏览器本地，最多 50 条）。抓屏滚轮走 CDP `Input.dispatchWheelEvent`（含必需的 `type: mouseWheel`），滚动、Ctrl+滚轮缩放与鼠标点击一样生效。
    - **抓屏方式 Cast / Snap（全局、随时可切）**：详情栏画质按钮旁可切 Cast 或 Snap，选择保存在浏览器本地，对所有实例统一生效；旁边的 **快 / 标准 / 省** 三档间隔两种模式共用，切换后画面自动重连。
      - **Cast**（默认，最跟手）：`Page.startScreencast` 变化即推帧，**同时按所选间隔用 `Page.captureScreenshot` 定时保底抓帧**。两路互补——页面在变走 screencast（最快），静止或变化太细微 CDP 不推帧时走定时保底，最长滞后从旧版固定的 1.5 秒缩短到所选间隔（默认 0.5 秒），验证码/挑战页不会冻结。收到任意一路的实帧都会重置保底计时，不会重复抓图。
      - **Snap**：不发 `startScreencast`，纯 `Page.captureScreenshot` 按所选间隔定时抓图，帧率固定不依赖页面变化；间隔可选 **快 0.2 秒 / 标准 0.5 秒 / 省 1 秒**（约 5 / 2 / 1 帧）。适合需要稳定节奏或 screencast 推帧异常的环境。
      - 监控主面板固定走 Cast 的监控档，不受此切换影响。
    - **地址栏实时跟随**：抓屏 WS 复用 CDP 的 `Page.frameNavigated` / `Page.navigatedWithinDocument` 事件，页面一导航就把新 URL 秒级推给面板更新地址栏，网页自动化快速跳转也能跟上；VNC 模式没有抓屏 WS，靠 2.5 秒轮询更新地址栏。用户正在地址栏输入时不会被推送覆盖。
- **画质三档（省流 / 均衡 / 高清）**：在实例详情栏「VNC / 抓屏」切换旁边的快捷按钮切换，**全局统一对所有实例生效**，切换后画面自动重连。抓屏按分辨率与码率生效——省流 960×540 / 低码率、均衡 1280×720、高清保持窗口原始上限；三档都**不隔帧**，刷新速度一致，区别只在清晰度与流量（省流是缩图省带宽，不会慢半拍）；VNC 使用 noVNC 内置的 `quality` / `compression` 参数。首次打开会按浏览器报告的网络（`navigator.connection`）自动建议一档，之后以你手动保存的设置为准。网络差选省流更省流量，网络好用高清更清晰。
    - 实现上由 manager 的 `/ws/cast/{sid}/{tid}` 把容器内的 screencast 帧与定时 `captureScreenshot` 帧代理到浏览器 canvas，并转发输入事件。查询参数 `mode=cast|snap` 指定方式，`snap=fast|normal|eco` 指定定时节奏（Cast 用作保底心跳、Snap 用作主节奏）；未知值后端回落默认值，未传 `snap` 时回落 `CAST_KEEPALIVE_SEC`。不直接用 Chrome 自带的 `devtools/inspector.html`——它的 CSP 只允许 `ws://127.0.0.1`，公网访客连不上。
    - **鼠标/键盘交互与 VNC 一致**：支持左键单击/双击（按 `detail` 传 `clickCount`）、右键菜单、中键、鼠标**前进/后退侧键**、拖拽、悬停移动、滚轮滚动与 **Ctrl+滚轮缩放**。所有按下/释放/移动事件都带修饰键（Ctrl/Alt/Shift/Meta），因此 `Ctrl+C/V`、组合拖拽等都能在远程生效；滚轮会把浏览器的行/页滚动量换算成 CDP 像素单位（`deltaMode` 归一），侧键映射为 CDP 的 `back`/`forward`，中键点击与右键菜单在管理页本地被拦截，不会误触发浏览器自身的自动滚动或菜单。多键组合状态通过原生 `e.buttons` 位图透传。
    - **鼠标坐标按真实视口换算**：画质档会把画面缩小（省流 960×540 / 均衡 1280×720），后端随帧推送浏览器真实视口尺寸，前端先扣除 `object-fit: contain` 的留白、再按比例换算成 CDP 坐标，因此点击位置与 VNC 一致；拖出画面边界再松开时坐标钳制到边缘并补发释放事件，不会卡在按下态。鼠标移动事件按帧合并，WS 意外断开会自动重连，切换实例或标签时会作废旧连接避免画面串台。
- **VNC 刷新速度（全局、随时可改）**：完整内核实例详情栏里有「VNC 刷新」下拉，可选 标准 / 快速 / 极速，控制容器内 `x11vnc` 的采集节流（`-wait` 轮询间隔 / `-defer` 合并延迟）：标准 20/30ms（x11vnc 默认，约 30fps 上限）、快速 10/10ms、极速 0/0ms（画面一变就推，最跟手，连续动画时 CPU 与带宽明显上升）。**全局统一、点一下立刻对所有主机所有运行实例生效**：manager 通过 `x11vnc -R wait/defer` 热改运行中的实例，无需重建、画面不断开；档位持久化到服务端数据目录（`vnc_refresh.json`），新建实例自动沿用上次选择，manager 重启后也会重新热改到已保存档位。前端 noVNC 的 `quality` / `compression` 只影响清晰度与带宽，改不了帧率；无头实例没有 VNC，不显示该下拉。
- **监控主面板**：入口是**实例列表标题行右侧的网格图标按钮**（打开时高亮），进入后网格同屏显示所有勾选实例的实时画面；**左侧实例列表保留**，可在监控模式下继续切换预览、筛选主机、拖实例进网格。左侧每个 sid 旁有「预览」**滑动开关**，拨到开即加入监控、拨到关即移出（勾选保存在浏览器本地）。**面板开关状态同样持久化**：停在监控视图时刷新页面会自动回到监控面板，不会掉回普通视图。监控走 **CDP 抓屏的专用监控档**（640×360 / 低码率 / 约 1 帧/秒限帧），多路并发也不会卡顿，完整内核与无头实例都能看；该档位不在画质三档里，避免误选。远端实例的画面同样走 `/remote/<host>` 代理接入。除滑动开关外，实例列表项也**可以直接拖进监控网格**加入预览；网格里的卡片可拖动排序，顺序保存在浏览器本地。卡片采用独立描边 + 状态灯的网格样式，画面区与信息栏分层更清晰。
- **侧栏显隐**：左侧实例列表**右边缘中部有一个折叠把手按钮**（箭头随状态切换方向），点它可**全局隐藏/显示列表**，普通视图和监控面板都生效；隐藏后画面/监控网格占满全宽，把手移到最左边仍可点回来。顶栏不再放「侧栏」按钮。状态持久化到浏览器本地，刷新后保持。
- **滚动条样式**：管理页与只读观看页统一使用深色细滚动条，覆盖侧栏、弹窗、列表等所有滚动区域，不会在深色界面里出现浏览器默认的亮色滚动条。
- **实例列表顺序与筛选**：本机实例排在前面、其它主机的排在后面，各自按创建时间倒序（最新创建的在前）；创建时间取容器实际创建时间，manager 重启恢复后排序依然稳定。列表上方有「全部 / 本机 / 各远程主机」筛选标签，点一下只看该主机的实例，选择保存在浏览器本地。列表按用户目录模式分成 **「临时实例」/「持久实例」两组**，点组标题可折叠/展开，折叠状态刷新后保持；**每组标题右侧都有新建 + 停止两个图标按钮**，且只作用于本组：临时组的新建只创建临时实例、停止只列临时实例；持久组的新建只创建持久实例（可选新建目录或复用已有目录）、停止只列持久实例，顶栏不再放这两个按钮。点停止会弹确认框，**默认全选当前组实例**，每条列出完整浏览器信息（完整 sid、状态、内核、目录模式与目录名、所属主机、CDP/VNC/noVNC 端口、CDP 与 VNC 地址、VNC 刷新档、创建时间、容器名），取消勾选即可只停选中实例，本机与远端实例一起处理，持久目录保留不删除。
- **顶栏精简**：实例状态每 10 秒自动刷新，顶栏的手动「刷新」按钮已移除；「密钥」按钮只在服务端设置了 `API_KEY` 时显示，没设密钥时不占位置。远端主机通过接入令牌连接，远端不用填密钥。「远程」弹窗里也已删掉手动刷新按钮，弹窗打开期间主机列表与令牌列表**每 3 秒自动刷新**，关闭即停止。
- **后台省流**：页面切到后台标签或窗口失焦时自动进入省流模式——断开普通视图与监控面板的全部画面通道（包括 iframe 里的 VNC），暂停状态轮询，后台不吃流量；一切回前台或重新聚焦**立即重挂画面并刷新状态**，不等下一轮 10 秒轮询。
- **只读分享**：顶栏「分享」打开管理弹窗，把左侧实例**拖进右侧「分享区」**即加入观看列表——加入后该实例**立即从左侧「可分享实例」列表消失**，不再两边重复；拖回左侧或点 × 即移出分享、重新出现在左侧，卡片可拖动排序。两侧的实例卡片都显示**完整信息**（完整 sid、状态、内核、目录模式、CDP 端口、所属主机），多个实例一眼可区分；选 1 小时 / 6 小时 / 24 小时 / 7 天有效期后保存，生成**一条全局观看链接**（`/share?token=...`）。链接有效期到自动失效，服务端配置保存在 `DATA_DIR/share.json`；保存分享设置只续期不改地址，「重置链接」会作废旧链接并生成新令牌。观看端是独立只读页面：默认网格同屏（低码率档），点卡片进入放大单看（高清档，Esc 返回），顶部显示剩余有效期；**不提供导航、输入、管理任何控件**，后端 WS 对分享令牌直接丢弃输入与视口消息，观看者无法反向操控浏览器。分享链接自带令牌，无需 API_KEY；令牌只授予被勾选实例的观看权，不在列表里的 sid 即使拼出地址也连不上。
- **屏幕锁（防误操作）**：顶栏「🔓 屏幕锁」可锁定当前画面。锁定后透明遮罩盖住画面、拦截全部鼠标/键盘输入，并禁用导航条（标签、地址栏、关闭等），避免在脚本/自动化执行期间被误点击打断。锁定状态持久化到浏览器本地存储，刷新页面后保持；再次点击「🔒 已锁定」或遮罩中的「解锁画面」即可恢复。
- **用户目录双模式**：
  - **临时目录**：停止实例即清理，适合一次性任务
  - **持久目录**：以 Docker Volume 保存在宿主机，停止实例后数据仍在；从「持久实例」组点 + 新建时可选择「新建持久目录」或「复用已有目录」恢复登录态，需在面板左侧手动点删除才会清除
- Web 面板预览浏览器画面（左侧点击实例切换，画面自动连接）。当前所在实例与标签会记在本机浏览器，刷新面板后自动回到原来的画面，不会跳回列表第一个；画面设置（VNC/抓屏、vnc_lite/vnc.html、缩放、全局画质档位、屏幕锁）同样保持
- 复制 CDP 调试地址，外部客户端可通过 CDP WebSocket 连接控制
- 停止单个浏览器实例，或按分组在弹窗勾选后批量停止（只列当前组，默认全选，可只停部分）
- manager 重启后自动恢复已有实例（通过容器标签识别）

### 用户目录说明

新建实例弹窗中可选三种模式：

| 模式 | 数据存储 | 停止实例后 | 适用场景 |
|---|---|---|---|
| 临时目录 | 容器内 `/tmp/cdp-profile` | 数据随容器删除 | 日常自动化，不需登录态 |
| 新建持久目录 | Docker Volume `cdp-profile-*` | 数据保留 | 需要保留 Cookie / 登录态 |
| 复用已有持久目录 | 挂载同名 Volume | 数据保留并复用 | 下次继续上次的登录状态 |

持久目录在面板左侧「持久用户目录」区列出。被运行中实例占用的目录显示「使用中」，不能删除；停止实例后可点「删除」永久清除。

API：

```bash
# 新建持久目录实例（完整内核）
curl -X POST http://你的IP:8000/instance/new \
  -H 'Content-Type: application/json' \
  -d '{"profile_mode":"persistent","profile_name":"shop-account","browser_mode":"full"}'

# 新建老无头实例（不分配 VNC/noVNC 端口，身份由 manager 自动伪装）
curl -X POST http://你的IP:8000/instance/new \
  -H 'Content-Type: application/json' \
  -d '{"browser_mode":"headless"}'

# 新建新无头实例（完整 Chrome --headless=new，身份同样由 manager 伪装）
curl -X POST http://你的IP:8000/instance/new \
  -H 'Content-Type: application/json' \
  -d '{"browser_mode":"headless-new"}'

# 列出所有持久目录
curl http://你的IP:8000/api/profiles

# 删除持久目录（需先停止使用它的实例）
curl -X DELETE http://你的IP:8000/profile/shop-account
```

面板抓屏用到的 CDP 代理接口（标签 id 从 `/api/instance/{sid}/targets` 获取）：

```bash
# 列出标签页
curl http://你的IP:8000/api/instance/{sid}/targets

# 新建标签（面板用此接口代理，浏览器 JS 不直接访问 CDP 端口）
curl -X POST http://你的IP:8000/api/instance/{sid}/targets \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com"}'

# 下发 CDP 命令
curl -X POST http://你的IP:8000/api/instance/{sid}/cmd \
  -H 'Content-Type: application/json' \
  -d '{"target_id":"标签ID","method":"Page.navigate","params":{"url":"https://example.com"}}'

# 查看 manager 全局运行日志（访问日志、异常堆栈；面板顶栏「运行日志」直接查看）
# 默认读取 manager 容器自身的 stdout/stderr，非容器环境自动回退到进程内存日志
curl http://你的IP:8000/api/logs/global?tail=300

# 查看某个 browser 实例容器的日志与退出码（排障用，面板不展示）
curl http://你的IP:8000/api/instance/{sid}/logs?tail=200

# 深入诊断：容器内进程 / 动态库 / 关键日志检查
# chrome 报错已改为输出到 stdout，直接用上面的 /logs 接口即可；
# 这个接口用于进一步查动态库缺失、进程状态等启动期问题。
curl http://你的IP:8000/api/instance/{sid}/diag
```

> 面板顶栏的「运行日志」是滑动开关：打开后日志抽屉从右侧滑出，与画面并排显示；
> 关上后抽屉收起、画面占满。日志为 manager 全局记录，每 10 秒自动刷新，不再有单实例来源切换。

## 部署（推荐 Docker Compose）

项目根目录已附带 `docker-compose.yml`（完整版还含 `AUTO_PULL_IMAGES`、`AUTO_UPDATE_INTERVAL_HOURS`、`PUBLIC_HOST` 与健康检查，以仓库内文件为准）。**多主机部署时下面这几个远程相关配置必须带上**：

```yaml
services:
  cdp-manager:
    image: ghcr.io/forallcodesouls/cdp-browser-manager:latest
    container_name: cdp-manager
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      # 关键：面板里添加的远程主机、本机生成的接入令牌都持久化在这里。
      # 控制端 A 存「远程主机列表」，被控端 B 存「接入令牌」，两端都要挂这一行，
      # 否则容器重建后远程接入关系全部丢失。
      - ./manager-data:/app/data
    environment:
      BROWSER_IMAGE_FULL: ghcr.io/forallcodesouls/cdp-browser-full:latest
      BROWSER_IMAGE_HEADLESS: ghcr.io/forallcodesouls/cdp-browser-headless:latest
      # 可选：本机管理 API 密钥（设置后打开面板需输入一次）
      # API_KEY: ${API_KEY:-}
      # 可选：环境变量方式接入远端主机。更推荐用面板「远程」按钮互连，无需改这里。
      # REMOTE_HOSTS: '[{"id":"b","name":"主机B","base":"https://b.example.com","key":"B主机的API_KEY"}]'
      # 也支持简易格式：b|主机B|https://b.example.com|B主机的API_KEY;c|主机C|http://10.0.0.2:8000|
      # REMOTE_HOSTS: ${REMOTE_HOSTS:-}
      # 可选调优：抓屏帧上限（CAST_KEEPALIVE_SEC 仅作旧客户端兜底，新面板用快/标准/省档位）
      # CAST_MAX_WIDTH: "1920"
      # CAST_MAX_HEIGHT: "1080"

  # 占位服务：让 docker compose pull 能同时更新两份 browser 大镜像
  cdp-browser-full-image:
    image: ghcr.io/forallcodesouls/cdp-browser-full:latest
    profiles: ["no-auto-start"]

  cdp-browser-headless-image:
    image: ghcr.io/forallcodesouls/cdp-browser-headless:latest
    profiles: ["no-auto-start"]
```

> 两个占位服务带 profile，不会被 `docker compose up -d` 启动，只用于让 `docker compose --profile no-auto-start pull` 把两份 browser 镜像一并更新。`upgrade.sh` 已带上该参数。
>
> **用 Dockge / 面板更新时**：这类面板的「更新」通常只拉 manager、跳过带 profile 的占位服务，导致浏览器镜像一直是旧的（典型症状：容器 `restarting` / `exit 127`、端口全 null）。本版本已内置**定时自动更新**兜底：manager 每 6 小时自动检查一次浏览器镜像，仅当**无任何运行实例**时才拉取更新（避免打断在用浏览器）；启动后也会先做一次检查。设置页会显示「每 6 小时检查一次 / 上次检查结果 / 下次检查时间」，接口为 `GET /api/images/auto-status`。镜像更新后需停止并重建实例才会用上新镜像。若不想自动更新，设 `AUTO_PULL_IMAGES=0`；改间隔设 `AUTO_UPDATE_INTERVAL_HOURS`（小时，最小 1）。
>
> **两种手动按钮**：设置里另有「立即更新（仅在无实例运行时）」和「强制拉取镜像（手动，可跨运行中实例）」。前者先检查运行实例数，有实例时直接拒绝并提示先停止（后端同样做 409 拦截，双保险）；后者不管有无实例都拉取，但不会应用到已运行实例，需重建才生效。定时自动更新与「立即更新」遵循同一规则：有实例就跳过、写日志，下轮无实例时自动补上。
>
> **更新红点**：顶栏「⚙ 设置」按钮上的红点表示本地浏览器镜像落后于 GHCR 最新版本（对比镜像摘要，`GET /api/images/status`）；点开设置可看到是完整内核还是无头内核有更新。为避免误报，后端对远端摘要做**三次采样取多数**（至少两次一致才算确认），本地缺镜像不算「有更新」，红点与设置面板共用同一缓存数据源。检测无法确认时（GHCR 多节点/CDN 抖动）显示为「检测中」，沿用上一轮结论且用短缓存尽快重试，**不会把"没检测准"误显示成"已是最新"或让状态来回横跳**。红点每 5 分钟自动刷新，确认后缓存 10 分钟，无法连接 GHCR 时不亮红点。
>
> **浏览器跟随官网自动更新**：`.github/workflows/chrome-auto-update.yml` 每天检查 [Chrome for Testing 官方稳定版](https://googlechromelabs.github.io/chrome-for-testing/)，发现新版本且 linux64 的 `chrome` 与 `chrome-headless-shell` 都可下载时，自动更新两个 Dockerfile 的 `CHROME_VERSION` 并提交，从而触发 `docker-build.yml` 重建 full/headless 镜像。你只需在服务器上更新 manager 并重建实例即可用上新内核。
>
> **CI 构建竞态**：推送代码后 GHCR 上的 `latest` 要等 GitHub Actions 跑完才会切换。若在这之前更新镜像，`pull` 会成功但拉到的是**上一版**镜像。现在拉取后会复核本地/远端摘要，发现不一致会在设置里明确提示“拉到旧版本”，并记为待重试状态，定时自动更新下一轮会再试；手动更新则需等 CI 结束后再拉一次。看到该提示时删除旧实例重建即可用上新版本。
>
> **内置连接自测**：设置里的「运行自测」用官方 CDP WebSocket 协议在**临时创建**的有头 / 老无头 / 新无头实例上跑一组用例（HTTP 探测、WebSocket 握手、JS 同步/异步求值、导航与刷新、页面源码 `outerHTML`、DOM 节点源码 `getOuterHTML`、盒模型、资源树、控制台事件、视口与布局尺寸、性能指标、Cookie 写入/读取/列举/清理、存储配额、鼠标点击、键盘输入、文本插入、截图并校验 PNG 文件头、抓屏帧、多标签页、UA 读取、身份无 HeadlessChrome、`navigator.webdriver` 已隐藏）。
> 自测**串行**跑有头 → 老无头 → 新无头三个临时实例；过程中画面区会**依次挂载被测浏览器**供你直观确认三种都真的启动了（每种跑完多停留约 6 秒展示），结束后自动切回你原来选中的实例。结果以**「完整内核 / 老无头 / 新无头」三栏卡片**呈现，每跑完一种即填充对应卡片，不必等全部结束；详细过程同时写入「运行日志」。测试实例用完即销毁，不影响你正在运行的实例。接口为 `POST /api/selftest/run` 与 `GET /api/selftest/status`。
>
> 兼容旧部署：如果仍只设置了 `BROWSER_IMAGE`，它会被当作 full 镜像使用；headless 镜像始终读 `BROWSER_IMAGE_HEADLESS`。建议尽快换成上面两个新变量。

在 `docker-compose.yml` 所在目录执行。**推荐直接用一键升级脚本**（含拉取镜像、删旧容器、重建、校验）：

```bash
bash upgrade.sh
```

等价的手动命令：

```bash
docker compose --profile no-auto-start pull   # 拉取 manager + full/headless 三份镜像（含占位服务）
docker ps -aq --filter label=cdp.browser.manager | xargs -r docker rm -f   # 升级时清理旧浏览器容器
docker compose up -d            # 后台启动 manager
docker compose ps              # 查看 manager 状态
docker compose logs -f         # 跟踪日志
docker compose down            # 停止并移除 manager（browser 实例会保留）
```

> **为什么升级必须删旧容器**：browser 容器一旦创建，就固定使用创建时的镜像和 `start.sh`。只更新镜像、不删容器，旧实例仍跑旧代码（典型症状：画面能开但 CDP 连不上、切换实例画面不变）。`upgrade.sh` 已包含这一步。

访问面板：

```
http://你的IP:8000
```

> 停止 manager 后 browser 容器仍会保留；需要彻底清理时执行 `docker ps -a --filter label=cdp.browser.manager` 查看，再逐个 `docker rm -f`。

若不用 Compose，等价的 `docker run` 命令：

```bash
docker run -d \
  --name cdp-manager \
  --restart unless-stopped \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e BROWSER_IMAGE_FULL=ghcr.io/forallcodesouls/cdp-browser-full:latest \
  -e BROWSER_IMAGE_HEADLESS=ghcr.io/forallcodesouls/cdp-browser-headless:latest \
  ghcr.io/forallcodesouls/cdp-browser-manager:latest
```

## 外部调用

每个实例都暴露标准的 Chrome DevTools Protocol（CDP）端点，任何实现 CDP 客户端的库都可以直接连接，无需针对本项目做适配。

连接信息从面板的实例详情里复制「CDP」地址，包含两部分：

- **HTTP 地址**：如 `https://你的域名/9222`，用于发现目标和读取元信息
  - `GET /json/version` —— 浏览器版本，以及 `webSocketDebuggerUrl`
  - `GET /json/list` —— 当前标签页列表，含每个页面的 `id` 与 `webSocketDebuggerUrl`
- **WebSocket 地址**：如 `wss://你的域名/9222/devtools/browser/...`（浏览器级）或 `wss://你的域名/9222/devtools/page/<id>`（页面级），连接后按 CDP 协议发送 `Page.navigate`、`Runtime.evaluate` 等命令即可。

**CDP 走 manager 同源代理**：面板显示的 CDP 地址是 `域名/端口`（不是 `域名:端口`）。manager 按路径里的端口找到对应实例，从容器内网访问真实 CDP，并把响应里的 WebSocket 地址改写成同源 `wss://...`。这样做的原因是反向代理通常只把 443 转给 manager、不转发浏览器的 CDP 端口，而且 Chrome 会拒绝 Host 非本机地址的直连。因此**只要能打开面板的域名，就能直接用同一域名连接 CDP**，无需暴露任何额外端口，也不必改动反向代理配置。直连宿主机 `http://IP:9222` 在宿主机本地仍可用。

三种方式的 CDP 端口与协议完全一致，连接代码不需要区分；无头实例只是不提供 VNC/noVNC 端口。

**VNC 画面同样走 manager 同源代理**：面板里 VNC 模式加载的是 `https://你的域名/<noVNC端口>/vnc_lite.html?...&path=<端口>/websockify`。manager 按路径里的端口找到对应实例，静态资源从容器内网 `:6080` 读取，WebSocket 通过 `/<端口>/websockify` 二进制透传到容器的 websockify。因此 VNC 和 CDP、抓屏一样，**共用面板域名、无需暴露额外端口、无需改反向代理**。抓屏本来就走 manager 的 `/ws/cast` 同源通道，域名下可直接用。若确实需要绕过代理，实例详情里仍保留了 `vnc_direct` / `novnc_direct` 直连地址字段（局域网直连场景）。

端口分配范围：CDP `9222~9322`，VNC `5901~6000`，noVNC `7901~7999`（无头内核只分配 CDP 端口）。页面显示的是宿主机端口，代理路由里的 `9222` / `7901` 就是该端口。

**旧客户端兼容**：同源代理是通过**新增字段**提供的，原有的连接字段格式保持不变，老项目无需任何改动：

| 字段 | 格式（未变） | 说明 |
| --- | --- | --- |
| `cdp_addr` | `主机:端口` | 如 `192.168.1.10:9222` |
| `cdp_http` | `http://主机:端口` | 直连式 CDP HTTP 地址 |
| `vnc_url` / `novnc_url` | `http://主机:端口` | 直连式 VNC/noVNC 地址 |

新版面板另用 `cdp_addr_proxy`、`cdp_http_proxy`、`vnc_url_proxy`、`novnc_url_proxy` 这四个字段访问同源代理（如 `https://域名/9222`）。`vnc_direct` / `novnc_direct` 是 `vnc_url` / `novnc_url` 的直连别名。也就是说：老脚本读旧字段走直连、新面板读新字段走域名代理，**两种方式并存、互不干扰**。宿主机端口映射始终保留，直连 `http://IP:9222` 不受影响。

实例详情栏会**同时列出两种 CDP 地址**（各自带「复制」按钮）：`CDP(域名)` 是同源代理地址，`CDP(直连)` 是旧格式直连地址；只有后端同时提供新旧字段时才显示两行，旧后端仍只显示一行。

## 多主机统一操控

两台主机各跑一套 manager 时，可以在**同一个网页**里创建、切换、操控两台主机上的浏览器。

**架构**：浏览器只连接面板所在主机（A）。A 根据服务端配置把请求与 WebSocket 转发到远端主机（B），并把 B 返回的实例地址改写为 A 域名下的 `/remote/<主机ID>/...`。B 的 API 密钥只写在 A 的服务端配置里，前端拿不到，也不会出现在 URL 或 localStorage 中。这样不存在跨域、混合内容（HTTPS 页面连 HTTP 主机）问题，两台主机的浏览器端口都不需要对外暴露。

**方式一：面板互连（推荐，两端都不用改服务端配置）**

两台主机各自用上面的 `docker-compose.yml` 部署（**务必都带 `./manager-data:/app/data` 挂载**），然后：

1. **被控主机 B**：打开自己的面板 → 顶栏「远程」→ 点「生成接入令牌」→ 令牌列表立即刷新显示 → 填 B 的对外地址（如 `https://b.example.com`，必须能被 A 访问到）→ 复制生成的连接串 `地址|令牌`。
2. **控制主机 A**：打开自己的面板 → 顶栏「远程」→ 粘贴连接串 → 点「添加远程主机」。后端先试连 B 的 `/api/list` 验证，通过后立即出现在「新建实例 → 目标主机」下拉里。
3. 之后 A 面板即可创建并远控 B 的浏览器。接入关系与令牌分别持久化在 A、B 的 `./manager-data/`，容器重建不丢；可在「远程」面板随时删除（只移除关系，不影响 B 上已有实例）。「远程」面板的已接入主机列表里，点主机地址可在新标签页直接打开那台主机的面板。

> 若 B 设了 `API_KEY`，先用自己的密钥登录 B 面板才能生成令牌；令牌等同 B 的完整权限，只发给自己的其他主机。
> B 的面板地址必须是 A **服务器**能访问到的地址（不是 A 用户浏览器的地址），因为代理请求由 A 的 manager 发出。

**方式二：环境变量配置（在 A 的 `docker-compose.yml` 中设置）**

```yaml
environment:
  # A 自己的密钥：设置后打开面板需输入一次，保存在当前浏览器
  API_KEY: ${API_KEY:-}
  # JSON 数组：远端地址 + 远端密钥
  REMOTE_HOSTS: >-
    [{"id":"b","name":"主机B","base":"https://b.example.com","key":"B主机的API_KEY"}]
```

也支持简易格式（用 `;` 分隔多台，每段 `id|名称|base|密钥`）：

```yaml
REMOTE_HOSTS: "b|主机B|https://b.example.com|B主机的API_KEY;c|主机C|http://10.0.0.2:8000|"
```

> B 端若设了 `API_KEY`，这里的 `key` 就填它；未设则留空。环境变量方式也要在 B 挂 `./manager-data:/app/data` 吗？——不需要存令牌，但**两端都挂上最省事**，日后想切换成面板互连不用改 compose。

- `id` 只允许字母、数字、下划线、短横线，且不能是 `local`；不合法的条目会被忽略并写入运行日志。
- 未设置 `API_KEY` 时完全不启用鉴权；未设置 `REMOTE_HOSTS` 时只有「本机」，界面与单主机版本一致。
- 面板顶栏「密钥」按钮可重新输入本机密钥；远端不可达时该主机的实例会自动跳过，本机不受影响，错误记入运行日志。
- 多主机时新建弹窗会出现「目标主机」下拉；实例列表用徽标标出所属主机。CDP、VNC、抓屏地址均由后端改写为走 A 的代理，可直接复制使用。
- 持久目录列表、镜像更新与内置自测仍只针对**面板所在主机**；远端主机请到它自己的面板维护。实例级操作（新建 / 停止 / 标签 / 导航 / 抓屏）支持远端。

**两种方式的关系**：环境变量 `REMOTE_HOSTS` 与面板接入的主机**合并生效**；同 id 时面板配置覆盖环境变量。环境变量配置的主机在界面上没有「删除」按钮，需在服务端修改并重建容器。令牌文件为 B 的 `/app/data/link_tokens.json`，远程主机列表为 A 的 `/app/data/remote_hosts.json`，都在 `./manager-data` 挂载下。

## 本地构建

```bash
# 两份 browser 镜像可单独构建，互不影响
docker build -f Dockerfile.browser         -t ghcr.io/forallcodesouls/cdp-browser-full:latest .
docker build -f Dockerfile.browser-headless -t ghcr.io/forallcodesouls/cdp-browser-headless:latest .
docker build -f Dockerfile.manager         -t ghcr.io/forallcodesouls/cdp-browser-manager:latest .

# 若改的是本地 tag，同步修改 compose 里的镜像名后再启动
docker compose up -d
```

## 修改后自检

改完代码可以先在本地跑一遍静态检查，确认没有低级错误再提交：

```bash
python -m py_compile app/main.py                  # 后端语法
python test_multihost.py                          # 多主机/鉴权自测（纯逻辑；装了 fastapi+httpx 会连 mock 远端跑完整集成测试）
bash -n start.sh upgrade.sh                       # 启动/升级脚本
docker compose config -q                          # Compose 配置
curl -fsS http://127.0.0.1:8000/api/health        # 启动后探针
```

## 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| CDP 一直"未响应"，端口连不上 | browser 用旧镜像，缺 `--user-data-dir`，Chrome 136+ 静默忽略远程调试参数 | 执行 `bash upgrade.sh`（会删旧容器并用新镜像重建） |
| 切换实例画面仍是上一个 | 目标实例容器卡死，iframe 加载失败保留旧画面 | 新版面板会显示"画面无法连接"并可重试；执行 `bash upgrade.sh` 重建 |
| noVNC 打开是目录列表 | Debian novnc 包无根目录 index.html | 面板已自动用 `/vnc_lite.html` 或 `/vnc.html`，勿手动访问根路径 |
| VNC 一直显示 Connecting、看不到画面 | manager 的 WS 代理把浏览器发来的 RFB 二进制帧按文本帧转发，websockify 回 `Unsupported: Text frames are not supported` 并断连 | 新版代理对二进制帧显式用 BINARY opcode 转发，直连/同源代理均已实测完成 RFB 握手；确认已重建 manager |
| 抓屏一直没画面 | 标签页列表为空，或 WS 代理连不上容器 | 确认实例 CDP 已就绪；查看 manager 日志里 `/ws/cast` 是否有报错；无头内核与完整内核都已实测支持 screencast 帧 |
| 抓屏画面卡住/定格（静态页、验证码页） | 页面长时间无视觉变化，CDP 不推 screencast 帧 | Cast 模式按所选间隔（默认 0.5 秒）主动保底抓帧，静态页不会冻结；想更快可把间隔切到「快」0.2 秒；若 screencast 推帧异常，可切到 **Snap** 模式（纯定时截图，不依赖变化推帧） |
| 刷新按钮点了没反应 | 当前没有选中标签，或刷新命令下发失败 | 新版刷新会真实下发 `Page.reload` 并同步地址栏，失败会明确提示；先确认已选中一个标签页 |
| 关闭最后一个标签后画面断连（`Connection refused`） | 有头 Chrome 关掉唯一窗口会退出进程，容器随之重启，来不及补建标签 | 已改为**关闭前先补建保底空白页**，保证始终有窗口、Chrome 不退出；若仍因其他原因短暂断连，面板会自动重试最多 3 次并重连画面 |
| 无头实例点 VNC 没反应 | 无头模式没有 Xvfb/VNC 服务 | 属正常，切到「抓屏」模式 |
| 老无头仍报 HeadlessChrome | manager 身份守护未挂上 | 确认 manager 已重建（守护代码在新版 manager 内），查看运行日志 `[身份伪装]` 条目；或改用新无头 headless-new |
| 创建实例报镜像不存在 | 本地未拉取对应内核镜像，或 compose 仍用旧的 `cdp-browser-browser` | 执行 `bash upgrade.sh`（会 pull full/headless 两份镜像）；自定义部署需设 `BROWSER_IMAGE_FULL` / `BROWSER_IMAGE_HEADLESS` |
| 持久目录删除报「实例正在使用」 | 有实例挂载了该 Volume | 先停止使用该目录的实例，再删除 |
| 想彻底清理持久目录数据 | Volume 不会随容器自动删除 | 面板左侧「持久用户目录」点删除，或 `docker volume rm cdp-profile-*` |

查看某个 browser 容器的日志（chrome 报错现在输出到 stdout，直接用 logs 即可）：

```bash
docker ps --filter label=cdp.browser.manager --format '{{.Names}}'
docker logs --tail 100 <容器名>
# 面板里也可以直接看：右上角「运行日志」开关，或 /api/logs/global 接口
```

## 免责声明

在下载、部署、修改或使用本项目之前，请完整阅读以下条款：

### 1. 用途限制

本项目是通用的浏览器实例管理工具，仅用于**自有系统或已获得明确授权的目标**上的自动化测试、开发调试、演示与内网运维。它不提供、也不应被用于规避任何网站的安全措施。

### 2. 禁止用途

使用者**不得**将本项目或其衍生成果用于：

- 未经授权访问、爬取、抓取他人网站、系统或数据；
- 绕过验证码、反爬机制、风控策略、访问控制或付费限制；
- 批量注册、刷量、刷单、薅羊毛、制造虚假流量、群发垃圾信息；
- 钓鱼、仿冒、窃取账号凭证或个人隐私数据；
- 对他人系统发起扫描、攻击、渗透或其他破坏性操作；
- 任何违反目标网站服务条款、`robots.txt` 或适用法律法规的行为。

### 3. 责任归属

- 使用者是其所创建的实例、运行的脚本及产生的流量的**唯一责任人**，须自行确认已获得目标方授权，并遵守所在地法律法规与目标网站条款。
- 本项目按「原样」提供，不附带任何明示或暗示的担保（包括但不限于适销性、特定用途适用性、不侵权）。作者与维护者对因使用、滥用或无法使用本项目造成的任何直接、间接或连带损失及法律后果**不承担任何责任**。
- 因使用本项目引发的投诉、调查或诉讼，由使用者自行处理并承担全部费用与后果。

### 4. 安全提示

默认配置使用 `--no-sandbox`、挂载宿主机 Docker socket，且 CDP/VNC 端口无认证。**仅限可信内网使用，禁止直接暴露到公网**。如需公网部署，须自行配置认证、防火墙与访问控制，由此产生的入侵、数据泄露等风险由部署者自负。

### 5. 其他

作者与维护者有权拒绝为可疑或违规用途提供任何支持，并可随时修改或停止本项目的分发。若你不接受以上条款，请勿下载、部署或使用本项目；继续使用即视为已充分理解并同意本免责声明。

## 注意

- 容器使用 `--no-sandbox`，适合内网受控环境，不要裸奔公网。
- manager 挂载了 `docker.sock`，等同于拥有宿主机 Docker 控制权，只能在可信内网使用。
- 三份镜像均由 GitHub Actions 自动构建推送至 ghcr.io；CI 里是三个独立步骤、独立构建缓存，改无头镜像不会重打完整镜像；同一分支的构建会排队，避免 `latest` 被旧 run 覆盖，也可在 Actions 页面手动触发重建。
- 每次构建成功后 CI 会自动做两类回归：manager 镜像启动并探测核心接口；full/headless 浏览器镜像分别启动容器，用几种独立的 Python 客户端连接 CDP，验证导航、DOM 读取、JS 执行和截图。测试脚本只在 CI 运行，不进入运行时镜像，也不会在本地开发机安装测试依赖。
- 创建实例时会扫描宿主机上所有 Docker 容器占用的端口，避让非本管理器创建的 9222/5900/6080 段端口。
- 旧镜像名 `cdp-browser-browser:latest` 已弃用；升级时执行 `bash upgrade.sh` 会用新镜像重建实例容器。
- GHCR 仓库名必须全小写，因此镜像路径使用 `forallcodesouls`（对应 GitHub 账号 `ForAllCodeSouls`）。
- 若 GHCR 镜像为私有，先执行 `docker login ghcr.io -u ForAllCodeSouls` 再启动。
