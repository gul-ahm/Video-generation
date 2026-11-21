// node --test cloud/cloudflare/worker.test.mjs
// The worker's decisions with Kaggle, Telegram and KV faked: a whole video, a failed chapter, a stale run, a full
// Kaggle, a kernel that dies without a word, two chapters finishing at once.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.js";

function world({ refuse = () => false, status = () => "running" } = {}) {
  const kv = new Map(), told = [], pushes = [];
  const KV = {
    get: async (k) => kv.get(k) ?? null, put: async (k, v) => kv.set(k, v), delete: async (k) => kv.delete(k),
    list: async ({ prefix }) => ({ keys: [...kv.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })) }),
  };
  globalThis.fetch = async (url, o = {}) => {
    const body = o.body ? JSON.parse(o.body) : null;
    if (String(url).includes("kernels/push")) {
      const run = JSON.parse(body.text.match(/RUN = (.*)/)[1]);
      if (refuse(run)) return new Response(JSON.stringify({ error: "Maximum batch CPU session count reached" }));
      pushes.push(run);
      return new Response(JSON.stringify({ ref: body.slug }));
    }
    if (String(url).includes("kernels/status")) return new Response(JSON.stringify({ status: status(String(url)) }));
    if (body?.text) told.push(body.text);
    return new Response("{}");
  };
  const env = { KV, HOOK_SECRET: "s", TELEGRAM_BOT_TOKEN: "t", TELEGRAM_CHAT_ID: "7", KAGGLE_USERNAME: "u", KAGGLE_KEY: "k" };
  kv.set("kernel", "RUN = __RUN__");
  kv.set("plan", JSON.stringify({ days: [1, 2, 3].map((d) => ({ day: d, title: `Battle number ${d}`, subject: "x", minutes: 22 })) }));
  kv.set("state", JSON.stringify({ paused: false, next_day: 1 }));
  const call = (path, body) => worker.fetch(new Request(`https://w${path}`, {
    method: "POST", headers: { "X-Studio-Secret": "s" }, body: JSON.stringify(body) }), env).then((r) => r.json());
  const last = (stage, part) => pushes.filter((r) => r.stage === stage && (part === undefined || r.part === part)).at(-1);
  const report = (run, event, extra = {}) => call("/hook", { event, id: run.id, stage: run.stage, part: run.part, nonce: run.nonce, ...extra });
  const state = () => JSON.parse(kv.get("state"));
  return { kv, told, pushes, env, call, last, report, state, tick: () => call("/tick") };
}

async function toParts(w, chapters = 3) {
  await w.tick();
  await w.report(w.last("research"), "done");
  await w.report(w.last("gpu"), "stage");
  await w.report(w.last("gpu"), "done", { chapters });
}

test("a whole video: research, gpu, every chapter, join, ready", async () => {
  const w = world();
  await toParts(w, 3);
  assert.deepEqual(w.pushes.filter((r) => r.stage === "part").map((r) => r.part), [0, 1, 2]);
  for (const k of [0, 1, 2]) await w.report(w.last("part", k), "done");
  assert.equal(w.pushes.filter((r) => r.stage === "join").length, 1);
  await w.report(w.last("join"), "ready", { link: "L", best: "Best", quality: "9 of 10 checks pass" });
  assert.match(w.told.at(-2) ?? "", /Video ready: Best/);
  assert.equal(w.last("research").day, 2, "the next day starts once the video is ready");
});

test("a report from a replaced run changes nothing and says nothing", async () => {
  const w = world();
  await toParts(w, 2);
  const old = w.last("part", 0);
  await w.report(old, "failed", { error: "boom" });  // retried with a new nonce
  const told = w.told.length, pushed = w.pushes.length;
  await w.report(old, "done");
  await w.report(old, "progress", { line: "render 1/2 done" });
  assert.equal(w.told.length, told);
  assert.equal(w.pushes.length, pushed);
});

test("a chapter that fails twice fails the day once, and the day is tried again later", async () => {
  const w = world();
  await toParts(w, 2);
  await w.report(w.last("part", 1), "failed", { error: "boom" });
  await w.report(w.last("part", 1), "failed", { error: "boom again" });
  assert.equal(w.state().running, null);
  assert.equal(w.state().tries["1"], 1);
  assert.equal(w.pushes.filter((r) => r.stage === "join").length, 0);
});

test("a quality failure is not run again", async () => {
  const w = world();
  await toParts(w, 1);
  await w.report(w.last("part", 0), "done");
  const joins = w.pushes.filter((r) => r.stage === "join").length;
  await w.report(w.last("join"), "failed", { error: "quality gate: 6 of 10", kind: "quality" });
  assert.equal(w.pushes.filter((r) => r.stage === "join").length, joins);
  assert.equal(w.state().running, null);
});

test("chapters Kaggle refuses wait and are pushed when one finishes", async () => {
  let busy = true;
  const w = world({ refuse: (run) => busy && run.stage === "part" && run.part >= 2 });
  await toParts(w, 4);
  assert.deepEqual(w.pushes.filter((r) => r.stage === "part").map((r) => r.part), [0, 1]);
  busy = false;
  await w.report(w.last("part", 0), "done");
  assert.deepEqual(w.pushes.filter((r) => r.stage === "part").map((r) => r.part).sort(), [0, 1, 2, 3]);
  for (const k of [1, 2, 3]) await w.report(w.last("part", k), "done");
  assert.equal(w.pushes.filter((r) => r.stage === "join").length, 1);
});

test("two chapters finishing at the same moment still lead to one join", async () => {
  const w = world();
  await toParts(w, 2);
  await Promise.all([w.report(w.last("part", 0), "done"), w.report(w.last("part", 1), "done")]);
  await w.tick();  // whatever the order, the watch finds both done
  assert.equal(w.pushes.filter((r) => r.stage === "join").length, 1);
});

test("a kernel that ended without a word is noticed by the watch and run again", async () => {
  const w = world({ status: (url) => (url.includes("studio-render-1") ? "error" : "running") });
  await toParts(w, 2);
  const unit = JSON.parse(w.kv.get(`unit:${w.last("part", 1).id}:part1`));
  w.kv.set(`unit:${w.last("part", 1).id}:part1`, JSON.stringify({ ...unit, at: Date.now() - 3600e3 }));
  const before = w.pushes.filter((r) => r.stage === "part" && r.part === 1).length;
  await w.tick();
  assert.equal(w.pushes.filter((r) => r.stage === "part" && r.part === 1).length, before + 1);
});

test("pause stops new days but the running video is finished, retries included", async () => {
  const w = world();
  await toParts(w, 1);
  w.kv.set("state", JSON.stringify({ ...w.state(), paused: true }));
  await w.report(w.last("part", 0), "failed", { error: "boom" });
  assert.equal(w.pushes.filter((r) => r.stage === "part").length, 2);
  await w.report(w.last("part", 0), "done");
  await w.report(w.last("join"), "ready", { link: "L" });
  assert.equal(w.pushes.filter((r) => r.stage === "research").length, 1, "no new day while paused");
});

test("every push carries the code version the datasets hold", async () => {
  const w = world();
  w.kv.set("code_version", "abc123");
  await w.tick();
  assert.equal(w.last("research").code, "abc123");
});

test("cloud.py can go on with the chapters still missing", async () => {
  const w = world();
  const out = await w.call("/start", { day: 3, from: "parts", chapters: 6, done: [0, 1, 2, 3, 4] });
  assert.match(out.message, /goes on from parts/);
  assert.deepEqual(w.pushes.map((r) => [r.stage, r.part]), [["part", 5]]);
  await w.report(w.last("part", 5), "done");
  assert.equal(w.pushes.filter((r) => r.stage === "join").length, 1);
});

test("a stage whose service is down waits and runs again, without counting as a failure", async () => {
  const w = world();
  await w.tick();
  await w.report(w.last("research"), "done");
  await w.report(w.last("gpu"), "stage");
  await w.report(w.last("gpu"), "failed", { error: "service busy: no model could look at x.jpg", kind: "busy" });
  const unit = () => JSON.parse(w.kv.get(`unit:${w.last("gpu").id}:gpu`));
  assert.equal(unit().state, "later");
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 1, "not pushed again at once");
  await w.tick();
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 1, "not before the half hour");
  w.kv.set(`unit:${w.last("gpu").id}:gpu`, JSON.stringify({ ...unit(), after: Date.now() - 1 }));
  await w.tick();
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 2);
  assert.equal(unit().tries, 0);
  for (let i = 0; i < 6; i++) {
    await w.report(w.last("gpu"), "failed", { error: "service busy", kind: "busy" });
    w.kv.set(`unit:${w.last("gpu").id}:gpu`, JSON.stringify({ ...unit(), after: Date.now() - 1 }));
    await w.tick();
  }
  assert.equal(w.told.filter((t) => /stayed down/.test(t)).length, 1, "after three hours the day fails, once");
  assert.equal(w.state().tries["1"], 1);
});
