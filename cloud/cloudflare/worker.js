// History Studio on Cloudflare: the videos start by themselves, and the Telegram side of every video.
//
//   every 30 min  watches the day's videos (a kernel that ended without a word counts as failed, a chapter waiting
//                 for a free machine is pushed); when nothing runs and today's count is not reached, the day's batch
//                 starts: the next days of plan.json together. Each video's research runs on its own CPU machine;
//                 when all are done, one GPU session makes every video's voice and pictures, so Kaggle's GPU queue
//                 is waited out once a day; each video's chapters render as soon as its GPU part is done, then its
//                 join. The count resets daily at the start hour (6:00 India); none start when the week's GPU hours
//                 run out. A failed stage runs once more; failing again, the day is tried in a later batch, then
//                 skipped.
//   Yes           the video is approved and gets the next free publish slot: two a day, 11:00 AM and 5:30 PM New
//                 York. Four made a day fill two days of slots, so the extra ones wait for the next free day.
//                 Making never waits for an approval.
//   No            asks what is wrong and keeps the reason and the screenshots for the fix step
//   /settings     the upload settings, asked once with their options; title, description, hashtags and captions
//                 are made by the studio, paid promotion and AI use are always "No"
//   /status /next /day N /pause /resume /perday N /starthour H /help
//
// State lives in KV: "state", "plan", "kernel" (cloud/kernel.py), "settings", "job:<id>", "await".
// The Kaggle kernels report back on /hook with the shared secret; cloud.py uses /start and /tick.

const SLOTS = ["11:00", "17:30"];  // New York time
const FIXED = ["Paid promotion: No", "Altered or synthetic content (AI): No"];

const SETTINGS = [
  ["visibility", "Visibility after publish", ["Public", "Unlisted"]],
  ["category", "Category", ["Education", "Entertainment", "People & Blogs", "Film & Animation"]],
  ["made_for_kids", "Made for kids", ["No", "Yes"]],
  ["age_restriction", "Age restriction (18+)", ["No", "Yes"]],
  ["language", "Video language", ["English", "Hindi", "Urdu"]],
  ["license", "License", ["Standard YouTube", "Creative Commons"]],
  ["comments", "Comments", ["On", "Hold potentially inappropriate", "Hold all", "Off"]],
  ["notify_subscribers", "Notify subscribers", ["Yes", "No"]],
  ["embedding", "Allow embedding", ["Yes", "No"]],
];

const HELP = `History Studio
/status  what is running, the last videos
/next  start the next day of the plan now
/day N  start day N now
/pause  stop the automatic start, /resume  start it again (/resume 2 goes on from day 2)
/perday N  videos a day (now 4), /starthour H  when the day's videos begin, India time (now 6)
/settings  choose the upload settings (/settings reset to choose again)
/undo  take back the last Yes`;

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    try {
      if (url.pathname === "/telegram" && req.method === "POST") {
        if (req.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.HOOK_SECRET) return new Response("no", { status: 403 });
        await telegram(await req.json(), env);
        return new Response("ok");
      }
      if (req.headers.get("X-Studio-Secret") !== env.HOOK_SECRET) return new Response("no", { status: 403 });
      if (url.pathname === "/hook" && req.method === "POST") return Response.json(await hook(await req.json(), env));
      if (url.pathname === "/start" && req.method === "POST") {  // cloud.py: a day from a stage, by hand
        const b = await req.json();
        return Response.json({ message: await startJob(env, b.day, b.from, b) });
      }
      if (url.pathname === "/tick") return Response.json({ ok: await tick(env).then(() => true) });
      if (url.pathname === "/check") return Response.json(await check(env));
      return new Response("History Studio", { status: 404 });
    } catch (err) {
      return new Response(String(err), { status: 500 });
    }
  },

  async scheduled(_, env, ctx) {
    ctx.waitUntil(tick(env));
  },
};

// ---------- KV ----------

const get = async (env, key, fallback) => JSON.parse((await env.KV.get(key)) || "null") ?? fallback;
const put = (env, key, value) => env.KV.put(key, JSON.stringify(value));
// a new object each time: one shared default had its counters filled by every later read
const fresh = () => ({ next_day: 1, paused: true, batch: null, last_slot: null, per_day: 4, start_hour: 6, started: {},
                       tries: {}, retry: [], gpu: {}, gpu_start: {}, told: null });
async function state(env) {
  const s = { ...fresh(), ...(await get(env, "state", {})) };
  if (s.running) s.batch = { ids: [s.running.id], since: s.running.since };  // a state from before batches
  delete s.running;
  return s;
}

const GPU_WEEK = 29;        // Kaggle gives 30 GPU hours a week; one is kept spare

// the studio's day: from the start hour in India to the same hour the next day
function today(s) {
  const ist = Date.now() + 5.5 * 3600e3 - s.start_hour * 3600e3;
  return new Date(ist).toISOString().slice(0, 10);
}

// Kaggle's GPU week starts on Saturday 00:00 UTC
function week() {
  const d = new Date();
  d.setUTCHours(0, 0, 0, 0);
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 1) % 7));
  return d.toISOString().slice(0, 10);
}

function gpuAverage(s) {
  const runs = Object.values(s.gpu_runs || {});
  return runs.length ? runs.reduce((a, b) => a + b, 0) / runs.length : 2;
}

// ---------- the videos: the worker alone decides what runs next ----------
//
// A video is a job: research, gpu, one part per chapter, join. Each of these units is pushed to Kaggle by the
// worker with a fresh nonce; a kernel only works and reports (stage, progress, done, failed, ready) with that nonce.
// A report with another nonce comes from a run that was replaced: it is ignored, so a stray run can neither start
// anything nor write to Telegram. Earlier the kernels pushed each other and kept Drive markers; one broken marker
// made every chapter push all six again. Now nothing on Kaggle pushes, and every push happens here.
// Each unit has its own KV key (unit:<job>:<unit>), so two chapters finishing at once never overwrite each other.

const LIMIT = { research: 3, gpu: 3, part: 3, join: 2 };  // hours Kaggle gives each kernel
const GRACE = 20 * 60e3;  // a kernel Kaggle reports finished but that never said so: counted as failed after this

function slugOf(title) {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
}

const kindOf = (key) => (key.startsWith("part") ? "part" : key);
// the videos of a batch run side by side, so each has its own CPU kernels (slot 0, 1, 2...); the GPU kernel is one
const kernelName = (r) => (r.stage === "gpu" ? "studio-gpu" : r.stage === "part" ? `studio-render-${r.slot || 0}-${r.part}`
  : `studio-${r.stage}-${r.slot || 0}`);
const kernelOf = (job, key) => kernelName({ stage: kindOf(key), slot: job.run.slot, part: Number(key.slice(4)) });
const unitKey = (job, key) => `unit:${job.id}:${key}`;

async function units(env, job) {
  const keys = (await env.KV.list({ prefix: `unit:${job.id}:` })).keys.map((k) => k.name);
  const all = await Promise.all(keys.map((k) => get(env, k, null)));
  return Object.fromEntries(keys.map((k, i) => [k.split(":")[2], all[i]]).filter(([, u]) => u));
}

function newJob(d, slot = 0) {
  const slug = slugOf(d.title);
  return { id: slug.slice(0, 40), day: d.day, title: d.title, status: "running", started: new Date().toISOString(),
           run: { id: slug.slice(0, 40), title: d.title, minutes: d.minutes || 22, style: "documentary", slug,
                  subject: d.subject || "", day: d.day, slot, ...(d.smoke ? { smoke: true } : {}) },
           chapters: null, feedback: [] };
}

async function batchJobs(env, s) {
  const jobs = await Promise.all((s.batch?.ids || []).map((id) => get(env, `job:${id}`, null)));
  return jobs.filter((j) => j && j.status === "running");
}

// one GPU session for these videos; a refused push (Kaggle runs too many at once) waits for the next tick
async function pushGpu(env, jobs) {
  const nonce = crypto.randomUUID().slice(0, 8);
  const run = { ...jobs[0].run, stage: "gpu", nonce, jobs: jobs.map((j) => j.run) };
  let ok = null, code = null;
  try {
    code = await kagglePush(env, run);
  } catch (err) {
    ok = String(err).slice(0, 200);
  }
  for (const job of jobs) {
    const u = await get(env, unitKey(job, "gpu"), { tries: 0 });
    await put(env, unitKey(job, "gpu"), ok === null
      ? { ...u, state: "pushed", nonce, at: Date.now(), waiting: null, started: null, code, batch: jobs.map((j) => j.id) }
      : { ...u, state: "waiting", nonce: null, waiting: u.waiting || Date.now(), why: ok });
  }
  return ok === null ? "pushed" : "waiting";
}

// the GPU session, once every video of the batch has its research. Two researches ending at the same moment both
// see all done, so only the one that ended last pushes; the watch pushes it otherwise. A video whose GPU part failed
// or waits for a service goes into the next session, after the one running has finished every video it was given.
async function gpuWhenReady(env, s, by) {
  const jobs = await batchJobs(env, s);
  const all = await Promise.all(jobs.map((j) => units(env, j)));
  if (!jobs.length || all.some((a) => ["pushed", "running"].includes(a.gpu?.state))) return;
  if (!all.every((a) => a.research?.state === "done")) return;
  if (by) {
    const last = jobs.map((j, i) => [j.id, all[i].research.finished || 0]).sort((a, b) => b[1] - a[1] || (b[0] > a[0] ? 1 : -1))[0][0];
    if (by !== last) return;
  }
  const todo = jobs.filter((_, i) => !all[i].gpu || ["waiting", "failed"].includes(all[i].gpu.state) ||
    (all[i].gpu.state === "later" && Date.now() >= all[i].gpu.after));
  if (!todo.length) return;
  const st = await pushGpu(env, todo);
  if (st === "pushed") await say(env, `GPU session for ${todo.map((j) => `day ${j.day}`).join(", ")}: voice, pictures and picture check, one video after another.`);
}

// push one unit; a refused push (Kaggle runs too many at once) waits for the next tick
async function pushUnit(env, job, key, u = { tries: 0 }) {
  if (key === "gpu") return pushGpu(env, [job]).then(() => get(env, unitKey(job, "gpu"), u));
  const run = { ...job.run, stage: kindOf(key), nonce: crypto.randomUUID().slice(0, 8),
                ...(key.startsWith("part") ? { part: Number(key.slice(4)) } : {}), chapters: job.chapters };
  try {
    const code = await kagglePush(env, run);
    u = { ...u, state: "pushed", nonce: run.nonce, at: Date.now(), waiting: null, code };
  } catch (err) {
    u = { ...u, state: "waiting", nonce: null, waiting: u.waiting || Date.now(), why: String(err).slice(0, 200) };
  }
  await put(env, unitKey(job, key), u);
  return u;
}

async function startJob(env, day, from = "research", extra = {}) {
  const plan = await get(env, "plan", { days: [] });
  // day 0 is a test film from cloud.py test: its own title, not in the plan and not counted
  const d = day === 0 && extra.title ? { day: 0, title: extra.title, subject: extra.subject, minutes: extra.minutes, smoke: extra.smoke }
    : plan.days.find((x) => x.day === day);
  if (!d) return `Day ${day} is not in the plan.`;
  const s = await state(env);
  const running = await batchJobs(env, s);
  const same = running.find((j) => j.id === newJob(d).id);  // by the video, not the day: test and smoke are both day 0
  if (running.length && !same) return `${running.map((j) => j.title).join(", ")} still running; this starts after it.`;
  const job = { ...newJob(d, same?.run.slot || 0), chapters: extra.chapters ?? null };
  for (const k of (await env.KV.list({ prefix: `unit:${job.id}:` })).keys) await env.KV.delete(k.name);  // a fresh run
  const done = extra.done || [];
  for (const k of done) await put(env, unitKey(job, `part${k}`), { state: "done", tries: 0 });
  const keys = from === "parts"
    ? [...Array(job.chapters).keys()].filter((k) => !done.includes(k)).map((k) => `part${k}`) : [from];
  if (from !== "research") await put(env, unitKey(job, "research"), { state: "done", tries: 0, finished: Date.now() });
  if (!["research", "gpu"].includes(from)) await put(env, unitKey(job, "gpu"), { state: "done", tries: 0 });
  await put(env, `job:${job.id}`, job);
  s.batch = { ids: [job.id], since: Date.now() };
  const pushed = [];
  for (const key of keys) pushed.push([key, (await pushUnit(env, job, key)).state]);
  if (from === "research" && day) s.started[today(s)] = (s.started[today(s)] || 0) + 1;
  await put(env, "state", s);
  const waiting = pushed.filter(([, st]) => st === "waiting").map(([k]) => k);
  return `Day ${day} ${from === "research" ? "started" : `goes on from ${from}`}: ${d.title}` +
    (waiting.length ? `\n(${waiting.join(", ")} wait for a free Kaggle machine)` : "");
}

// the day's batch: the next n days of the plan (a day that failed once comes first), each with its own CPU
// machines (slot), their research all at once
async function startBatch(env, s, days) {
  const plan = await get(env, "plan", { days: [] });
  const jobs = days.map((day, slot) => newJob(plan.days.find((x) => x.day === day), slot));
  s.batch = { ids: jobs.map((j) => j.id), since: Date.now() };
  s.started[today(s)] = (s.started[today(s)] || 0) + jobs.length;
  s.retry = (s.retry || []).filter((d) => !days.includes(d));
  s.next_day = Math.max(s.next_day, ...days.map((d) => d + 1));
  const waiting = [];
  for (const job of jobs) {
    for (const k of (await env.KV.list({ prefix: `unit:${job.id}:` })).keys) await env.KV.delete(k.name);
    await put(env, `job:${job.id}`, job);
    if ((await pushUnit(env, job, "research")).state === "waiting") waiting.push(job.day);
  }
  await put(env, "state", s);
  return `Today's videos started: ${jobs.map((j) => `day ${j.day} ${j.title}`).join("; ")}.\nResearch first, then one GPU ` +
    `session for all of them.${waiting.length ? `\n(research of day ${waiting.join(", ")} waits for a free machine)` : ""}`;
}

// a day that failed: into a later batch once, then skipped so the plan goes on
function failedDay(s, day) {
  s.tries[day] = (s.tries[day] || 0) + 1;
  if (!day) return "This was the test film.";
  if (s.tries[day] < 2) {
    s.retry = [...new Set([...(s.retry || []), day])];
    return "It will be tried once more in the next batch.";
  }
  return `It failed twice, so it is skipped for now (start it again with /day ${day}).`;
}

async function jobFailed(env, job, s, why) {
  job.status = "failed";
  job.error = why;
  await put(env, `job:${job.id}`, job);
  await say(env, `${job.run.smoke ? "SMOKE FILM failed, the daily videos keep the code they have" : `Day ${job.day}`}: ${why}\n` +
    failedDay(s, job.day));
  await batchEnded(env, s);
  await gpuWhenReady(env, s);  // the others may have been waiting for this one's research
}

// the batch is over when none of its videos runs any more
async function batchEnded(env, s) {
  if (s.batch && !(await batchJobs(env, s)).length) s.batch = null;
}

// what comes after a unit is done
async function advance(env, s, job, key) {
  if (key === "research") return gpuWhenReady(env, s, job.id);
  if (key === "gpu") {
    for (let k = 0; k < job.chapters; k++) await pushUnit(env, job, `part${k}`);
    return gpuWhenReady(env, s);  // the session has finished every video: one that failed goes into the next
  }
  // a machine came free: chapters of any video of the batch that wait for one
  for (const other of await batchJobs(env, s)) {
    const all = await units(env, other);
    for (const [k, u] of Object.entries(all)) if (u.state === "waiting" && k !== "gpu") await pushUnit(env, other, k, u);
  }
  if (key.startsWith("part")) await joinWhenReady(env, job, await units(env, job), key);
}

// the join, once every chapter is done. Two chapters finishing at the same moment both see all done, so only the one
// that finished last pushes it; if each saw the other still running, the watch pushes it at the next tick
async function joinWhenReady(env, job, all, by) {
  const parts = Object.keys(all).filter((k) => k.startsWith("part"));
  if (parts.length !== job.chapters || !parts.every((k) => all[k].state === "done") || all.join) return;
  const last = parts.reduce((a, b) => ((all[b].finished || 0) > (all[a].finished || 0) || ((all[b].finished || 0) ===
    (all[a].finished || 0) && b > a) ? b : a));
  if (by && by !== last) return;
  await pushUnit(env, job, "join");
  await say(env, `Day ${job.day}: all ${job.chapters} chapters rendered, putting the film together.`);
}

// an outside service down (kind "busy"): the stage runs again at a later tick, as often as BUSY allows, without
// counting as a failure; what it made is on Drive, so a later run only does the part that needs the service
const BUSY = 6;  // half hour waits: three hours

// wait (seconds): the vision API's free calls for the day are used up and come back then; that wait counts once
async function unitLater(env, job, s, key, u, why, wait) {
  const busy = (u.busy || 0) + 1;
  if (busy > BUSY) return jobFailed(env, job, s, `${key} waited ${BUSY / 2} hours for a service that stayed down: ${why.slice(0, 400)}`);
  const after = Date.now() + Math.max(25 * 60, (wait || 0) + 300) * 1e3;
  await put(env, unitKey(job, key), { ...u, state: "later", busy, why, nonce: null, after });
  const when = wait ? `at ${new Date(after).toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" })} India time, when the free calls of the day come back`
    : "in half an hour";
  return say(env, `Day ${job.day}: ${key} paused, a service it needs is down (${why.slice(0, 300)}). ` +
    `Nothing is lost; it runs again ${when} (${busy} of ${BUSY}).`);
}

async function unitFailed(env, job, s, key, u, why, retry = true) {
  u = { ...u, tries: (u.tries || 0) + 1, state: "failed", why };
  await put(env, unitKey(job, key), u);
  if (key === "gpu" && retry && u.tries < 2) {  // not pushed now: that would stop the session with the other videos
    await say(env, `Day ${job.day}: gpu failed (${why.slice(0, 160)}). It runs again when the GPU session is free.`);
    return gpuWhenReady(env, s);
  }
  if (retry && u.tries < 2) {  // pause only stops new videos; the one running is finished
    const again = await pushUnit(env, job, key, u);
    return say(env, `Day ${job.day}: ${key} failed (${why.slice(0, 160)}). ` +
      (again.state === "pushed" ? "Running it once more." : "It runs once more when a Kaggle machine is free."));
  }
  await jobFailed(env, job, s, retry ? `${key} failed: ${why.slice(0, 300)}` : why.slice(0, 900));
}

function readyText(e) {
  const warn = e.title_check && !e.title_check.ok
    ? `\n\nTitle check: ${(e.title_check.problems || []).join("; ").slice(0, 600)}\nSuggested title: ${e.title_check.title}` : "";
  const quality = e.quality ? `\n\nQuality: ${e.quality}${(e.fix || []).map((x) => `\n- ${x}`).join("")}` : "";
  return `Video ready: ${e.best || e.title}\n\nWatch: ${e.link}${warn}${quality}\n\nCheck the screenshots and facts, then approve.`;
}

async function hook(e, env) {
  const job = await get(env, `job:${e.id}`, null);
  const key = e.stage === "part" ? `part${e.part}` : e.stage;
  const u = job && await get(env, unitKey(job, key), null);
  if (!u || u.nonce !== e.nonce || job.status !== "running") return { ok: true, ignored: "not a run the worker is waiting for" };
  if (e.event === "progress") {
    await say(env, `Day ${job.day} (${key}): ${String(e.line).slice(0, 200)}`);
    return { ok: true };
  }
  const s = await state(env);
  if (e.event === "stage") {
    await put(env, unitKey(job, key), { ...u, state: "running", started: Date.now() });
    if (key === "gpu") s.gpu_start[job.id] = Date.now();
  }
  if (key === "gpu" && ["done", "failed"].includes(e.event) && s.gpu_start[job.id]) {
    const hours = (Date.now() - s.gpu_start[job.id]) / 3600e3;
    s.gpu[week()] = (s.gpu[week()] || 0) + hours;
    if (e.event === "done") s.gpu_runs = { ...(s.gpu_runs || {}), [job.id]: hours };
    delete s.gpu_start[job.id];
  }
  if (e.event === "done") {
    await put(env, unitKey(job, key), { ...u, state: "done", finished: Date.now() });
    if (key === "gpu") {
      job.chapters = e.chapters;
      await put(env, `job:${job.id}`, job);
    }
    await advance(env, s, job, key);
  }
  if (e.event === "failed" && e.kind === "busy") {
    await unitLater(env, job, s, key, u, e.error || "no reason given", Number(e.wait) || 0);
    if (key === "gpu") await gpuWhenReady(env, s);
  }
  else if (e.event === "failed") await unitFailed(env, job, s, key, u, e.error || "no reason given", e.kind !== "quality");
  if (e.event === "ready") {
    await put(env, unitKey(job, key), { ...u, state: "done", finished: Date.now() });
    Object.assign(job, { status: "ready", link: e.link, ready: new Date().toISOString() });
    await put(env, `job:${job.id}`, job);
    await batchEnded(env, s);
    if (job.run.smoke) {  // every stage worked on this code: cloud.py smoke now gives it to the day's videos
      const code = (await get(env, unitKey(job, "join"), {})).code;
      await env.KV.put("smoke_passed", code || "");
      await say(env, `SMOKE FILM passed: every stage worked on code ${code}. It becomes the code of the daily videos.\n` +
        `Quality (not checked for a smoke film): ${e.quality || "?"}\nWatch: ${e.link}`);
    } else if (job.day === 0) await say(env, `TEST FILM, not for publishing\n\n${readyText(e)}`);
    else await say(env, readyText(e), [[{ text: "Yes, publish", callback_data: `yes:${job.id}` },
                                        { text: "No", callback_data: `no:${job.id}` }]]);
  }
  for (const w of Object.keys(s.gpu)) if (w < week()) delete s.gpu[w];
  for (const d of Object.keys(s.started)) if (d < today(s)) delete s.started[d];
  if (e.event !== "stage" || key === "gpu") await put(env, "state", s);
  if (e.event === "ready" && !s.paused) await tick(env);  // the next video starts right away
  return { ok: true };
}

async function kaggleStatus(env, kernel) {
  const r = await fetch(`https://www.kaggle.com/api/v1/kernels/status?userName=${env.KAGGLE_USERNAME}&kernelSlug=${kernel}`,
    { headers: { Authorization: "Basic " + btoa(`${env.KAGGLE_USERNAME}:${env.KAGGLE_KEY}`) } });
  return String((await r.json().catch(() => ({}))).status || r.status).toLowerCase();
}

// the day's videos: kernels that ended without a word, units waiting for a machine or a service
async function watch(env, s) {
  const jobs = await batchJobs(env, s);
  let gpu = null;  // one GPU kernel for the batch: asked once
  for (const job of jobs) {
    const all = await units(env, job);
    if (!Object.keys(all).length && Date.now() - s.batch.since > 3600e3) {
      await jobFailed(env, job, s, "the worker has no record of its stages (started by an older version)");
      continue;
    }
    await joinWhenReady(env, job, all);
    for (const [key, u] of Object.entries(all)) {
      if (key === "gpu" && ["waiting", "later"].includes(u.state)) continue;  // gpuWhenReady below
      if (u.state === "later") {
        if (Date.now() >= u.after) await pushUnit(env, job, key, u);
        continue;
      }
      if (u.state === "waiting") {
        const now = await pushUnit(env, job, key, u);
        if (now.state === "waiting" && Date.now() - now.waiting > 3 * 3600e3) {
          await jobFailed(env, job, s, `${key} found no free Kaggle machine in 3 hours (${now.why})`);
          break;
        }
        continue;
      }
      if (!["pushed", "running"].includes(u.state) || Date.now() - u.at < GRACE) continue;
      const status = key === "gpu" ? (gpu ??= await kaggleStatus(env, "studio-gpu")) : await kaggleStatus(env, kernelOf(job, key));
      // Kaggle's time limit counts from the start, not from the push: a kernel two hours in the GPU queue had only
      // two of its four hours left before this counted it as lost and pushed it to the back of the queue again
      // the GPU session's videos wait their turn inside it: only Kaggle's own session limit ends that one
      const since = u.started || (status === "queued" ? Date.now() : u.at);
      const late = key !== "gpu" && Date.now() - since > (LIMIT[kindOf(key)] + 1) * 3600e3;
      if (/error|cancel|complete/.test(status) || late) {
        await unitFailed(env, job, s, key, u, `the kernel ended without reporting (Kaggle says ${status})`);
        if ((await get(env, `job:${job.id}`, job)).status !== "running") break;
      }
    }
  }
  for (const job of await batchJobs(env, s)) {  // a GPU part that waited for a machine too long
    const u = await get(env, unitKey(job, "gpu"), null);
    if (u?.state === "waiting" && Date.now() - u.waiting > 3 * 3600e3) await jobFailed(env, job, s, `gpu found no free Kaggle machine in 3 hours (${u.why})`);
  }
  await gpuWhenReady(env, s);
  await batchEnded(env, s);
}

async function tick(env) {
  const s = await state(env);
  if (s.batch) {
    const before = JSON.stringify(s);
    await watch(env, s);
    if (JSON.stringify(s) !== before) await put(env, "state", s);  // KV gives 1000 writes a day: only real changes
  }
  const room = s.per_day - (s.started[today(s)] || 0);
  if (s.paused || s.batch || room <= 0) return;
  const plan = await get(env, "plan", { days: [] });
  const fresh = plan.days.map((x) => x.day).filter((x) => x >= s.next_day).sort((a, b) => a - b);
  if (!fresh.length && !(s.retry || []).length) {
    s.paused = true;
    await put(env, "state", s);
    return say(env, "Every day of the plan has been made. The automatic start is paused.");
  }
  const used = s.gpu[week()] || 0;
  const fit = Math.min(room, Math.floor((GPU_WEEK - used) / gpuAverage(s)));
  if (fit <= 0) {
    if (s.told !== week()) {
      s.told = week();
      await put(env, "state", s);
      await say(env, `This week's Kaggle GPU hours are used (${used.toFixed(1)} h). Videos start again on Saturday.`);
    }
    return;
  }
  const days = [...(s.retry || []), ...fresh].slice(0, fit);
  await say(env, await startBatch(env, s, days).catch((err) => `Could not start days ${days.join(", ")}: ${err}`));
}

async function kagglePush(env, run) {
  const code = await env.KV.get("kernel");
  if (!code) throw new Error("kernel.py is not in KV, run: python cloud.py worker");
  // the day's videos run the code the last smoke film passed (history-studio-code-good); a test or smoke film runs
  // what was synced last (history-studio-code), so a change reaches the daily videos only after a whole film
  const good = await env.KV.get("good_version");
  const latest = run.day === 0 || !good;
  run = { ...run, code: latest ? await env.KV.get("code_version") : good };  // the kernel stops on other code
  const me = env.KAGGLE_USERNAME;
  const name = kernelName(run);
  const gpu = run.stage === "gpu";
  const body = {
    slug: `${me}/${name}`, newTitle: name, text: code.replace("__RUN__", JSON.stringify(run)),
    language: "python", kernelType: "script", isPrivate: true, enableGpu: gpu, enableTpu: false, enableInternet: true,
    datasetDataSources: [latest ? "code" : "code-good", "assets", "secrets"].map((x) => `${me}/history-studio-${x}`),
    competitionDataSources: [], kernelDataSources: [], modelDataSources: [], categoryIds: [],
    // a GPU kernel goes as a notebook saved in the browser does: no machine shape, no time limit (the default is
    // two T4s and Kaggle's own 12 hours). With machineShape "NvidiaTeslaT4" and a time limit set, every push waited
    // in Kaggle's queue for hours, on 21 and 22 November up to 13, while the same kernel without them started in
    // six seconds on the same two T4s
    ...(gpu ? {} : { sessionTimeoutSeconds: LIMIT[run.stage] * 3600 }),
  };
  const auth = "Basic " + btoa(`${me}:${env.KAGGLE_KEY}`);
  const r = await fetch("https://www.kaggle.com/api/v1/kernels/push", {
    method: "POST", headers: { Authorization: auth, "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok || out.error) throw new Error(`Kaggle push: ${r.status} ${out.error || out.message || ""}`.trim());
  return run.code;
}

// ---------- publish slots ----------

function nyHour(ms) {
  return Number(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", hourCycle: "h23" })
    .format(new Date(ms)));
}

function nyDate(ms) {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "America/New_York" }).format(new Date(ms)); // YYYY-MM-DD
}

// the UTC moment of hh:mm New York time on a New York date
function nyTime(date, hhmm) {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = hhmm.split(":").map(Number);
  let t = Date.UTC(y, m - 1, d, hh + 5, mm); // EST; one hour less in summer
  if (nyHour(t) !== hh) t -= 3600e3;
  return t;
}

// the first slot after the last one given out and at least 2 hours away (time for the upload)
function nextSlot(last) {
  const after = Math.max(Date.now() + 2 * 3600e3, last ? Date.parse(last) : 0);
  let date = nyDate(after - 24 * 3600e3);
  for (let i = 0; i < 400; i++) {
    for (const hhmm of SLOTS) {
      const t = nyTime(date, hhmm);
      if (t > after) return new Date(t).toISOString();
    }
    date = nyDate(nyTime(date, "12:00") + 24 * 3600e3);
  }
}

const ist = (iso) => new Date(iso).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short" });
const ny = (iso) => new Date(iso).toLocaleString("en-US", { timeZone: "America/New_York", dateStyle: "medium", timeStyle: "short" });

// ---------- Telegram ----------

async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  return r.json();
}

const say = (env, text, buttons) => tg(env, "sendMessage", {
  chat_id: env.TELEGRAM_CHAT_ID, text: text.slice(0, 4000), disable_web_page_preview: true,
  ...(buttons ? { reply_markup: { inline_keyboard: buttons } } : {}),
});

async function telegram(u, env) {
  const msg = u.message || u.callback_query?.message;
  if (!msg || String(msg.chat.id) !== String(env.TELEGRAM_CHAT_ID)) return; // only your chat
  if (u.callback_query) return button(u.callback_query, env);
  const text = (msg.text || msg.caption || "").trim();
  if (text.startsWith("/")) return command(text, env);
  const waiting = await get(env, "await", null);
  if (waiting) return feedback(msg, text, waiting, env);
  await say(env, HELP);
}

async function button(q, env) {
  const [what, a, b] = q.data.split(":");
  const done = (text) => tg(env, "answerCallbackQuery", { callback_query_id: q.id, text });
  const unbutton = () => tg(env, "editMessageReplyMarkup", { chat_id: q.message.chat.id, message_id: q.message.message_id,
                                                             reply_markup: { inline_keyboard: [] } });
  if (what === "yes") {
    const job = await get(env, `job:${a}`, { id: a, feedback: [] });
    const s = await state(env);
    const prefs = await get(env, "settings", {});
    const slot = nextSlot(s.last_slot);
    Object.assign(job, { status: "approved", approved: new Date().toISOString(), publish_at: slot });
    s.last_slot = slot;
    await put(env, `job:${a}`, job);
    await put(env, "state", s);
    await Promise.all([done("Approved"), unbutton()]);
    const missing = SETTINGS.filter(([k]) => !(k in prefs)).length;
    return say(env, `Approved: ${job.title || a}\nPublish: ${ny(slot)} New York (${ist(slot)} India)` +
      (missing ? `\n\n${missing} upload settings are not chosen yet, send /settings` : ""));
  }
  if (what === "no") {
    const job = await get(env, `job:${a}`, { id: a, feedback: [] });
    await put(env, `job:${a}`, { ...job, status: "rejected", publish_at: null });
    await put(env, "await", a);
    await Promise.all([done("Tell me what is wrong"), unbutton()]);
    return say(env, "What is wrong in the video? Write it (a time like 4:32 helps) and send screenshots of the " +
      "problem. Send /done when you have sent everything.");
  }
  if (what === "set") {
    const prefs = await get(env, "settings", {});
    const [key, , options] = SETTINGS.find(([k]) => k === a);
    prefs[key] = options[Number(b)];
    await put(env, "settings", prefs);
    await Promise.all([done(prefs[key]), unbutton()]);
    return askNext(env, prefs);
  }
}

async function feedback(msg, text, id, env) {
  const job = await get(env, `job:${id}`, { id, feedback: [] });
  const photo = msg.photo ? msg.photo[msg.photo.length - 1].file_id : msg.document?.mime_type?.startsWith("image/")
    ? msg.document.file_id : null;
  job.feedback = [...(job.feedback || []), { text, photo, at: new Date().toISOString() }];
  job.status = "rejected";
  await put(env, `job:${id}`, job);
  await tg(env, "setMessageReaction", { chat_id: msg.chat.id, message_id: msg.message_id,
                                       reaction: [{ type: "emoji", emoji: "👍" }] });
}

async function askNext(env, prefs) {
  const next = SETTINGS.find(([k]) => !(k in prefs));
  if (!next) {
    const lines = SETTINGS.map(([k, label]) => `${label}: ${prefs[k]}`);
    return say(env, `Upload settings saved:\n${[...lines, ...FIXED].join("\n")}`);
  }
  const [key, label, options] = next;
  return say(env, `${label}?`, options.map((o, i) => [{ text: o, callback_data: `set:${key}:${i}` }]));
}

async function command(text, env) {
  const [cmd, arg] = text.split(/\s+/);
  const s = await state(env);
  if (cmd === "/done") {
    const id = await get(env, "await", null);
    if (!id) return say(env, "Nothing is waiting for a reason.");
    await env.KV.delete("await");
    const job = await get(env, `job:${id}`, { feedback: [] });
    return say(env, `Saved ${job.feedback.length} notes for "${job.title || id}". The fix step will use them.`);
  }
  if (cmd === "/pause" || cmd === "/resume") {
    s.paused = cmd === "/pause";
    if (cmd === "/resume" && Number(arg)) s.next_day = Number(arg);
    await put(env, "state", s);
    if (s.paused) return say(env, "Automatic start paused. A video already running still finishes.");
    await say(env, `Automatic start on: ${s.per_day} videos a day from ${s.start_hour}:00 India time, ` +
      `one after another, from day ${s.next_day}. You only need to review them.`);
    return tick(env);
  }
  if (cmd === "/perday" || cmd === "/starthour") {
    const n = Number(arg);
    if (!(n >= (cmd === "/perday" ? 1 : 0) && n <= (cmd === "/perday" ? 8 : 23))) return say(env, `Send it like: ${cmd} 4`);
    s[cmd === "/perday" ? "per_day" : "start_hour"] = n;
    await put(env, "state", s);
    return say(env, `Now ${s.per_day} videos a day, starting ${s.start_hour}:00 India time.`);
  }
  if (cmd === "/next" || cmd === "/day") {
    const day = cmd === "/day" ? Number(arg) : s.next_day;
    if (!day) return say(env, "Send it like: /day 5");
    return say(env, await startJob(env, day).catch((err) => `Could not start day ${day}: ${err}`));
  }
  if (cmd === "/undo") {  // the last approval taken back: the video gets its Yes and No again and frees its slot
    const keys = (await env.KV.list({ prefix: "job:" })).keys.map((k) => k.name);
    const jobs = (await Promise.all(keys.map((k) => get(env, k, null)))).filter(Boolean);
    const last = jobs.filter((j) => j.status === "approved").sort((a, b) => b.approved.localeCompare(a.approved))[0];
    if (!last) return say(env, "No approved video to take back.");
    await put(env, `job:${last.id}`, { ...last, status: "ready", publish_at: null });
    const left = jobs.filter((j) => j.status === "approved" && j.id !== last.id).map((j) => j.publish_at).sort();
    s.last_slot = left.at(-1) || null;
    await put(env, "state", s);
    return say(env, `Approval taken back: ${last.title || last.id}${last.link ? `\n${last.link}` : ""}`,
      [[{ text: "Yes, publish", callback_data: `yes:${last.id}` }, { text: "No", callback_data: `no:${last.id}` }]]);
  }
  if (cmd === "/settings") {
    if (arg === "reset") await put(env, "settings", {});
    return askNext(env, arg === "reset" ? {} : await get(env, "settings", {}));
  }
  if (cmd === "/status") {
    const keys = (await env.KV.list({ prefix: "job:" })).keys.map((k) => k.name);
    const jobs = (await Promise.all(keys.map((k) => get(env, k, null)))).filter(Boolean)
      .sort((a, b) => (b.started || b.ready || "").localeCompare(a.started || a.ready || "")).slice(0, 6);
    const live = {};
    for (const j of jobs.filter((j) => j.status === "running")) {
      live[j.id] = Object.entries(await units(env, j)).filter(([, u]) => u.state !== "done")
        .map(([k, u]) => `${k} ${u.state}`).join(", ") || "between stages";
    }
    const lines = jobs.map((j) => `day ${j.day ?? "?"} ${j.status}${live[j.id] ? ` (${live[j.id]})` : ""}: ` +
      `${j.title || j.id}${j.publish_at ? `, publish ${ny(j.publish_at)} NY` : ""}`);
    return say(env, `Automatic start: ${s.paused ? "paused" : "on"}, ${s.per_day} a day from ${s.start_hour}:00, ` +
      `next day ${s.next_day}\nToday: ${s.started[today(s)] || 0} started\n` +
      `Running: ${s.batch ? (await batchJobs(env, s)).map((j) => `day ${j.day}`).join(", ") || "finishing" : "nothing"}\n` +
      `GPU this week: ${(s.gpu[week()] || 0).toFixed(1)} of ${GPU_WEEK} h, about ${gpuAverage(s).toFixed(1)} h a video` +
      `\n\n${lines.join("\n") || "No videos yet."}`);
  }
  return say(env, HELP);
}

// ---------- deploy check ----------

async function check(env) {
  const r = await fetch(`https://www.kaggle.com/api/v1/kernels/status?userName=${env.KAGGLE_USERNAME}&kernelSlug=studio-gpu`,
    { headers: { Authorization: "Basic " + btoa(`${env.KAGGLE_USERNAME}:${env.KAGGLE_KEY}`) } });
  const plan = await get(env, "plan", { days: [] });
  const s = await state(env);
  return { kaggle: r.status, kaggle_status: (await r.json().catch(() => ({}))).status || null,
           kernel: Boolean(await env.KV.get("kernel")), days: plan.days.length, state: s, next_slot: nextSlot(s.last_slot) };
}
