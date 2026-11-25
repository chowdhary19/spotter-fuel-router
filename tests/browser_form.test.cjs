// Exercise the actual browser submission handler without external tiles/routing.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures/synthetic_route_response.json")));

class Element {
  constructor() { this.value = ""; this.textContent = ""; this.children = []; this.handlers = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, handler) { this.handlers[name] = handler; }
}

function load(saved) {
  const elements = new Map();
  const get = id => {
    if (id === "saved-route" && !saved) return null;
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  if (saved) get("saved-route").textContent = JSON.stringify(saved);
  let sent;
  vm.runInNewContext(source, {
    document: { getElementById: get, createElement: () => new Element() },
    fetch: async (_, options) => {
      sent = JSON.parse(options.body);
      return { ok: true, json: async () => fixture };
    },
  });
  return { get, submit: async () => { await get("planner").handlers.submit({preventDefault(){}}); return sent; } };
}

test("saved coordinate route preserves exact input coordinates and reserve on resubmit", async () => {
  const saved = structuredClone(fixture);
  saved.start = { label: "41.87810, -87.62980", latitude: 41.8781234, longitude: -87.6298765, resolution: "user-supplied coordinates; Census boundary check" };
  saved.finish = { label: "Miami, FL", latitude: 25.7, longitude: -80.2, resolution: "Census representative place point" };
  saved.initial_fuel_gallons = "31.123456";
  saved.reserve_gallons = "5.123456";
  const app = load(saved);
  const sent = await app.submit();
  assert.deepEqual(sent.start, { latitude: 41.8781234, longitude: -87.6298765 });
  assert.equal(sent.finish, "Miami, FL");
  assert.equal(sent.initial_fuel_gallons, 31.123456);
  assert.equal(sent.reserve_gallons, 5.123456);
});

test("new numeric coordinates and zero quantities survive form serialization", async () => {
  const app = load();
  app.get("start").value = " 41.5, -87.8 ";
  app.get("finish").value = " Dallas, TX ";
  app.get("initial").value = "0";
  app.get("reserve").value = "0";
  assert.deepEqual(await app.submit(), {start:{latitude:41.5,longitude:-87.8},finish:"Dallas, TX",initial_fuel_gallons:0,reserve_gallons:0});
});

test("ambiguous textual inputs remain strings for authoritative backend validation", async () => {
  const app = load();
  app.get("start").value = "123 Main St, TX";
  app.get("finish").value = "Chicago, IL";
  app.get("initial").value = "50";
  app.get("reserve").value = "5";
  assert.equal((await app.submit()).start, "123 Main St, TX");
});
