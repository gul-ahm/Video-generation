// node --test cloud/cloudflare/worker.test.mjs
// The worker's decisions with Kaggle, Telegram and KV faked: a whole video, a failed chapter, a stale run, a full
// Kaggle, a kernel that dies without a word, two chapters finishing at once.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "./worker.js";

// Kaggle's status of a kernel: the GPU kernel ended (nothing of ours or anyone's is there), the others running
function world({ refuse = () => false, status = (url) => (url.includes("studio-gpu") ? "complete" : "running"), perDay = 1 } = {}) {
  const kv = new Map(), told = [], pushes = [], deleted = [];
  const KV = {
    get: async (k) => kv.get(k) ?? null, put: async (k, v) => kv.set(k, v), delete: async (k) => kv.delete(k),
    list: async ({ prefix }) => ({ keys: [...kv.keys()].filter((k) => k.startsWith(prefix)).map((name) => ({ name })) }),
  };
  globalThis.fetch = async (url, o = {}) => {
    const body = o.body ? JSON.parse(o.body) : null;
    if (String(url).includes("DeleteKernel")) {
      deleted.push(body.kernelSlug);
      return new Response("{}");
    }
    if (String(url).includes("kernels/push") || String(url).includes("SaveKernel")) {
      const text = body.kernelType === "notebook" ? JSON.parse(body.text).cells[0].source : body.text;
      const run = JSON.parse(text.match(/RUN = (.*)/)[1]);
      if (refuse(run)) return new Response(JSON.stringify({ error: "Maximum batch CPU session count reached" }));
      pushes.push({ ...run, kernel: body.slug, limit: body.sessionTimeoutSeconds, shape: body.machineShape, gpuOn: body.enableGpu,
                    type: body.kernelType, how: body.kernelExecutionType, api: String(url).includes("api.kaggle.com"),
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
  const telegram = (text) => worker.fetch(new Request("https://w/telegram", {
    method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "s" },
    body: JSON.stringify({ message: { chat: { id: 7 }, message_id: 1, text } }) }), env).then(() => told.at(-1));
  const button = (data) => worker.fetch(new Request("https://w/telegram", {
    method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": "s" },
    body: JSON.stringify({ callback_query: { id: "q", data, message: { chat: { id: 7 }, message_id: 2 } } }) }), env);
  return { kv, told, pushes, deleted, env, call, last, report, state, telegram, button, tick: () => call("/tick") };
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
  const w = world({ status: (url) => (url.includes("studio-render-0-1") ? "error" : url.includes("studio-gpu") ? "complete" : "running") });
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
  const w = world({ status: (url) => (url.includes("studio-gpu") ? "complete" : "queued") });
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
  assert.deepEqual([gpu[0].kernel.slice(0, 13), gpu[0].gpuOn, gpu[0].shape, gpu[0].limit], ["u/studio-gpu-", true, undefined, undefined],
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
  const w = world({ perDay: 3, status: (url) => (url.includes("studio-gpu") && dead ? "error" : url.includes("studio-gpu") ? "complete" : "running") });
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

test("every GPU session is a kernel of its own, the watch asks Kaggle about that one", async () => {
  const asked = [];
  const w = world({ status: (url) => { asked.push(url); return "running"; } });
  await toParts(w, 1);
  const first = w.last("gpu").kernel;
  assert.match(first, /^u\/studio-gpu-[0-9a-f]{8}$/);
  const key = `unit:${w.last("gpu").id}:gpu`;
  await w.report(w.last("gpu"), "failed", { error: "boom" });
  assert.notEqual(w.last("gpu").kernel, first, "the retry gets a new kernel name");
  w.kv.set(key, JSON.stringify({ ...JSON.parse(w.kv.get(key)), at: Date.now() - 3600e3 }));
  await w.tick();
  assert.ok(asked.some((u) => u.includes(w.last("gpu").kernel.split("/")[1])));
});

test("a GPU session goes as a notebook saved with Save & Run All, the other stages as scripts", async () => {
  const w = world({ status: () => "running" });
  await toParts(w, 1);
  const g = w.last("gpu"), r = w.last("research"), p = w.last("part", 0);
  assert.deepEqual([g.type, g.how, g.api], ["notebook", "SAVE_AND_RUN_ALL", true]);
  assert.deepEqual([r.type, r.how, r.api, p.type], ["script", undefined, false, "script"]);
});

test("a GPU session left in Kaggle's queue is sent again under a new name, three times at most", async () => {
  const w = world({ perDay: 2, status: (url) => (url.includes("studio-gpu") ? "queued" : "running") });
  await w.tick();
  for (const r of w.pushes.filter((x) => x.stage === "research")) await w.report(r, "done");
  const first = w.last("gpu").kernel.split("/")[1];
  const ids = [...new Set(w.pushes.filter((x) => x.stage === "research").map((x) => x.id))];
  const age = (mins) => { for (const id of ids) { const k = `unit:${id}:gpu`; w.kv.set(k, JSON.stringify({ ...JSON.parse(w.kv.get(k)), at: Date.now() - mins * 60e3 })); } };
  age(10);
  await w.tick();
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 1, "not before half an hour");
  age(31);
  await w.tick();
  const gpu = w.pushes.filter((r) => r.stage === "gpu");
  assert.equal(gpu.length, 2, "one new session for both videos");
  assert.deepEqual(w.deleted, [first]);
  assert.notEqual(gpu[1].kernel.split("/")[1], first);
  assert.equal(gpu[1].jobs.length, 2);
  for (const id of ids) assert.equal(JSON.parse(w.kv.get(`unit:${id}:gpu`)).kernel, gpu[1].kernel.split("/")[1]);
  assert.match(w.told.at(-1), /sat \d+ min in Kaggle's queue. Sent again as studio-gpu-\w+ \(1 of 3\)/);
  for (let i = 0; i < 4; i++) { age(31); await w.tick(); }
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 4, "three times, then it waits for Kaggle");
  assert.equal(JSON.parse(w.kv.get(`unit:${ids[0]}:gpu`)).state, "pushed", "not failed");
});

// ---------- the daily notebook Kaggle starts itself ----------

async function twoWaiting(w) {
  await w.tick();
  for (const r of w.pushes.filter((x) => x.stage === "research")) await w.report(r, "done");
  return [...new Set(w.pushes.filter((x) => x.stage === "research").map((x) => x.id))];
}

test("the daily notebook takes the videos the queued API session holds, and only it is listened to", async () => {
  const w = world({ perDay: 2, status: (url) => (url.includes("studio-gpu") ? "queued" : "running") });
  w.kv.set("good_version", "g1");
  const ids = await twoWaiting(w);
  const api = w.last("gpu");
  const got = await w.call("/claim", { version: "g1" });
  assert.match(got.message, /started by the notebook studio-gpu-daily for day 1, day 2/);
  const run = JSON.parse(got.code.match(/RUN = (.*)/)[1]);
  assert.deepEqual([run.kernel, run.code, run.jobs.length], ["studio-gpu-daily", "g1", 2]);
  assert.deepEqual(w.deleted, [api.kernel.split("/")[1]], "the API session is deleted");
  assert.equal(w.kv.get("gpu_kernel"), "studio-gpu-daily");
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 1, "nothing pushed");
  assert.match(JSON.stringify(await w.report(api, "stage")), /ignored/, "the old session stops itself if it starts");
  const job = { ...run, ...run.jobs[0] };
  await w.report(job, "stage");
  assert.equal(JSON.parse(w.kv.get(`unit:${ids[0]}:gpu`)).state, "running");
  await w.report(job, "done", { chapters: 2 });
  assert.equal(w.pushes.filter((r) => r.stage === "part").length, 2, "the chapters follow as always");
  assert.match((await w.call("/claim", { version: "g1" })).message, /No video waits/, "day 2 is in the running session");
});

test("the daily notebook with old code does nothing and says how to fix it", async () => {
  const w = world({ perDay: 2, status: () => "queued" });
  w.kv.set("good_version", "g2");
  await twoWaiting(w);
  const got = await w.call("/claim", { version: "g1" });
  assert.equal(got.code, undefined);
  assert.match(w.told.at(-1), /mounts code g1, the daily videos run g2/);
  assert.deepEqual(w.deleted, []);
});

test("nothing to claim: no batch, research not done, or a test film", async () => {
  const w = world({ status: () => "queued" });
  assert.match((await w.call("/claim", { version: "x" })).message, /No video waits/);
  await w.tick();
  assert.match((await w.call("/claim", { version: "x" })).message, /No video waits/, "research still running");
  const t = world({ status: () => "queued" });
  await t.call("/start", { day: 0, title: "A test", subject: "x", minutes: 8 });
  await t.report(t.last("research"), "done");
  assert.match((await t.call("/claim", { version: "x" })).message, /No video waits/, "a test film stays with the API");
});

test("the queue message carries the daily notebook's link, and the daily notebook itself is never requeued", async () => {
  const w = world({ perDay: 2, status: (url) => (url.includes("studio-gpu") ? "queued" : "running") });
  const ids = await twoWaiting(w);
  for (const id of ids) { const k = `unit:${id}:gpu`; w.kv.set(k, JSON.stringify({ ...JSON.parse(w.kv.get(k)), at: Date.now() - 31 * 60e3 })); }
  await w.tick();
  assert.match(w.told.at(-1), /kaggle.com\/code\/u\/studio-gpu-daily\/edit/);
  await w.call("/claim", { version: "x" });
  for (const id of ids) { const k = `unit:${id}:gpu`; w.kv.set(k, JSON.stringify({ ...JSON.parse(w.kv.get(k)), at: Date.now() - 31 * 60e3 })); }
  const deleted = w.deleted.length;
  await w.tick();
  assert.equal(w.deleted.length, deleted);
  assert.equal(w.pushes.filter((r) => r.stage === "gpu").length, 2);
});

test("/hold stops a batch that waits, and its videos come first in the next one", async () => {
  const w = world({ perDay: 2, status: () => "running" });
  await w.tick();
  assert.match(await w.telegram("/hold"), /has research on Kaggle now/, "not while a stage runs");
  for (const r of w.pushes.filter((x) => x.stage === "research")) await w.report(r, "done");
  const gpu = w.last("gpu");
  await w.report({ ...gpu, ...gpu.jobs[0] }, "failed", { kind: "busy", error: "service busy: wait=3600 quota", wait: 3600 });
  await w.report({ ...gpu, ...gpu.jobs[1] }, "failed", { kind: "busy", error: "service busy: wait=3600 quota", wait: 3600 });
  assert.match(await w.telegram("/hold"), /Held: day 1, day 2/);
  const s = w.state();
  assert.deepEqual([s.batch, s.retry, s.tries], [null, [1, 2], {}]);
});

test("the chat is kept both ways, and a kernel's message goes through the worker", async () => {
  const w = world({ status: () => "running" });
  await w.telegram("/status");
  await w.call("/say", { text: "Day 7 (gpu): voice 1/6 took 10 min" });
  const log = JSON.parse(w.kv.get("log"));
  assert.match(log.at(-1), /IST {2}you: \/status$/);
  assert.match(log[0], /bot: Day 7 \(gpu\): voice 1\/6 took 10 min$/);
  assert.equal(w.told.at(-1), "Day 7 (gpu): voice 1/6 took 10 min");
  assert.ok(log.length >= 3);
});

test("AI Studio calls are counted for the day, and a GPU stage they are not enough for waits for them", async () => {
  const w = world({ perDay: 3, status: () => "running" });
  await w.call("/usage", { "gemini-3.5-flash-lite": 330, "gemini-3.1-flash-lite": 40 });
  await w.call("/usage", { "gemini-3.5-flash-lite": 10 });
  await w.tick();
  for (const r of w.pushes.filter((x) => x.stage === "research")) await w.report(r, "done");
  const gpu = w.pushes.filter((x) => x.stage === "gpu");
  assert.equal(gpu.length, 1);
  assert.equal(gpu[0].jobs.length, 1, "160 see calls left: one video, not three");
  assert.match(w.told.find((t) => t.includes("waits for tomorrow")), /Day 2, Day 3: .*see 160 left today/);
  assert.match(await w.telegram("/status"), /AI Studio calls left today: read 460 of 500, see 160 of 500, write 80 of 80/);
});

test("Yes uploads the film for its slot with the settings; YouTube's answer goes to Telegram", async () => {
  const w = world({ status: () => "running" });
  await w.call("/start", { day: 3, title: "Day three", subject: "x", minutes: 20 });
  const job = JSON.parse(w.kv.get(`job:${w.last("research").id}`));
  w.kv.set(`job:${job.id}`, JSON.stringify({ ...job, status: "ready", best: "The Best Title" }));
  await w.telegram("/settings reset");
  await w.button(`yes:${job.id}`);
  assert.match(w.told.at(-1), /upload settings are not chosen yet/);
  assert.equal(w.pushes.filter((p) => p.stage === "upload").length, 0, "not before the settings");
  const SET = [["visibility", 0], ["category", 0], ["made_for_kids", 0], ["age_restriction", 1], ["language", 0],
    ["license", 0], ["comments", 0], ["notify_subscribers", 0], ["embedding", 0]];
  for (const [k, i] of SET) await w.button(`set:${k}:${i}`);
  const up = w.last("upload");
  assert.equal(up.upload_title, "The Best Title");
  assert.equal(up.settings.visibility, "Public");
  assert.ok(up.publish_at);
  await w.report(up, "failed", { error: "YouTube refused the upload: 401" });
  assert.match(w.told.at(-1), /Upload of The Best Title failed \(1 of 3\)/);
  await w.tick();
  const again = w.last("upload");
  assert.notEqual(again.nonce, up.nonce, "pushed again at the tick");
  await w.report(again, "uploaded", { video: "abc123", missed: ["thumbnail (403 forbidden)"] });
  assert.match(w.told.at(-1), /On YouTube: The Best Title\nhttps:\/\/youtu.be\/abc123\nPrivate until .* then public\.\nNot set: thumbnail \(403 forbidden\)\nSet in YouTube Studio \(the API cannot\): age restriction 18\+/);
  assert.equal(JSON.parse(w.kv.get(`job:${job.id}`)).status, "scheduled");
});

test("a day tried again keeps its research and goes on from the GPU stage", async () => {
  const w = world({ perDay: 1, status: () => "running" });
  await w.tick();
  const r = w.last("research");
  await w.report(r, "done");
  w.kv.set(`job:${r.id}`, JSON.stringify({ ...JSON.parse(w.kv.get(`job:${r.id}`)), status: "failed" }));
  w.kv.set("state", JSON.stringify({ ...w.state(), batch: null, started: {}, retry: [1], next_day: 2 }));
  const before = w.pushes.filter((p) => p.stage === "research").length;
  await w.tick();
  assert.equal(w.pushes.filter((p) => p.stage === "research").length, before, "no second research");
  assert.equal(w.last("gpu").jobs[0].id, r.id);
  assert.match(w.told.find((t) => t.includes("keep their research")), /Day 1 keep their research/);
});
