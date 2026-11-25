// Offline checks for the actual collection's shared response assertions.
// Run: node --test tests/postman_contract.test.cjs
// The JSON fixture was produced by build_plan using synthetic station prices,
// projected positions, and explicit road legs. It is not a measured live trip.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const collection = JSON.parse(
  fs.readFileSync(path.join(root, "docs/spotter.postman_collection.json"), "utf8"),
);
const fixture = JSON.parse(
  fs.readFileSync(path.join(__dirname, "fixtures/synthetic_route_response.json"), "utf8"),
);
const script = collection.event.find((event) => event.listen === "test").script.exec.join("\n");

// A deliberately small adapter for the Chai assertions used in the shared
// Postman script. Postman itself supplies the full pm.expect implementation.
function expect(actual) {
  const chain = {
    eql: (expected) => assert.deepEqual(actual, expected),
    match: (pattern) => assert.match(actual, pattern),
    include: (expected) => assert.ok(actual.includes(expected)),
    least: (minimum) => assert.ok(actual >= minimum, `${actual} < ${minimum}`),
    most: (maximum) => assert.ok(actual <= maximum, `${actual} > ${maximum}`),
    above: (minimum) => assert.ok(actual > minimum),
    within: (minimum, maximum) => assert.ok(actual >= minimum && actual <= maximum),
  };
  for (const name of ["to", "be", "at"]) chain[name] = chain;
  return chain;
}

function runSharedChecks(body, status = 200) {
  const failures = [];
  const variables = {};
  vm.runInNewContext(script, {
    pm: {
      request: { method: "POST" },
      response: { code: status, json: () => body },
      expect,
      test: (name, assertion) => {
        try {
          assertion();
        } catch (error) {
          failures.push({ name, message: error.message });
        }
      },
      collectionVariables: { set: (name, value) => { variables[name] = value; } },
    },
  });
  return { failures, variables };
}

test("every collection response script has valid JavaScript syntax", () => {
  for (const scope of [collection, ...collection.item]) {
    for (const event of scope.event || []) {
      assert.doesNotThrow(() => new vm.Script(event.script.exec.join("\n")));
    }
  }
});

test("the serialized two-call fixture passes every fuel contract check", () => {
  const { failures, variables } = runSharedChecks(fixture);
  assert.deepEqual(failures, []);
  assert.equal(variables.route_id, fixture.route_id);
  assert.equal(variables.map_url, fixture.map_url);
});

test("incorrect final fuel fails conservation", () => {
  const broken = structuredClone(fixture);
  broken.final_fuel_gallons = "1.000000";
  const { failures } = runSharedChecks(broken);
  assert.ok(failures.some((failure) => failure.name.includes("Fuel is conserved")));
});

test("incorrect total cost fails charge reconciliation", () => {
  const broken = structuredClone(fixture);
  broken.total_fuel_cost_usd = "123.01";
  const { failures } = runSharedChecks(broken);
  assert.ok(failures.some((failure) => failure.name.includes("Total purchase cost")));
});

test("a fourth routing request fails the dependency budget", () => {
  const broken = structuredClone(fixture);
  broken.performance.route_api_calls = 4;
  const { failures } = runSharedChecks(broken);
  assert.ok(failures.some((failure) => failure.name.includes("At most three")));
});

test("an over-range road leg fails physical feasibility", () => {
  const broken = structuredClone(fixture);
  broken.road_leg_distances_miles[1] = "501.000";
  const { failures } = runSharedChecks(broken);
  assert.ok(failures.some((failure) => failure.name.includes("Every road leg")));
});

test("an error response is left to the request-specific error checks", () => {
  assert.deepEqual(runSharedChecks({ error: { code: "outside_usa" } }, 400).failures, []);
});
