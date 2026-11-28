"""Self-contained offline HTML/CSS/JS template for the local PCB Web workbench."""
from __future__ import annotations

WORKBENCH_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>PCB 压合排查工作台</title>
<style>
  :root {
    --bg: #f4f6f9;
    --panel: #ffffff;
    --panel-alt: #f8fafc;
    --border: #e2e8f0;
    --text: #0f172a;
    --muted: #64748b;
    --accent: #2563eb;
    --accent-soft: #eff6ff;
    --accent-hover: #1d4ed8;
    --ok: #15803d;
    --ok-bg: #dcfce7;
    --warn: #b45309;
    --warn-bg: #fef3c7;
    --danger: #b91c1c;
    --danger-bg: #fee2e2;
    --radius: 10px;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; height: 100%;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.55;
  }
  .app {
    display: grid;
    grid-template-columns: 260px 1fr 340px;
    height: 100vh;
    overflow: hidden;
  }
  .sidebar, .inspector {
    background: var(--panel);
    display: flex; flex-direction: column;
    overflow: hidden;
  }
  .sidebar { border-right: 1px solid var(--border); }
  .inspector { border-left: 1px solid var(--border); }
  .brand {
    padding: 16px; border-bottom: 1px solid var(--border);
  }
  .brand h1 { margin: 0 0 4px 0; font-size: 16px; font-weight: 700; }
  .brand .sub { font-size: 12px; color: var(--muted); word-break: break-all; }
  .side-actions {
    padding: 12px 16px; display: flex; gap: 8px; border-bottom: 1px solid var(--border);
  }
  .btn {
    border: 1px solid var(--border); background: var(--panel); color: var(--text);
    padding: 7px 12px; border-radius: 8px; cursor: pointer; font-size: 13px;
    transition: background .15s, border-color .15s;
  }
  .btn:hover { background: var(--panel-alt); border-color: #cbd5e1; }
  .btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .btn.primary:hover { background: var(--accent-hover); }
  .btn.sm { padding: 4px 9px; font-size: 12px; }
  .btn:disabled { opacity: 0.55; cursor: not-allowed; }
  .case-list {
    flex: 1; overflow-y: auto; padding: 8px;
  }
  .case-item {
    padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px;
    border: 1px solid transparent;
  }
  .case-item:hover { background: var(--panel-alt); }
  .case-item.active {
    background: var(--accent-soft); border-color: #bfdbfe;
  }
  .case-item .cid { font-weight: 600; font-size: 13px; display: flex; justify-content: space-between; }
  .case-item .ctitle {
    font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px;
  }
  .side-footer {
    padding: 12px 16px; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 8px;
  }
  .main {
    display: flex; flex-direction: column; overflow: hidden; background: var(--bg);
  }
  .topbar {
    background: var(--panel); border-bottom: 1px solid var(--border);
    padding: 10px 18px; display: flex; align-items: center; justify-content: space-between; gap: 12px;
    flex-wrap: wrap;
  }
  .topbar-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .topbar-title { font-weight: 600; font-size: 15px; }
  .badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 500;
    background: var(--panel-alt); color: var(--muted); border: 1px solid var(--border);
  }
  .badge.ok { background: var(--ok-bg); color: var(--ok); border-color: #bbf7d0; }
  .badge.warn { background: var(--warn-bg); color: var(--warn); border-color: #fde68a; }
  .seg {
    display: inline-flex; background: var(--panel-alt); border: 1px solid var(--border);
    border-radius: 8px; padding: 2px;
  }
  .seg button {
    border: none; background: transparent; padding: 5px 11px; border-radius: 6px;
    cursor: pointer; font-size: 12px; color: var(--muted); font-weight: 500;
  }
  .seg button.active {
    background: var(--panel); color: var(--accent); box-shadow: 0 1px 2px rgba(0,0,0,.08);
  }
  .chat-scroll {
    flex: 1; overflow-y: auto; padding: 20px 24px;
  }
  .empty-state {
    max-width: 680px; margin: 48px auto; text-align: center;
  }
  .empty-state h2 { margin: 0 0 8px 0; font-size: 20px; }
  .empty-state p { margin: 0 0 20px 0; color: var(--muted); }
  .sample-grid { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
  .sample-chip {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 9px 13px; cursor: pointer; font-size: 13px; color: var(--text); text-align: left;
  }
  .sample-chip:hover { border-color: var(--accent); background: var(--accent-soft); }
  .msg { max-width: 820px; margin: 0 auto 16px auto; display: flex; flex-direction: column; }
  .msg.user { align-items: flex-end; }
  .msg.assistant { align-items: flex-start; }
  .bubble {
    padding: 12px 16px; border-radius: 12px; max-width: 100%; word-break: break-word;
  }
  .msg.user .bubble {
    background: var(--accent); color: #fff; border-bottom-right-radius: 4px; white-space: pre-wrap;
  }
  .msg.assistant .bubble {
    background: var(--panel); border: 1px solid var(--border); border-bottom-left-radius: 4px; width: 100%;
  }
  .md h2, .md h3 { margin: 12px 0 6px 0; font-size: 15px; border-bottom: 1px solid #f1f5f9; padding-bottom: 4px; }
  .md h2:first-child, .md h3:first-child { margin-top: 0; }
  .md p { margin: 6px 0; }
  .md ul { margin: 6px 0; padding-left: 20px; }
  .md li { margin: 4px 0; }
  .cite-pill {
    display: inline-block; padding: 1px 6px; margin: 0 2px; border-radius: 5px;
    background: var(--accent-soft); color: var(--accent); border: 1px solid #bfdbfe;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px;
    cursor: pointer; text-decoration: none;
  }
  .cite-pill:hover { background: #dbeafe; }
  .meta-bar {
    margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border);
    font-size: 12px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  }
  .trace-box {
    margin-top: 8px; background: var(--panel-alt); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 10px; font-size: 12px; font-family: ui-monospace, Consolas, monospace;
  }
  .composer {
    background: var(--panel); border-top: 1px solid var(--border); padding: 12px 20px;
  }
  .composer-inner {
    max-width: 820px; margin: 0 auto; display: flex; gap: 10px; align-items: flex-end;
  }
  .composer textarea {
    flex: 1; resize: none; min-height: 46px; max-height: 160px; padding: 10px 12px;
    border: 1px solid var(--border); border-radius: 10px; font-family: inherit; font-size: 14px;
    outline: none;
  }
  .composer textarea:focus { border-color: var(--accent); }
  .tabs {
    display: flex; border-bottom: 1px solid var(--border);
  }
  .tabs button {
    flex: 1; padding: 11px 8px; border: none; background: transparent; cursor: pointer;
    font-size: 13px; font-weight: 600; color: var(--muted); border-bottom: 2px solid transparent;
  }
  .tabs button.active { color: var(--accent); border-bottom-color: var(--accent); }
  .insp-body { flex: 1; overflow-y: auto; padding: 12px; }
  .cite-card {
    border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-bottom: 10px;
    background: var(--panel-alt);
  }
  .cite-card.highlight { border-color: var(--accent); background: var(--accent-soft); }
  .cite-card .chead {
    font-size: 12px; font-weight: 600; color: var(--accent); font-family: ui-monospace, Consolas, monospace;
    display: flex; justify-content: space-between; gap: 6px;
  }
  .cite-card .ctitle { font-size: 12px; color: var(--muted); margin: 4px 0; }
  .cite-card .ctext { font-size: 12.5px; white-space: pre-wrap; color: var(--text); }
  .note-form { display: flex; gap: 6px; margin-bottom: 12px; }
  .note-form input {
    flex: 1; padding: 7px 9px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px;
  }
  .note-item {
    padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 8px;
    background: var(--panel-alt); font-size: 13px;
  }
  .note-item .ts { font-size: 11px; color: var(--muted); margin-bottom: 2px; }
  .modal-mask {
    position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
    display: none; align-items: center; justify-content: center; z-index: 50;
  }
  .modal-mask.open { display: flex; }
  .modal {
    background: var(--panel); border-radius: 12px; width: 520px; max-width: 94vw;
    max-height: 90vh; overflow-y: auto; padding: 20px; box-shadow: 0 20px 50px rgba(0,0,0,.2);
  }
  .modal h3 { margin: 0 0 12px 0; font-size: 16px; }
  .field { margin-bottom: 12px; }
  .field label { display: block; font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 4px; }
  .field input[type="text"], .field input[type="password"], .field select {
    width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px;
  }
  .field.row { display: flex; align-items: center; gap: 8px; }
  .field.row label { margin: 0; color: var(--text); font-weight: 500; }
  .modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
  .hint { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #cbd5e1; border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (max-width: 1080px) {
    .app { grid-template-columns: 220px 1fr; }
    .inspector { display: none; }
  }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <h1>PCB 压合排查工作台</h1>
      <div class="sub" id="catalogInfo">正在连接本机服务…</div>
    </div>
    <div class="side-actions">
      <button class="btn primary" style="flex:1" id="newCaseBtn">+ 新建病例</button>
      <button class="btn" id="refreshCasesBtn" title="刷新列表">↻</button>
    </div>
    <div class="case-list" id="caseList"></div>
    <div class="side-footer">
      <button class="btn" id="exportReportBtn" disabled>📄 导出当前病例 Markdown</button>
      <button class="btn" id="openSettingsBtn">⚙ 模型渠道与脱敏配置</button>
      <button class="btn" id="openDoctorBtn">🩺 运行环境与自检</button>
      <button class="btn" id="openMcpInfoBtn">🔌 MCP / 数字员工接入</button>
    </div>
  </aside>

  <main class="main">
    <header class="topbar">
      <div class="topbar-left">
        <span class="topbar-title" id="activeCaseTitle">临时会话</span>
        <span class="badge" id="profileBadge" style="cursor:pointer;" title="点击切换或配置模型渠道">检测配置…</span>
      </div>
      <div style="display:flex; align-items:center; gap:10px;">
        <div class="seg" role="group" aria-label="运行模式">
          <button id="modeAgentBtn" class="active">脱敏出境 Agent</button>
          <button id="modeLocalBtn">纯本地离线</button>
        </div>
        <button class="btn sm" id="clearHistoryBtn">清空上下文</button>
      </div>
    </header>

    <section class="chat-scroll" id="chatScroll"></section>

    <footer class="composer">
      <div class="composer-inner">
        <textarea id="questionInput" rows="2" placeholder="输入压合异常现象、材料术语或叠构问题（Enter 发送，Shift+Enter 换行）…"></textarea>
        <button class="btn primary" id="sendBtn" style="height:46px; padding:0 18px;">发送</button>
      </div>
    </footer>
  </main>

  <aside class="inspector">
    <div class="tabs">
      <button class="active" id="tabCitations">引用原文 (<span id="citeCount">0</span>)</button>
      <button id="tabNotes">备注与缺口 (<span id="noteCount">0</span>)</button>
    </div>
    <div class="insp-body" id="inspectorBody"></div>
  </aside>
</div>

<!-- Settings Modal -->
<div class="modal-mask" id="settingsModal">
  <div class="modal" style="width:580px;">
    <h3>模型渠道与脱敏出境配置</h3>
    <div class="hint" style="margin-bottom:14px;">配置统一保存在本机 <code>.private/agent_profile.json</code>（Web、CLI 与 MCP 服务自动共享），密钥绝不回显明文。</div>
    <div class="field">
      <label>当前命名渠道（可保存多套网关随时切换）</label>
      <div style="display:flex; gap:6px;">
        <select id="cfgProfileSelect" style="flex:1;"></select>
        <button class="btn sm" type="button" id="newProfileBtn">+ 新建渠道</button>
        <button class="btn sm" type="button" id="deleteProfileBtn">删除</button>
      </div>
    </div>
    <div class="field">
      <label>快速套用预设模板（可选）</label>
      <select id="cfgPresetSelect">
        <option value="">-- 选择常见网关模板自动填充 --</option>
      </select>
    </div>
    <div class="field">
      <label>渠道显示名称</label>
      <input type="text" id="cfgLabel" placeholder="例如：内部 NewAPI 网关 / 本机 Ollama" />
    </div>
    <div class="field row">
      <input type="checkbox" id="cfgAllowEgress" />
      <label for="cfgAllowEgress">允许通过本机脱敏网关调用模型接口（PCB_ALLOW_CLOUD_EGRESS）</label>
    </div>
    <div class="field row">
      <input type="checkbox" id="cfgAgentLoop" />
      <label for="cfgAgentLoop">启用多步受控 Agent 查证循环（PCB_AGENT_LOOP）</label>
    </div>
    <div class="field row">
      <input type="checkbox" id="cfgLocalHttp" />
      <label for="cfgLocalHttp">允许连接本机 HTTP 模型服务（如 Ollama/vLLM 127.0.0.1，PCB_ALLOW_LOCALHOST_HTTP）</label>
    </div>
    <div class="field">
      <label>脱敏出境范围（PCB_EGRESS_SCOPE）</label>
      <select id="cfgScope">
        <option value="public_only">public_only（仅公开参考资料）</option>
        <option value="sanitized_catalog">sanitized_catalog（允许二次脱敏零残留的 Catalog 摘录）</option>
      </select>
    </div>
    <div class="field">
      <label>接口协议（PCB_LLM_PROVIDER）</label>
      <select id="cfgProvider">
        <option value="openai_chat_completions">openai_chat_completions（DeepSeek / 通义千问 / OpenAI / NewAPI / Ollama）</option>
        <option value="openai_responses">openai_responses（OpenAI Responses API）</option>
      </select>
    </div>
    <div class="field">
      <label>Base URL（PCB_LLM_BASE_URL）</label>
      <input type="text" id="cfgBaseUrl" placeholder="https://api.deepseek.com/v1" />
    </div>
    <div class="field">
      <label>模型名称（PCB_LLM_MODEL）</label>
      <input type="text" id="cfgModel" placeholder="deepseek-chat" />
    </div>
    <div class="field">
      <label>API 密钥环境变量名（PCB_LLM_API_KEY_ENV）</label>
      <input type="text" id="cfgKeyEnv" placeholder="DEEPSEEK_API_KEY" />
    </div>
    <div class="field">
      <label>API Key（留空保持原密钥；本机 Ollama 可免填）</label>
      <input type="password" id="cfgApiKey" placeholder="填入 API Key" autocomplete="off" />
      <div class="hint" id="cfgKeyStatus"></div>
    </div>
    <div id="probeResultBox" class="trace-box" style="display:none; margin-top:8px;"></div>
    <div class="modal-foot" style="justify-content:space-between;">
      <button class="btn" type="button" id="testConnBtn">⚡ 测试连接</button>
      <div style="display:flex; gap:8px;">
        <button class="btn" type="button" id="closeSettingsBtn">取消</button>
        <button class="btn primary" type="button" id="saveSettingsBtn">保存并激活渠道</button>
      </div>
    </div>
  </div>
</div>

<!-- Doctor Modal -->
<div class="modal-mask" id="doctorModal">
  <div class="modal" style="width:580px;">
    <h3>运行环境与知识库自检</h3>
    <div id="doctorBody" style="margin-top:10px;">正在检测…</div>
    <div class="modal-foot">
      <button class="btn" type="button" id="doctorProbeBtn">⚡ 含模型连通性实测</button>
      <button class="btn primary" type="button" id="closeDoctorBtn">关闭</button>
    </div>
  </div>
</div>

<!-- MCP / Digital Employee Info Modal -->
<div class="modal-mask" id="mcpModal">
  <div class="modal" style="width:600px;">
    <h3>MCP / 数字员工一键接入配置</h3>
    <div class="hint" style="margin-bottom:10px;">
      <code>phase6_mcp.py</code> 已自动读取本机保存的模型渠道配置，因此外部客户端（Claude Desktop / Cursor / Cherry Studio 等）<strong>无需在配置文件中写入明文 API Key</strong>：
    </div>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
      <strong style="font-size:12px;">推荐：本地 stdio MCP（免密钥配置）</strong>
      <button class="btn sm" type="button" id="copyStdioMcpBtn">复制 JSON</button>
    </div>
    <pre class="trace-box" id="mcpStdioPre" style="white-space:pre-wrap; user-select:all; margin-bottom:12px;"></pre>
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
      <strong style="font-size:12px;">HTTP 平台：Streamable HTTP MCP（Dify / FastGPT / MaxKB）</strong>
      <button class="btn sm" type="button" id="copyHttpMcpBtn">复制 JSON</button>
    </div>
    <pre class="trace-box" id="mcpInfoPre" style="white-space:pre-wrap; user-select:all;"></pre>
    <div class="modal-foot">
      <button class="btn primary" id="closeMcpBtn">关闭</button>
    </div>
  </div>
</div>

<script>
const state = {
  mode: 'agent',
  activeCaseId: null,
  activeCase: null,
  ephemeralTurns: [],
  ephemeralHistory: [],
  lastCitations: [],
  lastMissing: [],
  activeTab: 'citations',
  highlightedCite: null,
  sending: false,
  health: null,
  profile: null,
};

const SAMPLES = [
  '8层板 IT-180A 压合后板边出现白斑，真空曲线正常，怎么排查？',
  '压合后板边分层和起泡怎么区分排查方向？',
  'Tg 和 Td 在材料表里分别指什么？Tg 能否直接当作卸压温度？',
  '本地知识库有多少文档？',
];

function esc(s) {
  return String(s || '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderMarkdown(text) {
  const lines = String(text || '').replace(/\r\n/g, '\n').split('\n');
  let html = '';
  let inList = false;
  const formatInline = (raw) => {
    let s = esc(raw);
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\[(EXC_[0-9a-f]{24})\]/g,
      '<button type="button" class="cite-pill" data-cite="$1">$1</button>');
    return s;
  };
  for (const line of lines) {
    const t = line.trim();
    if (!t) {
      if (inList) { html += '</ul>'; inList = false; }
      continue;
    }
    if (t.startsWith('## ')) {
      if (inList) { html += '</ul>'; inList = false; }
      html += '<h2>' + formatInline(t.slice(3)) + '</h2>';
    } else if (t.startsWith('### ')) {
      if (inList) { html += '</ul>'; inList = false; }
      html += '<h3>' + formatInline(t.slice(4)) + '</h3>';
    } else if (t.startsWith('- ')) {
      if (!inList) { html += '<ul>'; inList = true; }
      html += '<li>' + formatInline(t.slice(2)) + '</li>';
    } else {
      if (inList) { html += '</ul>'; inList = false; }
      html += '<p>' + formatInline(t) + '</p>';
    }
  }
  if (inList) html += '</ul>';
  return '<div class="md">' + html + '</div>';
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

async function loadHealthAndProfile() {
  try {
    state.health = await api('/api/health');
    document.getElementById('catalogInfo').textContent =
      `版本 ${state.health.catalog_version || 'unknown'} · MCP工具箱=${state.health.toolset}`;
    state.profile = await api('/api/profile');
    updateProfileBadge();
  } catch (e) {
    document.getElementById('catalogInfo').textContent = '连接失败: ' + e.message;
  }
}

function updateProfileBadge() {
  const badge = document.getElementById('profileBadge');
  const p = state.profile || {};
  const ch = p.active_profile || 'default';
  if (p.PCB_ALLOW_CLOUD_EGRESS && (p.has_api_key || p.PCB_ALLOW_LOCALHOST_HTTP) && p.PCB_LLM_MODEL) {
    badge.className = 'badge ok';
    badge.textContent = `渠道 [${ch}] · ${p.PCB_LLM_MODEL} (${p.PCB_EGRESS_SCOPE || 'public_only'})`;
  } else {
    badge.className = 'badge warn';
    badge.textContent = `渠道 [${ch}] · 纯本地离线模式（点击配置外部模型）`;
  }
}

async function loadCases() {
  const data = await api('/api/cases');
  const list = document.getElementById('caseList');
  list.innerHTML = '';
  const ep = document.createElement('div');
  ep.className = 'case-item' + (!state.activeCaseId ? ' active' : '');
  ep.innerHTML = `<div class="cid"><span>临时会话</span><span>${state.ephemeralTurns.length}轮</span></div>
                  <div class="ctitle">不保存病例文件</div>`;
  ep.onclick = () => selectCase(null);
  list.appendChild(ep);

  for (const item of data.cases || []) {
    const el = document.createElement('div');
    el.className = 'case-item' + (state.activeCaseId === item.case_id ? ' active' : '');
    el.innerHTML = `<div class="cid"><span>${esc(item.case_id)}</span><span>${item.turns}轮</span></div>
                    <div class="ctitle">${esc(item.title || item.case_id)}</div>`;
    el.onclick = () => selectCase(item.case_id);
    list.appendChild(el);
  }
}

async function selectCase(caseId) {
  state.activeCaseId = caseId;
  state.highlightedCite = null;
  if (!caseId) {
    state.activeCase = null;
    document.getElementById('activeCaseTitle').textContent = '临时会话';
    document.getElementById('exportReportBtn').disabled = true;
  } else {
    state.activeCase = await api('/api/cases/' + encodeURIComponent(caseId));
    document.getElementById('activeCaseTitle').textContent =
      `病例：${state.activeCase.case_id}`;
    document.getElementById('exportReportBtn').disabled = false;
  }
  syncLastTurnInspector();
  await loadCases();
  renderChat();
  renderInspector();
}

function currentTurns() {
  return state.activeCase ? (state.activeCase.turns || []) : state.ephemeralTurns;
}

function syncLastTurnInspector() {
  const turns = currentTurns();
  if (turns.length) {
    const last = turns[turns.length - 1];
    state.lastCitations = last.citations || [];
    state.lastMissing = last.missing_information || [];
  } else {
    state.lastCitations = [];
    state.lastMissing = [];
  }
}

function renderChat() {
  const box = document.getElementById('chatScroll');
  const turns = currentTurns();
  if (!turns.length && !state.sending) {
    box.innerHTML = `
      <div class="empty-state">
        <h2>PCB 压合知识库与受控排查 Agent</h2>
        <p>所有回答基于本机只读脱敏 Catalog；每个结论可追溯至原文 EXC 引用，证据不足时明确列出缺口。</p>
        <div class="sample-grid">
          ${SAMPLES.map(q => `<button type="button" class="sample-chip" data-sample="${esc(q)}">${esc(q)}</button>`).join('')}
        </div>
      </div>`;
    box.querySelectorAll('.sample-chip').forEach(btn => {
      btn.onclick = () => {
        document.getElementById('questionInput').value = btn.dataset.sample;
        sendQuestion();
      };
    });
    return;
  }
  box.innerHTML = '';
  for (const turn of turns) {
    const u = document.createElement('div');
    u.className = 'msg user';
    u.innerHTML = `<div class="bubble">${esc(turn.question)}</div>`;
    box.appendChild(u);

    const a = document.createElement('div');
    a.className = 'msg assistant';
    const trace = turn.agent_trace || [];
    const traceHtml = trace.length
      ? `<details style="margin-top:8px;"><summary style="cursor:pointer;font-size:12px;color:var(--muted);">查看查证轨迹（${trace.length} 次只读工具调用）</summary>
         <div class="trace-box">${trace.map(t => `${esc(t.tool)} · ${esc(t.status)} · +${t.evidence_added || 0}摘录 · ${t.elapsed_ms || 0}ms`).join('<br/>')}</div></details>`
      : '';
    const badges = [
      `路由: ${turn.route || '-'}`,
      `模式: ${turn.answer_mode || '-'}`,
      `状态: ${turn.status || '-'}`,
      `出境: ${turn.data_egress || 'none'}`,
    ];
    if (turn.elapsed_ms) badges.push(`耗时: ${(turn.elapsed_ms / 1000).toFixed(1)}s`);
    if (turn.total_tokens) badges.push(`Tokens: ${turn.total_tokens}`);
    if (turn.fallback_reason) badges.push(`回退原因: ${turn.fallback_reason}`);
    a.innerHTML = `<div class="bubble">
        ${renderMarkdown(turn.answer)}
        ${traceHtml}
        <div class="meta-bar">${badges.map(b => `<span class="badge">${esc(b)}</span>`).join('')}</div>
      </div>`;
    a.querySelectorAll('.cite-pill').forEach(pill => {
      pill.onclick = () => {
        state.lastCitations = turn.citations || state.lastCitations;
        state.activeTab = 'citations';
        state.highlightedCite = pill.dataset.cite;
        renderInspector();
      };
    });
    box.appendChild(a);
  }
  if (state.sending) {
    const wait = document.createElement('div');
    wait.className = 'msg assistant';
    wait.innerHTML = `<div class="bubble"><span class="spinner"></span>正在检索本地只读 Catalog 并核验引用…</div>`;
    box.appendChild(wait);
  }
  box.scrollTop = box.scrollHeight;
}

function renderInspector() {
  document.getElementById('citeCount').textContent = state.lastCitations.length;
  const notes = state.activeCase ? (state.activeCase.notes || []) : [];
  document.getElementById('noteCount').textContent = notes.length + state.lastMissing.length;
  document.getElementById('tabCitations').className = state.activeTab === 'citations' ? 'active' : '';
  document.getElementById('tabNotes').className = state.activeTab === 'notes' ? 'active' : '';
  const body = document.getElementById('inspectorBody');

  if (state.activeTab === 'citations') {
    if (!state.lastCitations.length) {
      body.innerHTML = '<div class="hint" style="padding:16px;text-align:center;">暂无引用摘录。发起提问后可点击回答中的 EXC 编号定位原文。</div>';
      return;
    }
    body.innerHTML = state.lastCitations.map(c => {
      const id = c.excerpt_id || c.citation_id || '';
      const hl = state.highlightedCite === id ? ' highlight' : '';
      return `<div class="cite-card${hl}" id="card_${esc(id)}">
        <div class="chead"><span>${esc(id)}</span><span>${esc(c.source_type || '')}</span></div>
        <div class="ctitle">${esc(c.title || '未命名来源')}</div>
        <div class="ctext">${esc(c.text || '（本条历史归档仅保留引用标识）')}</div>
      </div>`;
    }).join('');
    if (state.highlightedCite) {
      const target = document.getElementById('card_' + state.highlightedCite);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    return;
  }

  let html = '';
  if (state.activeCase) {
    html += `<form class="note-form" id="noteForm">
      <input type="text" id="noteInput" placeholder="记录现场观测、切片或批次备注…" />
      <button class="btn primary sm" type="submit">添加</button>
    </form>`;
    if (notes.length) {
      html += '<div style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:6px;">现场备注</div>';
      html += notes.map(n => `<div class="note-item"><div class="ts">${esc(n.timestamp)}</div><div>${esc(n.text)}</div></div>`).join('');
    }
  } else {
    html += '<div class="hint" style="margin-bottom:12px;">新建或打开一个病例后可持久化记录现场备注并导出 Markdown 草稿。</div>';
  }
  if (state.lastMissing.length) {
    html += '<div style="font-size:12px;font-weight:600;color:var(--muted);margin:12px 0 6px 0;">待补充现场信息</div>';
    html += state.lastMissing.map(m => {
      const text = typeof m === 'string' ? m : (m.prompt || m.code || '');
      return `<div class="note-item">${esc(text)}</div>`;
    }).join('');
  }
  body.innerHTML = html || '<div class="hint" style="padding:16px;text-align:center;">暂无备注或待补项。</div>';
  const form = document.getElementById('noteForm');
  if (form) {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const inp = document.getElementById('noteInput');
      const text = inp.value.trim();
      if (!text) return;
      await api(`/api/cases/${encodeURIComponent(state.activeCaseId)}/notes`, {
        method: 'POST', body: JSON.stringify({ text }),
      });
      state.activeCase = await api('/api/cases/' + encodeURIComponent(state.activeCaseId));
      renderInspector();
    };
  }
}

async function sendQuestion() {
  if (state.sending) return;
  const input = document.getElementById('questionInput');
  const q = input.value.trim();
  if (!q) return;
  input.value = '';
  state.sending = true;
  document.getElementById('sendBtn').disabled = true;
  renderChat();
  try {
    const payload = {
      question: q,
      mode: state.mode,
      case_id: state.activeCaseId,
      history: state.activeCaseId ? undefined : state.ephemeralHistory.slice(-8),
    };
    const resp = await api('/api/answer', { method: 'POST', body: JSON.stringify(payload) });
    const r = resp.result || {};
    const turn = {
      question: q,
      status: resp.status,
      route: (r.workflow || {}).route,
      answer_mode: r.answer_mode,
      data_egress: resp.data_egress,
      answer: r.answer || '',
      citations: r.citations || [],
      missing_information: r.missing_information || [],
      agent_trace: r.agent_trace || [],
      elapsed_ms: resp.elapsed_ms,
      total_tokens: ((r.model || {}).usage || {}).total_tokens || 0,
      fallback_reason: (r.model || {}).fallback_reason || null,
    };
    if (state.activeCaseId) {
      state.activeCase = await api('/api/cases/' + encodeURIComponent(state.activeCaseId));
      // Keep full text citations for the fresh turn in memory for immediate inspection.
      if (state.activeCase.turns && state.activeCase.turns.length) {
        const last = state.activeCase.turns[state.activeCase.turns.length - 1];
        last.citations = turn.citations;
        last.elapsed_ms = turn.elapsed_ms;
        last.total_tokens = turn.total_tokens;
        last.fallback_reason = turn.fallback_reason;
      }
      await loadCases();
    } else {
      state.ephemeralTurns.push(turn);
      state.ephemeralHistory.push({ role: 'user', content: q });
      state.ephemeralHistory.push({ role: 'assistant', content: turn.answer.slice(0, 2800) });
      await loadCases();
    }
    state.lastCitations = turn.citations;
    state.lastMissing = turn.missing_information;
    state.highlightedCite = null;
  } catch (e) {
    alert('请求失败：' + e.message);
  } finally {
    state.sending = false;
    document.getElementById('sendBtn').disabled = false;
    renderChat();
    renderInspector();
  }
}

document.getElementById('sendBtn').onclick = sendQuestion;
document.getElementById('questionInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendQuestion();
  }
});
document.getElementById('modeAgentBtn').onclick = () => {
  state.mode = 'agent';
  document.getElementById('modeAgentBtn').className = 'active';
  document.getElementById('modeLocalBtn').className = '';
};
document.getElementById('modeLocalBtn').onclick = () => {
  state.mode = 'local';
  document.getElementById('modeLocalBtn').className = 'active';
  document.getElementById('modeAgentBtn').className = '';
};
document.getElementById('tabCitations').onclick = () => { state.activeTab = 'citations'; renderInspector(); };
document.getElementById('tabNotes').onclick = () => { state.activeTab = 'notes'; renderInspector(); };
document.getElementById('refreshCasesBtn').onclick = loadCases;
document.getElementById('newCaseBtn').onclick = async () => {
  const def = 'CASE_' + new Date().toISOString().slice(0,10).replace(/-/g,'') + '_' + String(Date.now()).slice(-3);
  const cid = prompt('输入新病例编号（字母/数字/下划线/短横线）：', def);
  if (!cid || !cid.trim()) return;
  try {
    await api('/api/cases', { method: 'POST', body: JSON.stringify({ case_id: cid.trim() }) });
    await selectCase(cid.trim());
  } catch (e) {
    alert('创建失败：' + e.message);
  }
};
document.getElementById('clearHistoryBtn').onclick = async () => {
  if (state.activeCaseId) {
    await api(`/api/cases/${encodeURIComponent(state.activeCaseId)}/clear`, { method: 'POST', body: '{}' });
    state.activeCase = await api('/api/cases/' + encodeURIComponent(state.activeCaseId));
  } else {
    state.ephemeralHistory = [];
  }
  alert('本病例对话上下文已清空（历史回合记录保留）。');
};
document.getElementById('exportReportBtn').onclick = () => {
  if (!state.activeCaseId) return;
  window.location.href = `/api/cases/${encodeURIComponent(state.activeCaseId)}/report`;
};

// Settings modal (multi-profile + presets + live connectivity test)
let editingProfileName = 'default';

function fillSettingsForm(p) {
  editingProfileName = p.active_profile || 'default';
  const sel = document.getElementById('cfgProfileSelect');
  sel.innerHTML = '';
  const profiles = (p.profiles && p.profiles.length)
    ? p.profiles
    : [{ name: editingProfileName, label: editingProfileName }];
  for (const item of profiles) {
    const opt = document.createElement('option');
    opt.value = item.name;
    opt.textContent = item.label && item.label !== item.name
      ? `${item.name} — ${item.label}`
      : item.name;
    if (item.name === editingProfileName) opt.selected = true;
    sel.appendChild(opt);
  }
  const presetSel = document.getElementById('cfgPresetSelect');
  presetSel.innerHTML = '<option value="">-- 选择常见网关模板自动填充 --</option>';
  for (const pr of (p.presets || [])) {
    const opt = document.createElement('option');
    opt.value = pr.id;
    opt.textContent = pr.label;
    presetSel.appendChild(opt);
  }
  const activeItem = profiles.find(x => x.name === editingProfileName) || {};
  document.getElementById('cfgLabel').value = activeItem.label || '';
  document.getElementById('cfgAllowEgress').checked = !!p.PCB_ALLOW_CLOUD_EGRESS;
  document.getElementById('cfgAgentLoop').checked = p.PCB_AGENT_LOOP !== false;
  document.getElementById('cfgLocalHttp').checked = !!p.PCB_ALLOW_LOCALHOST_HTTP;
  document.getElementById('cfgScope').value = p.PCB_EGRESS_SCOPE || 'sanitized_catalog';
  document.getElementById('cfgProvider').value = p.PCB_LLM_PROVIDER || 'openai_chat_completions';
  document.getElementById('cfgBaseUrl').value = p.PCB_LLM_BASE_URL || '';
  document.getElementById('cfgModel').value = p.PCB_LLM_MODEL || '';
  document.getElementById('cfgKeyEnv').value = p.PCB_LLM_API_KEY_ENV || 'DEEPSEEK_API_KEY';
  document.getElementById('cfgApiKey').value = '';
  document.getElementById('cfgKeyStatus').textContent = p.has_api_key
    ? `状态：渠道 [${editingProfileName}] 已保存密钥（或已配置环境变量）；留空保存则保持原密钥不变。`
    : `状态：渠道 [${editingProfileName}] 尚未保存密钥。`;
  const probeBox = document.getElementById('probeResultBox');
  probeBox.style.display = 'none';
  probeBox.textContent = '';
}

function collectSettingsPayload() {
  const payload = {
    profile_name: editingProfileName,
    label: document.getElementById('cfgLabel').value.trim() || editingProfileName,
    PCB_ALLOW_CLOUD_EGRESS: document.getElementById('cfgAllowEgress').checked,
    PCB_AGENT_LOOP: document.getElementById('cfgAgentLoop').checked,
    PCB_ALLOW_LOCALHOST_HTTP: document.getElementById('cfgLocalHttp').checked,
    PCB_EGRESS_SCOPE: document.getElementById('cfgScope').value,
    PCB_LLM_PROVIDER: document.getElementById('cfgProvider').value,
    PCB_LLM_BASE_URL: document.getElementById('cfgBaseUrl').value.trim(),
    PCB_LLM_MODEL: document.getElementById('cfgModel').value.trim(),
    PCB_LLM_API_KEY_ENV: document.getElementById('cfgKeyEnv').value.trim() || 'DEEPSEEK_API_KEY',
  };
  const key = document.getElementById('cfgApiKey').value.trim();
  if (key) payload.api_key = key;
  return payload;
}

async function openSettingsModal() {
  state.profile = await api('/api/profile');
  fillSettingsForm(state.profile);
  document.getElementById('settingsModal').classList.add('open');
}

document.getElementById('openSettingsBtn').onclick = openSettingsModal;
document.getElementById('profileBadge').onclick = openSettingsModal;
document.getElementById('cfgProfileSelect').onchange = async (e) => {
  const chosen = e.target.value;
  const view = await api('/api/profile?name=' + encodeURIComponent(chosen));
  fillSettingsForm(view);
};
document.getElementById('cfgPresetSelect').onchange = (e) => {
  const pid = e.target.value;
  if (!pid || !state.profile) return;
  const pr = (state.profile.presets || []).find(x => x.id === pid);
  if (!pr) return;
  document.getElementById('cfgLabel').value = pr.label;
  document.getElementById('cfgAllowEgress').checked = !!pr.PCB_ALLOW_CLOUD_EGRESS;
  document.getElementById('cfgAgentLoop').checked = !!pr.PCB_AGENT_LOOP;
  document.getElementById('cfgLocalHttp').checked = !!pr.PCB_ALLOW_LOCALHOST_HTTP;
  document.getElementById('cfgScope').value = pr.PCB_EGRESS_SCOPE;
  document.getElementById('cfgProvider').value = pr.PCB_LLM_PROVIDER;
  document.getElementById('cfgBaseUrl').value = pr.PCB_LLM_BASE_URL;
  document.getElementById('cfgModel').value = pr.PCB_LLM_MODEL;
  document.getElementById('cfgKeyEnv').value = pr.PCB_LLM_API_KEY_ENV;
};
document.getElementById('newProfileBtn').onclick = () => {
  const raw = prompt('输入新渠道标识（字母/数字/下划线/短横线，如 newapi 或 ollama）：', 'channel_2');
  if (!raw || !raw.trim()) return;
  editingProfileName = raw.trim();
  const sel = document.getElementById('cfgProfileSelect');
  const opt = document.createElement('option');
  opt.value = editingProfileName;
  opt.textContent = editingProfileName + '（新建未保存）';
  opt.selected = true;
  sel.appendChild(opt);
  document.getElementById('cfgLabel').value = editingProfileName;
  document.getElementById('cfgApiKey').value = '';
  document.getElementById('cfgKeyStatus').textContent = `状态：新建渠道 [${editingProfileName}]，请填写配置后点击保存。`;
};
document.getElementById('deleteProfileBtn').onclick = async () => {
  if (!confirm(`确定删除渠道 [${editingProfileName}] 吗？`)) return;
  try {
    state.profile = await api('/api/profile', {
      method: 'POST',
      body: JSON.stringify({ action: 'delete', profile_name: editingProfileName }),
    });
    updateProfileBadge();
    fillSettingsForm(state.profile);
  } catch (e) {
    alert('删除失败：' + e.message);
  }
};
document.getElementById('testConnBtn').onclick = async () => {
  const box = document.getElementById('probeResultBox');
  const btn = document.getElementById('testConnBtn');
  box.style.display = 'block';
  box.style.color = 'var(--muted)';
  box.textContent = '正在发送零数据探针测试模型网关连通性…';
  btn.disabled = true;
  try {
    const res = await api('/api/profile/test', {
      method: 'POST',
      body: JSON.stringify(collectSettingsPayload()),
    });
    box.style.color = res.ok ? 'var(--ok)' : 'var(--danger)';
    box.textContent = (res.ok ? '✅ ' : '❌ ') + (res.message || '');
  } catch (e) {
    box.style.color = 'var(--danger)';
    box.textContent = '❌ 测试请求失败：' + e.message;
  } finally {
    btn.disabled = false;
  }
};
document.getElementById('closeSettingsBtn').onclick = () => {
  document.getElementById('settingsModal').classList.remove('open');
};
document.getElementById('saveSettingsBtn').onclick = async () => {
  try {
    state.profile = await api('/api/profile', {
      method: 'POST',
      body: JSON.stringify(collectSettingsPayload()),
    });
    updateProfileBadge();
    document.getElementById('settingsModal').classList.remove('open');
  } catch (e) {
    alert('保存失败：' + e.message);
  }
};

// Doctor modal
async function loadDoctor(probe = false) {
  const body = document.getElementById('doctorBody');
  body.innerHTML = '<span class="spinner"></span>正在运行本机环境自检…';
  try {
    const rep = await api('/api/doctor' + (probe ? '?probe=1' : ''));
    let html = (rep.checks || []).map(c => {
      const cls = c.status === 'ok' ? 'ok' : (c.status === 'warn' ? 'warn' : '');
      return `<div class="note-item" style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
        <div><strong>${esc(c.name)}</strong><div style="font-size:12px;color:var(--muted);">${esc(c.summary)}</div></div>
        <span class="badge ${cls}">${esc(c.status)}</span>
      </div>`;
    }).join('');
    if (rep.recommendations && rep.recommendations.length) {
      html += '<div class="hint" style="margin-top:8px;"><strong>建议：</strong><br/>'
        + rep.recommendations.map(r => '• ' + esc(r)).join('<br/>') + '</div>';
    }
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = `<div style="color:var(--danger);">自检失败：${esc(e.message)}</div>`;
  }
}
document.getElementById('openDoctorBtn').onclick = () => {
  document.getElementById('doctorModal').classList.add('open');
  loadDoctor(false);
};
document.getElementById('doctorProbeBtn').onclick = () => loadDoctor(true);
document.getElementById('closeDoctorBtn').onclick = () => {
  document.getElementById('doctorModal').classList.remove('open');
};

// MCP info modal
document.getElementById('openMcpInfoBtn').onclick = async () => {
  try {
    const cfg = await api('/api/mcp-config');
    document.getElementById('mcpStdioPre').textContent = JSON.stringify(
      cfg.recommended_mcpServers_using_local_profile || { mcpServers: cfg.mcpServers }, null, 2
    );
    document.getElementById('mcpInfoPre').textContent = JSON.stringify(
      cfg.streamable_http_mcp || {}, null, 2
    );
  } catch (e) {
    document.getElementById('mcpInfoPre').textContent = '获取配置失败：' + e.message;
  }
  document.getElementById('mcpModal').classList.add('open');
};
document.getElementById('copyStdioMcpBtn').onclick = async () => {
  await navigator.clipboard.writeText(document.getElementById('mcpStdioPre').textContent);
  document.getElementById('copyStdioMcpBtn').textContent = '已复制 ✓';
  setTimeout(() => { document.getElementById('copyStdioMcpBtn').textContent = '复制 JSON'; }, 1500);
};
document.getElementById('copyHttpMcpBtn').onclick = async () => {
  await navigator.clipboard.writeText(document.getElementById('mcpInfoPre').textContent);
  document.getElementById('copyHttpMcpBtn').textContent = '已复制 ✓';
  setTimeout(() => { document.getElementById('copyHttpMcpBtn').textContent = '复制 JSON'; }, 1500);
};
document.getElementById('closeMcpBtn').onclick = () => {
  document.getElementById('mcpModal').classList.remove('open');
};

(async () => {
  await loadHealthAndProfile();
  await loadCases();
  renderChat();
  renderInspector();
})();
</script>
</body>
</html>
"""
