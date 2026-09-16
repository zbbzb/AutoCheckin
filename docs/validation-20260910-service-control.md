# 后台服务可视化开关 · 验证记录 2026-09-10

目标：把「后台服务」的启动/停止/重启做成可视化操作，并保留崩溃自愈能力。

## 前置发现

- 服务原本**已经**能开机自启：计划任务 `AutoCheckin-MuMu-Controller` 有登录触发器 + 每分钟看门狗触发器，`ExecutionTimeLimit=PT0S`、`MultipleInstances=IgnoreNew`。缺的不是自启，而是**停止入口**与**页面外的状态可见性**。
- `签到控制台.lnk` → `start_panel.vbs` 原本只做「启动并打开页面」，服务已在运行时启动的第二个实例会因端口占用静默退出，因此它是安全的，但无法表达「停止」。

## 最终设计

| 组件 | 作用 |
|---|---|
| `scripts/mumu_task_guard.py` | 计划任务唯一入口。先判断本次开机内是否有手动停止标记，有则什么都不做；否则确保服务在跑 |
| `scripts/mumu_tray.ps1` + `tray_text.json` | 常驻托盘：状态颜色、右键开始/停止/重启、打开控制台、状态变化气泡提示 |
| `scripts/start_tray.vbs` | 「启动」文件夹入口，隐藏启动托盘并做单实例判断 |
| `scripts/start_panel.vbs` | 打开控制台：清除停止标记 → 拉起服务 → 等 pid 出现 → 打开页面；起不来则提示 |
| `scripts/mumu_restart.ps1` | 重启助手：等旧进程退出并释放端口，清标记，再启动任务 |
| `data/service.stop` | 手动停止标记，**带本次开机标识**，重启电脑后自动失效 |
| `data/service.pid` `data/tray.pid` | 进程记录；进程消失即自动清理，避免 PID 复用误判 |

## 实测结果

服务 API `/api/service` 的 `stop` / `restart`，托盘，计划任务入口，快捷方式，全部实测通过：

| 场景 | 结果 |
|---|---|
| 控制台点「停止服务」 | 端口 **1.3 秒**释放，进程退出，停止标记写入 |
| 每分钟看门狗随后触发 | **没有**复活服务（尊重手动停止） |
| 双击「签到控制台」快捷方式 | 服务恢复（新 pid），停止标记被清除 |
| 控制台点「重启服务」 | **3.6 秒**完成，pid 38628 → 50688 |
| 连续两次启动托盘 | pid 28824 → 28824，保持单实例 |
| 重复启动服务（直接执行 `mumu_service.py`） | 退出码 0，监听数保持 1 |

## 过程中修掉的实际缺陷

1. **`ThreadingHTTPServer.daemon_threads = False` 拖住停机**：第一次实测点「停止服务」后端口 27 秒才释放，浏览器 keep-alive 残留的请求线程让进程无法退出。改为在后台线程里做优雅停机，并由 `initiate_shutdown()` 在完成后显式 `os._exit(0)`，另加 20 秒强制退出兜底。
2. **`tasklist | find <pid>` 的子串匹配**：进程已死时该管道仍返回 0，把已死的 PID 判成存活（实测撞上 PID 复用），导致「服务未运行却显示运行中」并不再启动。改为解析 `tasklist` 行取 PID 精确比对，VBS 侧改用 WMI `WHERE ProcessId=<pid>`。
3. **`start_panel.vbs` 漏掉清除停止标记**：手动停止后双击快捷方式只会弹出「服务没有启动」对话框，永远恢复不了。已补上（重新打开控制台＝明确的启动意图）。
4. **PowerShell 文件编码**：`.ps1` 含 UTF-8 中文且无 BOM 时，Windows PowerShell 5.1 按 ANSI 代码页读取，中文变成乱码并破坏字符串引号，脚本整体无法解析（计划任务重新注册因此失败且只报语法错）。现在所有 `.ps1` 保持纯 ASCII，中文文案放 `tray_text.json` 按 UTF-8 读取，并留下 `scripts/check_ps_syntax.ps1` 做自动检查。
5. **`mumu_common.boot_id()` 缺 `time` 导入**：停止标记相关函数会直接抛 `NameError`（测试发现）。

## 自动化检查

- `python -m unittest discover -s tests`：**38 项通过**（原 21 项 + 本次新增 17 项，覆盖停止标记语义、看门狗行为、服务端点、单实例绑定、OpenCV 跨版本叉号识别）。
- `powershell.exe -File scripts\check_ps_syntax.ps1`：4 个脚本全部 OK 且无非 ASCII 字节。

## 第二轮：托盘菜单「没反应 / 打开空白窗口」

首次交付后托盘右键两个菜单项都无效。日志（`logs/tray.log`，本轮新增）暴露了根因，共两个：

1. **向 pythonw.exe 要输出永远是空的。** 这是 **GUI 子系统解释器**，不向 stdout 或管道写任何东西，实测重定向到文件得到 0 字节（`python.exe` 与 `cmd` 重定向都正常）。托盘的存活判断原本是
   `Get-ServicePid { & pythonw -c "...print(running(pid))" }`，于是**每次返回空** → 托盘永远认为服务没在跑 → `Request-Service` 直接返回"服务未响应"，**重启和停止都静默什么都不做**；打开控制台也用不到正确状态。已改为 PowerShell 原生 CIM/`Get-Process` 精确匹配，并加端口占用作为兜底。
2. **内联 Python 的编码地雷。** Python 的 stdout 默认用系统 ANSI 代码页（本机 GBK），一旦输出涉及含中文的项目路径就抛
   `OSError: [Errno 22] Invalid argument`；把 Windows 路径塞进 `-c` 源码还会让反斜杠被当成续行符（日志里可见 `SyntaxError: unexpected character after line continuation character`）。
   现在所有 Python 侧查询都走 `scripts/tray_helper.py`（只输出 ASCII，例如 `running` / `1234` / 一行 JSON），调用方传参数而不是传代码。

顺带修掉的两点：

- **`DoEvents` + `Start-Sleep 1500` 的消息泵**改为真正的 `Application.Run` 消息循环、状态轮询改由 WinForms `Timer` 驱动，UI 线程不再被阻塞（已用带日志的最小复现验证过事件确实能送达）。
- **托盘单实例**：之前会残留第二个托盘进程（图标看得见但已失效）。现在托盘自己会检查同类进程并退出，`start_tray.vbs` 也据此判断。

修复后实测：

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 托盘「重启后台服务」 | 静默无效，54.8 秒后失败 | **1.5 秒**完成，pid 50688 → 45520 |
| 托盘「打开签到控制台」 | 无效 | 新开浏览器进程并指向控制台 |
| 双击「签到控制台」快捷方式 | 弹「服务没有启动」 | **0 秒**返回，正确识别服务已在运行 |
| 托盘存活判断 | 永远为"未运行" | 正确（`logs/tray.log` 无错误） |

## 第三轮：托盘图标消失

次日用户报告托盘图标不见了。排查结果：托盘进程已死、`data/tray.pid` 仍停在昨天的 16616、`tray.log` 从 12:21 之后再无任何记录。

根因是一条**设计缺陷**加一个**致命异常**：

1. **托盘没有任何监管。** 它只由「启动」文件夹拉起，而每分钟的计划任务看门狗只管服务（且 `MultipleInstances=IgnoreNew`）。托盘一旦因会话事件或崩溃消失，就没有任何东西会把它拉回来。
2. **托盘在写 pid 文件之前就崩了**，异常是
   `MethodException: 找到"Add"的多个不确定重载，参数计数为:"1"`。
   `ToolStripItemCollection.Add` 同时有 `Add(string)` 与 `Add(ToolStripItem)` 两个重载，而 `ConvertFrom-Json` 出来的值不是纯 `[string]`，PowerShell 无法选定重载；`-ErrorActionPreference='Stop'` 又把它升级成终止性错误，于是托盘启动即退出，**且不留痕迹**。
3. 排查中发现**日志本身也是坏的**：`Say` 用 `Add-Content` 写日志，在隐藏的长驻宿主里会失败，所以崩溃没有留下任何线索。已改为纯 .NET `FileStream` 追加。

修复：

- 所有本地化字符串显式 `[string]` 转换后再传给 `Add(...)` / `NotifyIcon.Text`。
- `Say` 改用 `[System.IO.File]::Open` + UTF8 字节写入，不再依赖 PowerShell 提供程序。
- **看门狗同时监管托盘**：`mumu_task_guard.py` 每次触发都先检查托盘 pid，不在就拉起。托盘先于服务启动，因为它是"服务被停掉之后唯一的恢复入口"。
- 顶层 `trap` 把任何异常写入 `tray.log`，托盘再也不会无声消失。
- 处理 explorer 重启：用 C# 覆写 `WndProc` 监听 `TaskbarCreated`，收到就重新注册图标（`Add-Type -Name` 会生成同名包装类导致编译冲突，改用 `-TypeDefinition`）。

实测：

| 场景 | 结果 |
|---|---|
| 杀掉托盘后等待计划任务 | **14:16:18 触发 → 14:16:21 托盘已回来**（约 3 秒） |
| 重启后的 `tray.log` | `tray started pid=... / tray ready`，无错误 |
| 用户实测菜单 | 日志记录 `menu: open console`，功能正常 |
| 语法与测试 | 4 个 PS 脚本 OK，38 项测试通过 |

顺带记一个排查陷阱：用 `CommandLine -like '*mumu_tray*'` 找托盘进程会**匹配到排查命令自己**（命令行里含这个字符串），产生假阳性。诊断脚本里改成运行时拼出关键字并排除 `$PID`。

## 第四轮：托盘菜单和提示条都没有文字

用户报告托盘右键菜单的空，鼠标悬停的提示条也没有文字。这一轮 4 个问题叠在一起，值得逐条记下来，因为每一个都属于"换一个宿主就失效"的类型。

1. **文案资源本身一直是对的。** 用 Python 读 `tray_text.json` 得到 `打开签到控制台`、码点 `25171,24320,31614,...`，托盘自己的日志（把码点写成纯 ASCII 后）也证实它取到了正确值。此前我一度以为文案是乱码，实际是**我读日志时用了错的编码**——`logs/tray.log` 是 UTF-8 无 BOM，用 `Get-Content` 默认按 ANSI 读就会花屏。结论：诊断这种问题时不要打印中文，要打印**码点**。
2. **空菜单的直接原因**：14:16:21 被看门狗拉起的那次，正好发生在我编辑 `mumu_tray.ps1` 的过程中，脚本处于半改状态。现在的读法改为显式 `ConvertFrom-Json -InputObject`（不再依赖管道语义），并在加载后做一次完整性校验。
3. **启动闸门的 VBScript 陷阱（新引入又新修掉）**：为防止"空菜单托盘"再次出现，启动器在拉起托盘前先跑 `tray_helper.py text-check`。但 `TextOk` 用 `Trim()` 去空白——**VBScript 的 `Trim()` 不去 CR/LF**，于是 `"ok" & vbCrLf <> "ok"`，闸门永远失败，托盘反而彻底起不来。已抽成 `HelperLine()`，显式 `Replace(vbCr, "")` + `Replace(vbLf, "")`。
4. **我又在 `.ps1` 里写了中文**：给兜底文案写中文时引入 36 个非 ASCII 字节，被自己的 `check_ps_syntax.ps1` 当场抓住（PS 5.1 按 ANSI 读会乱码并破坏引号）。兜底文案改回纯英文，中文只从 UTF-8 JSON 读。

本轮加固：

- `Get-TrayText()` 显式 `-InputObject` 解析 + 缺项校验 + ASCII 兜底；加载后只有异常才写日志，健康启动保持安静。
- `tray_helper.py text-check`：校验所有必需文案非空且 `menuRestart != menuStop`。
- `start_tray.vbs` 启动前先过闸门，**宁可不启动也不启动一个没字的托盘**。
- `check_ps_syntax.ps1` 现在也检查 `.vbs`：ASCII 约束 + "读取外部结果必须经 `HelperLine`"。
- 新增 3 项测试覆盖文案资源校验（共 41 项）。

实测：托盘 `pid=42444` 干净启动，日志只有 `tray started` / `tray ready`，无任何告警。

## 尚未实测

- 真实签到提交后的完整收尾（成功页叉号关闭）仍需首次真实签到验证；本次改动未触碰该路径，但 `find_close_cross` 的 OpenCV 5 兼容修复（见上一轮）也只能由真实签到最终确认。
- 重启电脑后的自动恢复：停止标记带本次开机标识，逻辑上重启即失效；实际重启验证待下次重启时确认。
