# AutoCheckin · MuMu 签到控制台

在本机后台运行的得力e+签到工具。[打开控制台](http://127.0.0.1:18765/)。双击项目目录的 **签到控制台.lnk** 可以启动服务并打开页面；也可以双击 `scripts/start_panel.vbs`。

## 默认安排

按**工作日历**执行（不再是简单的周一到周五）：默认接入国务院节假日安排（含**调休上班的周末**），数据来自开源的 [holiday-cn](https://github.com/NateScarlet/holiday-cn) 日历，服务每天自动下载缺失年份并缓存到 `data/workdays-年份.json`；断网或无缓存时自动回退为「周一到周五」规则。所有时间按北京时间（UTC+8）。

公司安排与国家日历不一致时，在 `data/workdays-override.json` 手工指定（优先级最高）：

```json
{ "work": ["2026-10-10"], "off": ["2026-09-30"] }
```

配置项 `workday_sync`（默认开）可关闭数据集同步，仅用周几规则；手工覆盖在任何模式下都生效。

| 时段 | 标准时间 | 随机签到窗口 |
|---|---|---|
| 上午上班 | 09:00 | 08:45–09:00 |
| 上午下班 | 11:30 | 11:30–11:45 |
| 下午上班 | 13:30 | 13:15–13:30 |
| 下午下班 | 17:30 | 17:30–17:45 |
| 晚上上班 | 19:00 | 18:45–19:00 |
| 晚上下班 | 20:30 | 20:30–20:45 |

每天各时段独立随机到秒，并保存至本地数据库。网页显示**计划点击签到的时间**。默认提前 180 秒启动准备；临近随机时间执行两次刷新，每次手势完成后等待完整 10 秒，然后核对并点击。识别或开机耗时可能让点击稍晚于计划时间，但超过窗口就停止提交。随机点避开窗口最后 5 秒。

电脑关机、休眠或没有登录时无法签到。恢复后若仍在窗口内会尽快执行，超过窗口会标记跳过，不补签。工作日按上文的工作日历判定。

## 控制台

- **自动签到总开关**立即生效。关闭后，已启动但未提交的定时流程会取消并清理；已经点击则先核实结果，再关闭应用和模拟器。
- 六个时段的时间、开关、随机范围及静默设置，点击**保存设置**生效。修改正在运行的配置会取消尚未提交的流程，新设置用于后续时段。
- 展开**模拟器与定位设置**可修改实例编号/名称、组织、经纬度、准备秒数和管理程序路径。实例编号与名称都必须匹配，防止操作错误实例。
- **运行一次演练**会启动模拟器、定位、打开得力、刷新两次、识别范围，然后关闭应用和模拟器。演练不会点击签到。定时签到前十分钟或已有运行时不接受演练。
- **停止本次运行**会取消当前流程并清理。已经提交的动作无法撤回。
- **执行记录**显示成功、失败、待核实、跳过和演练结果；可点击查看日志。截图及本地 OCR 结果保存在对应日志目录。

关闭页面后服务继续工作。总开关关闭时服务也继续运行，只停止自动签到。

## 后台服务开关

页面上的**自动签到**开关只管签到逻辑；**后台服务**本身（调度器与网页）另有开关，在控制台右上方「后台服务」卡片里，也可以右键任务栏托盘图标操作。

| 操作 | 位置 | 行为 |
|---|---|---|
| 停止服务 | 控制台卡片 / 托盘右键 | 取消未提交的流程（若正在签到，先核实结果再关模拟器），然后退出服务并**记下这次开机内的停止标记**，看门狗不会把它拉回来 |
| 重启服务 | 控制台卡片 / 托盘右键 | 先停再起，约 3–10 秒；已保存的设置与今日计划不受影响 |
| 启动服务 | 双击**签到控制台**快捷方式 / 托盘右键 | 清除停止标记并启动服务，然后打开页面 |
| 停用/启用飞书通知 | 托盘右键「飞书通知」勾选框 | 切换打卡结果推送的总开关（`config/notify.json` 的 `enabled`）。停用只停止推送，打卡照常执行；约一个轮询周期（8 秒）内生效，见 [通知器文档](docs/notifier.md) |
| 双击托盘图标 | 托盘 | 打开控制台 |

托盘图标颜色即状态：绿色＝运行中，红色＝已停止。鼠标悬停还能看到自动签到开关是开是闭。

停止标记只对**当前这次开机**有效，所以重启电脑后服务会自动恢复运行，不会因为一次手动停止而永久失效。反过来，如果哪天服务没有自动起来，双击「签到控制台」就能拉起来并跳到页面。

安装与计划任务：

- 计划任务 `AutoCheckin-MuMu-Controller` 的启动入口是 `scripts/mumu_task_guard.py`，它负责「登录时启动」和「每分钟看门狗」两种触发，并尊重停止标记。
- 托盘随登录启动，入口是「启动」文件夹里的 `AutoCheckin Tray.lnk`（调用 `scripts/start_tray.vbs`）。它常驻用户会话，是停止服务后仍然可用的操作入口。
- 托盘与重启逻辑是 PowerShell 脚本，**必须保持纯 ASCII**：Windows PowerShell 5.1 按 ANSI 代码页读取 `.ps1`，UTF-8 中文会变成乱码并破坏语法。中文文案放在 `scripts/tray_text.json`，运行时按 UTF-8 读取。改动后用 `powershell.exe -File scripts\check_ps_syntax.ps1` 自查。

## 每次流程

1. 核对并启动指定 MuMu 实例，连接其本机 ADB 地址。
2. 使用 MuMu 官方命令设置虚拟定位，启用 Android 定位。
3. 打开已登录的得力e+，进入指定组织工作台。
4. 从顶部组织标题 `(420,140)` 下拉至 `(420,740)`，900 ms，等待 10 秒；重复一次。
5. 本地截图识别目标组织、今日考勤、**已在打卡范围内**及相应的**上班打卡 / 下班打卡**按钮，再点击一次。
6. 等待并识别**打卡成功 / 签到成功 / 签退成功**，检测并点击左上角叉号，再确认成功页消失。
7. 强制关闭得力e+，通过 MuMu 官方命令关闭目标实例；失败路径也尝试清理。

提交意图在点击前写入数据库。点击后没有明确成功提示时记为**结果待核实**，不自动重试。即使修改配置或重启服务，已尝试的同一天同一时段也不会重跑。成功后如果叉号或模拟器关闭失败，会在记录中明确说明。

当前工作台识别基于 1080×1920 竖屏、得力e+ 3.5.5 和已登录的目标组织。更换界面、分辨率或登录失效时，识别失败会停止签到并记录。

## 静默运行

后台服务及子命令均不显示控制台。启用静默后，从启动阶段起持续调用 MuMu 的 `hide_window` 隐藏**目标实例**。MuMu 当前官方 CLI 没有公开无窗口启动选项，因此无法承诺首次启动绝不闪窗。该方式不依赖桌面鼠标键盘，关闭浏览器不影响流程。

## 安装与维护

当前环境已安装。Windows 计划任务 `AutoCheckin-MuMu-Controller` 在用户登录时启动服务，并每分钟检查启动；入口是 `mumu_task_guard.py`，单实例限制和端口绑定避免重复服务。它不主动唤醒休眠电脑。旧的 `AutoCheckin` 每天一次任务已备份并停用。安装脚本同时把托盘放进「启动」文件夹，因此登录后会常驻一个托盘图标。

重新安装：

```powershell
# 使用 Windows CPython 3.12 创建本地运行环境（python.org 安装后用 py 启动器；
# 不要用 MSYS2/Store 的 python——它们生成 bin/ 布局的 venv，脚本的 .venv\Scripts 路径会失效）
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# worker 与服务依赖 adb：把 Android platform-tools 压缩包解压到 android-sdk\platform-tools\（内含 adb.exe）
# 签到配置：复制模板并填写自己的信息（组织名/坐标/实例/时段，含隐私不入库）
Copy-Item config\mumu.example.json config\mumu.json
# 通知器凭据放 .env（勿提交），见 docs/notifier.md
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install_mumu_task.ps1
```

验证：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\check_ps_syntax.ps1
```

## Agent 部署要点（面向代替人类执行部署的 AI 代理）

按「先决条件 → 配置 → 安装 → 验证 → 坑位 → 红线」执行，所有命令可直接运行核对。

**先决条件（Windows 10/11，已登录的交互用户会话；逐项核对）**

1. Python 必须是 **Windows CPython 3.12**：`py -3.12 -V` 应出版本号。若 PATH 里的 `python` 属于 MSYS2（路径含 `msys64`）或 Microsoft Store（路径含 `WindowsApps`），**禁止使用**——它们创建的 venv 是 `bin/` 布局，而本项目全部脚本写死 `.venv\Scripts\python.exe`，会全部找不到解释器（真实踩坑）。
2. **MuMu 模拟器**已安装，且目标考勤 App 已由用户本人登录（代理无法代办登录）；实例分辨率需为 1080×1920 竖屏（页面识别坐标硬编码）。
3. **adb**：把 Android platform-tools 解压到 `<repo>\android-sdk\platform-tools\adb.exe`——路径是代码硬编码的 ROOT 相对路径，位置不可变。

**配置（三个私有文件，均已被 .gitignore 排除，永不入库）**

1. `config\mumu.json` ← 复制 `config\mumu.example.json`，填写：`manager_path`（MuMuManager.exe 绝对路径）、`vm_index`/`vm_name`（必须与目标 MuMu 实例**完全一致**，防误操作别的实例）、`package`、`organization`（目标组织工作台标题）、`location` 经纬度、六个时段。组织名与坐标属于用户隐私。
2. `.env`（可选，结果通知器）：`NOTIFY_FEISHU_APP_ID` / `NOTIFY_FEISHU_APP_SECRET` / `NOTIFY_FEISHU_PHONE`，详见 [docs/notifier.md](docs/notifier.md)。
3. 配置自检（非法会抛 ValueError，带中文原因）：

```powershell
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'scripts'); import mumu_common; mumu_common.read_config(); print('config ok')"
```

**安装**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install_mumu_task.ps1    # 服务+托盘（登录自启+看门狗）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install_adb_recycle.ps1  # 每日 03:00 防 adb 僵死（建议）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\notify_install.ps1       # 通知器（可选，先配 .env）
```

**验证（按序执行，全部通过才算部署成功）**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests    # 预期 79 tests OK；测试自包含，无需任何私有配置
.\.venv\Scripts\python.exe scripts\fetch_workdays.py        # 拉取节假日/调休日历（今年+明年；明年未发布会提示并自动回退）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\check_ps_syntax.ps1       # 预期全部 OK
Invoke-RestMethod http://127.0.0.1:18765/api/health                                        # 预期 service=running
```

端到端验证用**演练**（会真实启动模拟器、打开考勤 App、完成两次刷新并确认在打卡范围内，但**不会点击提交**）：控制台点「运行一次演练」，或带 token POST `/api/preview`。演练通过即全链路可用。

**已知坑位（症状 → 根因 → 处置）**

| 症状 | 根因 | 处置 |
|---|---|---|
| `.venv\Scripts\python.exe` 不存在 | 用 MSYS2/Store 的 python 建了 venv | 删除 `.venv`，用 `py -3.12 -m venv .venv` 重建 |
| 签到/演练报「MuMu 启动超时」且 `adb devices` 对模拟器显示 offline | 宿主 adb server 长期运行后僵死（缓存 offline 条目） | `adb kill-server` 即愈；worker 已内置自愈，另见 [docs/incident-20260915-adb.md](docs/incident-20260915-adb.md) |
| 托盘菜单文字为空/乱码 | `scripts\*.ps1` 被写入非 ASCII 字符（Windows PowerShell 5.1 按 ANSI 读 .ps1） | **铁律：.ps1 只准纯 ASCII**，中文文案只能放 `tray_text.json`（UTF-8）；改完必跑 check_ps_syntax.ps1 |
| 端口 18765 已占用 / 出现第二个服务实例 | 旧实例未退或重复安装 | 单实例由端口绑定失败保障；用 `/api/health` 找 pid，勿删 `data\service.pid` |
| 「已在打卡范围内」识别不到 | 分辨率不是 1080×1920、App 未进目标组织工作台、或登录失效 | 人工在 MuMu 里核对后重跑演练 |

**红线（代理必须遵守）**

- **未经用户明确指令不得触发真实签到**；一切验证只用「演练」（preview 不提交）。
- 不得提交/上传 `.env`、`config\mumu.json`、`config\notify.json`、`logs\`、`data\`（截图与执行记录含用户隐私）。
- 不得停止/重启用户机器上的 WSL、Docker 及其他无关服务；`adb kill-server` 是允许的（仅影响 adb 守护进程）。
- 服务仅监听 `127.0.0.1` 是安全边界，不得改为对外监听。
- 修改任何 `scripts\*.ps1` 后必须运行 `check_ps_syntax.ps1` 并全绿才算完成。
- 用户要求停止服务时走控制台「后台服务」卡片或托盘右键（会写停止标记），不要直接杀进程（看门狗会复活）。

| 路径 | 用途 |
|---|---|
| `config/mumu.json` | 面板设置 |
| `data/checkin.sqlite3` | 随机计划、状态及防重复记录，不要在运行中删除 |
| `data/service.pid` `data/tray.pid` | 当前服务/托盘进程号，进程没了就会自动清理 |
| `data/service.stop` | 手动停止标记，只对本次开机有效 |
| `data/legacy-AutoCheckin.xml` | 旧计划任务备份 |
| `scripts/mumu_common.py` | 配置校验、计划生成与记录 |
| `scripts/mumu_service.py` | 本机网页与后台调度 |
| `scripts/mumu_worker.py` | 完整 MuMu 流程 |
| `scripts/mumu_task_guard.py` | 计划任务入口，尊重停止标记 |
| `scripts/tray_helper.py` | 托盘/启动器查询服务状态的小工具（只输出 ASCII） |
| `scripts/mumu_tray.ps1` `scripts/tray_text.json` | 托盘图标与其中文文案 |
| `scripts/notify_daemon.py` `config/notify.json` `.env` | 打卡结果飞书通知器（凭据在 `.env`；托盘「飞书通知」勾选框开关；见 [通知器文档](docs/notifier.md)） |
| `scripts/adb_recycle.ps1` `scripts/install_adb_recycle.ps1` | 计划任务 `AutoCheckin-AdBRecycle`：每天 03:00 回收 adb server，防止长驻僵死（见 [9-15 事故记录](docs/incident-20260915-adb.md)） |
| `scripts/fetch_workdays.py` | 手动拉取节假日/调休日历（服务每天也会自动拉缺失年份） |
| `data/workdays-年份.json` `data/workdays-override.json` | holiday-cn 日历缓存；公司特有安排的手工覆盖（work/off 日期表） |
| `android-sdk/platform-tools/` | worker/服务使用的 adb（新机器需自行解压 platform-tools 到此路径） |
| `logs/tray.log` | 托盘动作与错误记录，排查托盘问题时先看这里 |
| `scripts/mumu_restart.ps1` | 重启服务的分离助手 |
| `scripts/check_ps_syntax.ps1` | PowerShell 语法/编码自查 |
| `web/` | 控制台页面 |
| `logs/mumu-日期-时间-编号/` | 每次流程的截图、识别结果与日志 |

服务仅监听 `127.0.0.1`，页面写操作验证本机来源与页面凭据。截图识别在本机运行，不上传外部 OCR 服务。

2026-09-10 验证：修复了成功页叉号识别在 OpenCV 5 下必然抛异常、依赖缺少版本上限、两个服务实例同时监听同一端口的问题；随后加入后台服务可视化开关（托盘＋控制台停止/重启），停止、看门狗不复活、快捷方式恢复、重启、托盘单实例均已实测通过。见 [服务开关验证记录](docs/validation-20260910-service-control.md)。

2026-09-09 验证：已完成复用运行实例和从完全关闭状态启动的两次完整演练，均确认刷新后在范围内，并成功关闭得力与 MuMu；两次都未提交签到。**真实提交、成功提示识别和左上角叉号仍待首次实际签到验证**。见 [验证记录](docs/validation-20260909.md)。

原 AVD 工具和配置保留供诊断，不再承担当前自动签到。旧说明见 [AVD 历史说明](docs/legacy-avd.md)。

