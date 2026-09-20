const $ = (id) => document.getElementById(id);
const state = { settings: {}, groups: [], selected: new Set(), providers: [], jobId: "", status: {}, startupNotice: "" };
const providerDefaults = {
  openai: ["OpenAI", "https://api.openai.com/v1"],
  deepseek: ["DeepSeek", "https://api.deepseek.com"],
  qwen: ["通义千问", "https://dashscope.aliyuncs.com/compatible-mode/v1"],
  zhipu: ["智谱", "https://open.bigmodel.cn/api/paas/v4"],
  doubao: ["豆包", "https://ark.cn-beijing.volces.com/api/v3"],
  siliconflow: ["硅基流动", "https://api.siliconflow.cn/v1"],
  minimax: ["MiniMax", "https://api.minimax.cn/v1"],
  custom: ["自定义兼容接口", ""]
};

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const type = response.headers.get("content-type") || "";
  const data = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) throw new Error(data.message || `请求失败 (${response.status})`);
  return data;
}

function toast(message, kind = "") {
  const node = document.createElement("div"); node.className = `toast ${kind}`; node.textContent = message;
  $("toastContainer").appendChild(node); setTimeout(() => node.remove(), 4200);
}
function fmt(n) { return Number(n || 0).toLocaleString("zh-CN"); }
function inputTime(value) {
  const d = value ? new Date(value) : new Date(); const z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${z(d.getMonth()+1)}-${z(d.getDate())}T${z(d.getHours())}:${z(d.getMinutes())}`;
}
function escapeHtml(value) { const d = document.createElement("div"); d.textContent = value == null ? "" : String(value); return d.innerHTML; }

async function loadAccounts() {
  try {
    const data = await api("/api/accounts"); const select = $("accountSelect"); select.innerHTML = "";
    if (!data.accounts.length) select.add(new Option("未发现微信 4.x 数据目录", ""));
    data.accounts.forEach((a) => select.add(new Option(`${a.wxid} · ${new Date(a.last_activity*1000).toLocaleString()}`, a.account)));
    const saved = state.settings.selected_account; if (saved) select.value = saved;
  } catch (e) { toast(e.message, "error"); }
}

async function loadSettings() {
  state.settings = await api("/api/settings"); state.providers = state.settings.providers || [];
  state.selected = new Set((state.settings.selected_groups || []).map(g => g.id));
  $("scheduleEnabled").checked = !!state.settings.schedule_enabled;
  $("scheduleTime").value = state.settings.daily_summary_time || "23:30";
  $("proxyUrl").value = state.settings.proxy_url || ""; $("proxyUsername").value = state.settings.proxy_username || "";
  renderProviders();
}

async function refreshStatus() {
  try {
    state.status = await api("/api/status"); const connected = state.status.connected;
    $("wechatBadge").className = `status-pill ${connected ? "status-on" : "status-off"}`;
    const startupConnecting = !connected && state.status.startup_state === "connecting";
    const accountLabel = state.status.account?.nickname || state.status.account?.wxid || state.status.account?.account || state.settings.selected_account || "当前账号";
    $("wechatBadge").querySelector("span").textContent = connected ? `已连接 ${accountLabel}` : (startupConnecting ? "正在自动连接微信…" : (state.status.wechat_running ? "微信运行中 · 未连接" : "未检测到微信"));
    $("accountState").className = `badge ${connected ? "badge-green" : "badge-gray"}`; $("accountState").textContent = connected ? "已连接" : "未连接";
    $("monitorBadge").className = `badge ${state.status.is_monitoring ? "badge-green" : "badge-gray"}`; $("monitorBadge").textContent = state.status.is_monitoring ? "读取中" : "已停止";
    $("btnMonitor").textContent = state.status.is_monitoring ? "■ 停止实时读取" : "▶ 启动实时读取";
    if (state.status.startup_state === "error" && state.status.startup_message && state.startupNotice !== state.status.startup_message) {
      state.startupNotice = state.status.startup_message; toast(state.status.startup_message, "error");
    }
  } catch (_) {}
}

async function connectWechat() {
  const account = $("accountSelect").value; if (!account) return toast("请选择微信账号", "error");
  const btn = $("btnConnect"); btn.disabled = true; btn.textContent = "正在获取内存密钥并解密索引…";
  try {
    const data = await api("/api/wechat/connect", {method:"POST", body:JSON.stringify({account})});
    state.groups = data.groups || []; renderGroups(); await refreshStatus(); toast(`已连接 ${data.account.nickname || data.account.wxid || data.account.account || "当前账号"}`, "success");
  } catch (e) { toast(e.message, "error"); if (/权限|解密信息/.test(e.message) && confirm("读取权限不足，是否以管理员身份重新启动应用？")) { try { await api("/api/wechat/relaunch-admin", {method:"POST", body:"{}"}); } catch (adminError) { toast(adminError.message,"error"); } } } finally { btn.disabled = false; btn.textContent = "授权并连接本地数据库"; }
}

function renderGroups() {
  const q = $("groupSearch").value.trim().toLowerCase(); const box = $("groupPicker"); box.innerHTML = "";
  const filtered = state.groups.filter(g => g.name.toLowerCase().includes(q));
  if (!filtered.length) { box.innerHTML = '<div class="empty-state compact">没有匹配的群聊</div>'; }
  filtered.forEach(g => {
    const label = document.createElement("label"); label.className = "group-option";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = state.selected.has(g.id);
    cb.onchange = () => { cb.checked ? state.selected.add(g.id) : state.selected.delete(g.id); updateGroupCount(); };
    const name = document.createElement("span"); name.textContent = g.name; const count = document.createElement("small"); count.textContent = `${g.member_count || 0} 人`;
    label.append(cb, name, count); box.appendChild(label);
  }); updateGroupCount(); refreshGroupSelects();
}
function toggleGroupDropdown(force) {
  if (!state.groups.length) return;
  const panel = $("groupDropdown"), button = $("groupToggle");
  const open = force === undefined ? panel.classList.contains("hidden") : !!force;
  panel.classList.toggle("hidden", !open); button.setAttribute("aria-expanded", String(open));
  if (open) { $("groupSearch").focus(); renderGroups(); }
}
function updateGroupCount() {
  $("groupCount").textContent = `${state.selected.size} 个`;
  const button = $("groupToggle"), text = $("groupToggleText"); button.disabled = !state.groups.length;
  text.textContent = !state.groups.length ? "请先连接微信" : (state.selected.size ? `已选择 ${state.selected.size} 个群聊` : "点击选择群聊");
}
function selectedGroups() { return state.groups.filter(g => state.selected.has(g.id)).map(g => ({id:g.id,name:g.name})); }
function refreshGroupSelects() {
  const select = $("statsGroup"), old = select.value; select.innerHTML = "";
  selectedGroups().forEach(g => select.add(new Option(g.name, g.id))); if ([...select.options].some(o=>o.value===old)) select.value=old;
}
async function saveGroups() {
  const groups = selectedGroups(); if (!groups.length) return toast("请至少选择一个群聊", "error");
  await api("/api/groups/select", {method:"POST", body:JSON.stringify({groups})}); await loadSettings(); refreshGroupSelects(); toggleGroupDropdown(false); toast("群聊选择已保存", "success");
}

async function toggleMonitor() {
  try { await api(state.status.is_monitoring ? "/api/monitor/stop" : "/api/monitor/start", {method:"POST", body:"{}"}); await refreshStatus(); }
  catch(e){toast(e.message,"error")}
}
async function loadLive() {
  try { const data = await api("/api/messages/live"); const list=data.messages||[]; $("liveCount").textContent=fmt(list.length); $("liveBadge").textContent=`${list.length} 条`; const box=$("liveFeed"); box.innerHTML="";
    if(!list.length){box.innerHTML='<div class="empty-state compact">暂无新文字消息</div>';return}
    list.forEach(m=>{const el=document.createElement("div");el.className="live-item";el.innerHTML=`<header><strong>${escapeHtml(m.sender)} · ${escapeHtml(m.group)}</strong><time>${escapeHtml(m.time)}</time></header><p>${escapeHtml(m.content)}</p>`;box.appendChild(el)})
  } catch(_){}
}

async function refreshStats() {
  const groupId=$("statsGroup").value, day=$("statsDate").value; if(!groupId||!day)return;
  const btn=$("btnRefreshStats");btn.disabled=true;
  try{const s=await api(`/api/stats/today?group_id=${encodeURIComponent(groupId)}&date=${encodeURIComponent(day)}`);renderStats(s)}catch(e){toast(e.message,"error")}finally{btn.disabled=false}
}
function renderStats(s){$("kpiMessages").textContent=fmt(s.total_messages);$("kpiMembers").textContent=fmt(s.total_members);$("kpiWords").textContent=fmt(s.total_words);$("kpiPeak").textContent=s.peak_hour==null?"--":`${String(s.peak_hour).padStart(2,"0")}:00–${String(s.peak_hour+1).padStart(2,"0")}:00`;$("kpiPeakCount").textContent=s.peak_hour_count?`${s.peak_hour_count} 条`:"";
  const body=$("leaderboardBody");body.innerHTML="";(s.leaderboard||[]).forEach(m=>{const tr=document.createElement("tr");tr.innerHTML=`<td><span class="rank ${m.rank<=3?'top':''}">${m.rank}</span></td><td><strong>${escapeHtml(m.nickname)}</strong></td><td>${fmt(m.count)}</td><td><div class="ratio-wrap"><span class="ratio-line"><i style="width:${Math.min(m.ratio,100)}%"></i></span>${m.ratio}%</div></td><td>${fmt(m.words)}</td><td>${escapeHtml(m.first_time||'--')}</td>`;body.appendChild(tr)});if(!body.children.length)body.innerHTML='<tr><td colspan="6" class="empty-cell">该日期没有文字消息</td></tr>';
  const chart=$("hourlyChart");chart.innerHTML="";const values=s.hourly_distribution||Array(24).fill(0),max=Math.max(...values,1);values.forEach((v,h)=>{const w=document.createElement("div");w.className="hour-bar-wrap";w.dataset.hour=String(h).padStart(2,"0");w.title=`${h}:00–${h+1}:00 · ${v} 条`;const b=document.createElement("i");b.className="hour-bar";b.style.height=`${Math.max(v/max*100,v?4:1)}%`;w.appendChild(b);chart.appendChild(w)})
}

function renderProviders(){const summary=$("summaryProvider"), profiles=$("providerProfile");summary.innerHTML='<option value="">请选择平台</option>';profiles.innerHTML='<option value="">新建配置</option>';state.providers.forEach(p=>{const readiness=!p.api_key_saved?' · 未保存 API Key':(!p.model?' · 未选择模型':' · '+p.model);const label=`${p.name}${readiness}`;summary.add(new Option(label,p.id));profiles.add(new Option(label,p.id))});const active=state.settings.active_provider_id||"";summary.value=active;
  const kinds=$("providerKind");if(!kinds.options.length)Object.entries(providerDefaults).forEach(([id,v])=>kinds.add(new Option(v[0],id)));
  if (!state.providers.length) loadProviderForm("");
}
function updateProviderAddressVisibility(){const custom=$("providerKind").value==="custom";$("providerBaseUrlFields").classList.toggle("hidden",!custom)}
function loadProviderForm(id){const p=state.providers.find(x=>x.id===id);if(!p){$("providerKind").value="openai";applyProviderDefault();$("providerApiKey").value="";$("providerModel").value="";return}$("providerKind").value=p.kind;$("providerName").value=p.name;$("providerBaseUrl").value=p.base_url;$("providerApiKey").value="";$("providerApiKey").placeholder=p.api_key_saved?"已安全保存；留空保持不变":"请输入 API Key";$("providerModel").value=p.model||"";updateProviderAddressVisibility()}
function applyProviderDefault(){const [name,url]=providerDefaults[$("providerKind").value]||["",""];$("providerName").value=name;$("providerBaseUrl").value=url;updateProviderAddressVisibility()}
async function saveProvider(silent=false){const kind=$("providerKind").value,isCustom=kind==="custom";let baseUrl=isCustom?$("providerBaseUrl").value.trim():(providerDefaults[kind]?.[1]||""),apiKey=$("providerApiKey").value.trim();if(isCustom&&baseUrl&&!/^https?:\/\//i.test(baseUrl))throw new Error("自定义 API 地址必须以 http:// 或 https:// 开头");const payload={id:$("providerProfile").value||undefined,kind,name:$("providerName").value,base_url:baseUrl,api_key:apiKey||undefined,model:$("providerModel").value||$("providerModelList").value};const data=await api("/api/providers/save",{method:"POST",body:JSON.stringify(payload)});await loadSettings();$("providerProfile").value=data.provider.id;loadProviderForm(data.provider.id);if(!silent)toast("平台配置已保存","success");return data.provider.id}
async function fetchModels(){const btn=$("btnFetchModels"),original=btn.textContent;try{btn.disabled=true;btn.textContent="正在获取…";const id=await saveProvider(true);const data=await api("/api/providers/models",{method:"POST",body:JSON.stringify({id})});const sel=$("providerModelList");sel.innerHTML='<option value="">请选择模型</option>';data.models.forEach(m=>sel.add(new Option(m,m)));toast(`获取到 ${data.models.length} 个模型`,"success")}catch(e){toast(`${e.message}；仍可手动输入模型名`,"error")}finally{btn.disabled=false;btn.textContent=original}}
async function testProvider(){try{const id=await saveProvider(true);const data=await api("/api/providers/test",{method:"POST",body:JSON.stringify({id})});toast(data.message||"连接成功","success")}catch(e){toast(e.message,"error")}}
async function saveProxy(){try{await api("/api/proxy/save",{method:"POST",body:JSON.stringify({url:$("proxyUrl").value,username:$("proxyUsername").value,password:$("proxyPassword").value||undefined})});$("proxyPassword").value="";toast("代理设置已保存","success")}catch(e){toast(e.message,"error")}}

async function runSummary(){const groups=selectedGroups(),providerId=$("summaryProvider").value;if(!groups.length)return toast("请先选择并保存群聊","error");if(!providerId)return toast("请选择 AI 平台","error");const profile=state.providers.find(p=>p.id===providerId);if(!profile?.api_key_saved){toast("该平台尚未保存 API Key，请先填写并保存","error");$("settingsDialog").showModal();$("providerProfile").value=providerId;loadProviderForm(providerId);return}if(!profile.model){toast("该平台尚未选择模型，请先自动获取或手动填写并保存","error");$("settingsDialog").showModal();$("providerProfile").value=providerId;loadProviderForm(providerId);return}const start=$("summaryStart").value,end=$("summaryEnd").value;if(!start||!end)return toast("请选择开始和结束时间","error");const btn=$("btnRunSummary");btn.disabled=true;btn.textContent="正在启动…";$("jobProgress").classList.remove("hidden");$("progressText").textContent="正在创建总结任务";$("progressPercent").textContent="0%";$("progressBar").style.width="0%";$("summaryStatus").textContent="准备中";$("summaryResults").innerHTML='<div class="empty-state"><span>⏳</span><p>正在读取聊天记录并生成总结</p></div>';try{const data=await api("/api/summary/run",{method:"POST",body:JSON.stringify({group_ids:groups.map(g=>g.id),start,end,provider_id:providerId})});state.jobId=data.job_id;btn.textContent="总结进行中";pollJob()}catch(e){$("summaryStatus").textContent="启动失败";$("progressText").textContent=e.message;btn.disabled=false;btn.textContent="立即开始总结";toast(e.message,"error")}}
async function pollJob(){try{const job=await api(`/api/summary/job?id=${encodeURIComponent(state.jobId)}`);$("progressText").textContent=job.message;$("progressPercent").textContent=`${job.progress}%`;$("progressBar").style.width=`${job.progress}%`;const labels={queued:"准备中",running:"总结中",completed:"已完成",failed:"失败"};$("summaryStatus").textContent=labels[job.status]||"处理中";if(["completed","failed"].includes(job.status)){renderSummaryResults(job);$("btnRunSummary").disabled=false;$("btnRunSummary").textContent="再次开始总结";return}setTimeout(pollJob,900)}catch(e){$("summaryStatus").textContent="查询失败";toast(e.message,"error");$("btnRunSummary").disabled=false;$("btnRunSummary").textContent="重新开始总结"}}
function evidenceHtml(items){return(items||[]).map(e=>`<span class="evidence">依据：${escapeHtml(e.sender)} · ${escapeHtml(e.time)}：“${escapeHtml(e.quote)}”</span>`).join("")}
function listHtml(items,format){if(!items||!items.length)return"<p>暂无</p>";return`<ul>${items.map(x=>`<li>${format(x)}${evidenceHtml(x.evidence)}</li>`).join("")}</ul>`}
function renderSummaryResults(job){const box=$("summaryResults");box.innerHTML="";Object.entries(job.results||{}).forEach(([gid,item])=>{const s=item.summary;const card=document.createElement("article");card.className="summary-card";card.innerHTML=`<header><div><h3>${escapeHtml(s.group_name)}</h3><p>${escapeHtml(s.start)} 至 ${escapeHtml(s.end)} · ${fmt(s.message_count)} 条</p></div><div class="summary-tools"><button class="btn btn-secondary copy-summary">复制</button><a class="btn btn-secondary" href="/api/summary/download?id=${encodeURIComponent(job.id)}&group_id=${encodeURIComponent(gid)}&format=md">Markdown</a><a class="btn btn-secondary" href="/api/summary/download?id=${encodeURIComponent(job.id)}&group_id=${encodeURIComponent(gid)}&format=txt">文本</a></div></header><div class="summary-content"><h4>核心摘要</h4><p>${escapeHtml(s.core_summary||"暂无")}</p><h4>主要话题</h4>${listHtml(s.topics,x=>`<strong>${escapeHtml(x.title)}</strong>：${escapeHtml(x.summary)}`)}<h4>关键结论</h4>${listHtml(s.decisions,x=>escapeHtml(x.text))}<h4>待办事项</h4>${listHtml(s.action_items,x=>`${escapeHtml(x.task)}（负责人：${escapeHtml(x.owner||'未明确')}；截止：${escapeHtml(x.deadline||'未明确')}）`)}<h4>重要链接/文件</h4>${listHtml(s.links_files,x=>`${escapeHtml(x.type)}：${escapeHtml(x.value)}（${escapeHtml(x.sender)} ${escapeHtml(x.time)}）`)}<h4>未解决问题</h4>${listHtml(s.open_questions,x=>escapeHtml(x.question))}<h4>活跃成员</h4>${listHtml(s.active_members,x=>`${escapeHtml(x.name)}：${escapeHtml(x.contribution)}`)}</div>`;card.querySelector(".copy-summary").onclick=async()=>{await navigator.clipboard.writeText(item.markdown);toast("总结已复制","success")};box.appendChild(card)});Object.entries(job.errors||{}).forEach(([gid,msg])=>{const e=document.createElement("div");e.className="security-note";e.textContent=`${state.groups.find(g=>g.id===gid)?.name||gid}：${msg}`;box.appendChild(e)});if(!box.children.length)box.innerHTML='<div class="empty-state">没有生成可用结果</div>'}

async function saveSchedule(){const groups=selectedGroups();const first={};if($("firstBackfill").value)groups.forEach(g=>first[g.id]=$("firstBackfill").value);try{await api("/api/settings/schedule",{method:"POST",body:JSON.stringify({enabled:$("scheduleEnabled").checked,time:$("scheduleTime").value,provider_id:$("summaryProvider").value,first_backfill:first})});toast("定时设置已保存","success");await loadSettings()}catch(e){toast(e.message,"error")}}

function bind(){
  $("btnReloadAccounts").onclick=loadAccounts;$("btnConnect").onclick=connectWechat;$("groupToggle").onclick=()=>toggleGroupDropdown();$("groupSearch").oninput=renderGroups;$("btnSaveGroups").onclick=saveGroups;$("btnMonitor").onclick=toggleMonitor;$("btnRefreshStats").onclick=refreshStats;$("btnRunSummary").onclick=runSummary;$("btnSaveSchedule").onclick=saveSchedule;
  document.addEventListener("click",e=>{if(!$("groupSelect").contains(e.target))toggleGroupDropdown(false)});document.addEventListener("keydown",e=>{if(e.key==="Escape")toggleGroupDropdown(false)});
  $("btnSettings").onclick=()=>$("settingsDialog").showModal();$("providerProfile").onchange=e=>loadProviderForm(e.target.value);$("providerKind").onchange=applyProviderDefault;$("providerModelList").onchange=e=>{if(e.target.value)$("providerModel").value=e.target.value};$("btnSaveProvider").onclick=()=>saveProvider();$("btnFetchModels").onclick=fetchModels;$("btnTestProvider").onclick=testProvider;$("btnSaveProxy").onclick=saveProxy;
  $("btnUpdate").onclick=async()=>{const btn=$("btnUpdate");btn.disabled=true;try{const check=await api("/api/update/check",{method:"POST",body:"{}"});if(!check.available)return toast(check.message,"success");if(confirm(`发现 ${check.latest_version}，是否下载已签名更新包？`)){const staged=await api("/api/update/stage",{method:"POST",body:"{}"});toast(staged.message,"success")}}catch(e){toast(e.message,"error")}finally{btn.disabled=false}};
  $("btnExcel").onclick=async()=>{try{await api("/api/report/excel",{method:"POST",body:JSON.stringify({group_id:$("statsGroup").value,date:$("statsDate").value})});toast("Excel 已生成并打开","success")}catch(e){toast(e.message,"error")}};$("btnReports").onclick=()=>api("/api/report/open-folder",{method:"POST",body:"{}"}).catch(e=>toast(e.message,"error"));$("btnExit").onclick=async()=>{await api("/api/shutdown",{method:"POST",body:"{}"});window.close()};
}

async function init(){bind();const now=new Date(),start=new Date(now);start.setHours(0,0,0,0);$("summaryStart").value=inputTime(start);$("summaryEnd").value=inputTime(now);$("firstBackfill").value=inputTime(start);$("statsDate").value=inputTime(now).slice(0,10);await loadSettings();await loadAccounts();await refreshStatus();if(state.status.connected){const data=await api("/api/groups");state.groups=data.groups||[];renderGroups()}setInterval(refreshStatus,5000);setInterval(loadLive,2000)}
document.addEventListener("DOMContentLoaded",()=>init().catch(e=>toast(e.message,"error")));
