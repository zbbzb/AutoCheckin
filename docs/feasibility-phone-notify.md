# 可行性分析：打卡结果推送手机 + 通知 App

> 2026-09-18 探索结论。本文只做分析，未改动现有软件的任何文件。
> ~~已确认的目标形态：Android 手机 + 微信渠道推送通知 + 自研 App 查看历史。~~
> **2026-09-18 范围变更（用户决定）：取消自研 APK；最终通道选定为飞书自建应用（用户手机已有飞书且常用；飞书 webhook 机器人无法直传图片，须自建应用上传图片换 image_key）。已实施：`scripts/notify_daemon.py`（飞书主通道 + 企微 webhook 备选，双通道 sink）+ `tests/test_notifier.py`（16 项测试）+ `scripts/notify_install.ps1`，使用说明见 [docs/notifier.md](notifier.md)，待用户提供飞书 App ID/Secret 后完成接入。**

## 一、结论

**总体可行，且可以做到「完全不动现有软件」。** 最终形态（2026-09-18 用户决定取消 APK 后）：

1. **通知层（微信）**：PC 上新增一个独立的「通知器」进程，轮询现有 SQLite 数据库检测打卡结果，通过 WxPusher 推到个人微信；需要看图的异常场景可叠加企业微信机器人原生发图（见第五节）。
2. **PC 侧全部为新增文件 + 独立计划任务**，不修改 `mumu_service.py` / `mumu_worker.py` / `mumu_common.py` 等任何现有代码。

## 二、现有软件盘点（挂钩点在哪）

| 事实 | 位置 | 对通知方案的意义 |
|---|---|---|
| 每次打卡的最终结果都以 `update_job(status, phase='done', message, finished)` 写入 `data/checkin.sqlite3` 的 `jobs` 表 | `mumu_worker.py:405`，孤儿进程由 `mumu_service.py` tick 兜底 | **单一可信数据源**。轮询此表即可捕获全部终态，无需改代码 |
| 数据库以 WAL 模式打开（`PRAGMA journal_mode=WAL`，`timeout=20`） | `mumu_common.py:208` | 第三方进程并发只读安全，不会阻塞服务 |
| 终态枚举：`success / preview / failed / uncertain / missed / cancelled` | `mumu_common.py:25` | 通知分级直接复用：success=绿色、uncertain/failed=需人工处理、missed/cancelled=提示 |
| 每次运行还在 `logs/mumu-日期-时间-编号/result.json` 落一份结果快照 | `mumu_worker.py:401` | 备用数据源，但 DB 更全（missed 无运行目录） |
| 服务只监听 `127.0.0.1:18765`，Host 校验拒绝非本机 | `mumu_service.py:275-279` | 手机不能直接访问现有服务（这是有意的安全设计，不应破坏） |
| 已有真实成功记录（如 2026-09-11 三次 success） | `logs/mumu-*/result.json` | 打卡结果事件每天 6 次、工作日发生，通知量极小，任何通道都够用 |
| 本机外网连通正常（ntfy.sh 1.7s、github 462ms 实测可达） | 2026-09-18 探测 | 公共推送服务可用；SDK 组件可在线补装 |

## 三、通道选择：微信渠道（用户已选定）

### 推荐：WxPusher（个人微信收通知）

- 免费个人服务，消息经**个人微信服务号**触达，国内可靠性最好。
- 接入形态二选一：
  - **极简推送 SPT**：扫码拿一个 `SPT_xxx` 令牌，之后一条 HTTP GET 即可发通知，零后台配置 —— 最快启动路径。
  - **标准推送**：扫码登录建应用拿 `appToken`，配合关注后取得的 `UID`，POST `https://wxpusher.zjiecode.com/api/send/message`，支持 Markdown/HTML、`summary` 通知摘要。一天 6 条远低于其 2 QPS 限流。
- 权威文档：wxpusher.zjiecode.com/docs（2026-07 仍在活跃更新）。

### 备选：企业微信群机器人 webhook

- 也是单条 HTTP POST，但通知落在**企业微信 App** 的群里，需要手机常装企业微信；个人习惯上通常不如 WxPusher 顺手。作为代码层的备用实现（同一通知器多写一个 sink 即可）。

### 已否决的通道（记录理由）

- FCM / 自研 App 直推：国内无厂商通道时，App 被国产 ROM 杀后台后收不到推送，恰恰在最需要通知时失效。
- 自建 VPS 中转：用户不倾向维护服务器。
- Tailscale 直连：用户不倾向手机常驻 VPN（可后续作为 App 拉全量历史的可选增强，不作为通知主通道）。

## 四、方案架构

```text
┌────────────── 现有软件（不动） ──────────────┐
│ mumu_service.py 调度 → mumu_worker.py 打卡  │
│        │ 写入                                │
│        ▼                                    │
│  data/checkin.sqlite3  (jobs 表, WAL)       │
└────────────────┬──────────────────────────── ┘
                 │ 只读轮询 (每 5–10 秒)
                 ▼
┌────────── notify_daemon.py（新增，独立进程）─┐
│ 检测进入终态的 job，去重（自记状态文件）      │
│  ├─→ WxPusher SPT/appToken  ⇒ 微信通知（主） │
│  └─→ [可选] 企业微信机器人 image 消息         │
│      （uncertain/failed 时发原图，见第五节）  │
└──────────────────────────────────────────────┘
                 │
                 ▼
   手机微信收通知（文字摘要 + 按状态附截图）
   （原 ntfy→App 数据摆渡方案已随 APK 取消而移除）
```

### PC 侧新增物（全部是新文件）

| 文件 | 作用 |
|---|---|
| `scripts/notify_daemon.py` | 通知器：轮询 jobs 表终态、去重、推送。配置放 `config/notify.json`（含 SPT/appToken、ntfy 话题、开关） |
| `data/notify_state.json` | 已推送 job 的去重水位（**不往现有库加表**，彻底不碰现有 schema） |
| 独立计划任务或「启动」文件夹快捷方式 | 拉起通知器；不修改现有 `mumu_task_guard.py`（未来经同意后可并入 guard 统一看护） |

推送失败自动退避重试；通知器崩溃不影响签到本身（完全隔离）。PC 休眠/关机时签到本就不发生，无通知是自洽行为。

### 通知内容分级（建议）

| 终态 | 通知 | 说明 |
|---|---|---|
| `success` | ✅ 签到成功 · {时段} {时间} | 摘要即可 |
| `uncertain` | ⚠️ 已点击但未确认成功，需人工核实 | **最重要**，正文带 message |
| `failed` | ❌ 执行失败 · 原因摘要 | 需人工处理 |
| `missed` / `cancelled` | ℹ️ 跳过/取消 | 提示性质 |
| `preview` | 🔁 演练通过 | 可配置关闭 |

隐私注意：`failed` 的 message 可能含组织名等，推送正文建议只发「时段+状态+时间」，详细原因留在 PC 记录里；WxPusher/ntfy 内容经第三方服务器，**不要推截图、坐标**。

## 五、附图可行性：随消息发打卡结果截图（2026-09-18 补充，用户已决定取消 APK）

**结论：可行。有两条路线，取舍在于「图片落在哪个微信」和「图片是否经过公开服务器」。**

### 实测基础数据（本机）

- 成功页截图 `08-result.png`：**PNG 102 KB → JPEG(质量80) 55 KB**（OpenCV 已随现有软件安装，可直接压缩）。
- 每次运行目录里 PNG 均有同名 `.json` OCR 旁车文件，可精确定位「含打卡成功横幅」的那一帧；异常时另有 `NN-error.png`。
- 每日 6 张 × ~60 KB ≈ 0.4 MB/天，对下述任何限额都绰绰有余。

### 路线 A（推荐）：企业微信群机器人原生 image 消息

- webhook 官方支持 `msgtype=image`：`{base64, md5}`，base64 前 ≤2 MB，JPG/PNG（[企业微信开发者文档](https://developer.work.weixin.qq.com/document/path/91770)）。实测 55–102 KB 余量巨大。
- **图片二进制直传、无需任何图床、不落第三方公开存储**——隐私最优。
- 文字+图片分两次 webhook 调用（先 markdown 后 image）即可组合成完整通知。
- 代价：消息落在**企业微信 App**，需手机安装它；不是个人微信。

### 路线 B：WxPusher（个人微信）+ ntfy 充当 3 小时图床

- ntfy 附件：`PUT https://ntfy.sh/<topic>` + `Filename` 头上传，得到公开链接 `https://ntfy.sh/file/<id>/x.jpg`；[ntfy.sh 单文件限 2 MB、每 visitor 总量 20 MB、附件 3 小时过期](https://docs.ntfy.sh/publish)。
- WxPusher 标准推送 `contentType` 2=HTML / 3=Markdown（[API 文档](https://wxpusher.zjiecode.com/docs/api-reference.html)）嵌 `![](url)`；也可把链接放进 `url` 字段作消息落地页。
- **风险：微信聊天正文里外链图片的渲染没有官方保证**——WxPusher API 无图片直传字段，社区 [issue #61](https://github.com/wxpusher/wxpusher-client/issues/61)（"发送html内容，无法正常显示图片"）至今无回复。最坏形态：正文只有文字，图片需点开落地页查看。
- 隐私弱于路线 A：截图经 ntfy 公开服务器（3h）与 WxPusher 服务器。

### 推荐策略：按状态分级发图

- `success`：**纯文字即可**（成功无需看图），走个人微信 WxPusher；
- `uncertain` / `failed`：**才是真正需要看图的场景**（人工核实现场）。若装有企业微信→路线 A 发原图；否则路线 B 并接受落地页形态。
- 这样把图床依赖与隐私暴露压缩到最小集合。

## 六、原 App 方案存档（已取消）

### 数据通路（关键设计点）

微信通道是单向 PC→微信，App 无法从微信取数，因此 App 历史用**并行 ntfy 话题**解决：

- PC 通知器每次同时 POST 一份到 ntfy 长随机话题（如 `autocheckin-<32位随机>`，话题名即凭据）。
- App 打开时 `GET https://ntfy.sh/<topic>/json?poll=1&since=12h` 拉缓存消息，合并进 App 本地数据库。
- **公共 ntfy.sh 缓存默认仅 12 小时**（docs.ntfy.sh/config 明确，内存缓存、不落盘）——所以 App 每天打开一次即可无损累积完整历史；超过 12h 未打开期间的消息仍在微信与 PC 库里，不会真丢。
- 增强（可选）：在家时 App 直连 PC 的局域网只读小接口拉全量历史（新增独立小 HTTP 服务，绑定局域网地址 + 令牌）。

### 通知到达性（为什么主通道是微信而不是 App）

自研 App 若要在后台收推送，需常驻前台服务或接入厂商推送（Mi Push 等需开发者审核），在国产 ROM 上不可靠。**微信收主通知、App 只做历史查看**的分工让 App 可以「零后台」，绕开全部电池优化/杀后台问题。

### 构建环境盘点（本机实测）

| 项 | 状态 |
|---|---|
| Java 17 | ✅ 已装（17.0.12），满足 AGP 8.x 与 d8 |
| `android-sdk/cmdline-tools`（含 sdkmanager） | ✅ 已装 |
| `platform-tools`（adb） | ✅ 已装（worker 正在用它） |
| `platforms/`（android.jar）与 `build-tools/`（aapt2/d8/apksigner） | ❌ **未装，需补**：`sdkmanager "platforms;android-34" "build-tools;34.0.0"`，约 150–250MB，外网已通；若 dl.google.com 不稳可用腾讯镜像 |
| 模拟器测试 | ✅ 现成 AVD（`avds/AutoCheckin.avd`）+ system-images + emulator |
| 真机安装 | USB adb sideload，免商店免签名费 |

### 两条构建路线

1. **免 Gradle 手工构建**（保底可行）：单 Activity、纯 `android.app.*`、零第三方依赖，`aapt2 + javac + d8 + apksigner` 一条脚本出 APK。历史列表 + 设置页 + WebView 都不需要 AndroidX。工作量约 1–2 天。
2. **Gradle 标准工程**（体验更好）：需再下载 Gradle 发行版 + AGP 依赖（~130MB+），网络允许则优先。

App 功能边界（已确认）：收历史推送数据 → 本地留存 → 列表/今日视图；不做远程控制（那会引入写操作鉴权与安全面，明确排除）。

## 七、风险与缓解

| 风险 | 评估 | 缓解 |
|---|---|---|
| WxPusher 是第三方免费服务，限流/政策可能变 | 低（文档活跃，2 QPS 远超需求） | 通知器 sink 抽象化，可切企业微信 webhook / ntfy 直推 App |
| ntfy.sh 未来被墙 | 只影响 App 历史摆渡，**不影响微信主通知** | App 可用局域网直连替代；或换自建 |
| ntfy 话题即凭据，可被猜测/订阅 | 中 | 32 位随机话题名；不发敏感正文；必要时 `Cache: no`（牺牲 App 历史） |
| 推送内容经第三方 | 隐私考量 | 只发「时段+状态+时间」，不发坐标/截图/组织详情 |
| 通知器与现有服务并发访问 SQLite | 低（WAL + 20s timeout） | 只读、短事务；不写现有库 |
| 「不动现有软件」约束 | 已满足 | 全部新增文件 + 新计划任务；唯一将来的可选项是经同意后把通知器并入 guard 看护 |

## 八、实施阶段（供决策，未开工；2026-09-18 已按取消 APK 修订）

1. **阶段一（半天–1 天）**：WxPusher 扫码拿 SPT → `notify_daemon.py` + 配置 + 独立计划任务 + 单元测试 → 微信收到真实打卡通知。**做完这步，核心诉求已闭环。**
2. **阶段二（+0.5 天）**：附图能力——截图选帧（OCR 旁车定位成功帧 / error 帧）、OpenCV 压 JPEG、按第五节选定路线 A 或 B 接入。
3. **阶段三（可选）**：每日 20:45 后当日汇总推送。

## 附：数据引用

- 现有结果事件样本：`logs/mumu-20260911-174120-16/result.json`（status=success, message=签到成功，成功页已关闭）；截图体积实测 `08-result.png` 102KB / JPEG 55KB
- 企业微信机器人 image 消息（base64+md5，≤2MB）：https://developer.work.weixin.qq.com/document/path/91770
- ntfy 附件上传、公链与限额（2MB/文件、20MB/visitor、3h 过期）：https://docs.ntfy.sh/publish
- ntfy 缓存 12 小时：https://docs.ntfy.sh/config
- WxPusher 发送 API（contentType 2=HTML/3=Markdown）：https://wxpusher.zjiecode.com/docs/api-reference.html ；极简推送 SPT：https://wxpusher.zjiecode.com
- WxPusher 外链图片显示问题（无回复 issue）：https://github.com/wxpusher/wxpusher-client/issues/61
