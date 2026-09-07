"use strict";

/* ------------------------------------------------------------------ state */
const state = {
  index: null,
  nodes: [],
  links: [],
  visibleNodes: [],
  visibleLinks: [],
  nodeById: new Map(),
  stemToId: new Map(),
  titleToId: new Map(),
  adjacency: new Map(),
  vaults: [],
  selectedId: null,
  graphFocusId: null,
  graph: null,
  activeView: "graph",
  editing: false,
  dirty: false,
  currentEditable: false,
  currentPath: null,
  currentFetchPath: null,
  currentRaw: "",
  gridSerial: 0,
  pendingStructuredGrids: [],
  activeGridApis: [],
  currentVault: null,
  answerGraph: null,
  graphLoadSerial: 0,
  answerGraphNodes: new Map(),
  vaultHistory: [],
  vaultHistoryOpen: false,
  providerPanelOpen: false,
  llmProvider: "",
  agentRunning: false,
};

const NODE_COLORS = {
  entity: "#aeb8a0",
  filing: "#f2eee8",
  fact: "#c9aa88",
  source: "#d49285",
  bundle: "#d5dfc9",
  constraint: "#9c8b9a",
  // Answers are deliberately neutral so they remain readable and distinct
  // from the evidence, cache, and skill nodes around them.
  answer: "#ffffff",
  claim: "#aeb8a0",
  skill: "#d49285",
  manifest: "#9c8b9a",
};

const FILING_COLORS = {
  "10-K": "#7fb3d5",
  "10-Q": "#82c9a5",
  "8-K": "#e79a87",
  "DEF 14A": "#b69ac8",
  OTHER: "#d8cec7",
};

const TYPE_LABELS = {
  "finance.entity": "Entity",
  "finance.filing": "Filing",
  "finance.fact": "Fact",
  "finance.source": "Source",
  "finokf.bundle_view": "Bundle",
  "finance.constraint": "Constraint",
  "certifacts.answer": "Answer",
  "certifacts.claim": "Claim",
  "finokf.skill_run": "Skill",
  "finokf.run_manifest": "Manifest",
  "finokf.chat_vault": "Chat vault",
  "finokf.cache_run": "Cache run",
};

const TRACE_ONLY_NODE_TYPES = new Set([
  "certifacts.answer",
  "certifacts.claim",
  "finokf.skill_run",
  "finokf.run_manifest",
]);
const COMPANY_FILING_EDGE_TYPES = new Set(["filed_by", "has_filing"]);

const els = {
  graphView: document.getElementById("graphView"),
  markdownView: document.getElementById("markdownView"),
  homeButton: document.getElementById("homeViewButton"),
  graphButton: document.getElementById("graphViewButton"),
  markdownButton: document.getElementById("markdownViewButton"),
  providerButton: document.getElementById("providerButton"),
  providerPanel: document.getElementById("providerPanel"),
  providerChoices: [...document.querySelectorAll(".provider-choice")],
  providerFields: [...document.querySelectorAll("[data-provider-fields]")],
  providerChangeButton: document.getElementById("providerChangeButton"),
  openaiApiKey: document.getElementById("openaiApiKey"),
  openaiModel: document.getElementById("openaiModel"),
  anthropicApiKey: document.getElementById("anthropicApiKey"),
  anthropicModel: document.getElementById("anthropicModel"),
  ollamaUrl: document.getElementById("ollamaUrl"),
  ollamaModel: document.getElementById("ollamaModel"),
  chatMessages: document.getElementById("chatMessages"),
  chatForm: document.getElementById("chatForm"),
  chatInput: document.getElementById("chatInput"),
  chatButton: document.querySelector("#chatForm button"),
  activeVaultLabel: document.getElementById("activeVaultLabel"),
  historyButton: document.getElementById("historyButton"),
  vaultHistoryPanel: document.getElementById("vaultHistoryPanel"),
  vaultHistoryList: document.getElementById("vaultHistoryList"),
  vaultInspector: document.getElementById("vaultInspector"),
  vaultTitle: document.getElementById("vaultTitle"),
  vaultSkill: document.getElementById("vaultSkill"),
  vaultChain: document.getElementById("vaultChain"),
  vaultMetrics: document.getElementById("vaultMetrics"),
  canvas: document.getElementById("graphCanvas"),
  search: document.getElementById("graphSearch"),
  searchResults: document.getElementById("searchResults"),
  status: document.getElementById("graphStatus"),
  legend: document.getElementById("graphLegend"),
  docTitle: document.getElementById("docTitle"),
  docRendered: document.getElementById("docRendered"),
  docEditor: document.getElementById("docEditor"),
  editToggle: document.getElementById("editToggle"),
  saveButton: document.getElementById("saveButton"),
  saveState: document.getElementById("saveState"),
};

function nodeKind(type) {
  if (type === "finance.entity") return "entity";
  if (type === "finance.filing") return "filing";
  if (type === "finance.fact") return "fact";
  if (type === "finance.source") return "source";
  if (type === "finokf.bundle_view") return "bundle";
  if (type === "finance.constraint") return "constraint";
  if (type === "certifacts.answer") return "answer";
  if (type === "certifacts.claim") return "claim";
  if (type === "finokf.skill_run") return "skill";
  if (type === "finokf.run_manifest") return "manifest";
  return "source";
}
function filingForm(node) {
  const raw = String(node?.finokf?.form || "").toUpperCase().replace(/\/A$/, "");
  if (raw.startsWith("10-K")) return "10-K";
  if (raw.startsWith("10-Q")) return "10-Q";
  if (raw.startsWith("8-K")) return "8-K";
  if (raw.startsWith("DEF 14A")) return "DEF 14A";
  return "OTHER";
}
function nodeColor(nodeOrType) {
  if (nodeOrType && typeof nodeOrType === "object" && nodeOrType.type === "finance.filing") {
    return FILING_COLORS[filingForm(nodeOrType)] || FILING_COLORS.OTHER;
  }
  const type = typeof nodeOrType === "string" ? nodeOrType : nodeOrType?.type;
  return NODE_COLORS[nodeKind(type)] || NODE_COLORS.source;
}
const nodeLabel = (node) => node?.type === "finance.filing" ? filingForm(node) : (TYPE_LABELS[node?.type] || node?.type || "File");
const normalizePath = (path) => String(path || "").replace(/\\/g, "/");
const stemOf = (path) => normalizePath(path).split("/").pop().replace(/\.(md|ya?ml)$/i, "");
const documentUrl = (relPath) => `../${normalizePath(relPath).replace(/^\.\//, "")}`;
const pathDir = (path) => normalizePath(path).split("/").slice(0, -1).join("/");
const isTraceOnlyNode = (node) => TRACE_ONLY_NODE_TYPES.has(node?.type);

function resolveRelativePath(baseFilePath, relativePath) {
  const rel = normalizePath(relativePath);
  if (!rel) return "";
  // Paths returned by the server can be full workspace-relative paths; retain
  // those while normalizing Windows separators for browser URLs.
  if (/^(?:data|ui)\//.test(rel)) return rel;
  const parts = pathDir(baseFilePath).split("/").filter(Boolean);
  // Answer graph nodes store paths such as "facts/turn-001.md" relative to
  // their graph.json. Resolve both bare and ./../ relative forms.
  for (const chunk of rel.split("/")) {
    if (!chunk || chunk === ".") continue;
    if (chunk === "..") parts.pop();
    else parts.push(chunk);
  }
  return parts.join("/");
}

/* ------------------------------------------------------------------ load */
async function loadIndex() {
  const response = await fetch("./vault-index.json", { cache: "no-store" });
  if (!response.ok) throw new Error("Missing ui/vault-index.json. Run scripts/build_vault_viewer_index.py first.");

  state.index = await response.json();
  state.nodes = (state.index.nodes || []).filter((node) => node.type === "finance.filing" || node.type === "finance.entity");
  state.nodeById = new Map(state.nodes.map((node) => [node.id, node]));

  for (const node of state.nodes) {
    state.stemToId.set(stemOf(node.path).toLowerCase(), node.id);
    if (node.title) state.titleToId.set(String(node.title).toLowerCase(), node.id);
  }

  state.visibleNodes = state.nodes;
  const available = new Set(state.visibleNodes.map((node) => node.id));
  state.visibleLinks = [];
  state.adjacency = new Map(state.visibleNodes.map((node) => [node.id, new Set()]));
  for (const node of state.visibleNodes) {
    for (const edge of node.edges || []) {
      if (!COMPANY_FILING_EDGE_TYPES.has(edge.rel)) continue;
      if (!available.has(edge.target)) continue;
      state.visibleLinks.push({ source: node.id, target: edge.target, rel: edge.rel || "edge" });
      state.adjacency.get(node.id).add(edge.target);
      state.adjacency.get(edge.target).add(node.id);
    }
  }
}

async function loadVaultHistory() {
  const response = await fetch("/api/vaults", { cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  state.vaults = payload.vaults || [];
  state.vaultHistory = payload.vaults || [];
  renderVaultHistory();
}

/* ------------------------------------------------------------------ view switch */
function setView(view) {
  state.activeView = view;
  els.graphView.classList.toggle("active", view === "graph");
  els.markdownView.classList.toggle("active", view === "markdown");
  updateRailButtons();
  if (view === "graph" && state.graph) requestAnimationFrame(() => state.graph.resize());
}

function setProviderPanel(open) {
  state.providerPanelOpen = Boolean(open);
  els.providerPanel.hidden = !state.providerPanelOpen;
  els.providerButton.classList.toggle("active", state.providerPanelOpen || Boolean(state.llmProvider));
  els.providerButton.setAttribute("aria-expanded", String(state.providerPanelOpen));
}

function selectedProviderConfig() {
  if (state.llmProvider === "openai") {
    return {
      provider: "openai",
      api_key: els.openaiApiKey.value.trim(),
      model: els.openaiModel.value.trim(),
    };
  }
  if (state.llmProvider === "anthropic") {
    return {
      provider: "anthropic",
      api_key: els.anthropicApiKey.value.trim(),
      model: els.anthropicModel.value.trim(),
    };
  }
  if (state.llmProvider === "ollama") {
    return {
      provider: "ollama",
      url: els.ollamaUrl.value.trim(),
      model: els.ollamaModel.value.trim(),
    };
  }
  return null;
}

function renderProviderControls() {
  for (const button of els.providerChoices) {
    const chosen = button.dataset.provider === state.llmProvider;
    button.classList.toggle("active", chosen);
    button.disabled = state.agentRunning || Boolean(state.llmProvider && !chosen);
    button.setAttribute("aria-pressed", String(chosen));
  }
  for (const group of els.providerFields) {
    const shown = group.dataset.providerFields === state.llmProvider;
    group.hidden = !shown;
    group.querySelectorAll("input").forEach((input) => {
      input.disabled = state.agentRunning || !shown;
    });
  }
  els.providerChangeButton.disabled = state.agentRunning || !state.llmProvider;
  els.providerButton.disabled = state.agentRunning;
  els.providerButton.classList.toggle("connected", Boolean(state.llmProvider));
  if (state.agentRunning) setProviderPanel(false);
}

function chooseProvider(provider) {
  if (state.agentRunning || state.llmProvider) return;
  state.llmProvider = provider;
  renderProviderControls();
  const firstField = els.providerPanel.querySelector(`[data-provider-fields="${provider}"] input`);
  if (firstField) firstField.focus();
}

function clearProviderChoice() {
  if (state.agentRunning) return;
  state.llmProvider = "";
  renderProviderControls();
}

function setAgentRunning(running) {
  state.agentRunning = Boolean(running);
  renderProviderControls();
  els.chatInput.disabled = state.agentRunning;
  els.chatButton.disabled = state.agentRunning;
  els.historyButton.disabled = state.agentRunning;
}

/* ------------------------------------------------------------------ graph data selection */
function degreeOf(id) {
  return state.adjacency.get(id)?.size || 0;
}

function mainGraphNodes() {
  return state.visibleNodes;
}

function mainGraphLinks() {
  return state.visibleLinks;
}

function overviewGraph() {
  const picked = mainGraphNodes();
  const ids = new Set(picked.map((node) => node.id));
  return {
    nodes: picked,
    links: mainGraphLinks().filter((l) => ids.has(l.source) && ids.has(l.target)),
  };
}

function neighborhood(centerId, hops = 2, cap = 120) {
  const ids = new Set([centerId]);
  let frontier = new Set([centerId]);
  for (let hop = 0; hop < hops && ids.size < cap; hop += 1) {
    const next = new Set();
    for (const id of frontier) {
      for (const neighbor of state.adjacency.get(id) || []) {
        if (!ids.has(neighbor) && ids.size < cap) {
          ids.add(neighbor);
          next.add(neighbor);
        }
      }
    }
    frontier = next;
  }
  return {
    nodes: [...ids].map((id) => state.nodeById.get(id)).filter(Boolean),
    links: mainGraphLinks().filter((l) => ids.has(l.source) && ids.has(l.target)),
  };
}

function renderGraph() {
  if (!state.graph) return;
  els.graphView.classList.toggle("answer-mode", Boolean(state.answerGraph));
  if (state.answerGraph) {
    state.graph.setData(state.answerGraph.nodes, state.answerGraph.links, state.selectedId, { directed: true });
    els.status.textContent = "";
    updateRailButtons();
    return;
  }
  const graph = overviewGraph();
  state.graph.setData(graph.nodes, graph.links, state.selectedId);
  els.status.textContent = "";
  updateRailButtons();
}

function updateRailButtons() {
  els.homeButton?.classList?.toggle("active", state.activeView === "graph" && !state.currentVault);
  els.graphButton?.classList?.toggle("active", state.activeView === "graph" && Boolean(state.currentVault));
  els.markdownButton?.classList?.toggle("active", state.activeView === "markdown");
}

function showHomeGraph() {
  if (state.agentRunning) return;
  clearVaultInspector();
  setActiveVault(null);
  state.selectedId = null;
  state.graphFocusId = null;
  els.chatMessages.innerHTML = "";
  els.docTitle.textContent = "No vault selected";
  els.docTitle.title = "";
  els.docRendered.textContent = "";
  els.docEditor.value = "";
  state.currentEditable = false;
  state.currentPath = null;
  state.currentFetchPath = null;
  state.currentRaw = "";
  setEditing(false);
  setDirty(false);
  els.editToggle.disabled = true;
  renderGraph();
  setView("graph");
  state.graph?.reheat(0.65);
  setTimeout(() => state.graph?.fit(true, true), 40);
}

function formatVaultTime(value) {
  if (!value || value.length < 13) return "";
  const year = value.slice(0, 4);
  const month = value.slice(4, 6);
  const day = value.slice(6, 8);
  const hour = value.slice(9, 11);
  const minute = value.slice(11, 13);
  return `${year}-${month}-${day} ${hour}:${minute}`;
}

function setActiveVault(vault) {
  state.currentVault = vault;
  els.activeVaultLabel.textContent = vault?.title || "No vault selected";
  updateRailButtons();
  renderVaultHistory();
}

function renderVaultHistory() {
  if (!els.vaultHistoryList) return;
  const items = state.vaults.map((vault) => ({
    ...vault,
    subtitle: `${vault.ticker || "Research"} · ${(vault.runs || []).length} turn${(vault.runs || []).length === 1 ? "" : "s"}${vault.updated_at || vault.created_at ? ` · ${formatVaultTime(vault.updated_at || vault.created_at)}` : ""}`,
  }));
  if (!items.length) {
    els.vaultHistoryList.innerHTML = '<div class="vault-history-empty">No saved vaults</div>';
    return;
  }
  els.vaultHistoryList.innerHTML = items.map((item) => {
    const active = state.currentVault?.vault_id === item.vault_id;
    return `
      <button class="vault-history-item${active ? " active" : ""}" type="button" data-kind="${escapeHtml(item.kind)}" data-id="${escapeHtml(item.vault_id)}">
        <span class="vault-history-name">${escapeHtml(item.title || item.question || "Vault")}</span>
        <span class="vault-history-meta">${escapeHtml(item.subtitle || "")}</span>
      </button>
    `;
  }).join("");
}

function toggleVaultHistory(force) {
  state.vaultHistoryOpen = typeof force === "boolean" ? force : !state.vaultHistoryOpen;
  els.vaultHistoryPanel.hidden = !state.vaultHistoryOpen;
  els.historyButton.classList.toggle("active", state.vaultHistoryOpen);
  els.historyButton.setAttribute("aria-expanded", String(state.vaultHistoryOpen));
}

/* ------------------------------------------------------------------ markdown rendering */
function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function resolveWikiTarget(name) {
  const key = name.split("|")[0].trim().toLowerCase();
  return state.stemToId.get(key) || state.titleToId.get(key) || null;
}

function inlineFormat(text) {
  let out = escapeHtml(text);
  const codeSpans = [];
  out = out.replace(/`([^`]+)`/g, (_m, code) => {
    codeSpans.push(`<code>${code}</code>`);
    return `__CODE_SPAN__${codeSpans.length - 1}__CODE_SPAN__`;
  });
  out = out.replace(/\[certified:([^\]]+)\]/g, '<span class="cert-badge">certified · $1</span>');
  out = out.replace(/\[\[([^\]]+)\]\]/g, (_m, name) => {
    const label = name.split("|").pop().trim();
    const target = resolveWikiTarget(name);
    if (target) return `<a class="wikilink" data-id="${escapeHtml(target)}" href="#">${escapeHtml(label)}</a>`;
    return `<a class="wikilink missing" href="#">${escapeHtml(label)}</a>`;
  });
  out = out.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  out = out.replace(/__CODE_SPAN__(\d+)__CODE_SPAN__/g, (_m, i) => codeSpans[Number(i)]);
  return out;
}

function renderTableRow(line, isHeader) {
  const cells = line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
  const tag = isHeader ? "th" : "td";
  return "<tr>" + cells.map((c) => `<${tag}>${inlineFormat(c)}</${tag}>`).join("") + "</tr>";
}

function renderMarkdownBody(body) {
  const lines = body.replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let i = 0;

  const closeList = (stack) => { while (stack.length) html.push(`</${stack.pop()}>`); };
  const listStack = [];

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    const fence = line.match(/^```(\w*)/);
    if (fence) {
      closeList(listStack);
      const lang = fence[1] || "";
      const buf = [];
      i += 1;
      while (i < lines.length && !lines[i].startsWith("```")) { buf.push(lines[i]); i += 1; }
      i += 1;
      html.push(`<pre><code class="lang-${lang}">${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    // table
    if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      closeList(listStack);
      const rows = [renderTableRow(line, true)];
      i += 2;
      while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) { rows.push(renderTableRow(lines[i], false)); i += 1; }
      html.push(`<table>${rows.join("")}</table>`);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList(listStack);
      const level = heading[1].length;
      html.push(`<h${level}>${inlineFormat(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { closeList(listStack); html.push("<hr>"); i += 1; continue; }

    if (/^\s*>\s?/.test(line)) {
      closeList(listStack);
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^\s*>\s?/, "")); i += 1; }
      html.push(`<blockquote>${inlineFormat(buf.join(" "))}</blockquote>`);
      continue;
    }

    const bullet = line.match(/^(\s*)([-*+])\s+(.*)$/);
    const ordered = line.match(/^(\s*)(\d+)\.\s+(.*)$/);
    if (bullet || ordered) {
      const type = bullet ? "ul" : "ol";
      if (listStack[listStack.length - 1] !== type) { closeList(listStack); html.push(`<${type}>`); listStack.push(type); }
      html.push(`<li>${inlineFormat((bullet || ordered)[3])}</li>`);
      i += 1;
      continue;
    }

    if (!line.trim()) { closeList(listStack); i += 1; continue; }

    // paragraph
    closeList(listStack);
    const buf = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*>|\s*[-*+]\s|\s*\d+\.\s)/.test(lines[i]) && !/\|/.test(lines[i])) {
      buf.push(lines[i]);
      i += 1;
    }
    html.push(`<p>${inlineFormat(buf.join(" "))}</p>`);
  }
  closeList(listStack);
  return html.join("\n");
}

function renderMarkdown(raw) {
  let fm = "";
  let body = raw;
  if (raw.startsWith("---\n")) {
    const end = raw.indexOf("\n---", 4);
    if (end !== -1) {
      fm = raw.slice(4, end);
      body = raw.slice(end + 4).replace(/^\n+/, "");
    }
  }
  const MAX = 160000;
  let notice = "";
  if (body.length > MAX) { notice = `<p><em>Preview truncated to ${MAX.toLocaleString()} characters — open Edit to see the full file.</em></p>`; body = body.slice(0, MAX); }
  const props = fm ? `<details class="doc-props"><summary>Properties</summary><pre>${escapeHtml(fm.trim())}</pre></details>` : "";
  return props + renderMarkdownBody(body) + notice;
}

function yamlScalarValue(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  if (text === "null") return null;
  if (text === "true") return true;
  if (text === "false") return false;
  if (/^-?\d+(\.\d+)?$/.test(text)) return Number(text);
  if ((text.startsWith('"') && text.endsWith('"')) || (text.startsWith("'") && text.endsWith("'"))) {
    try { return text.startsWith('"') ? JSON.parse(text) : text.slice(1, -1).replace(/''/g, "'"); }
    catch { return text.slice(1, -1); }
  }
  if (text === "[]") return [];
  if (text === "{}") return {};
  return text;
}

function parseStructuredYaml(raw) {
  const lines = raw.replace(/\r\n/g, "\n").split("\n");
  const indentation = (line) => (line.match(/^ */) || [""])[0].length;
  const nextContent = (start) => {
    let index = start;
    while (index < lines.length && !lines[index].trim()) index += 1;
    return index;
  };

  function parseBlock(start, indent) {
    let index = nextContent(start);
    if (index >= lines.length || indentation(lines[index]) < indent) return [{}, index];
    const isList = indentation(lines[index]) === indent && lines[index].slice(indent).startsWith("- ");
    const container = isList ? [] : {};

    while (index < lines.length) {
      if (!lines[index].trim()) { index += 1; continue; }
      const currentIndent = indentation(lines[index]);
      if (currentIndent < indent) break;
      if (currentIndent > indent) break;
      const content = lines[index].slice(indent);

      if (isList) {
        if (!content.startsWith("- ")) break;
        const first = content.slice(2).trim();
        if (!first) {
          const childAt = nextContent(index + 1);
          const childIndent = childAt < lines.length ? indentation(lines[childAt]) : indent + 2;
          const [child, next] = parseBlock(childAt, childIndent);
          container.push(child);
          index = next;
          continue;
        }
        const split = first.indexOf(":");
        if (split === -1) {
          container.push(yamlScalarValue(first));
          index += 1;
          continue;
        }
        const item = {};
        const key = first.slice(0, split).trim();
        const rest = first.slice(split + 1).trim();
        item[key] = rest ? yamlScalarValue(rest) : null;
        index += 1;
        const childAt = nextContent(index);
        if (childAt < lines.length && indentation(lines[childAt]) > indent) {
          const childIndent = indentation(lines[childAt]);
          const [tail, next] = parseBlock(childAt, childIndent);
          if (rest || Array.isArray(tail)) Object.assign(item, tail);
          else item[key] = tail;
          index = next;
        }
        container.push(item);
        continue;
      }

      if (content.startsWith("- ")) break;
      const split = content.indexOf(":");
      if (split === -1) { index += 1; continue; }
      const key = content.slice(0, split).trim();
      const rest = content.slice(split + 1).trim();
      if (rest === "|-" || rest === "|") {
        index += 1;
        const block = [];
        let blockIndent = null;
        while (index < lines.length) {
          const candidateIndent = indentation(lines[index]);
          if (lines[index].trim() && candidateIndent <= indent) break;
          if (blockIndent === null && lines[index].trim()) blockIndent = candidateIndent;
          const strip = blockIndent === null ? indent + 2 : blockIndent;
          block.push(lines[index].length >= strip ? lines[index].slice(strip) : "");
          index += 1;
        }
        while (block.length && block[block.length - 1] === "") block.pop();
        container[key] = block.join("\n");
        continue;
      }
      if (rest) {
        container[key] = yamlScalarValue(rest);
        index += 1;
        continue;
      }
      const childAt = nextContent(index + 1);
      if (childAt >= lines.length || indentation(lines[childAt]) <= indent) {
        container[key] = {};
        index = childAt;
        continue;
      }
      const childIndent = indentation(lines[childAt]);
      const [child, next] = parseBlock(childAt, childIndent);
      container[key] = child;
      index = next;
    }
    return [container, index];
  }

  return parseBlock(0, 0)[0];
}

function displayCell(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.map(displayCell).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function flatTableRow(row) {
  const flat = {};
  for (const [key, value] of Object.entries(row || {})) {
    if (key === "period" && value && typeof value === "object" && !Array.isArray(value)) {
      for (const [periodKey, periodValue] of Object.entries(value)) flat[`period.${periodKey}`] = periodValue;
    } else {
      flat[key] = value;
    }
  }
  return flat;
}

function isEmptyGridValue(value) {
  if (value === null || value === undefined || (Array.isArray(value) && value.length === 0)) return true;
  if (typeof value !== "string") return false;
  return new Set(["", "-", "—", "–", "n/a", "na", "null", "none"]).has(value.trim().toLowerCase());
}

function formatGridHeader(field) {
  return String(field)
    .replace(/\./g, " ")
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function destroyStructuredGrids() {
  for (const api of state.activeGridApis) api?.destroy?.();
  state.activeGridApis = [];
  state.pendingStructuredGrids = [];
}

function queueStructuredGrid(rows, preferredColumns = [], emptyText = "No records reported.") {
  if (!Array.isArray(rows) || !rows.length) return `<p class="empty-table">${escapeHtml(emptyText)}</p>`;
  const flattened = rows.map(flatTableRow);
  const discovered = [];
  for (const row of flattened) {
    for (const key of Object.keys(row)) if (!discovered.includes(key)) discovered.push(key);
  }
  const visibleColumns = discovered.filter((key) => flattened.some((row) => !isEmptyGridValue(row[key])));
  const columns = [
    ...preferredColumns.filter((key) => visibleColumns.includes(key)),
    ...visibleColumns.filter((key) => !preferredColumns.includes(key)),
  ];
  if (!columns.length) return `<p class="empty-table">${escapeHtml(emptyText)}</p>`;
  const gridId = `structured-grid-${++state.gridSerial}`;
  state.pendingStructuredGrids.push({ id: gridId, columns, rows: flattened });
  return `<div class="structured-grid-frame"><div id="${gridId}" class="structured-grid ag-theme-quartz-dark"></div></div>`;
}

function mountStructuredGrids() {
  if (!window.agGrid || !state.pendingStructuredGrids.length) return;
  const gridConfigs = state.pendingStructuredGrids;
  state.pendingStructuredGrids = [];
  for (const config of gridConfigs) {
    const element = document.getElementById(config.id);
    if (!element) continue;
    const hasPagination = config.rows.length > 25;
    const visibleRowCount = Math.min(config.rows.length, 25);
    element.style.height = `${Math.min(560, 46 + (visibleRowCount * 54) + (hasPagination ? 44 : 0))}px`;
    const defaultFlex = (field) => (
      /label|value|concept|source|title|dimensions|language/i.test(field) ? 1.5 : 1
    );
    const columnDefs = config.columns.map((field) => ({
      field,
      headerName: formatGridHeader(field),
      minWidth: /fact_id|concept|source|dimensions|file_name|sha256/i.test(field) ? 220 : 140,
      flex: defaultFlex(field),
      cellDataType: false,
      sortable: true,
      filter: "agTextColumnFilter",
      resizable: true,
      tooltipValueGetter: (params) => displayCell(params.value),
      valueFormatter: (params) => displayCell(params.value),
      cellStyle: {
        fontFamily: "var(--mono)",
        fontSize: "11.5px",
        lineHeight: "1.4",
      },
      wrapText: true,
    }));
    const gridOptions = {
      theme: "legacy",
      suppressFieldDotNotation: true,
      columnDefs,
      rowData: config.rows,
      defaultColDef: {
        cellDataType: false,
        sortable: true,
        filter: "agTextColumnFilter",
        resizable: true,
        floatingFilter: false,
      },
      pagination: hasPagination,
      paginationPageSize: 25,
      paginationPageSizeSelector: [25, 50, 100],
      animateRows: false,
      ensureDomOrder: true,
      domLayout: "normal",
      suppressCellFocus: true,
      suppressMovableColumns: true,
      rowHeight: 54,
      headerHeight: 44,
    };
    let api = null;
    if (typeof window.agGrid.createGrid === "function") api = window.agGrid.createGrid(element, gridOptions);
    else if (typeof window.agGrid.Grid === "function") {
      const legacyGrid = new window.agGrid.Grid(element, gridOptions);
      api = gridOptions.api || legacyGrid?.api || null;
    }
    if (api) state.activeGridApis.push(api);
  }
}

function renderCurrentDocument(raw, path = "") {
  destroyStructuredGrids();
  els.docRendered.innerHTML = renderDocument(raw, path);
  mountStructuredGrids();
}

function renderStructuredYaml(raw) {
  let record;
  try { record = parseStructuredYaml(raw); }
  catch (error) { return `<p><strong>Could not parse filing YAML:</strong> ${escapeHtml(error.message)}</p><pre>${escapeHtml(raw)}</pre>`; }
  const properties = record.properties || {};
  const tags = Array.isArray(properties.tags) ? properties.tags : [];
  const hiddenProperties = new Set(["tags", "edge_count", "graph_connections"]);
  const propertyItems = Object.entries(properties).filter(([key]) => !hiddenProperties.has(key));
  const propertyHtml = `<section class="structured-properties"><div class="property-grid">${propertyItems.map(([key, value]) => `<div class="property-item"><span>${escapeHtml(key)}</span><strong>${escapeHtml(displayCell(value))}</strong></div>`).join("")}</div>${tags.length ? `<div class="property-tags">${tags.map((tag) => `<span>${escapeHtml(displayCell(tag))}</span>`).join("")}</div>` : ""}</section>`;
  const sourceTable = queueStructuredGrid(record.sources || [], ["role", "file_name", "sha256", "bytes"]);
  const keyFacts = (record.facts || []).filter((fact) => fact.key_fact);
  const keyFactTable = queueStructuredGrid(keyFacts, [
    "label", "display_value", "period.start", "period.end", "period.kind", "period.context_id",
    "reporting_role", "concept", "unit", "dimensions", "fact_id",
  ]);
  const factTable = queueStructuredGrid(record.facts || [], [
    "key_fact", "label", "display_value", "raw_value", "unit", "period.start", "period.end",
    "period.kind", "period.context_id", "reporting_role", "concept", "decimals", "dimensions", "is_extension", "fact_id",
  ]);
  const textFactTable = queueStructuredGrid(record.text_facts || [], [
    "label", "value", "period.start", "period.end", "period.kind", "period.context_id",
    "reporting_role", "concept", "language", "dimensions", "is_extension", "fact_id",
  ]);
  const calculationTable = queueStructuredGrid(record.calculation_relationships || [], [
    "subtotal_concept", "component_concept", "weight", "order", "statement_role",
  ]);
  const filingText = record.filing_text || {};
  const textHtml = filingText.text
    ? `<details class="filing-text"><summary>View full filing text · ${escapeHtml(displayCell(filingText.source))}</summary><pre>${escapeHtml(displayCell(filingText.text))}</pre></details>`
    : '<p class="empty-table">No extractable filing text was found.</p>';
  return `<article class="structured-document"><h1>${escapeHtml(displayCell(record.title || "Structured filing"))}</h1>${propertyHtml}<h2>Key reported facts <small>${keyFacts.length.toLocaleString()}</small></h2>${keyFactTable}<h2>All reported facts <small>${(record.facts || []).length.toLocaleString()}</small></h2>${factTable}<h2>Reported text facts <small>${(record.text_facts || []).length.toLocaleString()}</small></h2>${textFactTable}<h2>Calculation relationships <small>${(record.calculation_relationships || []).length.toLocaleString()}</small></h2>${calculationTable}<h2>Source provenance <small>${(record.sources || []).length.toLocaleString()}</small></h2>${sourceTable}<h2>Filing text</h2>${textHtml}</article>`;
}

function renderDocument(raw, path = "") {
  // Cache-vault snapshots created by earlier versions used the source file's
  // .yml extension despite containing Markdown frontmatter and prose. Detect
  // actual structured filings by content so those legacy snapshots stay
  // readable, while real filing YAML retains the table renderer.
  const isStructuredFiling = /\.ya?ml$/i.test(path) && !raw.startsWith("---\\n");
  return isStructuredFiling ? renderStructuredYaml(raw) : renderMarkdown(raw);
}

/* ------------------------------------------------------------------ open / edit / save */
async function openDocument(path, title, options = {}) {
  let text;
  try {
    const response = await fetch(documentUrl(path), { cache: "no-store" });
    text = response.ok
      ? await response.text()
      : `# ${title}\n\nMarkdown file could not be loaded from \`${path}\`.\n\nHTTP status: ${response.status}`;
  } catch (error) {
    text = `# ${title}\n\nMarkdown file could not be loaded from \`${path}\`.\n\n${error.message}`;
  }

  state.currentEditable = Boolean(options.editable);
  state.currentPath = state.currentEditable ? options.savePath || null : null;
  state.currentFetchPath = path;
  state.currentRaw = text;
  els.docTitle.textContent = title;
  els.docTitle.title = path;
  els.docEditor.value = text;
  renderCurrentDocument(text, path);
  els.docRendered.scrollTop = 0;
  setEditing(false);
  setDirty(false);
  els.editToggle.disabled = !state.currentEditable;
  els.saveButton.disabled = !state.currentEditable || !state.dirty;

  if (options.showMarkdown !== false) setView("markdown");
}

async function openNode(id, options = {}) {
  if (state.agentRunning) return;
  const node = state.nodeById.get(id);
  if (!node) return;
  state.answerGraph = null;
  state.answerGraphNodes = new Map();
  setActiveVault(null);
  state.selectedId = id;
  if (options.focusGraph !== false) state.graphFocusId = id;
  renderGraph();
  await openDocument(`${state.index.processed_dir}/${node.path}`, stemOf(node.path), {
    editable: true,
    savePath: node.path,
    showMarkdown: options.showMarkdown,
  });
}

function setEditing(editing) {
  if (editing && !state.currentEditable) return;
  state.editing = editing;
  els.docEditor.hidden = !editing;
  els.docRendered.hidden = editing;
  els.editToggle.textContent = editing ? "Reading" : "Edit";
  els.editToggle.classList.toggle("active", editing);
  if (editing) els.docEditor.focus();
  else renderCurrentDocument(els.docEditor.value, state.currentFetchPath || "");
}

function setDirty(dirty) {
  state.dirty = dirty;
  els.saveButton.disabled = !state.currentEditable || !dirty;
  els.saveState.className = "save-state" + (dirty ? " dirty" : "");
  els.saveState.textContent = dirty ? "Unsaved" : "";
}

async function saveDocument() {
  if (!state.currentPath || !state.dirty) return;
  const content = els.docEditor.value;
  els.saveState.className = "save-state";
  els.saveState.textContent = "Saving…";
  try {
    const response = await fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.currentPath, content }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
    state.currentRaw = content;
    state.dirty = false;
    els.saveButton.disabled = true;
    els.saveState.className = "save-state saved";
    els.saveState.textContent = "Saved";
    setTimeout(() => { if (!state.dirty) els.saveState.textContent = ""; }, 1600);
  } catch (error) {
    els.saveState.className = "save-state error";
    els.saveState.textContent = "Save failed";
    addChatMessage("assistant", `Could not save ${state.currentPath}: ${error.message}. Is scripts/serve_vault.py running?`);
  }
}

/* ------------------------------------------------------------------ chat */
function addChatMessage(role, text, modifier = "") {
  const message = document.createElement("div");
  message.className = `chat-message ${role}`;
  if (modifier) message.classList.add(modifier);
  if (role === "assistant") message.innerHTML = renderMarkdownBody(text || "");
  else message.textContent = text;
  els.chatMessages.appendChild(message);
  els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
  return message;
}
function updateChatMessage(message, text, modifier = "") {
  message.className = "chat-message assistant";
  if (modifier) message.classList.add(modifier);
  message.innerHTML = renderMarkdownBody(text || "");
  els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
}
function answerPreviewText(answer, limit = 420) {
  const normalized = String(answer || "No answer returned.").replace(/\s+/g, " ").trim();
  if (normalized.length <= limit) return normalized;
  return `${normalized.slice(0, limit).replace(/\s+\S*$/, "")}...`;
}
function showChatTranscript(vault) {
  els.chatMessages.innerHTML = "";
  const messages = vault.messages || [];
  if (!messages.length) {
    addChatMessage("assistant", "This local chat vault is empty. Ask a question to bind its first cache entry.");
    return;
  }
  const turns = new Map();
  for (const message of messages) {
    if (message.result) {
      renderAgentResult(addChatMessage("assistant", ""), message.result);
      const answers = turns.get(message.turn) || [];
      answers.push(message.result);
      turns.set(message.turn, answers);
      if (answers.length === 2) addComparison(answers);
    } else addChatMessage(message.role === "user" ? "user" : "assistant", message.content || "");
  }
}

function renderAgentResult(element, result) {
  const naive = result.agent === "naive";
  element.className = `chat-message assistant agent-answer ${naive ? "naive" : "finokf"}${result.ok === false ? " error" : ""}`;
  element.dataset.expanded = "false";
  const avatar = naive
    ? '<circle cx="12" cy="8" r="4"/><path d="M4 22v-3a8 8 0 0 1 16 0v3"/>'
    : '<rect x="3" y="6" width="18" height="15" rx="4"/><path d="M12 2v4M8 12h1m6 0h1M8 17h8"/>';
  element.innerHTML = `<div class="agent-heading"><svg class="agent-avatar" viewBox="0 0 24 24" role="img" aria-label="${naive ? "Naive researcher" : "FinOKF robot"} profile picture">${avatar}</svg><div><strong>${naive ? "Naive" : "FinOKF"}</strong><small>${naive ? "Independent web research · uncached" : "Local evidence + web research · cache enabled"}</small></div></div><div class="agent-preview">${escapeHtml(answerPreviewText(result.answer))}</div><div class="agent-body" hidden>${renderMarkdownBody(result.answer || "No answer returned.")}</div>`;
  const answerButton = document.createElement("button");
  answerButton.type = "button";
  answerButton.className = "show-more-button";
  answerButton.textContent = "Show more";
  answerButton.setAttribute("aria-expanded", "false");
  const body = element.querySelector(".agent-body");
  const preview = element.querySelector(".agent-preview");
  const extra = document.createElement("div");
  extra.className = "agent-extra";
  extra.hidden = true;
  answerButton.addEventListener("click", () => {
    const expanded = body.hidden;
    body.hidden = !expanded;
    preview.hidden = expanded;
    extra.hidden = !expanded;
    element.dataset.expanded = String(expanded);
    answerButton.textContent = expanded ? "Show less" : "Show more";
    answerButton.setAttribute("aria-expanded", String(expanded));
    els.chatMessages.scrollTop = els.chatMessages.scrollHeight;
  });
  element.appendChild(answerButton);
  if (result.sources?.length) {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = `Evidence supplied (${result.sources.length})`;
    details.appendChild(summary);
    for (const source of result.sources) {
      const row = document.createElement("div");
      if (typeof source === "string") {
        row.textContent = source.includes("prices/") ? `Yahoo Finance · local CSV: ${source}` : source;
      } else if (/^https?:\/\//.test(source.url || "")) {
        const link = document.createElement("a");
        link.href = source.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = source.title || source.url;
        row.appendChild(link);
      }
      details.appendChild(row);
    }
    extra.appendChild(details);
  }
  if (result.warnings?.length) {
    const warning = document.createElement("p");
    warning.className = "agent-warning";
    warning.textContent = `Some searches failed: ${result.warnings.join("; ")}`;
    extra.appendChild(warning);
  }
  element.appendChild(extra);
}

function addComparison(answers) {
  const naive = answers.find((answer) => answer.agent === "naive") || {};
  const finokf = answers.find((answer) => answer.agent === "finokf") || {};
  const wrapper = document.createElement("div");
  wrapper.className = "agent-comparison";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "compare-button";
  button.textContent = "Compare metadata";
  button.setAttribute("aria-expanded", "false");
  const panel = document.createElement("div");
  panel.hidden = true;
  const number = (value) => value == null ? "Unavailable" : Number(value).toLocaleString();
  const rows = [
    ["Status", (r) => r.ok ? "Answered" : "Failed"],
    ["Provider / model", (r) => `${r.provider || "—"} / ${r.model || "—"}`],
    ["Data access", (r) => r.data_access || "—"],
    ["Route", (r) => r.route || "—"],
    ["Cache hit", (r) => r.cache_hit ? "Yes" : "No"],
    ["Elapsed (ms)", (r) => number(r.metrics?.total_ms)],
    ["Model time (ms)", (r) => number(r.metrics?.model_ms)],
    ["Input tokens", (r) => number(r.metrics?.prompt_tokens)],
    ["Output tokens", (r) => number(r.metrics?.completion_tokens)],
    ["Total tokens", (r) => number(r.metrics?.total_tokens)],
    ["Web searches", (r) => number(r.metrics?.web_requests)],
    ["Web pages fetched", (r) => number(r.metrics?.page_requests)],
    ["Evidence sources", (r) => number(r.metrics?.source_count)],
  ];
  panel.innerHTML = `<table><caption>Measured for this question</caption><thead><tr><th scope="col">Metadata</th><th scope="col">Naive</th><th scope="col">FinOKF</th></tr></thead><tbody>${rows.map(([label, read]) => `<tr><th scope="row">${escapeHtml(label)}</th><td>${escapeHtml(read(naive))}</td><td>${escapeHtml(read(finokf))}</td></tr>`).join("")}</tbody></table><p>Naive totals include query planning and synthesis. Cache replays consume zero new model tokens. Elapsed time includes shared-resource contention; speed and accuracy are not guaranteed.</p>`;
  button.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    button.setAttribute("aria-expanded", String(!panel.hidden));
  });
  wrapper.append(button, panel);
  els.chatMessages.appendChild(wrapper);
}

function clearVaultInspector() {
  state.graphLoadSerial += 1;
  state.answerGraph = null;
  state.answerGraphNodes = new Map();
  els.graphView.classList.remove("answer-mode");
  els.vaultInspector.hidden = true;
  els.vaultTitle.textContent = "No answer cached yet";
  if (els.vaultSkill) {
    els.vaultSkill.hidden = true;
    els.vaultSkill.textContent = "";
  }
  if (els.vaultChain) els.vaultChain.textContent = "";
  if (els.vaultMetrics) els.vaultMetrics.innerHTML = "";
}

function renderVaultInspector(vault) {
  els.vaultInspector.hidden = false;
  els.vaultTitle.textContent = vault.question || "Cached answer";
  if (els.vaultSkill) {
    els.vaultSkill.hidden = true;
    els.vaultSkill.textContent = "";
  }
  if (els.vaultChain) els.vaultChain.textContent = "";
  if (els.vaultMetrics) els.vaultMetrics.innerHTML = "";
}

function answerGraphData(vault, payload) {
  const latestRun = (vault.runs || []).at(-1);
  const runId = latestRun ? `${vault.vault_id}:${latestRun.turn}` : null;
  const metadata = new Map((vault.nodes || []).map((node) => [node.id, node]));
  let nodes = payload.nodes || [];
  let links = payload.links || [];
  if (vault.kind === "chat-vault" && latestRun) {
    // Legacy graph files contain every conversation turn. Show only the
    // current execution's actual bindings, including multi-company answers.
    const evidenceIds = new Set(latestRun.evidence_nodes
      ? latestRun.evidence_nodes.map((node) => node.id)
      : links.filter((link) => link.source === runId).map((link) => link.target));
    const answerIds = links
      .filter((link) => link.source === runId && link.rel === "produces")
      .map((link) => link.target);
    const activeIds = new Set([vault.vault_id, runId, ...answerIds, ...evidenceIds]);
    nodes = nodes.filter((node) => activeIds.has(node.id));
    links = links.filter((link) => activeIds.has(link.source) && activeIds.has(link.target));
  }
  const unique = new Map();
  const remap = new Map();
  for (const node of nodes) {
    const details = { ...metadata.get(node.id), ...node };
    const sourcePath = normalizePath(details.source_path);
    const identity = details.type === "finance.fact" ? `${sourcePath}#${details.source_id || details.id}` : sourcePath || details.id;
    const canonicalId = unique.get(identity)?.id || details.id;
    remap.set(details.id, canonicalId);
    unique.set(identity, {
      ...details,
      id: canonicalId,
      title: details.id === vault.vault_id && latestRun ? (latestRun.question || vault.question || details.title) : details.title,
      ticker: details.ticker || sourcePath.match(/\/(?:filings|companies|entities)\/([^/.]+)/)?.[1] || "",
      path: vault.graph_path ? resolveRelativePath(vault.graph_path, details.path || "") : (details.path || ""),
    });
  }
  const uniqueLinks = new Map();
  for (const link of links) {
    const source = remap.get(link.source);
    const target = remap.get(link.target);
    if (source && target) uniqueLinks.set(`${source}:${target}:${link.rel}`, { ...link, source, target, directed: true });
  }
  return { nodes: [...unique.values()], links: [...uniqueLinks.values()], rootId: vault.vault_id, turn: latestRun?.turn };
}

async function applyAnswerGraph(vault) {
  const serial = ++state.graphLoadSerial;
  let payload = vault.graph || null;
  if (!payload && vault.graph_path) {
    const response = await fetch(documentUrl(vault.graph_path), { cache: "no-store" });
    if (!response.ok) throw new Error(`graph trace missing: HTTP ${response.status}`);
    payload = await response.json();
  }
  if (serial !== state.graphLoadSerial) return false;
  const graph = answerGraphData(vault, payload || { nodes: [], links: [] });
  const { nodes } = graph;
  state.answerGraphNodes = new Map(nodes.map((node) => [node.id, node]));
  state.answerGraph = graph;
  state.selectedId = vault.vault_id || nodes[0]?.id || state.selectedId;
  state.graphFocusId = null;
  setActiveVault(vault);
  renderVaultInspector(vault);
  renderGraph();
  state.graph.reheat(0.9);
  setView("graph");
  setTimeout(() => state.graph.fit(true), 40);
  return true;
}

async function selectVault(vault) {
  if (state.agentRunning) return;
  if (await applyAnswerGraph(vault)) showChatTranscript(vault);
}

function chatNeighbors(node) {
  return [...(state.adjacency.get(node.id) || [])].slice(0, 24).map((id) => {
    const neighbor = state.nodeById.get(id);
    return {
      id,
      title: neighbor?.title || id,
      type: nodeLabel(neighbor),
      ticker: neighbor?.ticker || "",
      path: neighbor?.path || "",
    };
  });
}

async function readAgentStream(response, onAnswer) {
  if (!response.ok) {
    const failure = await response.json().catch(() => ({}));
    throw new Error(failure.error || `HTTP ${response.status}`);
  }
  if (!response.headers.get("content-type")?.includes("application/x-ndjson")) {
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || "Research failed");
    return result;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = null;
  const consume = async (line) => {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.type === "answer") await onAnswer(event.answer, event.vault);
    if (event.type === "complete") completed = event;
  };
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done });
    let end;
    while ((end = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, end);
      buffer = buffer.slice(end + 1);
      await consume(line);
    }
    if (done) break;
  }
  await consume(buffer);
  if (!completed) throw new Error("Connection ended before both agents finished. Completed answers are shown above.");
  return completed;
}

async function askLocalModel(question, onAnswer) {
  const contextId = state.currentVault?.kind === "chat-vault" ? state.currentVault?.context?.id : null;
  const node = state.nodeById.get(contextId) || {};
  const providerConfig = selectedProviderConfig();
  if (!providerConfig) throw new Error("choose ChatGPT, Anthropic, or Local LLM from the key button first");

  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: question,
      stream: true,
      provider_config: providerConfig,
      vault_id: state.currentVault?.kind === "chat-vault" ? state.currentVault.vault_id : null,
      markdown: contextId ? (state.editing ? els.docEditor.value : state.currentRaw) : "",
      node: {
        id: node.id || "",
        title: node.title || "",
        type: node.type || "",
        ticker: node.ticker || "",
        path: node.path || "",
        finokf: node.finokf || {},
      },
      neighbors: chatNeighbors(node),
    }),
  });
  return readAgentStream(response, onAnswer);
}

async function submitChat(value) {
  const question = value.trim();
  if (!question || state.agentRunning) return;
  if (!state.llmProvider) {
    setProviderPanel(true);
    addChatMessage("assistant", "Choose ChatGPT, Anthropic, or Local LLM from the key button first.", "error");
    return;
  }
  addChatMessage("user", question);
  els.chatInput.value = "";
  setAgentRunning(true);
  const pendingNaive = addChatMessage("assistant", "");
  const pendingFinokf = addChatMessage("assistant", "");
  renderAgentResult(pendingNaive, { agent: "naive", answer: "Researching the web independently…" });
  renderAgentResult(pendingFinokf, { agent: "finokf", answer: "Checking local facts, prices and cached answers…" });
  pendingNaive.classList.add("pending");
  pendingFinokf.classList.add("pending");
  const completedAgents = new Set();
  const receiveAnswer = async (answer, vault) => {
    renderAgentResult(answer.agent === "naive" ? pendingNaive : pendingFinokf, answer);
    completedAgents.add(answer.agent);
    if (vault) {
      try { await applyAnswerGraph(vault); }
      catch (error) { addChatMessage("assistant", `Answer ready, but graph could not load: ${error.message}`, "error"); }
    }
  };
  try {
    const result = await askLocalModel(question, receiveAnswer);
    for (const answer of result.answers || []) {
      if (!completedAgents.has(answer.agent)) await receiveAnswer(answer);
    }
    addComparison(result.answers || []);
    if (result.persistence_error) addChatMessage("assistant", result.persistence_error, "error");
    if (result.vault) {
      state.vaults = [result.vault, ...state.vaults.filter((item) => item.vault_id !== result.vault.vault_id)];
      state.vaultHistory = [result.vault, ...state.vaultHistory.filter((item) => item.vault_id !== result.vault.vault_id)];
      renderVaultHistory();
      await applyAnswerGraph(result.vault);
    }
  } catch (error) {
    if (!completedAgents.has("naive")) renderAgentResult(pendingNaive, {agent: "naive", ok: false, answer: `Request failed: ${error.message}`});
    if (!completedAgents.has("finokf")) renderAgentResult(pendingFinokf, {agent: "finokf", ok: false, answer: `Request failed: ${error.message}`});
    if (completedAgents.size === 2) addChatMessage("assistant", error.message, "error");
  } finally {
    setAgentRunning(false);
    els.chatInput.focus();
  }
}

/* ------------------------------------------------------------------ force-directed graph */
class ForceGraph {
  constructor(canvas, onSelect) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onSelect = onSelect;
    this.nodes = [];
    this.links = [];
    this.nodeById = new Map();
    this.selectedId = null;
    this.hoverId = null;
    this.alpha = 0;
    this.largeLayout = false;
    this.hasLoaded = false;
    this.hasMotion = false;
    this.layoutStartedAt = 0;
    this.bloomEndsAt = 0;
    this.pendingLocalFitAt = 0;
    this.needsDraw = true;
    this.transform = { x: 0, y: 0, k: 1 };
    this.pointer = { down: false, dragNode: null, panning: false, moved: 0, lastX: 0, lastY: 0, downX: 0, downY: 0 };

    // Physics constants tuned to Obsidian's soft, slightly elastic graph motion.
    this.LINK_DIST = 145;
    this.LINK_STRENGTH = 0.022;
    this.CHARGE = 6500;
    this.GRAVITY = 0.006;
    this.CLUSTER_GRAVITY = 0.075;
    this.VELOCITY_DECAY = 0.87;
    this.ALPHA_DECAY = 0.018;
    this.ALPHA_MIN = 0.0015;
    this.MAX_V = 18;
    this.BLOOM_DURATION = 9000;
    this.BLOOM_SETTLE = 2200;

    this.resize();
    window.addEventListener("resize", () => this.resize());
    canvas.addEventListener("pointerdown", (e) => this.onPointerDown(e));
    canvas.addEventListener("pointermove", (e) => this.onPointerMove(e));
    canvas.addEventListener("pointerleave", () => {
      if (this.pointer.down) return;
      this.hoverId = null;
      this.canvas.style.cursor = "grab";
      this.needsDraw = true;
    });
    window.addEventListener("pointerup", (e) => this.onPointerUp(e));
    canvas.addEventListener("wheel", (e) => this.onWheel(e), { passive: false });
    requestAnimationFrame(() => this.tick());
  }

  resize() {
    const rect = this.canvas.getBoundingClientRect();
    this.dpr = window.devicePixelRatio || 1;
    this.width = Math.max(320, rect.width);
    this.height = Math.max(320, rect.height);
    this.canvas.width = Math.floor(this.width * this.dpr);
    this.canvas.height = Math.floor(this.height * this.dpr);
    this.needsDraw = true;
  }

  setData(nodes, links, selectedId, options = {}) {
    const old = new Map(this.nodes.map((n) => [n.id, n]));
    const first = !this.hasLoaded;
    const largeLayout = nodes.length > 600;
    const layoutModeChanged = this.hasLoaded && this.largeLayout !== largeLayout;
    const continuingBloom = largeLayout && this.bloomEndsAt > performance.now();
    this.selectedId = selectedId;
    this.directed = Boolean(options.directed);

    const degree = new Map();
    for (const l of links) {
      degree.set(l.source, (degree.get(l.source) || 0) + 1);
      degree.set(l.target, (degree.get(l.target) || 0) + 1);
    }

    const tickers = [...new Set(nodes.map((node) => String(node.ticker || "UNKNOWN")))].sort();
    const clusterCounts = new Map();
    for (const node of nodes) {
      const ticker = String(node.ticker || "UNKNOWN");
      clusterCounts.set(ticker, (clusterCounts.get(ticker) || 0) + 1);
    }
    const largestCluster = Math.max(1, ...clusterCounts.values());
    const localRadius = 36 + Math.sqrt(largestCluster) * 17;
    const clusterSpacing = Math.max(240, localRadius * 1.42);
    const goldenAngle = 2.399963229728653;
    this.clusterCenters = new Map(tickers.map((ticker, index) => {
      if (tickers.length === 1) return [ticker, { x: 0, y: 0 }];
      const angle = index * goldenAngle + (this.stableUnit(ticker) - 0.5) * 0.18;
      const radius = clusterSpacing * Math.sqrt(index);
      return [ticker, {
        x: Math.cos(angle) * radius * 1.16,
        y: Math.sin(angle) * radius * 0.88,
      }];
    }));
    const maxCenterDistance = Math.max(1, ...[...this.clusterCenters.values()].map((center) => Math.hypot(center.x / 1.16, center.y / 0.88)));
    const clusterOffsets = new Map();
    this.nodes = nodes.map((node, index) => {
      const prev = old.get(node.id);
      const ticker = String(node.ticker || "UNKNOWN");
      const center = this.clusterCenters.get(ticker) || { x: 0, y: 0 };
      const within = clusterOffsets.get(ticker) || 0;
      if (node.type !== "finance.entity") clusterOffsets.set(ticker, within + 1);
      const seed = this.stableUnit(node.id);
      const orientation = (this.stableUnit(`${ticker}:orientation`) - 0.5) * Math.PI;
      const angle = within * goldenAngle + (seed - 0.5) * (largeLayout ? 0.78 : 2.4);
      const lobe = 1 + Math.sin(angle * 3 + orientation) * 0.12;
      const seedR = node.type === "finance.entity"
        ? 0
        : ((largeLayout ? 32 : 54) + Math.sqrt(within + 0.5) * (largeLayout ? 16.5 : 27))
          * (0.75 + seed * 0.5) * lobe;
      const rawX = Math.cos(angle) * seedR * 1.1;
      const rawY = Math.sin(angle) * seedR * 0.94;
      const layoutDx = rawX * Math.cos(orientation) - rawY * Math.sin(orientation);
      const layoutDy = rawX * Math.sin(orientation) + rawY * Math.cos(orientation);
      const targetX = center.x + layoutDx;
      const targetY = center.y + layoutDy;
      const centerProgress = Math.min(1, Math.hypot(center.x / 1.16, center.y / 0.88) / maxCenterDistance);
      const revealDelay = node.type === "finance.entity"
        ? 160 + centerProgress * 6200
        : 220 + centerProgress * 6500 + seed * 1300;
      const startScale = first && largeLayout ? 0.018 : 1;
      return {
        ...node,
        deg: degree.get(node.id) || 0,
        x: (!layoutModeChanged && prev?.x !== undefined) ? prev.x : targetX * startScale + Math.cos(index * 1.618) * (first && largeLayout ? 2.5 : 0),
        y: (!layoutModeChanged && prev?.y !== undefined) ? prev.y : targetY * startScale + Math.sin(index * 1.618) * (first && largeLayout ? 2.5 : 0),
        layoutDx: (!layoutModeChanged && prev?.layoutDx !== undefined) ? prev.layoutDx : layoutDx,
        layoutDy: (!layoutModeChanged && prev?.layoutDy !== undefined) ? prev.layoutDy : layoutDy,
        revealDelay: prev?.revealDelay ?? (first && largeLayout ? revealDelay : 0),
        vx: 0,
        vy: 0,
        fx: null,
        fy: null,
      };
    });
    this.nodeById = new Map(this.nodes.map((n) => [n.id, n]));
    this.companyNodes = new Map(this.nodes
      .filter((node) => node.type === "finance.entity")
      .map((node) => [String(node.ticker || "UNKNOWN"), node]));
    this.links = this.dedupe(links, this.directed).filter((l) => this.nodeById.has(l.source) && this.nodeById.has(l.target));
    this.largeLayout = largeLayout;
    if (first && this.largeLayout) {
      this.layoutStartedAt = performance.now();
      this.bloomEndsAt = this.layoutStartedAt + this.BLOOM_DURATION + this.BLOOM_SETTLE;
    } else if (!continuingBloom) {
      this.layoutStartedAt = performance.now() - this.BLOOM_DURATION;
      this.bloomEndsAt = 0;
    }
    this.hasLoaded = true;
    this.hasMotion = true;
    this.pendingLocalFitAt = this.largeLayout ? 0 : performance.now() + 700;
    this.reheat(this.largeLayout ? (first ? 0.95 : 0.48) : (first ? 1 : 0.7));
    this.needsDraw = true;
    if (first) requestAnimationFrame(() => this.fit(false, this.largeLayout));
  }

  stableUnit(value) {
    let hash = 2166136261;
    const text = String(value || "");
    for (let i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0) / 4294967295;
  }

  dedupe(links, directed = false) {
    const seen = new Set();
    const out = [];
    for (const l of links) {
      const key = directed
        ? `${l.source}|${l.target}|${l.rel || "edge"}`
        : (l.source < l.target ? `${l.source}|${l.target}` : `${l.target}|${l.source}`);
      if (!seen.has(key)) { seen.add(key); out.push(l); }
    }
    return out;
  }

  reheat(value = 0.6) {
    this.alpha = Math.max(this.alpha, value);
    this.needsDraw = true;
  }

  radius(node) {
    if (node.type === "finance.entity") return this.largeLayout ? 24 : 34;
    if (node.type === "finance.filing") return this.largeLayout ? 3.6 : 5.8;
    return 3.4 + Math.sqrt(node.deg) * 1.7 + (node.type === "finance.entity" ? 2 : 0);
  }

  revealProgress(node, now = performance.now()) {
    if (!this.largeLayout || !this.bloomEndsAt) return 1;
    const elapsed = now - this.layoutStartedAt - (node.revealDelay || 0);
    return Math.max(0, Math.min(1, elapsed / 900));
  }

  easeOut(value) {
    return 1 - Math.pow(1 - value, 3);
  }

  /* ---- simulation ---- */
  step() {
    const nodes = this.nodes;
    const alpha = this.alpha;
    const now = performance.now();
    let motion = 0;

    // Small graphs use pairwise repulsion. Large graphs skip the O(n²) term
    // and animate with company-center and link forces only.
    if (!this.largeLayout) {
      for (let i = 0; i < nodes.length; i += 1) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j += 1) {
          const b = nodes[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let d2 = dx * dx + dy * dy;
          if (d2 < 0.01) { dx = (Math.random() - 0.5) * 0.6; dy = (Math.random() - 0.5) * 0.6; d2 = dx * dx + dy * dy + 0.01; }
          const d = Math.sqrt(d2);
          const force = (this.CHARGE * alpha) / d2;
          const fx = (dx / d) * force;
          const fy = (dy / d) * force;
          a.vx -= fx; a.vy -= fy;
          b.vx += fx; b.vy += fy;

          // Keep the loose Obsidian-like layout, but prevent nodes from
          // settling on top of one another.
          const minDistance = this.radius(a) + this.radius(b) + 13;
          if (d < minDistance) {
            const collision = ((minDistance - d) / minDistance) * 2.8 * alpha;
            a.vx -= (dx / d) * collision;
            a.vy -= (dy / d) * collision;
            b.vx += (dx / d) * collision;
            b.vy += (dy / d) * collision;
          }
        }
      }
    }

    // Link springs are useful for small local graphs. The complete graph uses
    // stable radial targets so thousands of filings do not collapse onto hubs.
    if (!this.largeLayout) {
      for (const link of this.links) {
        const a = this.nodeById.get(link.source);
        const b = this.nodeById.get(link.target);
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
        const diff = ((d - this.LINK_DIST) / d) * this.LINK_STRENGTH * alpha;
        const fx = dx * diff;
        const fy = dy * diff;
        a.vx += fx; a.vy += fy;
        b.vx -= fx; b.vy -= fy;
      }
    }

    // Company clustering + gentle gravity to the overall center + integrate.
    // On a large graph, filings are elastic satellites of the company hub. That
    // keeps all 11k+ nodes interactive without an O(n²) simulation.
    for (const node of nodes) {
      if (node.fx !== null) { node.x = node.fx; node.y = node.fy; node.vx = 0; node.vy = 0; continue; }
      const cluster = this.clusterCenters?.get(String(node.ticker || "UNKNOWN"));
      if (cluster && this.largeLayout) {
        let targetX = cluster.x;
        let targetY = cluster.y;
        const progress = this.easeOut(this.revealProgress(node, now));
        if (node.type === "finance.entity") {
          targetX = cluster.x * progress;
          targetY = cluster.y * progress;
        } else {
          const hub = this.companyNodes.get(String(node.ticker || "UNKNOWN"));
          targetX = (hub?.x ?? cluster.x) + node.layoutDx * progress;
          targetY = (hub?.y ?? cluster.y) + node.layoutDy * progress;
        }
        const spring = 0.026 + this.CLUSTER_GRAVITY * Math.max(0.22, alpha);
        node.vx += (targetX - node.x) * spring;
        node.vy += (targetY - node.y) * spring;
      }
      node.vx -= node.x * this.GRAVITY * alpha;
      node.vy -= node.y * this.GRAVITY * alpha;
      node.vx = Math.max(-this.MAX_V, Math.min(this.MAX_V, node.vx * this.VELOCITY_DECAY));
      node.vy = Math.max(-this.MAX_V, Math.min(this.MAX_V, node.vy * this.VELOCITY_DECAY));
      node.x += node.vx;
      node.y += node.vy;
      motion += Math.abs(node.vx) + Math.abs(node.vy);
    }

    this.hasMotion = motion / Math.max(1, nodes.length) > 0.002;
    this.alpha += (0 - this.alpha) * (this.largeLayout ? 0.04 : this.ALPHA_DECAY);
    if (this.alpha < this.ALPHA_MIN) this.alpha = 0;
  }

  /* ---- view transforms ---- */
  toScreen(x, y) { return { x: x * this.transform.k + this.transform.x, y: y * this.transform.k + this.transform.y }; }
  toWorld(x, y) { return { x: (x - this.transform.x) / this.transform.k, y: (y - this.transform.y) / this.transform.k }; }

  fit(animated = true, useTargets = this.largeLayout, options = {}) {
    if (!this.nodes.length) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const node of this.nodes) {
      const cluster = this.clusterCenters?.get(String(node.ticker || "UNKNOWN"));
      const x = useTargets && cluster ? cluster.x + (node.type === "finance.entity" ? 0 : node.layoutDx) : node.x;
      const y = useTargets && cluster ? cluster.y + (node.type === "finance.entity" ? 0 : node.layoutDy) : node.y;
      minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y);
    }
    const w = Math.max(1, maxX - minX);
    const h = Math.max(1, maxY - minY);
    const padding = options.padding ?? 0.9;
    const maxZoom = options.maxZoom ?? 1.8;
    const k = Math.max(0.025, Math.min(maxZoom, padding * Math.min(this.width / w, this.height / h)));
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const target = { k, x: this.width / 2 - cx * k, y: this.height / 2 - cy * k };
    if (!animated) { this.transform = target; this.needsDraw = true; return; }
    this.animateTo(target);
  }

  fitLocal(animated = true) {
    const count = this.nodes.length;
    const maxZoom = count <= 12 ? 3 : count <= 40 ? 2.5 : count <= 120 ? 2.1 : 1.4;
    const padding = count <= 12 ? 0.78 : count <= 40 ? 0.86 : count <= 120 ? 0.94 : 0.92;
    this.fit(animated, false, { padding, maxZoom });
  }

  animateTo(target) {
    const start = { ...this.transform };
    const t0 = performance.now();
    const dur = 380;
    const easeOut = (t) => 1 - Math.pow(1 - t, 3);
    const run = (now) => {
      const t = Math.min(1, (now - t0) / dur);
      const e = easeOut(t);
      this.transform = { k: start.k + (target.k - start.k) * e, x: start.x + (target.x - start.x) * e, y: start.y + (target.y - start.y) * e };
      this.needsDraw = true;
      if (t < 1) requestAnimationFrame(run);
    };
    requestAnimationFrame(run);
  }

  zoomBy(factor, cx, cy) {
    const k = Math.max(0.025, Math.min(6, this.transform.k * factor));
    const px = cx ?? this.width / 2;
    const py = cy ?? this.height / 2;
    const world = this.toWorld(px, py);
    this.transform.k = k;
    this.transform.x = px - world.x * k;
    this.transform.y = py - world.y * k;
    this.needsDraw = true;
  }

  /* ---- interaction ---- */
  eventPos(event) {
    const rect = this.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  nearest(sx, sy) {
    const world = this.toWorld(sx, sy);
    let best = null;
    let bestD = 22 / this.transform.k;
    for (const node of this.nodes) {
      const d = Math.hypot(node.x - world.x, node.y - world.y);
      const hit = Math.max(bestD, this.radius(node) + 6 / this.transform.k);
      if (d < hit && (!best || d < bestD)) { best = node; bestD = d; }
    }
    return best;
  }

  onPointerDown(event) {
    const p = this.eventPos(event);
    const node = this.nearest(p.x, p.y);
    this.pointer = { down: true, dragNode: node, panning: !node, moved: 0, lastX: p.x, lastY: p.y, downX: p.x, downY: p.y };
    if (node) { node.fx = node.x; node.fy = node.y; this.reheat(0.4); }
    else this.canvas.classList.add("grabbing");
    this.canvas.setPointerCapture?.(event.pointerId);
  }

  onPointerMove(event) {
    const p = this.eventPos(event);
    if (this.pointer.down) {
      const dx = p.x - this.pointer.lastX;
      const dy = p.y - this.pointer.lastY;
      this.pointer.moved += Math.abs(dx) + Math.abs(dy);
      if (this.pointer.dragNode) {
        const world = this.toWorld(p.x, p.y);
        this.pointer.dragNode.fx = world.x;
        this.pointer.dragNode.fy = world.y;
        if (this.pointer.dragNode.type === "finance.entity") this.clusterCenters.set(String(this.pointer.dragNode.ticker || "UNKNOWN"), { x: world.x, y: world.y });
        this.reheat(0.35);
      } else if (this.pointer.panning) {
        this.transform.x += dx;
        this.transform.y += dy;
        this.needsDraw = true;
      }
      this.pointer.lastX = p.x;
      this.pointer.lastY = p.y;
      return;
    }
    const node = this.nearest(p.x, p.y);
    const nextHoverId = node?.id || null;
    if (nextHoverId !== this.hoverId) {
      this.hoverId = nextHoverId;
      this.needsDraw = true;
    }
    this.canvas.style.cursor = node ? "pointer" : "grab";
  }

  onPointerUp(event) {
    this.canvas.classList.remove("grabbing");
    const wasClick = this.pointer.down && this.pointer.moved < 5;
    const node = this.pointer.dragNode;
    if (node) {
      const cluster = this.clusterCenters?.get(String(node.ticker || "UNKNOWN"));
      if (cluster && node.type !== "finance.entity") {
        const hub = this.companyNodes?.get(String(node.ticker || "UNKNOWN"));
        node.layoutDx = node.x - (hub?.x ?? cluster.x);
        node.layoutDy = node.y - (hub?.y ?? cluster.y);
      } else if (cluster && node.type === "finance.entity") {
        cluster.x = node.x;
        cluster.y = node.y;
      }
      node.fx = null;
      node.fy = null;
      this.reheat(0.15);
    }
    if (wasClick && node) this.onSelect(node.id);
    this.pointer = { down: false, dragNode: null, panning: false, moved: 0, lastX: 0, lastY: 0, downX: 0, downY: 0 };
  }

  onWheel(event) {
    event.preventDefault();
    const p = this.eventPos(event);
    const factor = Math.exp(Math.max(-80, Math.min(80, -event.deltaY)) * 0.0022);
    this.zoomBy(factor, p.x, p.y);
  }

  /* ---- rendering ---- */
  tick() {
    const now = performance.now();
    const blooming = this.bloomEndsAt > now;
    const moving = this.alpha > 0 || blooming || this.hasMotion || Boolean(this.pointer.dragNode);
    if (moving) this.step();
    if (this.pendingLocalFitAt && now >= this.pendingLocalFitAt) {
      this.pendingLocalFitAt = 0;
      this.fitLocal(true);
    }
    if (moving || this.needsDraw) {
      this.draw();
      this.needsDraw = false;
    }
    requestAnimationFrame(() => this.tick());
  }

  neighborsOf(id) {
    const set = new Set([id]);
    for (const l of this.links) {
      if (l.source === id) set.add(l.target);
      if (l.target === id) set.add(l.source);
    }
    return set;
  }

  draw() {
    const ctx = this.ctx;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);

    // background wash
    const grad = ctx.createRadialGradient(this.width / 2, this.height / 2, 40, this.width / 2, this.height / 2, Math.max(this.width, this.height) * 0.75);
    grad.addColorStop(0, "#181818");
    grad.addColorStop(1, "#111111");
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, this.width, this.height);

    const t = this.transform;
    ctx.save();
    ctx.translate(t.x, t.y);
    ctx.scale(t.k, t.k);

    const now = performance.now();
    const focusId = this.hoverId;
    const active = focusId ? this.neighborsOf(focusId) : null;

    // links
    ctx.lineWidth = 1 / t.k;
    for (const link of this.links) {
      const a = this.nodeById.get(link.source);
      const b = this.nodeById.get(link.target);
      if (!a || !b) continue;
      const reveal = Math.min(this.revealProgress(a, now), this.revealProgress(b, now));
      if (reveal <= 0.01) continue;
      const lit = active && (active.has(a.id) && active.has(b.id) && (a.id === focusId || b.id === focusId));
      const stroke = lit ? "rgba(213,223,201,0.72)" : active ? "rgba(150,150,150,0.035)" : this.directed ? "rgba(174,184,160,0.38)" : "rgba(150,150,150,0.075)";
      ctx.strokeStyle = stroke;
      ctx.globalAlpha = reveal;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
      if (link.directed || this.directed) this.drawArrowhead(ctx, a, b, stroke, t.k);
      ctx.globalAlpha = 1;
    }

    // Keep the graph clear: a node's name is visible only while it is hovered.
    for (const node of this.nodes) {
      const reveal = this.revealProgress(node, now);
      if (reveal <= 0.01) continue;
      const r = Math.max(this.radius(node), (node.type === "finance.entity" ? 2.8 : 0.65) / t.k);
      const selected = node.id === this.selectedId;
      const hovered = node.id === this.hoverId;
      const dim = active && !active.has(node.id);
      ctx.globalAlpha = reveal * (dim ? 0.11 : 1);

      if (selected || hovered) {
        ctx.beginPath();
        ctx.arc(node.x, node.y, r + 5 / t.k, 0, Math.PI * 2);
        ctx.fillStyle = selected ? "rgba(174,184,160,0.22)" : "rgba(242,238,232,0.14)";
        ctx.fill();
      }

      ctx.beginPath();
      ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
      ctx.fillStyle = nodeColor(node);
      ctx.fill();
      ctx.lineWidth = (selected ? 2 : node.type === "finance.filing" ? 0.45 : 1) / t.k;
      ctx.strokeStyle = selected ? "#f2eee8" : "rgba(18,15,15,0.85)";
      ctx.stroke();

      const labeled = hovered || (this.directed && this.nodes.length <= 24);
      if (labeled) {
        const fontPx = (selected || hovered ? 11.5 : 9) / t.k;
        ctx.font = `${selected || hovered ? 600 : 500} ${fontPx}px ${getComputedStyle(document.body).fontFamily}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = selected ? "#f2eee8" : dim ? "rgba(130,119,114,0.6)" : "#b9aea8";
        const label = String(node.type === "finance.entity" && !selected && !hovered ? (node.ticker || node.title) : (node.title || node.id));
        const max = selected || hovered ? 46 : 30;
        ctx.fillText(label.length > max ? label.slice(0, max) + "…" : label, node.x, node.y + r + 3 / t.k);
      }
      ctx.globalAlpha = 1;
    }
    ctx.restore();
  }

  drawArrowhead(ctx, a, b, stroke, zoom) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.hypot(dx, dy) || 1;
    const ux = dx / dist;
    const uy = dy / dist;
    const targetRadius = this.radius(b) + 2 / zoom;
    const tipX = b.x - ux * targetRadius;
    const tipY = b.y - uy * targetRadius;
    const len = 8 / zoom;
    const width = 4.5 / zoom;
    const baseX = tipX - ux * len;
    const baseY = tipY - uy * len;
    const px = -uy * width;
    const py = ux * width;
    ctx.fillStyle = stroke;
    ctx.beginPath();
    ctx.moveTo(tipX, tipY);
    ctx.lineTo(baseX + px, baseY + py);
    ctx.lineTo(baseX - px, baseY - py);
    ctx.closePath();
    ctx.fill();
  }
}

/* ------------------------------------------------------------------ search */
function runSearch(query) {
  const q = query.trim().toLowerCase();
  if (!q) { els.searchResults.classList.remove("open"); els.searchResults.innerHTML = ""; return; }
  const results = [];
  const pool = state.answerGraph?.nodes || state.visibleNodes;
  for (const node of pool) {
    const hay = `${node.title} ${node.ticker || ""}`.toLowerCase();
    if (hay.includes(q)) { results.push(node); if (results.length >= 40) break; }
  }
  els.searchResults.innerHTML = results.map((node) =>
    `<div class="search-item" data-id="${node.id}">
       <span class="dot" style="background:${nodeColor(node)}"></span>
       <span class="st-title">${escapeHtml(node.title || node.id)}</span>
       <span class="st-type">${nodeLabel(node)}</span>
     </div>`).join("");
  els.searchResults.classList.toggle("open", results.length > 0);
}

/* ------------------------------------------------------------------ legend */
function buildLegend() {
  const present = new Set(state.nodes.filter((node) => node.type === "finance.filing").map(filingForm));
  const filingItems = Object.entries(FILING_COLORS)
    .filter(([form]) => form !== "OTHER" ? present.has(form) : present.has("OTHER"))
    .map(([form, color]) =>
      `<span class="legend-item"><span class="dot" style="background:${color}"></span>${form === "OTHER" ? "Other filing" : form}</span>`);
  els.legend.innerHTML = [
    `<span class="legend-item"><span class="dot" style="background:${NODE_COLORS.entity}"></span>Company</span>`,
    ...filingItems,
  ].join("");
}

/* ------------------------------------------------------------------ events */
function bindEvents() {
  els.homeButton.addEventListener("click", showHomeGraph);
  els.graphButton.addEventListener("click", () => setView("graph"));
  els.markdownButton.addEventListener("click", () => setView("markdown"));
  els.providerButton.addEventListener("click", () => {
    if (!state.agentRunning) setProviderPanel(!state.providerPanelOpen);
  });
  for (const button of els.providerChoices) {
    button.addEventListener("click", () => chooseProvider(button.dataset.provider));
  }
  els.providerChangeButton.addEventListener("click", clearProviderChoice);

  els.chatForm.addEventListener("submit", (event) => { event.preventDefault(); submitChat(els.chatInput.value); });
  els.historyButton.addEventListener("click", () => toggleVaultHistory());
  els.vaultHistoryList.addEventListener("click", async (event) => {
    const item = event.target.closest(".vault-history-item");
    if (!item) return;
    toggleVaultHistory(false);
    const vault = state.vaults.find((entry) => entry.vault_id === item.dataset.id);
    if (vault) await selectVault(vault);
  });
  els.editToggle.addEventListener("click", () => setEditing(!state.editing));
  els.saveButton.addEventListener("click", saveDocument);
  els.docEditor.addEventListener("input", () => { if (els.docEditor.value !== state.currentRaw) setDirty(true); else setDirty(false); });
  els.docRendered.addEventListener("click", (event) => {
    const link = event.target.closest("a.wikilink");
    if (link && link.dataset.id) { event.preventDefault(); openNode(link.dataset.id); }
    else if (link && link.classList.contains("missing")) event.preventDefault();
  });

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (state.activeView === "markdown") saveDocument();
    }
  });

  els.search.addEventListener("input", () => runSearch(els.search.value));
  els.search.addEventListener("focus", () => runSearch(els.search.value));
  els.searchResults.addEventListener("click", (event) => {
    const item = event.target.closest(".search-item");
    if (!item) return;
    els.search.value = "";
    els.searchResults.classList.remove("open");
    handleGraphSelect(item.dataset.id);
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".graph-search")) els.searchResults.classList.remove("open");
    if (!event.target.closest(".vault-switcher-dock")) toggleVaultHistory(false);
    if (!event.target.closest(".provider-panel") && !event.target.closest("#providerButton")) setProviderPanel(false);
  });

}

function handleGraphSelect(id) {
  if (state.answerGraph && state.answerGraphNodes.has(id)) {
    const node = state.answerGraphNodes.get(id);
    state.selectedId = id;
    renderGraph();
    if (node?.path) openDocument(node.path, node.title || stemOf(node.path), { editable: false, showMarkdown: true });
    return;
  }
  const node = state.nodeById.get(id);
  openNode(id, { showMarkdown: node?.type !== "finance.entity" });
}

/* ------------------------------------------------------------------ init */
async function init() {
  try {
    await loadIndex();
    await loadVaultHistory();
    buildLegend();
    state.graph = new ForceGraph(els.canvas, (id) => handleGraphSelect(id));
    bindEvents();
    renderProviderControls();
    toggleVaultHistory(false);
    showHomeGraph();
  } catch (error) {
    els.docRendered.textContent = error.message;
    setView("markdown");
    addChatMessage("assistant", error.message);
  }
}

init();
