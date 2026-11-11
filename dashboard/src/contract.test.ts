import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

// The dashboard talks only to the API in docs/03-api/openapi.yaml: every /v1 path named
// anywhere in src/ must be one of its paths.
const src = import.meta.dirname;
const spec = readFileSync(join(src, "../../docs/03-api/openapi.yaml"), "utf8");
const canon = (path: string) => path.replace(/\$\{[^}]*\}|\{[^}]*\}/g, "*").replace(/\/+$/, "");
const documented = new Set([...spec.matchAll(/^ {2}(\/\S+):\s*$/gm)].map((m) => canon(m[1])));

test("every API path in the dashboard is in the contract", () => {
  const used = new Map<string, string>();
  for (const file of readdirSync(src, { recursive: true, encoding: "utf8" })) {
    if (!/\.tsx?$/.test(file) || file.endsWith(".test.ts")) continue;
    for (const m of readFileSync(join(src, file), "utf8").matchAll(/\/v1\/[^\s`"'?]*/g)) used.set(canon(m[0]), file);
  }
  assert.ok(used.size > 10, "found the dashboard's API calls");
  for (const [path, file] of used) assert.ok(documented.has(path), `${path} (in ${file}) is not in openapi.yaml`);
});
