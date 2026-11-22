// node --test cloud/cloudflare/worker.test.mjs
// The worker's decisions with Kaggle, Telegram and KV faked: a whole video, a failed chapter, a stale run, a full
// Kaggle, a kernel that dies without a word, two chapters finishing at once.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.js";

function world({ refuse = () => false, status = () => "running", perDay = 1 } = {}) {
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
      pushes.push({ ...run, kernel: body.slug, limit: body.sessionTimeoutSeconds, shape: body.machineShape, gpuOn: body.enableGpu,
                    dataset: body.datasetDataSources[0].split("/")[1] });
      return new Response(JSON.stringify({ ref: body.slug }));
    }
    if (String(url).includes("kernels/status")) return new Response(JSON.stringify({ status: status(String(url)) }));
    if (body?.text) told.push(body.text);
    return new Response("{}");
  };
  const env = { KV, HOOK_SECRET: "s", TELEGRAM_BOT_TOKEN: "t", TELEGRAM_CHAT_ID: "7", KAGGLE_USERNAME: "u", KAGGLE_KEY: "k" };
  kv.set("kernel", "RUN = __RUN__");
  kv.set("plan", JSON.stringify({ days: [1, 2, 3].map((d) => ({ day: d, title: `Battle number ${d}`, subject: "x", minutes: 22 })) }));
  kv.set("state", JSON.stringify({ paused: false, next_day: 1, per_day: perDay }));
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
  assert.match(w.told.at(-1) ?? "", /Video ready: Best/);
  assert.equal(w.state().batch, null, "the day's batch is over");
  assert.equal(w.pushes.filter((r) => r.stage === "research").length, 1, "today's one video is made: no other starts");
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
  assert.equal(w.state().batch, null);
  assert.equal(w.state().tries["1"], 1);
  assert.deepEqual(w.state().retry, [1], "tried again in the next batch");
  assert.equal(w.pushes.filter((r) => r.stage === "join").length, 0);
});

test("a quality failure is not run again", async () => {
  const w = world();
  await toParts(w, 1);
  await w.report(w.last("part", 0), "done");
  const joins = w.pushes.filter((r) => r.stage === "join").length;
  await w.report(w.last("join"), "failed", { error: "quality gate: 6 of 10", kind: "quality" });
  assert.equal(w.pushes.filter((r) => r.stage === "join").length, joins);
  assert.equal(w.state().batch, null);
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
  const w = world({ status: (url) => (url.includes("studio-render-0-1") ? "error" : "running") });
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

test("a stage out of the vision API's free calls waits until they come back", async () => {
  const w = world();
  await w.tick();
  await w.report(w.last("research"), "done");
  await w.report(w.last("gpu"), "failed", { error: "service busy: wait=7200 quota", kind: "busy", wait: 7200 });
  const unit = JSON.parse(w.kv.get(`unit:${w.last("gpu").id}:gpu`));
  assert.equal(unit.state, "later");
  assert.ok(Math.abs(unit.after - (Date.now() + 7500e3)) < 5000, "two hours and five minutes, not half an hour");
  assert.match(w.told.at(-1), /free calls of the day come back/);
});

test("a kernel waiting in Kaggle's queue for hours is not counted as lost", async () => {
  const w = world({ status: () => "queued" });
  await toParts(w, 1);
  const key = `unit:${w.last("part", 0).id}:part0`;
  w.kv.set(key, JSON.stringify({ ...JSON.parse(w.kv.get(key)), at: Date.now() - 6 * 3600e3 }));
  const before = w.pushes.length;
  await w.tick();
  assert.equal(w.pushes.length, before, "not pushed again");
  assert.equal(JSON.parse(w.kv.get(key)).state, "pushed");
});

// ---------- the day's batch: one GPU session for all of the day's videos ----------

async function batchToGpu(w) {
  await w.tick();
  const research = w.pushes.filter((r) => r.stage === "research");
  for (const r of research) await w.report(r, "done");
  return research.map((r) => r.id);
}
const gpuReport = (w, id, event, extra = {}) =>
  w.call("/hook", { event, id, stage: "gpu", nonce: w.last("gpu").nonce, ...extra });

test("the day's videos start together and share one GPU session after all their research", async () => {
  const w = world({ perDay: 3 });
  await w.tick();
  const research = w.pushes.filter((r) => r.stage === "research");
  assert.deepEqual(research.map((r) => [r.day, r.kernel]), [[1, "u/studio-research-0"], [2, "u/studio-research-1"], [3, "u/studio-research-2"]]);
  assert.equal(w.state().started[Object.keys(w.state().started)[0]], 3);
  await w.report(research[0], "done");
  await w.report(research[1], "done");
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 0, "not before the last research");
  await w.report(research[2], "done");
  const gpu = w.pushes.filter((r) => r.stage === "gpu");
  assert.equal(gpu.length, 1);
  assert.deepEqual(gpu[0].jobs.map((j) => j.day), [1, 2, 3]);
  assert.deepEqual([gpu[0].kernel, gpu[0].gpuOn, gpu[0].shape, gpu[0].limit], ["u/studio-gpu", true, undefined, undefined],
    "pushed like a notebook saved in the browser: a machine shape or a time limit put it in a queue for hours");
});

test("a video's chapters render while the GPU makes the next one, each on its own kernels", async () => {
  const w = world({ perDay: 2 });
  const [a, b] = await batchToGpu(w);
  await gpuReport(w, a, "stage");
  await gpuReport(w, a, "done", { chapters: 2 });
  assert.deepEqual(w.pushes.filter((r) => r.stage === "part").map((r) => r.kernel), ["u/studio-render-0-0", "u/studio-render-0-1"]);
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 1, "the session goes on with the next video");
  await gpuReport(w, b, "done", { chapters: 1 });
  assert.deepEqual(w.pushes.filter((r) => r.stage === "part").map((r) => r.kernel).at(-1), "u/studio-render-1-0");
});

test("a video whose GPU part fails goes into a second session once the first has finished the others", async () => {
  const w = world({ perDay: 3 });
  const [a, b, c] = await batchToGpu(w);
  await gpuReport(w, a, "done", { chapters: 1 });
  await gpuReport(w, b, "failed", { error: "boom" });
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 1, "the session still has day 3 to make");
  await gpuReport(w, c, "done", { chapters: 1 });
  const gpu = w.pushes.filter((r) => r.stage === "gpu");
  assert.equal(gpu.length, 2);
  assert.deepEqual(gpu[1].jobs.map((j) => j.id), [b]);
});

test("two researches ending at the same moment still lead to one GPU session", async () => {
  const w = world({ perDay: 2 });
  await w.tick();
  const research = w.pushes.filter((r) => r.stage === "research");
  await Promise.all(research.map((r) => w.report(r, "done")));
  await w.tick();
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 1);
});

test("a GPU session that ends without a word sends its unfinished videos into one new session", async () => {
  let dead = false;
  const w = world({ perDay: 3, status: (url) => (dead && url.includes("studio-gpu") ? "error" : "running") });
  const [a, b, c] = await batchToGpu(w);
  await gpuReport(w, a, "done", { chapters: 1 });
  dead = true;
  for (const id of [b, c]) {
    const key = `unit:${id}:gpu`;
    w.kv.set(key, JSON.stringify({ ...JSON.parse(w.kv.get(key)), at: Date.now() - 3600e3 }));
  }
  await w.tick();
  const gpu = w.pushes.filter((r) => r.stage === "gpu");
  assert.equal(gpu.length, 2);
  assert.deepEqual(gpu[1].jobs.map((j) => j.id).sort(), [b, c].sort());
});

test("the batch is only as big as the week's GPU hours allow", async () => {
  const w = world({ perDay: 3 });
  const s = w.state();
  const d = new Date(); d.setUTCHours(0, 0, 0, 0); d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 1) % 7));
  w.kv.set("state", JSON.stringify({ ...s, gpu: { [d.toISOString().slice(0, 10)]: 25 }, gpu_runs: { x: 1.5 } }));
  await w.tick();
  assert.equal(w.pushes.filter((r) => r.stage === "research").length, 2, "29 h a week, 25 used, 1.5 a video");
});

// ---------- smoke film and the code the daily videos run ----------

test("the daily videos run the code the last smoke film passed; test films run the newest", async () => {
  const w = world();
  w.kv.set("code_version", "new1");
  w.kv.set("good_version", "good0");
  await w.tick();
  assert.deepEqual([w.last("research").code, w.last("research").dataset], ["good0", "history-studio-code-good"]);
  const t = world();
  t.kv.set("code_version", "new1");
  t.kv.set("good_version", "good0");
  await t.call("/start", { day: 0, title: "A test", subject: "x", minutes: 8 });
  assert.deepEqual([t.last("research").code, t.last("research").dataset], ["new1", "history-studio-code"]);
});

test("before any smoke film has passed, the daily videos run the newest code", async () => {
  const w = world();
  w.kv.set("code_version", "new1");
  await w.tick();
  assert.deepEqual([w.last("research").code, w.last("research").dataset], ["new1", "history-studio-code"]);
});

test("a smoke film that gets through every stage marks its code as passed, without Yes or No", async () => {
  const w = world();
  w.kv.set("code_version", "cand7");
  await w.call("/start", { day: 0, title: "Smoke test", subject: "x", minutes: 1, smoke: true });
  assert.equal(w.last("research").smoke, true);
  await w.report(w.last("research"), "done");
  await w.report(w.last("gpu"), "done", { chapters: 1 });
  await w.report(w.last("part", 0), "done");
  await w.report(w.last("join"), "ready", { link: "L", quality: "9 of 15 checks pass" });
  assert.equal(w.kv.get("smoke_passed"), "cand7");
  assert.ok(w.told.some((t) => /SMOKE FILM passed/.test(t)));
  assert.ok(!w.told.some((t) => /Video ready/.test(t)), "no Yes or No for a smoke film");
});

test("a smoke film does not take the place of a test film that is still running", async () => {
  const w = world();
  await w.call("/start", { day: 0, title: "A test", subject: "x", minutes: 8 });
  const out = await w.call("/start", { day: 0, title: "Smoke test", subject: "x", minutes: 1, smoke: true });
  assert.match(out.message, /still running/);
  assert.equal(w.pushes.filter((r) => r.stage === "research").length, 1);
});
