// History Studio on Cloudflare: the videos start by themselves, and the Telegram side of every video.
//
//   every 30 min  watches the running video (a kernel that ended without a word counts as failed, a chapter waiting
//                 for a free machine is pushed); if nothing runs and today's count (default 4) is not reached, the
//                 next day of plan.json starts. The worker pushes every stage: research, gpu, one machine per chapter,
//                 join. The count resets daily at the start hour (6:00 India). One video at a time, none when the
//                 week's GPU hours run out. A failed stage runs once more; failing again, the day is skipped.
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
const fresh = () => ({ next_day: 1, paused: true, running: null, last_slot: null, per_day: 4, start_hour: 6, started: {},
                       tries: {}, gpu: {}, gpu_start: {}, told: null });
const state = async (env) => ({ ...fresh(), ...(await get(env, "state", {})) });

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
const kernelOf = (key) => (key.startsWith("part") ? `studio-render-${key.slice(4)}` : `studio-${key}`);
const unitKey = (job, key) => `unit:${job.id}:${key}`;

async function units(env, job) {
  const keys = (await env.KV.list({ prefix: `unit:${job.id}:` })).keys.map((k) => k.name);
  const all = await Promise.all(keys.map((k) => get(env, k, null)));
  return Object.fromEntries(keys.map((k, i) => [k.split(":")[2], all[i]]).filter(([, u]) => u));
}

function newJob(d) {
  const slug = slugOf(d.title);
  return { id: slug.slice(0, 40), day: d.day, title: d.title, status: "running", started: new Date().toISOString(),
           run: { id: slug.slice(0, 40), title: d.title, minutes: d.minutes || 22, style: "documentary", slug,
                  subject: d.subject || "", day: d.day },
           chapters: null, feedback: [] };
}

// push one unit; a refused push (Kaggle runs too many at once) waits for the next tick
async function pushUnit(env, job, key, u = { tries: 0 }) {
  const run = { ...job.run, stage: kindOf(key), nonce: crypto.randomUUID().slice(0, 8),
                ...(key.startsWith("part") ? { part: Number(key.slice(4)) } : {}), chapters: job.chapters };
  try {
    await kagglePush(env, run);
    u = { ...u, state: "pushed", nonce: run.nonce, at: Date.now(), waiting: null };
  } catch (err) {
    u = { ...u, state: "waiting", nonce: null, waiting: u.waiting || Date.now(), why: String(err).slice(0, 200) };
  }
  await put(env, unitKey(job, key), u);
  return u;
}

async function startJob(env, day, from = "research", extra = {}) {
  const plan = await get(env, "plan", { days: [] });
  // day 0 is a test film from cloud.py test: its own title, not in the plan and not counted
  const d = day === 0 && extra.title ? { day: 0, title: extra.title, subject: extra.subject, minutes: extra.minutes }
    : plan.days.find((x) => x.day === day);
  if (!d) return `Day ${day} is not in the plan.`;
  const s = await state(env);
  if (s.running && s.running.day !== day) return `Day ${s.running.day} is still running; one video at a time.`;
  const job = { ...newJob(d), chapters: extra.chapters ?? null };
  for (const k of (await env.KV.list({ prefix: `unit:${job.id}:` })).keys) await env.KV.delete(k.name);  // a fresh run
  const done = extra.done || [];
  for (const k of done) await put(env, unitKey(job, `part${k}`), { state: "done", tries: 0 });
  const keys = from === "parts"
    ? [...Array(job.chapters).keys()].filter((k) => !done.includes(k)).map((k) => `part${k}`) : [from];
  const pushed = [];
  for (const key of keys) pushed.push([key, (await pushUnit(env, job, key)).state]);
  s.running = { id: job.id, day, since: Date.now() };
  if (from === "research" && day) s.started[today(s)] = (s.started[today(s)] || 0) + 1;
  await put(env, `job:${job.id}`, job);
  await put(env, "state", s);
  const waiting = pushed.filter(([, st]) => st === "waiting").map(([k]) => k);
  return `Day ${day} ${from === "research" ? "started" : `goes on from ${from}`}: ${d.title}` +
    (waiting.length ? `\n(${waiting.join(", ")} wait for a free Kaggle machine)` : "");
}

// a day that failed: once more from the start, then skipped so the plan goes on
function failedDay(s, day) {
  s.tries[day] = (s.tries[day] || 0) + 1;
  if (!day) return "This was the test film.";
  if (s.tries[day] < 2) return `It will be tried once more.`;
  if (s.next_day === day) s.next_day = day + 1;
  return `It failed twice, so it is skipped for now (start it again with /day ${day}).`;
}

async function jobFailed(env, job, s, why) {
  job.status = "failed";
  job.error = why;
  if (s.running?.id === job.id) s.running = null;
  await put(env, `job:${job.id}`, job);
  await say(env, `Day ${job.day}: ${why}\n${failedDay(s, job.day)}`);
}

// what comes after a unit is done
async function advance(env, job, key) {
  if (key === "research") return pushUnit(env, job, "gpu");
  if (key === "gpu") {
    for (let k = 0; k < job.chapters; k++) await pushUnit(env, job, `part${k}`);
    return;
  }
  if (!key.startsWith("part")) return;
  const all = await units(env, job);
  for (const [k, u] of Object.entries(all)) if (u.state === "waiting") await pushUnit(env, job, k, u);
  await joinWhenReady(env, job, all, key);
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

async function unitFailed(env, job, s, key, u, why, retry = true) {
  u = { ...u, tries: (u.tries || 0) + 1, state: "failed", why };
  await put(env, unitKey(job, key), u);
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
    await advance(env, job, key);
  }
  if (e.event === "failed") await unitFailed(env, job, s, key, u, e.error || "no reason given", e.kind !== "quality");
  if (e.event === "ready") {
    await put(env, unitKey(job, key), { ...u, state: "done", finished: Date.now() });
    Object.assign(job, { status: "ready", link: e.link, ready: new Date().toISOString() });
    await put(env, `job:${job.id}`, job);
    if (s.running?.id === job.id) s.running = null;
    if (job.day >= s.next_day) s.next_day = job.day + 1;
    if (job.day === 0) await say(env, `TEST FILM, not for publishing\n\n${readyText(e)}`);
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

// the running video: kernels that ended without a word, chapters waiting for a machine
async function watch(env, s) {
  const job = await get(env, `job:${s.running.id}`, null);
  if (!job || job.status !== "running") {
    s.running = null;
    return;
  }
  const all = await units(env, job);
  if (!Object.keys(all).length && Date.now() - s.running.since > 3600e3) {
    return jobFailed(env, job, s, "the worker has no record of its stages (started by an older version)");
  }
  await joinWhenReady(env, job, all);
  for (const [key, u] of Object.entries(all)) {
    if (u.state === "waiting") {
      const now = await pushUnit(env, job, key, u);
      if (now.state === "waiting" && Date.now() - now.waiting > 3 * 3600e3) {
        return jobFailed(env, job, s, `${key} found no free Kaggle machine in 3 hours (${now.why})`);
      }
      continue;
    }
    if (!["pushed", "running"].includes(u.state) || Date.now() - u.at < GRACE) continue;
    const status = await kaggleStatus(env, kernelOf(key));
    if (/error|cancel|complete/.test(status) || Date.now() - u.at > (LIMIT[kindOf(key)] + 1) * 3600e3) {
      await unitFailed(env, job, s, key, u, `the kernel ended without reporting (Kaggle says ${status})`);
      if (job.status !== "running") return;
    }
  }
}

async function tick(env) {
  const s = await state(env);
  if (s.running) {
    const before = JSON.stringify(s);
    await watch(env, s);
    if (JSON.stringify(s) !== before) await put(env, "state", s);  // KV gives 1000 writes a day: only real changes
  }
  if (s.paused || s.running || (s.started[today(s)] || 0) >= s.per_day) return;
  const plan = await get(env, "plan", { days: [] });
  if (!plan.days.some((x) => x.day >= s.next_day)) {
    s.paused = true;
    await put(env, "state", s);
    return say(env, "Every day of the plan has been made. The automatic start is paused.");
  }
  const used = s.gpu[week()] || 0;
  if (used + gpuAverage(s) > GPU_WEEK) {
    if (s.told !== week()) {
      s.told = week();
      await put(env, "state", s);
      await say(env, `This week's Kaggle GPU hours are used (${used.toFixed(1)} h). Videos start again on Saturday.`);
    }
    return;
  }
  const day = plan.days.map((x) => x.day).filter((x) => x >= s.next_day).sort((a, b) => a - b)[0];
  await say(env, await startJob(env, day).catch((err) => `Could not start day ${day}: ${err}`));
}

async function kagglePush(env, run) {
  const code = await env.KV.get("kernel");
  if (!code) throw new Error("kernel.py is not in KV, run: python cloud.py worker");
  run = { ...run, code: await env.KV.get("code_version") };  // the kernel stops if Kaggle mounts other code
  const me = env.KAGGLE_USERNAME;
  const name = run.stage === "part" ? `studio-render-${run.part}` : `studio-${run.stage}`;
  const gpu = run.stage === "gpu";
  const body = {
    slug: `${me}/${name}`, newTitle: name, text: code.replace("__RUN__", JSON.stringify(run)),
    language: "python", kernelType: "script", isPrivate: true, enableGpu: gpu, enableTpu: false, enableInternet: true,
    datasetDataSources: ["code", "assets", "secrets"].map((x) => `${me}/history-studio-${x}`),
    competitionDataSources: [], kernelDataSources: [], modelDataSources: [], categoryIds: [],
    sessionTimeoutSeconds: LIMIT[run.stage] * 3600, ...(gpu ? { machineShape: "NvidiaTeslaT4" } : {}),
  };
  const auth = "Basic " + btoa(`${me}:${env.KAGGLE_KEY}`);
  const r = await fetch("https://www.kaggle.com/api/v1/kernels/push", {
    method: "POST", headers: { Authorization: auth, "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok || out.error) throw new Error(`Kaggle push: ${r.status} ${out.error || out.message || ""}`.trim());
  return out;
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
    if (s.running) return say(env, `Day ${s.running.day} is still running; one video at a time.`);
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
      `Running: ${s.running ? `day ${s.running.day}` : "nothing"}\n` +
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
