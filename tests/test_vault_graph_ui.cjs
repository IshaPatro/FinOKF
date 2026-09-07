const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

function app() {
  const context = vm.createContext({
    document: { getElementById: () => ({}), querySelector: () => ({}), querySelectorAll: () => [] },
    setTimeout: () => {},
  });
  const source = fs.readFileSync(path.join(__dirname, "../ui/app.js"), "utf8");
  vm.runInContext(source.replace(/\ninit\(\);\s*$/, ""), context);
  vm.runInContext(`
    state.graph = { reheat() {} };
    renderVaultHistory = () => {};
    renderVaultInspector = () => {};
    renderGraph = () => {};
    setView = () => {};
  `, context);
  return context;
}

function fixture(id, ticker) {
  const source = { id: `snapshot:turn-001:${ticker}`, title: ticker, type: "finance.entity",
    path: `notes/turn-001-${ticker}.md`, source_path: `data/processed/companies/${ticker}.md` };
  return {
    kind: "chat-vault", vault_id: id, question: `Research ${ticker}`, graph_path: `data/vaults/answers/${id}/graph.json`,
    runs: [{ turn: 1 }], nodes: [source],
    graph: { nodes: [{ id, title: "Chat" }, { id: `${id}:1` }, source],
      links: [{ source: id, target: `${id}:1` }, { source: `${id}:1`, target: source.id }] },
  };
}

test("legacy Apple history is excluded from the current Microsoft answer", () => {
  const context = app();
  const apple = fixture("vault", "AAPL");
  const microsoft = fixture("vault", "MSFT");
  microsoft.runs = [{ turn: 0 }, { turn: 1 }];
  microsoft.graph.nodes.push(apple.nodes[0], { id: "vault:0" });
  microsoft.nodes.push(apple.nodes[0]);
  microsoft.graph.links.push({ source: "vault", target: "vault:0" }, { source: "vault:0", target: apple.nodes[0].id });
  const result = context.answerGraphData(microsoft, microsoft.graph);
  assert.equal(result.nodes.length, 3);
  assert.equal(result.nodes[0].title, "Research MSFT");
  assert.equal(result.nodes[2].ticker, "MSFT");
  assert.equal(result.nodes[2].path, "data/vaults/answers/vault/notes/turn-001-MSFT.md");
  assert.ok(result.nodes.every(node => !node.id.includes("AAPL")));
  assert.equal(result.links.length, 2);
});

test("comparisons keep both companies, but duplicate paths appear once", () => {
  const context = app();
  const vault = fixture("compare", "MSFT");
  const apple = fixture("compare", "AAPL").nodes[0];
  const duplicate = { ...vault.nodes[0], id: "duplicate" };
  vault.graph.nodes.push(apple, duplicate);
  vault.graph.links.push(...[apple, duplicate].map(node => ({ source: "compare:1", target: node.id })));
  const result = context.answerGraphData(vault, vault.graph);
  assert.equal(result.nodes.length, 4);
  assert.equal(result.links.length, 3);
  assert.deepEqual(new Set(result.nodes.filter(n => n.ticker).map(n => n.ticker)), new Set(["MSFT", "AAPL"]));
});

test("switching vaults replaces all graph nodes; an empty vault shows no prior graph", async () => {
  const context = app();
  await context.applyAnswerGraph(fixture("apple", "AAPL"));
  await context.applyAnswerGraph(fixture("microsoft", "MSFT"));
  assert.equal(vm.runInContext("state.currentVault.vault_id", context), "microsoft");
  assert.ok(vm.runInContext("state.answerGraph.nodes.every(n => !n.id.includes('AAPL'))", context));
  await context.applyAnswerGraph({ kind: "chat-vault", vault_id: "empty" });
  assert.equal(vm.runInContext("state.answerGraph.nodes.length", context), 0);
  assert.equal(vm.runInContext("state.answerGraphNodes.size", context), 0);
});

test("a slow graph fetch cannot overwrite a newer vault selection", async () => {
  const context = app();
  const slow = fixture("slow-apple", "AAPL");
  const responseGraph = slow.graph;
  delete slow.graph;
  let resolveFetch;
  context.fetch = () => new Promise(resolve => { resolveFetch = resolve; });
  const pending = context.selectVault(slow);
  vm.runInContext("showChatTranscript = vault => { globalThis.shownTranscript = vault.vault_id; };", context);
  await context.selectVault(fixture("fast-microsoft", "MSFT"));
  resolveFetch({ ok: true, json: async () => responseGraph });
  await pending;
  assert.equal(vm.runInContext("state.currentVault.vault_id", context), "fast-microsoft");
  assert.equal(context.shownTranscript, "fast-microsoft");
});

test("startup shows corpus graph without creating a vault or injected company context", () => {
  const source = fs.readFileSync(path.join(__dirname, "../ui/app.js"), "utf8");
  const markup = fs.readFileSync(path.join(__dirname, "../ui/index.html"), "utf8");
  assert.doesNotMatch(source, /Local AI context loaded/);
  assert.doesNotMatch(source, /setActiveVault\(\{ kind: "knowledge-graph"/);
  assert.doesNotMatch(source, /state\.index\.companies\?\.\[0\]/);
  assert.match(source, /function showHomeGraph\(\)/);
  assert.match(source, /overviewGraph\(\)/);
  assert.match(markup, /homeViewButton/);
  assert.match(markup, /No vault selected/);
});

test("agent stream delivers a completed answer before the other agent and handles split UTF-8", async () => {
  const context = app();
  context.TextDecoder = TextDecoder;
  let release;
  const resumed = new Promise(resolve => { release = resolve; });
  let chunks = 0;
  const first = Buffer.from(JSON.stringify({type: "answer", answer: {agent: "finokf", answer: "Cash €100"}}) + "\n");
  const split = first.indexOf(Buffer.from("€")) + 1;
  const response = {ok: true, headers: {get: () => "application/x-ndjson"}, body: {getReader: () => ({read: async () => {
    chunks += 1;
    if (chunks === 1) return {value: first.subarray(0, split), done: false};
    if (chunks === 2) return {value: first.subarray(split), done: false};
    if (chunks === 3) {
      await resumed;
      return {value: Buffer.from('{"type":"answer","answer":{"agent":"naive"}}\n{"type":"complete","ok":true}\n'), done: false};
    }
    return {done: true};
  }})}};
  const answers = [];
  const pending = context.readAgentStream(response, answer => {
    answers.push(answer);
    if (answer.agent === "finokf") {
      assert.equal(chunks, 2);
      assert.equal(answer.answer, "Cash €100");
      release();
    }
  });
  assert.equal((await pending).ok, true);
  assert.deepEqual(answers.map(a => a.agent), ["finokf", "naive"]);
});

test("disconnect retains the answer already emitted", async () => {
  const context = app();
  context.TextDecoder = TextDecoder;
  let sent = false;
  const response = {ok: true, headers: {get: () => "application/x-ndjson"}, body: {getReader: () => ({read: async () => {
    if (sent) return {done: true};
    sent = true;
    return {value: Buffer.from('{"type":"answer","answer":{"agent":"naive"}}\n'), done: false};
  }})}};
  const answers = [];
  await assert.rejects(context.readAgentStream(response, answer => answers.push(answer)), /Connection ended/);
  assert.equal(answers[0].agent, "naive");
});

test("agent answers render collapsed with a show more toggle", () => {
  const context = app();
  function fakeElement(tag = "div") {
    const element = {
      tag,
      children: [],
      dataset: {},
      className: "",
      hidden: false,
      textContent: "",
      attributes: {},
      events: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, handler) { this.events[name] = handler; },
      appendChild(child) { this.children.push(child); return child; },
      append(...children) { children.forEach(child => this.appendChild(child)); },
      querySelector(selector) { return this.selectors?.[selector] || null; },
      click() { this.events.click?.(); },
    };
    Object.defineProperty(element, "innerHTML", {
      get() { return this.html || ""; },
      set(value) {
        this.html = value;
        if (value.includes("agent-preview") || value.includes("agent-body")) {
          this.selectors = {
            ".agent-preview": fakeElement("div"),
            ".agent-body": fakeElement("div"),
          };
          this.selectors[".agent-body"].hidden = value.includes('class="agent-body" hidden');
        }
      },
    });
    return element;
  }
  context.document.createElement = fakeElement;
  context.fakeChatMessages = { scrollTop: 0, scrollHeight: 120 };
  vm.runInContext("els.chatMessages = fakeChatMessages;", context);
  const card = fakeElement();
  context.renderAgentResult(card, {agent: "finokf", ok: true, answer: "First paragraph. " + "Detailed evidence. ".repeat(80)});

  const button = card.children.find(child => child.className === "show-more-button");
  assert.equal(card.dataset.expanded, "false");
  assert.equal(button.textContent, "Show more");
  assert.equal(card.querySelector(".agent-body").hidden, true);
  assert.equal(card.children.find(child => child.className === "agent-extra").hidden, true);

  button.click();
  assert.equal(card.dataset.expanded, "true");
  assert.equal(button.textContent, "Show less");
  assert.equal(card.querySelector(".agent-body").hidden, false);
  assert.equal(card.querySelector(".agent-preview").hidden, true);
  assert.equal(card.children.find(child => child.className === "agent-extra").hidden, false);
});
