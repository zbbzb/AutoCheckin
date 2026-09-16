# 打卡结果群机器人通知器（notify_daemon）

打卡结束后，把**结果摘要 + 现场截图**推送给飞书机器人的独立组件。默认模式是**机器人直接给你发私聊**（不用建群）；也可切换为发到群里。主通道为**飞书自建应用**（截图原生显示在聊天里），企微 webhook 作为备选通道保留。

- **完全增量**：只读现有调度数据库（`data/checkin.sqlite3`，WAL 模式并发安全），不修改签到软件的任何文件、不往现有库加表。
- **进程隔离**：独立进程 + 自己的 pid 锁（`data/notifier.pid`）、配置（`config/notify.json`）、去重状态（`data/notify_state.json`）、日志（`logs/notifier.log`）。通知器崩溃或停止都不影响签到本身。
- 与「后台服务开关」无关：即使手动停止了签到服务，通知器仍会继续报告（例如「服务停止，流程取消」这类事件）。

## 消息形态

每个进入终态的时段先发一条文字摘要，随后紧跟一条**原生图片消息**（成功页/异常现场截图）：

```text
✅ 签到成功
时段：晚上下班（2026-09-18）
计划：17:42:04 · 完成：17:44:31
签到成功，成功页已关闭
[截图]
```

| 终态 | 文字 | 截图 |
|---|---|---|
| success / uncertain | ✅ / ⚠️ | 有 run_dir 则发（用 OCR 旁车定位「打卡成功」帧） |
| failed | ❌ | 有 run_dir 则发（优先 `NN-error.png`） |
| missed / cancelled | ⏭️ / ⏹️ | 无截图，纯文字 |
| preview | 🔁 | 默认不通知（配置 `statuses` 加 `preview` 可开） |

可靠性规则：文字发送成功即算事件完成（截图失败只记警告，原图仍留在运行目录）；发送失败自动在下个轮询周期重试，最多 10 次后放弃并记日志；超过 24 小时的陈旧事件不发（防止停机一周后补发轰炸）；首次启动把已有历史记录全部静默基线化，不补发旧消息。飞书 token 缓存 2 小时并在到期前 5 分钟自动刷新，遇到过期错误码自动重取并重试一次。

## 飞书接入（一次性，约 5 分钟，默认私聊模式·无需建群）

### A. 创建自建应用（PC 浏览器）

1. 打开 [open.feishu.cn](https://open.feishu.cn) → 飞书扫码登录 → **开发者后台** → 创建企业自建应用（名字随意，如「打卡通知」）。
   - 个人版账号如提示需要团队，按引导免费创建一个自己的团队即可。
2. 应用详情 → **添加应用能力** → 开通**机器人**。
3. **权限管理** → 搜索并开通四个权限：
   - 「获取与发送单聊、群组消息」（im:message）
   - 「获取与上传图片或文件资源」（im:resource）
   - 「获取群组信息」（im:chat，仅群模式用到，一并开着省事）
   - 「通过手机号或邮箱获取用户 ID」（私聊模式反查你的 open_id 用）
4. **版本管理与发布** → 创建版本 → 发布（自建应用一般即时通过）。
5. **凭证与基础信息** → 复制 **App ID** 和 **App Secret**。
6. **把可用范围收紧到只有你自己**（不要用默认全员）：
   - 发布版本时：创建版本页面的「可用范围」选**指定用户** → 添加你自己（按姓名/手机号搜索）→ 发布；
   - 已发布想改：管理后台 [admin.feishu.cn](https://admin.feishu.cn) → 工作台 → 应用管理 → 找到该应用 → 修改可用范围为指定用户（仅自己），或回到开发者后台更新版本重新发布。
   - 这样这个应用与机器人只对你可见可用：别人工作台看不到它，机器人无法向任何其他人发消息，「通过手机号或邮箱获取用户 ID」也只能查到你。通知器本身只向配置的手机号对应的 open_id 发送，此设置是额外的保险。

### B. 配置并验证（PC，手机不用任何操作）

凭据放在项目根目录的 **`.env`**（已列入 `.gitignore`），不写进代码、也不进 `config/notify.json`：

```ini
NOTIFY_FEISHU_APP_ID=cli_xxxx
NOTIFY_FEISHU_APP_SECRET=你的AppSecret
NOTIFY_FEISHU_PHONE=138xxxxxxxx      # 你登录飞书的手机号
```

优先级：**真实环境变量 > `.env` > `config/notify.json`**（`--set-feishu`/`--set-phone` 等 CLI 写的是 json，仅在 env 缺失时生效；`NOTIFY_CHANNEL`/`NOTIFY_WECOM_WEBHOOK` 同理可覆盖）。

验证：

```powershell
.\.venv\Scripts\python.exe scripts\notify_daemon.py --test                      # 私聊收到文字
.\.venv\Scripts\python.exe scripts\notify_daemon.py --test --image logs\mumu-20260911-203458-18\08-result.png   # 文字+真实截图
```

- 机器人会通过手机号反查你的 open_id（缓存下来，只查一次），然后**直接给你发私聊**——消息出现在飞书消息列表里，和真人聊天一样弹通知。
- 收到测试消息和截图即通。然后装成随登录自启的后台任务（新增计划任务，不碰现有任务）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\notify_install.ps1
```

### 想改成发群里？（可选）

建群 → 群设置 → 群机器人 → 添加应用机器人 → 显式指定群 ID：

```powershell
.\.venv\Scripts\python.exe scripts\notify_daemon.py --set-chat oc_xxxxxxxxxxxxxxxx
```

群 ID（`oc_` 开头）可在飞书群设置的部分版本里看到，或从开放平台 API 调试台查询；机器人在恰好一个群时，`--set-chat` 之外也可以直接把配置里 `feishu.receive_mode` 改为 `chat` 并留空 `chat_id`，通知器会自动发现并缓存。**简化建议：保持默认私聊模式即可，无需任何群。**

## 配置（config/notify.json）

| 字段 | 默认 | 说明 |
|---|---|---|
| `channel` | `feishu` | `feishu`（自建应用）或 `wecom`（群机器人 webhook）；`--set-feishu`/`--set-webhook` 会自动切换 |
| `feishu.app_id` / `app_secret` | 空 | 自建应用凭证；为空时守护进程空转等待，不报错 |
| `feishu.receive_mode` | `p2p` | `p2p`=机器人直接私聊你；`chat`=发群里 |
| `feishu.phone` / `email` | 空 | 私聊接收人（你登录飞书的手机号或邮箱）；`--set-phone`/`--set-email` 设置并自动切换 p2p，换号自动失效缓存的 open_id |
| `feishu.chat_id` | 空 | 群模式目标群；留空则自动发现（要求机器人只在一个群） |
| `wecom.webhook` | 空 | 企微备选通道 |
| `poll_seconds` | 8 | 轮询间隔（3–60 秒），即打卡结束后最多延迟这么多秒收到通知 |
| `statuses` | success/failed/uncertain/missed/cancelled | 要通知的终态；加 `preview` 可通知演练 |
| `send_image` | true | 是否跟随截图 |
| `jpeg_quality` / `image_max_bytes` | 80 / 2000000 | 截图压缩；实测成功页 102KB→55KB |
| `max_age_hours` | 24 | 超过该时限的未发事件直接跳过 |

## 运维

**总开关在托盘**：右键托盘图标 → **「飞书通知」**勾选框（勾=启用）。点击后立即写 `config/notify.json` 的 `enabled` 并弹气泡确认，守护进程每周期重读配置，**约 8 秒内生效**；停用只停止推送，打卡照常执行。命令行等价：`python scripts/notify_daemon.py --set-enabled off|on`。

```powershell
# 查看日志（发送记录、失败原因、群发现、开关切换、放弃记录都在这里）
Get-Content logs\notifier.log -Tail 30 -Encoding UTF8

# 停止：结束进程即可（计划任务 5 分钟内会拉回；想彻底停用见下）
Get-Content data\notifier.pid   # 然后 Stop-Process -Id <pid>

# 彻底停用/卸载（日常临时停用请用托盘开关，别卸载）
Unregister-ScheduledTask -TaskName AutoCheckin-Notifier -Confirm:$false

# 重新安装 / 立即拉起
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\notify_install.ps1
```

单实例：计划任务 `IgnoreNew` + 进程自身 pid 锁双重保证，重复触发会安静退出。看门狗触发器每 5 分钟兜底拉起，登录时自启。

## 常见错误码（飞书）

| 现象/错误 | 原因 | 处理 |
|---|---|---|
| `code=99991663/99991661` | token 过期 | 通知器自动重取重试；若持续失败检查 app_secret |
| `code=230002` 类权限错误 | 未开通上传图片权限 | 回到权限管理开通 im:resource 并重新发布版本 |
| 提示「没有匹配到飞书用户」 | 手机号/邮箱不是登录飞书的那个，或该用户不在应用可用范围 | 核对号码；把你自己加进可用范围（建议范围只含你一人） |
| 提示「获取用户 ID 失败」 | 缺「通过手机号或邮箱获取用户 ID」权限 | 权限管理开通后重新发布版本 |
| 提示机器人不在群里 | 群模式下应用机器人没进群 | 群设置 → 群机器人 → 添加应用机器人，或改回私聊模式 |
| 提示多群需要 chat_id | 群模式下机器人在多个群 | `--set-chat oc_xxxx` 显式指定 |

## 隐私与限制

- 凭据只存在本机 `.env`（已 gitignore）；App Secret 泄漏后可在开发者后台**重置**，然后改 `.env` 里对应行即可，其他都不用动。
- 建议把飞书应用**可用范围收紧为仅自己**（见接入步骤 A.6）：别人看不到该应用，机器人无法联系任何其他人，手机号反查也只认你。
- 截图经飞书开放平台上传（企业自建应用，图片在飞书体系内，不经公开存储）；文字摘要只含时段/状态/时间/结果描述，不含坐标。
- 飞书接口限频宽松（发消息级）；本场景每次事件最多 3 个 API 调用（token 缓存后 2 个），远低于限额。
- 单元测试：`python -m unittest tests.test_notifier`（22 项，覆盖选帧、消息形态、去重、重试/放弃、.env 加载、飞书 token/上传/群发现/私聊反查、企微载荷）。
