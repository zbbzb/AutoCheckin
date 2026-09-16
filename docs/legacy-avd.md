# AutoCheckin — 安卓模拟器定时签到自动化

在 `D:\MYCODES\AutoCheckin` 下自包含一套 **Android 官方模拟器 (AVD) + ADB 自动化 + 虚拟定位 + Windows 计划任务** 方案，不依赖 Android Studio。

> 2026-09-09 排查结果：定位脚本已修复 console 响应错位，并新增 `--verify` 系统回读验证。当前 AVD 无法启动得力e+ 3.5.5；同一 APK 在独立 MuMu 安卓 15 测试实例能进入游客主界面。详见 [排查报告](logs/diagnostics-20260909/REPORT.md)。`config/checkin.json` 仍是示例包名，尚不是可用的得力业务流程。

> MuMu 工作台刷新已复测：必须从顶部组织标题文字处下拉；从“今日考勤”卡片起手只会滚动页面。当前 1080×1920 竖屏使用 `(420,140) → (420,740)`，900 ms。打开已登录的得力工作台后，可运行 `python scripts\mumu_refresh_preview.py` 执行两次下拉并保存过程截图；脚本不点击打卡。刷新图标出现并消失后再读取范围提示。见 [更正与复测记录](logs/mumu-refresh-correction-20260909/RESULT.md)。

## 目录结构

```
AutoCheckin\
├── android-sdk\        本地 Android SDK（adb、emulator、系统镜像）
├── avds\               模拟器数据（AVD 磁盘镜像）
├── apps\               ★ 把要自动化的 APK 放这里
├── scripts\            全部脚本
│   ├── env.bat                 环境变量（其他脚本都会引用）
│   ├── start_emulator.bat      启动模拟器（参数 head = 无窗口后台运行）
│   ├── install_app.bat         安装 apps\ 下所有 APK
│   ├── set_location.py         虚拟定位（手动指定经纬度）
│   ├── checkin.py              自动化引擎（读 config\checkin.json 执行动作）
│   ├── run_checkin.bat         定时任务入口（计划任务调这个）
│   └── register_task.bat       注册/删除 Windows 计划任务
├── config\checkin.json ★ 定时操作配置（定位坐标 + 每个App的动作序列）
├── logs\               每次运行的截图与日志（自动生成）
└── downloads\          安装包缓存，可删除
```

## 首次使用步骤

1. **启动模拟器**（首次开机较慢，之后有快照秒开）：
   ```
   scripts\start_emulator.bat
   ```
2. **放入 APK**：把目标 App 的 APK 复制到 `apps\` 目录，然后：
   ```
   scripts\install_app.bat
   ```
3. **确认包名**：安装脚本最后会列出第三方包名（如 `com.xxx.yyy`），填入 `config\checkin.json` 的 `package` 字段。
4. **编辑动作序列**：修改 `config\checkin.json`（动作类型见下表）。获取控件坐标/文本的方法：模拟器打开目标界面后运行
   ```
   android-sdk\platform-tools\adb.exe shell uiautomator dump
   android-sdk\platform-tools\adb.exe exec-out cat /sdcard/uidump.xml
   ```
   从输出 XML 里找 `text`、`resource-id`、`bounds`。
5. **手动试跑一次**：
   ```
   scripts\run_checkin.bat
   ```
   到 `logs\` 里查看截图确认每一步是否点对了。
6. **注册每天定时执行**（默认 08:30）：
   ```
   scripts\register_task.bat create          每天 08:30
   scripts\register_task.bat create 21:00    指定时间
   scripts\register_task.bat delete          删除任务
   ```

## 虚拟定位

- 定时流程中的定位：改 `config\checkin.json` 顶部的 `location`（经度在前、纬度在后）。
- 手动即时定位：
  ```
  python scripts\set_location.py 121.490317 31.239066
  ```
- 原理：通过 `adb -s emulator-5554 emu geo fix` 注入模拟 GPS（WGS84），由 ADB 处理 console 认证并核对实际命令响应；无需 root。命令被接受与应用实际收到定位是两个阶段。
- 验证：先打开请求 GPS 的应用，再运行 `python scripts\set_location.py 121.490317 31.239066 --verify`，脚本会等待系统 GPS 回读到目标附近且不超过 5 秒的新鲜定位。无人请求 GPS 时，缓存可能不刷新；网络定位也不一定随 GPS 改变，不能用缓存或单条 `OK` 判断应用定位成功。
- 自动化中可在 `launch` 之后放置 `{"type":"geo","lon":121.490317,"lat":31.239066,"verify":true}`。地图自测配置为 `config\diagnostic-maps.json`；它只启动地图、设置测试坐标并截图。
- 坐标不要跨地图/模拟器直接复用。当前 MuMu CLI 的输入坐标与 Android GPS 回读实测存在固定偏移，详见报告；迁移时需单独核对坐标系。
- 注意：部分 App 会结合网络定位（IP 归属地）做交叉校验。模拟器流量走代理时 IP 归属地是代理出口的位置，GPS 坐标与 IP 归属不一致时个别 App 可能触发风控，属正常现象。

## 模拟器上网（重要）

国内网络下 Google 服务被墙，模拟器默认 NAT 直连会导致地图/接口空白。本方案的处理：

- `scripts\env.bat` 里 `EMU_PROXY=http://127.0.0.1:7890` 指向宿主机代理（当前是 iKuuu 的 HTTP 端口）。**代理软件必须开着**，否则 Google 系 App 无法联网（国内网站不受影响）。换了代理软件记得同步改端口并重开模拟器。
- 模拟器 HTTP 代理环境下，Google 系 App 的 QUIC(UDP 443) 可能需要禁用才能回落 TCP。这项处理会调用 `adb root`，当前已改为显式可选：配置顶层加入 `"network":{"block_quic":true}` 才执行；默认不改 root 状态。规则重启会丢。手动补一次的命令：
  ```
  adb root
  adb shell iptables -A OUTPUT -p udp --dport 443 -j REJECT
  ```
- ping 不通 Google 属正常（ICMP 不走代理），以 App 实际表现为准。

## checkin.json 动作类型

| 动作 | 说明 |
|---|---|
| `{"type":"launch","wait":8}` | 启动当前 app 的主界面并等待 |
| `{"type":"wait","seconds":5}` | 等待秒数 |
| `{"type":"tap_text","text":"签到","index":0}` | 按文本/内容描述查找控件并点击（推荐，抗界面变动） |
| `{"type":"tap_id","id":"com.xxx:id/btn"}` | 按 resource-id 点击 |
| `{"type":"tap_xy","x":540,"y":1800}` | 按坐标点击（屏幕 1080×2400） |
| `{"type":"swipe","x1":540,"y1":1500,"x2":540,"y2":500}` | 滑动 |
| `{"type":"key","code":"back"}` | 按键：back/home/enter |
| `{"type":"text","content":"abc"}` | 输入文本 |
| `{"type":"geo","lon":116.39,"lat":39.91}` | 流程中途重新定位 |
| `{"type":"screenshot","name":"after"}` | 截图存证到 logs\ |

## 日常维护

- 手动起停：`start_emulator.bat` 启动；直接关闭窗口或 `adb emu kill` 关闭。
- 定时任务复用已运行的指定 AVD；未运行时在指定 console 端口无窗口冷启动（不加载旧快照），继承 `env.bat` 的代理设置，并等待开机完成。
- 启动后进程退出、ADB 失败或找不到必须点击的控件，流程会报错；`run_checkin.bat` 会将失败退出码返回给计划任务。得力启动诊断可运行 `python scripts\checkin.py config\diagnostic-deli.json`（只启动和截图，不提交业务操作）。
- 运行历史：`logs\run_history.log`；每次运行截图在 `logs\日期_时间\`。
- 磁盘紧张时可删除 `downloads\`（约 150MB）和 `logs\` 旧截图。

## 常见问题

- **App 检测模拟器/Root**：本镜像为 `google_apis` 版（非 Play 商店版），可 `adb root`。若仍被检测，可尝试改用真机 + Tasker 方案。
- **计划任务没跑**：任务默认只在当前用户登录时可用；运行历史见 `logs\run_history.log`。笔记本注意别在触发时刻睡眠（可在电源选项里设置唤醒定时器）。
- **点击坐标偏移**：`tap_xy` 依赖分辨率，AVD 为 Pixel 6 (1080×2400, 420dpi)；改用 `tap_text`/`tap_id` 更稳。
- **机器上插着真机**：Python 脚本只使用 `ANDROID_SERIAL` 指定的 AVD（默认 `emulator-5554`）。设备缺失时直接报错，不回退到真机或其他模拟器。AVD 定位脚本不能用于 MuMu 的 TCP ADB 序列号。
- **bat 脚本乱码**：`scripts\` 下的 bat 均为 GBK 编码 + CRLF 行尾（cmd 的要求），编辑时别另存为 UTF-8。
