"""Self-contained offline HTML/CSS/JS template for PCB Workbench 2.0."""
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
    grid-template-columns: 248px 1fr 380px;
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
  .brand { padding: 15px 16px; border-bottom: 1px solid var(--border); }
  .brand h1 { margin: 0 0 4px 0; font-size: 15.5px; font-weight: 700; display: flex; align-items: center; justify-content: space-between; }
  .brand .sub { font-size: 11.5px; color: var(--muted); word-break: break-all; }
  .side-actions {
    padding: 10px 14px; display: flex; gap: 6px; border-bottom: 1px solid var(--border);
  }
  .btn {
    border: 1px solid var(--border); background: var(--panel); color: var(--text);
    padding: 6px 11px; border-radius: 8px; cursor: pointer; font-size: 12.5px;
    transition: background .15s, border-color .15s;
  }
  .btn:hover { background: var(--panel-alt); border-color: #cbd5e1; }
  .btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
  .btn.primary:hover { background: var(--accent-hover); }
  .btn.danger { color: var(--danger); border-color: #fecaca; }
  .btn.danger:hover { background: var(--danger-bg); }
  .btn.sm { padding: 3px 8px; font-size: 11.5px; }
  .btn:disabled { opacity: 0.55; cursor: not-allowed; }
  .case-list { flex: 1; overflow-y: auto; padding: 8px; }
  .case-item {
    padding: 9px 11px; border-radius: 8px; cursor: pointer; margin-bottom: 4px;
    border: 1px solid transparent;
  }
  .case-item:hover { background: var(--panel-alt); }
  .case-item.active { background: var(--accent-soft); border-color: #bfdbfe; }
  .case-item .cid { font-weight: 600; font-size: 12.5px; display: flex; justify-content: space-between; }
  .case-item .ctitle {
    font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px;
  }
  .side-footer {
    padding: 10px 14px; border-top: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 6px;
  }
  .main { display: flex; flex-direction: column; overflow: hidden; background: var(--bg); }
  .topbar {
    background: var(--panel); border-bottom: 1px solid var(--border);
    padding: 9px 16px; display: flex; align-items: center; justify-content: space-between; gap: 10px;
    flex-wrap: wrap;
  }
  .topbar-left { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
  .topbar-title { font-weight: 600; font-size: 14.5px; }
  .badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 8px; border-radius: 999px; font-size: 11.5px; font-weight: 500;
    background: var(--panel-alt); color: var(--muted); border: 1px solid var(--border);
  }
  .badge.ok { background: var(--ok-bg); color: var(--ok); border-color: #bbf7d0; }
  .badge.warn { background: var(--warn-bg); color: var(--warn); border-color: #fde68a; }
  .badge.danger { background: var(--danger-bg); color: var(--danger); border-color: #fecaca; }
  .seg {
    display: inline-flex; background: var(--panel-alt); border: 1px solid var(--border);
    border-radius: 8px; padding: 2px;
  }
  .seg button {
    border: none; background: transparent; padding: 4px 10px; border-radius: 6px;
    cursor: pointer; font-size: 12px; color: var(--muted); font-weight: 600;
  }
  .seg button.active {
    background: var(--panel); color: var(--accent); box-shadow: 0 1px 2px rgba(0,0,0,.08);
  }
  .chat-scroll, .search-pane {
    flex: 1; overflow-y: auto; padding: 18px 22px;
  }
  .search-pane { display: none; }
  .search-bar {
    max-width: 840px; margin: 0 auto 16px auto; display: flex; gap: 8px; flex-wrap: wrap;
    background: var(--panel); padding: 12px; border-radius: 12px; border: 1px solid var(--border);
  }
  .search-bar input {
    flex: 1; min-width: 220px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 13.5px;
  }
  .search-bar select {
    padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; background: var(--panel);
  }
  .search-results { max-width: 840px; margin: 0 auto; }
  .search-hit {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px 14px; margin-bottom: 10px;
  }
  .search-hit:hover { border-color: #93c5fd; }
  .empty-state { max-width: 680px; margin: 40px auto; text-align: center; }
  .empty-state h2 { margin: 0 0 8px 0; font-size: 19px; }
  .empty-state p { margin: 0 0 18px 0; color: var(--muted); }
  .sample-grid { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
  .sample-chip, .quick-chip {
    background: var(--panel); border: 1px solid var(--border); border-radius: 999px;
    padding: 5px 11px; cursor: pointer; font-size: 12px; color: var(--text); text-align: left;
  }
  .sample-chip { border-radius: 10px; padding: 8px 12px; font-size: 12.5px; }
  .sample-chip:hover, .quick-chip:hover { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }
  .msg { max-width: 840px; margin: 0 auto 16px auto; display: flex; flex-direction: column; }
  .msg.user { align-items: flex-end; }
  .msg.assistant { align-items: flex-start; }
  .bubble { padding: 12px 16px; border-radius: 12px; max-width: 100%; word-break: break-word; }
  .msg.user .bubble {
    background: var(--accent); color: #fff; border-bottom-right-radius: 4px; white-space: pre-wrap;
  }
  .msg.assistant .bubble {
    background: var(--panel); border: 1px solid var(--border); border-bottom-left-radius: 4px; width: 100%;
  }
  .md h2, .md h3 { margin: 12px 0 6px 0; font-size: 14.5px; border-bottom: 1px solid #f1f5f9; padding-bottom: 4px; }
  .md h2:first-child, .md h3:first-child { margin-top: 0; }
  .md p { margin: 6px 0; }
  .md ul { margin: 6px 0; padding-left: 20px; }
  .md li { margin: 4px 0; }
  .cite-pill {
    display: inline-block; padding: 1px 6px; margin: 0 2px; border-radius: 5px;
    background: var(--accent-soft); color: var(--accent); border: 1px solid #bfdbfe;
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 11px;
    cursor: pointer;
  }
  .cite-pill:hover { background: #dbeafe; }
  .meta-bar {
    margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border);
    font-size: 12px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 6px; align-items: center; justify-content: space-between;
  }
  .trace-box {
    margin-top: 8px; background: var(--panel-alt); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 10px; font-size: 12px; font-family: ui-monospace, Consolas, monospace;
  }
  .live-steps {
    margin-top: 8px; display: flex; flex-direction: column; gap: 4px; font-size: 12.5px; color: var(--muted);
  }
  .live-step { display: flex; align-items: center; gap: 6px; }
  .live-step .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); display: inline-block; }
  .composer {
    background: var(--panel); border-top: 1px solid var(--border); padding: 11px 18px;
  }
  .composer-inner {
    max-width: 840px; margin: 0 auto; display: flex; gap: 10px; align-items: flex-end;
  }
  .composer textarea {
    flex: 1; resize: none; min-height: 44px; max-height: 150px; padding: 9px 12px;
    border: 1px solid var(--border); border-radius: 10px; font-family: inherit; font-size: 13.5px; outline: none;
  }
  .composer textarea:focus { border-color: var(--accent); }
  .tabs { display: flex; border-bottom: 1px solid var(--border); background: var(--panel-alt); }
  .tabs button {
    flex: 1; padding: 10px 4px; border: none; background: transparent; cursor: pointer;
    font-size: 12px; font-weight: 600; color: var(--muted); border-bottom: 2px solid transparent;
  }
  .tabs button.active { color: var(--accent); border-bottom-color: var(--accent); background: var(--panel); }
  .insp-body { flex: 1; overflow-y: auto; padding: 12px; }
  .sop-section {
    border: 1px solid var(--border); border-radius: 9px; padding: 10px 11px; margin-bottom: 10px; background: var(--panel-alt);
  }
  .sop-section h4 { margin: 0 0 7px 0; font-size: 12.5px; color: var(--text); display: flex; justify-content: space-between; }
  .fact-grid { display: flex; flex-wrap: wrap; gap: 5px; }
  .hyp-row {
    background: var(--panel); border: 1px solid var(--border); border-radius: 7px; padding: 8px 9px; margin-bottom: 6px;
  }
  .hyp-bar-bg { height: 5px; background: #e2e8f0; border-radius: 99px; margin-top: 5px; overflow: hidden; }
  .hyp-bar-fill { height: 100%; background: var(--accent); border-radius: 99px; }
  .hyp-row.damped { opacity: 0.78; border-style: dashed; }
  .hyp-row.damped .hyp-bar-fill { background: var(--warn); }
  .cite-card {
    border: 1px solid var(--border); border-radius: 8px; padding: 10px; margin-bottom: 9px; background: var(--panel-alt);
  }
  .cite-card.highlight { border-color: var(--accent); background: var(--accent-soft); }
  .cite-card .chead {
    font-size: 11.5px; font-weight: 600; color: var(--accent); font-family: ui-monospace, Consolas, monospace;
    display: flex; justify-content: space-between; align-items: center; gap: 6px;
  }
  .cite-card .ctitle { font-size: 12px; color: var(--muted); margin: 4px 0; }
  .cite-card .ctext { font-size: 12.5px; white-space: pre-wrap; color: var(--text); }
  .reader-box {
    background: var(--panel-alt); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px; font-size: 12.5px; white-space: pre-wrap; max-height: 360px; overflow-y: auto;
  }
  .note-form { display: flex; gap: 6px; margin-bottom: 10px; }
  .note-form input {
    flex: 1; padding: 7px 9px; border: 1px solid var(--border); border-radius: 8px; font-size: 12.5px;
  }
  .note-item {
    padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; margin-bottom: 7px;
    background: var(--panel-alt); font-size: 12.5px;
  }
  .note-item .ts { font-size: 11px; color: var(--muted); margin-bottom: 2px; }
  .modal-mask {
    position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45);
    display: none; align-items: center; justify-content: center; z-index: 50;
  }
  .modal-mask.open { display: flex; }
  .modal {
    background: var(--panel); border-radius: 12px; width: 560px; max-width: 94vw;
    max-height: 90vh; overflow-y: auto; padding: 20px; box-shadow: 0 20px 50px rgba(0,0,0,.2);
  }
  .modal h3 { margin: 0 0 12px 0; font-size: 16px; }
  .field { margin-bottom: 11px; }
  .field label { display: block; font-size: 12px; font-weight: 600; color: var(--muted); margin-bottom: 4px; }
  .field input[type="text"], .field input[type="password"], .field select, .field textarea {
    width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px; font-family: inherit;
  }
  .field.row { display: flex; align-items: center; gap: 8px; }
  .field.row label { margin: 0; color: var(--text); font-weight: 500; }
  .modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
  .hint { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .spinner { display: inline-block; width: 13px; height: 13px; border: 2px solid #cbd5e1; border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; vertical-align: middle; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  @media (max-width: 1120px) {
    .app { grid-template-columns: 220px 1fr; }
    .inspector { display: none; }
  }
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <h1><span>PCB 压合排查工作台</span><span class="badge">2.0</span></h1>
      <div class="sub" id="catalogInfo">正在连接本机服务…</div>
    </div>
    <div class="side-actions">
      <button class="btn primary" style="flex:1" id="newCaseBtn">+ 新建病例</button>
      <button class="btn" id="refreshCasesBtn" title="刷新病例列表">↻</button>
    </div>
    <div class="case-list" id="caseList"></div>
    <div class="side-footer">
      <button class="btn" id="exportReportBtn" disabled>📄 导出当前病例草稿</button>
      <button class="btn" id="openSettingsBtn">⚙ 模型渠道与脱敏配置</button>
      <button class="btn" id="openDoctorBtn">🩺 运行环境与自检</button>
      <button class="btn" id="openMcpInfoBtn">🔌 MCP / 数字员工接入</button>
      <div style="display:flex; gap:6px;">
        <button class="btn sm" style="flex:1;" id="createShortcutBtn" title="在桌面生成无黑框快捷方式">🖥 桌面图标</button>
        <button class="btn sm danger" id="shutdownBtn" title="停止本机后台工作台服务">⏻ 退出服务</button>
      </div>
    </div>
  </aside>

  <main class="main">
    <header class="topbar">
      <div class="topbar-left">
        <div class="seg" role="group" aria-label="工作台视图">
          <button id="viewChatBtn" class="active">💬 智能排查</button>
          <button id="viewSearchBtn">🔍 知识库直查</button>
        </div>
        <span class="topbar-title" id="activeCaseTitle">临时会话</span>
        <span class="badge" id="profileBadge" style="cursor:pointer;" title="点击切换或配置模型渠道">检测配置…</span>
      </div>
      <div style="display:flex; align-items:center; gap:8px;" id="chatModeControls">
        <div class="seg" role="group" aria-label="引擎模式">
          <button id="modeAgentBtn" class="active">脱敏 Agent</button>
          <button id="modeLocalBtn">纯本地离线</button>
        </div>
        <button class="btn sm" id="clearHistoryBtn">清空上下文</button>
      </div>
    </header>

    <section class="chat-scroll" id="chatScroll"></section>

    <section class="search-pane" id="searchPane">
      <form class="search-bar" id="searchForm">
        <input type="text" id="searchInput" placeholder="输入关键词毫秒级直查 7.8 万篇本地邮件、案例、叠构表、附件（如：IT-180A 白斑 / 压合曲线 / Tg）…" />
        <select id="searchDocType">
          <option value="">全部类型</option>
          <option value="CASE">CASE 案例</option>
          <option value="MAIL">MAIL 邮件</option>
          <option value="ATTACHMENT">ATTACHMENT 附件</option>
          <option value="KNOWLEDGE">KNOWLEDGE 知识单元</option>
          <option value="PUBLIC">PUBLIC 公开参考</option>
        </select>
        <button class="btn primary" type="submit">检索</button>
      </form>
      <div class="search-results" id="searchResults">
        <div class="hint" style="text-align:center; padding:30px;">不经过大模型，0.1 秒直接检索本地只读 Catalog，点击任意结果可在右侧阅读完整正文、关联附件与相似案例。</div>
      </div>
    </section>

    <footer class="composer" id="composerBar">
      <div class="composer-inner">
        <textarea id="questionInput" rows="2" placeholder="输入压合异常现象、现场补测结果、材料术语或叠构问题（Enter 发送，Shift+Enter 换行）…"></textarea>
        <button class="btn primary" id="sendBtn" style="height:44px; padding:0 18px;">发送</button>
      </div>
    </footer>
  </main>

  <aside class="inspector">
    <div class="tabs">
      <button class="active" id="tabSop">SOP 看板</button>
      <button id="tabCitations">引用 (<span id="citeCount">0</span>)</button>
      <button id="tabReader">原文阅读</button>
      <button id="tabNotes">备注/回流 (<span id="noteCount">0</span>)</button>
    </div>
    <div class="insp-body" id="inspectorBody"></div>
  </aside>
</div>

<!-- Settings Modal -->
<div class="modal-mask" id="settingsModal">
  <div class="modal" style="width:580px;">
    <h3>模型渠道与脱敏出境配置</h3>
    <div class="hint" style="margin-bottom:12px;">配置统一保存在本机 <code>.private/agent_profile.json</code>（Web、CLI 与 MCP 服务自动共享），密钥绝不回显明文。</div>
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
      <label for="cfgLocalHttp">允许连接本机 HTTP 模型服务（如 Ollama/vLLM 127.0.0.1）</label>
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

<!-- Feedback / Knowledge Reflow Modal -->
<div class="modal-mask" id="feedbackModal">
  <div class="modal">
    <h3>📝 现场结论沉淀 / 回答纠错回流</h3>
    <div class="hint" style="margin-bottom:12px;">
      记录将绑定本轮回答指纹并加密锁写入本机 <code>.private/internal_pilot_feedback/</code>（绝不篡改只读知识库），供后续规则迭代与经验审核。
    </div>
    <div class="field">
      <label>反馈类型</label>
      <select id="fbKind">
        <option value="other">经验回流：记录现场确认根因与有效对策</option>
        <option value="wrong_conclusion">结论纠偏：候选原因排序或判断有偏差</option>
        <option value="missing_context">条件补充：遗漏了关键设备/叠构/工艺前提</option>
        <option value="wrong_citation">引用纠错：引用的摘录上下文不匹配</option>
        <option value="wrong_retrieval">检索漏召：未查到库内已知相关邮件/表格</option>
      </select>
    </div>
    <div class="field">
      <label>工程师核实结论 / 纠错说明</label>
      <textarea id="fbText" rows="4" placeholder="例如：经切片与压机真空表复核，实际原因为板边流胶过快导致局部树脂饥饿，调整升温速率后验证改善。"></textarea>
    </div>
    <div class="modal-foot">
      <button class="btn" type="button" id="closeFbBtn">取消</button>
      <button class="btn primary" type="button" id="submitFbBtn">写入私有经验账本</button>
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

<!-- MCP Modal -->
<div class="modal-mask" id="mcpModal">
  <div class="modal" style="width:600px;">
    <h3>MCP / 数字员工一键接入配置</h3>
    <div class="hint" style="margin-bottom:10px;">
      <code>phase6_mcp.py</code> 已自动共享本机保存的模型渠道配置，外部客户端<strong>无需写入明文 API Key</strong>：
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
  view: 'chat', // 'chat' | 'search'
  mode: 'agent',
  activeCaseId: null,
  activeCase: null,
  ephemeralTurns: [],
  ephemeralHistory: [],
  lastCitations: [],
  lastMissing: [],
  lastClarifying: [],
  lastSop: null,
  readerDoc: null,
  readerLoading: false,
  feedbackStats: null,
  feedbackTargetTurn: null,
  activeTab: 'sop', // 'sop' | 'citations' | 'reader' | 'notes'
  highlightedCite: null,
  sending: false,
  liveEvents: [],
  health: null,
  profile: null,
};

const SAMPLES = [
  '8层板 IT-180A 压合后板边出现白斑，真空曲线正常，怎么排查？',
  '压合后板边分层和起泡怎么区分排查方向？',
  'Tg 和 Td 在材料表里分别指什么？Tg 能否直接当作卸压温度？',
  '本地知识库有多少文档？',
];

const FACT_LABELS = {
  stage: '工序阶段', location: '发生位置', material: '板材型号', stackup: '层数/叠构',
  equipment: '压机/设备', batch: '批次范围', timing: '发生时机',
};

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
      `版本 ${state.health.catalog_version || 'unknown'} · 工具箱=${state.health.toolset}`;
    state.profile = await api('/api/profile');
    updateProfileBadge();
    state.feedbackStats = await api('/api/feedback').catch(() => null);
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
    badge.textContent = `渠道 [${ch}] · 纯本地离线（点击配置模型）`;
  }
}

function switchView(view) {
  state.view = view;
  document.getElementById('viewChatBtn').className = view === 'chat' ? 'active' : '';
  document.getElementById('viewSearchBtn').className = view === 'search' ? 'active' : '';
  document.getElementById('chatScroll').style.display = view === 'chat' ? 'block' : 'none';
  document.getElementById('composerBar').style.display = view === 'chat' ? 'block' : 'none';
  document.getElementById('chatModeControls').style.display = view === 'chat' ? 'flex' : 'none';
  document.getElementById('searchPane').style.display = view === 'search' ? 'block' : 'none';
  if (view === 'search') document.getElementById('searchInput').focus();
}

async function openDocumentReader(docId) {
  if (!docId) return;
  state.activeTab = 'reader';
  state.readerLoading = true;
  renderInspector();
  try {
    state.readerDoc = await api('/api/documents/' + encodeURIComponent(docId));
  } catch (e) {
    state.readerDoc = { error: e.message, doc_id: docId };
  } finally {
    state.readerLoading = false;
    renderInspector();
  }
}

function prefillFollowup(promptText) {
  switchView('chat');
  const inp = document.getElementById('questionInput');
  const clean = String(promptText || '').replace(/[？?]$/, '');
  inp.value = `补充现场情况（关于「${clean}」）：`;
  inp.focus();
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
    document.getElementById('activeCaseTitle').textContent = `病例：${state.activeCase.case_id}`;
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
    state.lastClarifying = last.clarifying_questions || [];
    state.lastSop = last.sop || null;
  } else {
    state.lastCitations = [];
    state.lastMissing = [];
    state.lastClarifying = [];
    state.lastSop = null;
  }
}

function renderChat() {
  const box = document.getElementById('chatScroll');
  const turns = currentTurns();
  if (!turns.length && !state.sending) {
    box.innerHTML = `
      <div class="empty-state">
        <h2>PCB 压合知识库与受控排查数字员工</h2>
        <p>实时展示查证轨迹、SOP 候选阻尼看板与原文附件阅读器；所有结论强制校验 EXC 引用。</p>
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
  turns.forEach((turn, idx) => {
    const u = document.createElement('div');
    u.className = 'msg user';
    u.innerHTML = `<div class="bubble">${esc(turn.question)}</div>`;
    box.appendChild(u);

    const a = document.createElement('div');
    a.className = 'msg assistant';
    const trace = turn.agent_trace || [];
    const traceHtml = trace.length
      ? `<details style="margin-top:8px;"><summary style="cursor:pointer;font-size:12px;color:var(--muted);">查证轨迹（${trace.length} 次只读工具调用）</summary>
         <div class="trace-box">${trace.map(t => `${esc(t.tool)} · ${esc(t.status)} · +${t.evidence_added || 0}摘录 · ${t.elapsed_ms || 0}ms`).join('<br/>')}</div></details>`
      : '';

    const quickPrompts = [
      ...(turn.clarifying_questions || []),
      ...(turn.missing_information || []).map(m => typeof m === 'string' ? m : (m.prompt || '')),
    ].filter(Boolean).slice(0, 4);
    const chipsHtml = (idx === turns.length - 1 && quickPrompts.length)
      ? `<div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;">
           <span style="font-size:11.5px;color:var(--muted);align-self:center;">快捷补录追问：</span>
           ${quickPrompts.map(p => `<button type="button" class="quick-chip" data-followup="${esc(p)}">+ ${esc(p.slice(0, 28))}${p.length > 28 ? '…' : ''}</button>`).join('')}
         </div>`
      : '';

    const badges = [
      `路由: ${turn.route || '-'}`,
      `模式: ${turn.answer_mode || '-'}`,
      `出境: ${turn.data_egress || 'none'}`,
    ];
    if (turn.elapsed_ms) badges.push(`${(turn.elapsed_ms / 1000).toFixed(1)}s`);
    if (turn.total_tokens) badges.push(`${turn.total_tokens} tok`);
    if (turn.fallback_reason) badges.push(`回退: ${turn.fallback_reason}`);

    const canFeedback = /^[0-9a-f]{64}$/.test(turn.result_fingerprint || '');
    a.innerHTML = `<div class="bubble">
        ${renderMarkdown(turn.answer)}
        ${chipsHtml}
        ${traceHtml}
        <div class="meta-bar">
          <div style="display:flex;flex-wrap:wrap;gap:5px;">${badges.map(b => `<span class="badge">${esc(b)}</span>`).join('')}</div>
          ${canFeedback ? `<button type="button" class="btn sm fb-trigger" data-turn-idx="${idx}">📝 纠错 / 沉淀结论</button>` : ''}
        </div>
      </div>`;
    a.querySelectorAll('.cite-pill').forEach(pill => {
      pill.onclick = () => {
        state.lastCitations = turn.citations || state.lastCitations;
        state.activeTab = 'citations';
        state.highlightedCite = pill.dataset.cite;
        renderInspector();
      };
    });
    a.querySelectorAll('.quick-chip').forEach(chip => {
      chip.onclick = () => prefillFollowup(chip.dataset.followup);
    });
    a.querySelectorAll('.fb-trigger').forEach(btn => {
      btn.onclick = () => openFeedbackModal(turns[Number(btn.dataset.turnIdx)]);
    });
    box.appendChild(a);
  });

  if (state.sending) {
    const wait = document.createElement('div');
    wait.className = 'msg assistant';
    const stepsHtml = state.liveEvents.map(ev =>
      `<div class="live-step"><span class="dot"></span><span>${esc(ev.message || ev.tool || '')}</span></div>`
    ).join('');
    wait.innerHTML = `<div class="bubble">
      <div><span class="spinner"></span><strong>正在执行受控查证与引用校验…</strong></div>
      <div class="live-steps" id="liveStepBox">${stepsHtml}</div>
    </div>`;
    box.appendChild(wait);
  }
  box.scrollTop = box.scrollHeight;
}

function renderInspector() {
  document.getElementById('citeCount').textContent = state.lastCitations.length;
  const notes = state.activeCase ? (state.activeCase.notes || []) : [];
  const fbTotal = ((state.feedbackStats || {}).summary || {}).total_feedback || 0;
  document.getElementById('noteCount').textContent = notes.length + fbTotal;

  for (const [id, tab] of [['tabSop','sop'],['tabCitations','citations'],['tabReader','reader'],['tabNotes','notes']]) {
    document.getElementById(id).className = state.activeTab === tab ? 'active' : '';
  }
  const body = document.getElementById('inspectorBody');

  if (state.activeTab === 'sop') {
    let html = '';
    const sop = state.lastSop;
    if (sop) {
      const facts = sop.facts || {};
      const factBadges = [];
      if (sop.defect_label) factBadges.push(`<span class="badge danger">缺陷: ${esc(sop.defect_label)}</span>`);
      for (const [k, label] of Object.entries(FACT_LABELS)) {
        if (facts[k]) factBadges.push(`<span class="badge">${esc(label)}: ${esc(typeof facts[k] === 'object' ? JSON.stringify(facts[k]) : facts[k])}</span>`);
      }
      for (const obs of (facts.normal_observations || [])) {
        factBadges.push(`<span class="badge ok">已排除/正常: ${esc(obs)}</span>`);
      }
      html += `<div class="sop-section">
        <h4><span>已提取病例事实</span><span class="badge">SOP</span></h4>
        <div class="fact-grid">${factBadges.join('') || '<span class="hint">待补充批次与材料条件</span>'}</div>
      </div>`;

      if (sop.hypotheses && sop.hypotheses.length) {
        const maxScore = Math.max(...sop.hypotheses.map(h => Number(h.score) || 1), 1);
        html += `<div class="sop-section"><h4><span>候选原因评估（非最终定论）</span></h4>`;
        for (const h of sop.hypotheses) {
          const damped = h.contradicting && h.contradicting.length > 0;
          const pct = Math.max(12, Math.min(100, Math.round(((Number(h.score) || 0.3) / maxScore) * 100)));
          html += `<div class="hyp-row${damped ? ' damped' : ''}">
            <div style="display:flex;justify-content:space-between;font-size:12.5px;font-weight:600;">
              <span>#${esc(h.rank)} ${esc(h.cause_label || h.cause_id)}</span>
              <span class="badge ${damped ? 'warn' : ''}">${damped ? '已被正常观测降权' : esc(h.evidence_strength || '')}</span>
            </div>
            ${damped ? `<div class="hint" style="color:var(--warn);">反证阻尼：${esc(h.contradicting.join('；'))}</div>` : ''}
            <div class="hyp-bar-bg"><div class="hyp-bar-fill" style="width:${pct}%"></div></div>
          </div>`;
        }
        html += `</div>`;
      }
    }
    const missingList = [
      ...(state.lastClarifying || []),
      ...(state.lastMissing || []).map(m => typeof m === 'string' ? m : (m.prompt || m.code || '')),
    ].filter(Boolean);
    if (missingList.length) {
      html += `<div class="sop-section">
        <h4><span>待核实 SOP 清单（点击快速补录）</span></h4>
        <div style="display:flex;flex-direction:column;gap:6px;">
          ${missingList.map(m => `<button type="button" class="quick-chip" data-sop-fill="${esc(m)}">+ ${esc(m)}</button>`).join('')}
        </div>
      </div>`;
    }
    body.innerHTML = html || '<div class="hint" style="padding:20px;text-align:center;">发起缺陷排查提问后，此处将实时展示已锁定板材事实、反证降权候选条与交互式待核实清单。</div>';
    body.querySelectorAll('[data-sop-fill]').forEach(btn => {
      btn.onclick = () => prefillFollowup(btn.dataset.sopFill);
    });
    return;
  }

  if (state.activeTab === 'citations') {
    if (!state.lastCitations.length) {
      body.innerHTML = '<div class="hint" style="padding:20px;text-align:center;">暂无引用摘录。点击回答中的 EXC 编号可在此定位原文。</div>';
      return;
    }
    body.innerHTML = state.lastCitations.map(c => {
      const id = c.excerpt_id || c.citation_id || '';
      const hl = state.highlightedCite === id ? ' highlight' : '';
      const docBtn = c.doc_id
        ? `<button type="button" class="btn sm open-doc-btn" data-doc="${esc(c.doc_id)}">📖 全文与附件</button>`
        : '';
      return `<div class="cite-card${hl}" id="card_${esc(id)}">
        <div class="chead"><span>${esc(id)}</span><span>${esc(c.source_type || '')}</span></div>
        <div class="ctitle">${esc(c.title || '未命名来源')}</div>
        <div class="ctext">${esc(c.text || '（本条历史归档仅保留引用标识，点击下方按钮读取全文）')}</div>
        ${docBtn ? `<div style="margin-top:7px;display:flex;justify-content:flex-end;">${docBtn}</div>` : ''}
      </div>`;
    }).join('');
    body.querySelectorAll('.open-doc-btn').forEach(btn => {
      btn.onclick = () => openDocumentReader(btn.dataset.doc);
    });
    if (state.highlightedCite) {
      const target = document.getElementById('card_' + state.highlightedCite);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    return;
  }

  if (state.activeTab === 'reader') {
    if (state.readerLoading) {
      body.innerHTML = '<div style="padding:20px;"><span class="spinner"></span>正在读取只读文档全文、关联附件与相似案例…</div>';
      return;
    }
    if (!state.readerDoc) {
      body.innerHTML = '<div class="hint" style="padding:20px;text-align:center;">在「引用」卡片或「🔍 知识库直查」结果中点击「📖 全文与附件」，即可在此阅读完整文档、附件解析内容与上下游关联图谱。</div>';
      return;
    }
    if (state.readerDoc.error) {
      body.innerHTML = `<div class="note-item" style="color:var(--danger);">读取失败：${esc(state.readerDoc.error)}</div>`;
      return;
    }
    const d = state.readerDoc.document || {};
    const atts = d.attachments || [];
    const rels = ((state.readerDoc.related || {}).relationships) || [];
    const sims = ((state.readerDoc.similar_cases || {}).results) || [];
    let html = `<div class="sop-section">
      <div style="font-size:11px;font-family:monospace;color:var(--accent);">${esc(d.doc_id)} · ${esc(d.type || '')}</div>
      <div style="font-weight:700;margin:4px 0 8px 0;">${esc(d.title || '无标题文档')}</div>
      <div class="reader-box">${esc(d.text || '（无正文文本）')}</div>
    </div>`;
    if (atts.length) {
      html += `<div class="sop-section"><h4><span>关联附件解析 (${atts.length})</span></h4>`;
      for (const att of atts) {
        html += `<details style="margin-bottom:6px;"><summary style="cursor:pointer;font-size:12px;font-weight:600;">📎 ${esc(att.title || att.doc_id)}</summary>
          <div class="reader-box" style="margin-top:5px;">${esc(att.text || '（无文本）')}</div></details>`;
      }
      html += `</div>`;
    }
    if (sims.length) {
      html += `<div class="sop-section"><h4><span>相似历史案例 (${sims.length})</span></h4>`;
      for (const s of sims) {
        html += `<div class="note-item" style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
          <div style="min-width:0;"><div style="font-weight:600;font-size:12px;">${esc(s.title || s.doc_id)}</div></div>
          <button type="button" class="btn sm jump-doc-btn" data-doc="${esc(s.doc_id)}">查看</button>
        </div>`;
      }
      html += `</div>`;
    }
    if (rels.length) {
      html += `<div class="sop-section"><h4><span>上下游关系链 (${rels.length})</span></h4>`;
      for (const r of rels.slice(0, 10)) {
        const otherId = r.from_id === d.doc_id ? r.to_id : r.from_id;
        html += `<div class="note-item" style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
          <span style="font-size:11.5px;font-family:monospace;">${esc(r.relation)} → ${esc(otherId.slice(0, 20))}…</span>
          <button type="button" class="btn sm jump-doc-btn" data-doc="${esc(otherId)}">跳转</button>
        </div>`;
      }
      html += `</div>`;
    }
    body.innerHTML = html;
    body.querySelectorAll('.jump-doc-btn').forEach(btn => {
      btn.onclick = () => openDocumentReader(btn.dataset.doc);
    });
    return;
  }

  // Notes & feedback ledger tab
  let html = '';
  if (state.activeCase) {
    html += `<form class="note-form" id="noteForm">
      <input type="text" id="noteInput" placeholder="记录现场观测、切片或批次备注…" />
      <button class="btn primary sm" type="submit">添加</button>
    </form>`;
    if (notes.length) {
      html += '<div style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:6px;">当前病例现场备注</div>';
      html += notes.map(n => `<div class="note-item"><div class="ts">${esc(n.timestamp)}</div><div>${esc(n.text)}</div></div>`).join('');
    }
  } else {
    html += '<div class="hint" style="margin-bottom:12px;">新建或打开一个病例后可持久化记录现场备注并导出 Markdown 排查草稿。</div>';
  }
  const fbSummary = (state.feedbackStats || {}).summary || {};
  const fbItems = (state.feedbackStats || {}).items || [];
  html += `<div class="sop-section" style="margin-top:10px;">
    <h4><span>本机私有经验回流账本</span><span class="badge ok">${fbSummary.total_feedback || 0} 条</span></h4>
    <div class="hint">在任意回答右下角点击「📝 纠错 / 沉淀结论」即可将现场验证结果记入待审账本。</div>
    ${fbItems.slice(0, 5).map(item => `<div class="note-item" style="margin-top:6px;">
      <div class="ts">${esc(item.created_at || '')} · ${esc(item.kind)}</div>
      <div style="font-family:monospace;font-size:11.5px;">${esc(item.feedback_id)} (指纹 ${esc(item.turn_fingerprint_prefix || '')})</div>
    </div>`).join('')}
  </div>`;
  body.innerHTML = html;
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
  state.liveEvents = [{ message: '提交问题至本机工作台…' }];
  document.getElementById('sendBtn').disabled = true;
  renderChat();

  const payload = {
    question: q,
    mode: state.mode,
    case_id: state.activeCaseId,
    history: state.activeCaseId ? undefined : state.ephemeralHistory.slice(-8),
  };

  try {
    const res = await fetch('/api/answer/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const errJson = await res.json().catch(() => ({}));
      throw new Error(errJson.error || ('HTTP ' + res.status));
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    let finalResp = null;

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sepIdx;
      while ((sepIdx = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, sepIdx);
        buffer = buffer.slice(sepIdx + 2);
        let evName = 'message';
        let dataStr = '';
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) evName = line.slice(6).trim();
          else if (line.startsWith('data:')) dataStr += line.slice(5).trim();
        }
        if (!dataStr) continue;
        const parsed = JSON.parse(dataStr);
        if (evName === 'step') {
          state.liveEvents.push(parsed);
          const stepBox = document.getElementById('liveStepBox');
          if (stepBox) {
            stepBox.innerHTML = state.liveEvents.map(ev =>
              `<div class="live-step"><span class="dot"></span><span>${esc(ev.message || ev.tool || '')}</span></div>`
            ).join('');
          }
        } else if (evName === 'done') {
          finalResp = parsed;
        } else if (evName === 'error') {
          throw new Error(parsed.error || 'stream_error');
        }
      }
    }

    if (!finalResp) throw new Error('empty_stream_response');
    const r = finalResp.result || {};
    const turn = {
      question: q,
      status: finalResp.status,
      route: (r.workflow || {}).route,
      answer_mode: r.answer_mode,
      evidence_status: r.evidence_status,
      data_egress: finalResp.data_egress,
      answer: r.answer || '',
      citations: r.citations || [],
      missing_information: r.missing_information || [],
      clarifying_questions: r.clarifying_questions || [],
      sop: finalResp.sop || null,
      agent_trace: r.agent_trace || [],
      result_fingerprint: r.result_fingerprint || null,
      elapsed_ms: finalResp.elapsed_ms,
      total_tokens: ((r.model || {}).usage || {}).total_tokens || 0,
      fallback_reason: (r.model || {}).fallback_reason || null,
    };
    if (state.activeCaseId) {
      state.activeCase = await api('/api/cases/' + encodeURIComponent(state.activeCaseId));
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
    state.lastClarifying = turn.clarifying_questions;
    state.lastSop = turn.sop;
    if (turn.sop) state.activeTab = 'sop';
    state.highlightedCite = null;
  } catch (e) {
    alert('请求失败：' + e.message);
  } finally {
    state.sending = false;
    state.liveEvents = [];
    document.getElementById('sendBtn').disabled = false;
    renderChat();
    renderInspector();
  }
}

// Direct search pane
document.getElementById('searchForm').onsubmit = async (e) => {
  e.preventDefault();
  const q = document.getElementById('searchInput').value.trim();
  const dt = document.getElementById('searchDocType').value;
  const box = document.getElementById('searchResults');
  box.innerHTML = '<div style="padding:20px;"><span class="spinner"></span>正在检索本地 Catalog…</div>';
  try {
    const params = new URLSearchParams({ q, limit: '25' });
    if (dt) params.set('doc_type', dt);
    const data = await api('/api/search?' + params.toString());
    const items = data.results || [];
    if (!items.length) {
      box.innerHTML = '<div class="hint" style="padding:24px;text-align:center;">未找到匹配记录，请尝试放宽关键词或切换文档类型。</div>';
      return;
    }
    box.innerHTML = `<div class="hint" style="margin-bottom:10px;">共命中 ${data.total || items.length} 篇文档（展示前 ${items.length} 条）：</div>`
      + items.map(it => `<div class="search-hit">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
          <div>
            <span class="badge">${esc(it.type || '')}</span>
            <strong style="margin-left:6px;">${esc(it.title || it.doc_id)}</strong>
          </div>
          <div style="display:flex;gap:6px;">
            <button type="button" class="btn sm hit-read-btn" data-doc="${esc(it.doc_id)}">📖 阅读全文与附件</button>
            <button type="button" class="btn sm hit-ask-btn" data-doc="${esc(it.doc_id)}">💬 追问此文档</button>
          </div>
        </div>
        <div style="font-size:12px;color:var(--muted);margin-top:6px;">${esc(it.snippet || it.summary || it.doc_id)}</div>
      </div>`).join('');
    box.querySelectorAll('.hit-read-btn').forEach(btn => {
      btn.onclick = () => openDocumentReader(btn.dataset.doc);
    });
    box.querySelectorAll('.hit-ask-btn').forEach(btn => {
      btn.onclick = () => {
        switchView('chat');
        document.getElementById('questionInput').value = `读取文档及附件 ${btn.dataset.doc}`;
        sendQuestion();
      };
    });
  } catch (err) {
    box.innerHTML = `<div style="color:var(--danger);padding:16px;">检索失败：${esc(err.message)}</div>`;
  }
};

// Feedback modal
function openFeedbackModal(turn) {
  state.feedbackTargetTurn = turn;
  document.getElementById('fbText').value = '';
  document.getElementById('feedbackModal').classList.add('open');
}
document.getElementById('closeFbBtn').onclick = () => {
  document.getElementById('feedbackModal').classList.remove('open');
};
document.getElementById('submitFbBtn').onclick = async () => {
  const turn = state.feedbackTargetTurn;
  if (!turn) return;
  const text = document.getElementById('fbText').value.trim();
  if (!text) { alert('请填写核实结论或纠错说明。'); return; }
  try {
    const receipt = await api('/api/feedback', {
      method: 'POST',
      body: JSON.stringify({
        kind: document.getElementById('fbKind').value,
        text,
        turn: {
          result_fingerprint: turn.result_fingerprint,
          answer_mode: turn.answer_mode,
          evidence_status: turn.evidence_status,
          citations: (turn.citations || []).map(c => ({ excerpt_id: c.excerpt_id || c.citation_id })),
        },
      }),
    });
    state.feedbackStats = await api('/api/feedback').catch(() => state.feedbackStats);
    document.getElementById('feedbackModal').classList.remove('open');
    state.activeTab = 'notes';
    renderInspector();
    alert(`已写入本机私有回流账本：${receipt.feedback_id}`);
  } catch (e) {
    alert('提交失败：' + e.message);
  }
};

// Desktop shortcut & graceful shutdown
document.getElementById('createShortcutBtn').onclick = async () => {
  try {
    const res = await api('/api/desktop-shortcut', { method: 'POST', body: '{}' });
    alert('已在桌面创建无黑框快捷方式：\n' + res.shortcut_path);
  } catch (e) {
    alert('创建快捷方式失败：' + e.message);
  }
};
document.getElementById('shutdownBtn').onclick = async () => {
  if (!confirm('确定停止本机后台工作台服务吗？')) return;
  try {
    await api('/api/shutdown', { method: 'POST', body: '{}' });
    document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;font-family:sans-serif;color:#475569;"><h2>PCB 压合排查工作台服务已安全停止，可直接关闭此窗口。</h2></div>';
    window.close();
  } catch (e) {
    alert('停止失败：' + e.message);
  }
};

// Bindings
document.getElementById('viewChatBtn').onclick = () => switchView('chat');
document.getElementById('viewSearchBtn').onclick = () => switchView('search');
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
document.getElementById('tabSop').onclick = () => { state.activeTab = 'sop'; renderInspector(); };
document.getElementById('tabCitations').onclick = () => { state.activeTab = 'citations'; renderInspector(); };
document.getElementById('tabReader').onclick = () => { state.activeTab = 'reader'; renderInspector(); };
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

// Settings modal
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
    opt.textContent = item.label && item.label !== item.name ? `${item.name} — ${item.label}` : item.name;
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
    ? `状态：渠道 [${editingProfileName}] 已保存密钥（或环境变量）；留空则保持不变。`
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
  const view = await api('/api/profile?name=' + encodeURIComponent(e.target.value));
  fillSettingsForm(view);
};
document.getElementById('cfgPresetSelect').onchange = (e) => {
  const pr = ((state.profile || {}).presets || []).find(x => x.id === e.target.value);
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
  const raw = prompt('输入新渠道标识（字母/数字/下划线/短横线）：', 'channel_2');
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
};
document.getElementById('deleteProfileBtn').onclick = async () => {
  if (!confirm(`确定删除渠道 [${editingProfileName}] 吗？`)) return;
  state.profile = await api('/api/profile', {
    method: 'POST', body: JSON.stringify({ action: 'delete', profile_name: editingProfileName }),
  });
  updateProfileBadge();
  fillSettingsForm(state.profile);
};
document.getElementById('testConnBtn').onclick = async () => {
  const box = document.getElementById('probeResultBox');
  const btn = document.getElementById('testConnBtn');
  box.style.display = 'block';
  box.style.color = 'var(--muted)';
  box.textContent = '正在测试模型网关连通性…';
  btn.disabled = true;
  try {
    const res = await api('/api/profile/test', { method: 'POST', body: JSON.stringify(collectSettingsPayload()) });
    box.style.color = res.ok ? 'var(--ok)' : 'var(--danger)';
    box.textContent = (res.ok ? '✅ ' : '❌ ') + (res.message || '');
  } catch (e) {
    box.style.color = 'var(--danger)';
    box.textContent = '❌ 测试失败：' + e.message;
  } finally {
    btn.disabled = false;
  }
};
document.getElementById('closeSettingsBtn').onclick = () => document.getElementById('settingsModal').classList.remove('open');
document.getElementById('saveSettingsBtn').onclick = async () => {
  try {
    state.profile = await api('/api/profile', { method: 'POST', body: JSON.stringify(collectSettingsPayload()) });
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
document.getElementById('closeDoctorBtn').onclick = () => document.getElementById('doctorModal').classList.remove('open');

// MCP modal
document.getElementById('openMcpInfoBtn').onclick = async () => {
  try {
    const cfg = await api('/api/mcp-config');
    document.getElementById('mcpStdioPre').textContent = JSON.stringify(
      cfg.recommended_mcpServers_using_local_profile || { mcpServers: cfg.mcpServers }, null, 2
    );
    document.getElementById('mcpInfoPre').textContent = JSON.stringify(cfg.streamable_http_mcp || {}, null, 2);
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
document.getElementById('closeMcpBtn').onclick = () => document.getElementById('mcpModal').classList.remove('open');

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
