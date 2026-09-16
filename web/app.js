"use strict";
const $ = id => document.getElementById(id);
let current = null, dirty = false, initialized = false, loading = false;
const labels = {pending:"等待执行",starting:"正在准备",running:"执行中",success:"签到成功",preview:"演练通过",failed:"执行失败",uncertain:"结果待核实",missed:"已跳过",cancelled:"已停止"};
const phases = {emulator:"启动 MuMu",location:"设置定位",app:"打开得力e+",waiting:"等待随机时间",refresh1:"第一次刷新",refresh2:"第二次刷新",submitting:"提交签到",success:"确认成功",done:"已完成"};
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function clock(stamp, seconds=true) { return stamp ? new Date(stamp*1000).toLocaleTimeString("en-GB", {timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",...(seconds?{second:"2-digit"}:{})}) : "—"; }
function day(stamp) { return new Date(stamp*1000).toLocaleDateString("zh-CN", {timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit"}); }
function toast(message, error=false) { $("toast").textContent=message; $("toast").className=error?"error":""; $("toast").hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(()=>$("toast").hidden=true,6000); }
async function post(path, payload={}) { if(!current) throw Error("请等待本机服务连接"); const response=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Checkin-Token":current.token},body:JSON.stringify(payload)}); const data=await response.json(); if(!response.ok) throw Error(data.error || "操作失败"); return data; }
function windowText(slot, minutes) { const [h,m]=slot.time.split(":").map(Number); const base=h*60+m; const fmt=n=>`${String(Math.floor((n+1440)%1440/60)).padStart(2,"0")}:${String((n+1440)%60).padStart(2,"0")}`; return slot.kind==="in"?`${fmt(base-minutes)} – ${fmt(base)}`:`${fmt(base)} – ${fmt(base+minutes)}`; }
function populate(cfg) {
  $("slotRows").innerHTML=cfg.slots.map((s,i)=>`<tr data-index="${i}"><td><div class="slot-name"><span class="slot-icon ${s.kind=== "out"?"out":""}">${s.kind==="in"?"↗":"↙"}</span>${escape(s.label)}</div></td><td><input type="time" data-time="${i}" aria-label="${escape(s.label)}标准时间" value="${escape(s.time)}" required></td><td class="window" data-window="${i}"></td><td><span class="planned" data-planned="${i}">—</span><span class="row-state" data-state="${i}"></span></td><td><label class="switch"><input data-enabled="${i}" type="checkbox" aria-label="启用${escape(s.label)}" ${s.enabled?"checked":""}><span></span></label></td></tr>`).join("");
  const fields={randomMinutes:"random_minutes",prepareSeconds:"prepare_seconds",vmIndex:"vm_index",vmName:"vm_name",organization:"organization",package:"package",managerPath:"manager_path"};
  for(const [id,key] of Object.entries(fields)) $(id).value=cfg[key];
  $("longitude").value=cfg.location.longitude; $("latitude").value=cfg.location.latitude; $("silent").checked=cfg.silent;
  initialized=true; updateWindows();
}
function updateWindows() { if(!current) return; for(const [i,s] of current.config.slots.entries()) { const time=document.querySelector(`[data-time="${i}"]`).value; document.querySelector(`[data-window="${i}"]`).textContent=time?windowText({...s,time},Number($("randomMinutes").value)):"—"; document.querySelector(`tr[data-index="${i}"]`).classList.toggle("disabled",!document.querySelector(`[data-enabled="${i}"]`).checked); } }
function render(data) {
  current=data; const cfg=data.config;
  if(!initialized) populate(cfg);
  $("connection").textContent="本机服务运行中"; $("connectionDot").classList.remove("off");
  $("dateLabel").textContent=new Date(data.now).toLocaleDateString("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"long",day:"numeric",weekday:"long"})+" · 今天也按自己的节奏来";
  $("master").checked=cfg.enabled; $("master").disabled=false; $("masterDot").classList.toggle("off",!cfg.enabled);
  $("masterStatus").textContent=cfg.enabled?"自动签到已开启":"自动签到已暂停";
  $("heroTitle").textContent=!cfg.enabled?"暂歇一下，安排由你决定。":data.weekend?"周末好好休息，周一再继续。":"让每天的签到，有条不紊。";
  $("heroSub").textContent=cfg.enabled?"按你的时间自动准备，完成后关闭应用与模拟器。":"开启总开关后，将在有效时间窗口内继续执行。";
  $("randomBadge").textContent=`${cfg.random_minutes} 分钟随机窗口`; $("silentBadge").textContent=cfg.silent?"后台静默":"显示模拟器";
  const active=data.active[0], next=active?.mode==="live"?active:data.next;
  $("nextTime").textContent=next?clock(next.planned):"—"; $("nextLabel").textContent=next?`${day(next.planned)} · ${next.label} · 提前 ${cfg.prepare_seconds/60} 分钟准备`:cfg.enabled?"今天的安排已结束":"自动签到已暂停";
  $("done").textContent=data.today.filter(j=>j.status==="success").length; $("total").textContent=` / ${data.weekend?0:cfg.slots.filter(s=>s.enabled).length} 次`;
  const trouble=data.today.filter(j=>["failed","uncertain"].includes(j.status)).length; $("todayNote").textContent=data.weekend?"周末不执行签到":trouble?`${trouble} 次需要查看执行记录`:"每个时段独立随机，完成后留存记录";
  $("runStatus").textContent=active?(phases[active.phase] || labels[active.status]):"空闲待命";
  $("runDetail").textContent=active?active.message:"关闭网页后，后台服务仍会运行";
  $("stop").hidden=!active; $("preview").disabled=!!active;
  for(const [i,s] of cfg.slots.entries()) { const job=data.today.find(j=>j.slot_id===s.id); const status=document.querySelector(`[data-state="${i}"]`); document.querySelector(`[data-planned="${i}"]`).textContent=job?clock(job.planned):"—"; status.textContent=!s.enabled?"此时段已关闭":job?labels[job.status]:data.weekend?"周末休息":"未安排"; status.className="row-state "+(job?.status||""); }
  renderService(data.service);
  $("historyRows").innerHTML=data.history.length?data.history.map(j=>`<div class="history-item"><div class="history-time">${day(j.finished||j.planned)} ${clock(j.finished||j.planned,false)}</div><div class="history-copy"><strong>${escape(j.label)}<span class="badge ${escape(j.status)}">${escape(labels[j.status]||j.status)}</span></strong><p>${escape(j.message)}</p></div>${j.log_url?`<a href="${escape(j.log_url)}" target="_blank" rel="noopener">查看日志 ↗</a>`:""}</div>`).join(""):"<div class=\"empty\">还没有执行记录，下一次运行后会自动出现在这里。</div>";
}
function uptimeText(seconds) { if(!(seconds>=0)) return "刚刚启动"; const minutes=Math.floor(seconds/60); if(minutes<1) return "刚刚启动"; const hours=Math.floor(minutes/60); if(hours<1) return `已运行 ${minutes} 分钟`; if(hours<24) return `已运行 ${hours} 小时 ${minutes%60} 分钟`; return `已运行 ${Math.floor(hours/24)} 天 ${hours%24} 小时`; }
function renderService(service) {
  const state=$("serviceState"), note=$("serviceDetail");
  if(!service) {
    state.textContent="连接中断"; state.className="stat-value compact service-state off";
    note.textContent="请双击「签到控制台」快捷方式，或右键托盘图标 → 打开签到控制台";
    return;
  }
  state.textContent="运行中"; state.className="stat-value compact service-state";
  note.textContent=`进程 ${service.pid} · ${uptimeText(service.uptime)} · 关闭后重启电脑会自动恢复`;
}
function markDisconnected() {
  $("connection").textContent="后台服务未运行"; $("connectionDot").classList.add("off");
  $("master").disabled=true; $("preview").disabled=true;
  $("runStatus").textContent="服务已停止"; $("runDetail").textContent="请通过托盘图标启动后台服务";
  renderService(null);
}
async function refresh() { if(loading) return; loading=true; try { const response=await fetch("/api/state"); if(!response.ok) throw Error("连接失败"); render(await response.json()); } catch(error) { markDisconnected(); } finally { loading=false; } }
$("settings").addEventListener("input",()=>{dirty=true; $("saveNote").textContent="有未保存的修改，请点击保存设置。"; $("saveNote").classList.add("dirty"); updateWindows();});
$("settings").addEventListener("submit",async event=>{event.preventDefault(); if(!current) return; $("save").disabled=true; try { const cfg=structuredClone(current.config); cfg.random_minutes=Number($("randomMinutes").value); cfg.prepare_seconds=Number($("prepareSeconds").value); cfg.silent=$("silent").checked; cfg.vm_index=Number($("vmIndex").value); cfg.vm_name=$("vmName").value.trim(); cfg.organization=$("organization").value.trim(); cfg.package=$("package").value.trim(); cfg.manager_path=$("managerPath").value.trim(); cfg.location={longitude:Number($("longitude").value),latitude:Number($("latitude").value)}; cfg.slots.forEach((s,i)=>{s.time=document.querySelector(`[data-time="${i}"]`).value; s.enabled=document.querySelector(`[data-enabled="${i}"]`).checked;}); await post("/api/config",cfg); dirty=false; $("saveNote").textContent="设置已保存，后续时段按新配置执行。"; $("saveNote").classList.remove("dirty"); toast("设置已保存"); await refresh(); } catch(error) { toast(error.message,true); } finally { $("save").disabled=false; }});
$("master").addEventListener("change",async()=>{const enabled=$("master").checked; $("master").disabled=true; try { await post("/api/toggle",{enabled}); toast(enabled?"自动签到已开启":"自动签到已暂停，未提交的流程将停止"); } catch(error) { toast(error.message,true); } await refresh();});
$("preview").addEventListener("click",async()=>{ $("preview").disabled=true; try { await post("/api/preview"); toast("演练已开始：完成两次刷新后关闭应用，不提交签到"); } catch(error) { toast(error.message,true); } await refresh(); });
$("stop").addEventListener("click",async()=>{try {await post("/api/stop"); toast("正在停止；已提交的签到会先核实结果，再关闭模拟器");}catch(error){toast(error.message,true);} await refresh();});
async function serviceAction(action, confirmText, busyText) {
  if(confirmText && !window.confirm(confirmText)) return;
  $("serviceStop").disabled=true; $("serviceRestart").disabled=true;
  try { await post("/api/service",{action}); toast(busyText); } catch(error) { toast(error.message,true); }
  // A stop makes this page unreachable, so keep showing the stopped state.
  setTimeout(()=>{ $("serviceStop").disabled=false; $("serviceRestart").disabled=false; },8000);
  await refresh();
}
$("serviceStop").addEventListener("click",()=>serviceAction("stop",
  "确定停止后台服务吗？\n\n· 控制台将断开，定时签到不再执行\n· 若有流程正在运行，会先取消并关闭模拟器\n· 重启电脑后服务会自动恢复运行",
  "正在停止后台服务…"));
$("serviceRestart").addEventListener("click",()=>serviceAction("restart",
  "确定重启后台服务吗？\n\n· 已保存的设置与今日计划不受影响\n· 若有流程正在运行，会先取消并关闭模拟器\n· 约 10 秒后自动恢复",
  "正在重启后台服务…"));
window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue="";}});
refresh(); setInterval(refresh,3000);
