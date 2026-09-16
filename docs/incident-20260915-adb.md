# 事故记录：2026-09-15 全天 MuMu「启动超时」

## 现象

- 08:52 上午上班签到失败：`MuMu 启动超时；关闭得力失败: adb.exe: device offline`
- 用户手动演练两次（10:33、10:39）同样失败，报错一致
- 排查期间再次演练复现（10:50、11:05），共 5 次，全部同一症状

## 根因

**本机 adb server（端口 5037）僵死**。该 server 自 2026-09-07 17:30 持续运行，对 MuMu 实例 2 的 ADB 地址 `127.0.0.1:16448` 缓存了永久 offline 的设备条目。worker 判定「MuMu 启动完成」依赖 `adb shell getprop sys.boot_completed`，adb 永远 offline → 等满 240 秒 → 「MuMu 启动超时」；清理阶段 force-stop 同样 offline →「关闭得力失败」。

**Android 虚拟机本身一直是健康的**：`MuMuManager info` 各项正常，手动 `control launch` 后 10 秒内 `is_android_started=True`。`is_android_started` 只代表 VM 引擎已启动，不代表 Android 引导完成；两者之间的桥就是 adb。

## 排查路径（含弯路）

1. ~~MuMu 夜间自动更新~~：nx_main 全部 exe 时间戳仍是 9-9 安装日，排除。
2. ~~内存耗尽~~：vmmemWSL 占 8.3GB、系统仅剩 6.8GB（MuMu 实例需 6GB）——看似解释成立；经用户同意终止 `kynp-sdk-lab` 发行版后可用内存升至 9.5GB，**但演练仍失败，证伪**。内存紧张是真实存在的次级风险，但不是本次根因。
3. **破案关键**：对照 worker 判定链（manager RPC ✓ → adb connect → getprop），发现 manager 侧一切正常而 adb 侧永远 offline；随后发现 5037 上挂着一个运行 8 天的 adb server。
4. 修复：`adb kill-server`（新 server 由项目自带 adb 自动拉起）→ 手动 launch → `adb devices` 显示 `127.0.0.1:16448 device` 在线、`boot_completed=1`。
5. 终验：11:12 演练 56 秒完整通过（定位/开 App/两次刷新/在范围内/未提交/干净退出）。

## 修复动作

```powershell
.\android-sdk\platform-tools\adb.exe kill-server   # 杀掉僵死 server（必要时 Stop-Process 兜底）
# 之后任何 adb 命令会自动拉起新 server，无需手动 start-server
```

## 遗留与建议 → 已实施（A + B，2026-09-15 用户批准）

- **方案 A（已实施）**：`mumu_worker.py` 新增 `adb_connect()`——connect 前先 `adb disconnect` 清除缓存条目；`adb devices` 显示 offline 时自动 `kill-server` 一次并重连。仅作用于宿主侧 adb 守护进程，不触碰模拟器、WSL、Docker。单测见 `tests/test_regressions.py::AdbSelfHealTests`（3 项）。
- **方案 B（已实施）**：新计划任务 `AutoCheckin-AdBRecycle`，每天 03:00 跑 `scripts/adb_recycle.ps1`（`adb kill-server` + 一行审计日志 `logs/adb-recycle.log`），把 adb server 年龄压到 24 小时内。注册入口 `scripts/install_adb_recycle.ps1`。
- 内存次级风险：vmmemWSL 只涨不还（9-7 开机至今 6.4–8.3GB 波动）。用户已选择不做 .wslconfig 限制；若某天再次出现启动变慢，优先检查可用内存是否 >8GB。
- 本次上午上班时段按设计**不补签**，记录保持 failed。

## 相关事实备查

- 实例 2：`MuMuPlayer-15.0-2`，性能档 middle（4 核 / 6GB），ADB 宿主端口 16448
- 历史正常启动耗时约 7.5 秒（vm_config `recent_launch_durations_ms: 7490,7478,7448`）
- 通知器全程正常：昨天 6 连成功 + 今晨 3 次失败均已推送飞书
