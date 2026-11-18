// History Studio on Cloudflare: the videos start by themselves, and the Telegram side of every video.
//
//   every 30 min  if nothing is running and today's count (default 4) is not reached, the next day of plan.json
//                 starts on Kaggle (research, then gpu, then render chain themselves). The count resets every day at
//                 the start hour (6:00 India). One video at a time, and none when the week's GPU hours run out.
//                 A day that fails twice is skipped and reported, so one bad topic never stops the rest.
//   Yes           the video is approved and gets the next free publish slot: two a day, 11:00 AM and 5:30 PM New
//                 York. Four made a day fill two days of slots, so the extra ones wait for the next free day.
//                 Making never waits for an approval.
//   No            asks what is wrong and keeps the reason and the screenshots for the fix step
//   /settings     the upload settings, asked once with their options; title, description, hashtags and captions
//                 are made by the studio, paid promotion and AI use are always "No"
//   /status /next /day N /pause /resume /perday N /starthour H /help
//
// State lives in KV: "state", "plan", "kernel" (cloud/kernel.py), "settings", "job:<id>", "await".
// The Kaggle kernels report back on /hook with the shared secret.

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
const DEFAULT = { next_day: 1, paused: true, running: null, last_slot: null, per_day: 4, start_hour: 6, started: {},
                  tries: {}, gpu: {}, gpu_start: {}, told: null };
const state = async (env) => ({ ...DEFAULT, ...(await get(env, "state", {})) });

const GPU_WEEK = 29;        // Kaggle gives 30 GPU hours a week; one is kept spare
const STALE = 12 * 3600e3;  // a video still "running" after this never reported back: it counts as failed once

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

// ---------- starting a video ----------

function slugOf(title) {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
}

async function start(env, day) {
  const plan = await get(env, "plan", { days: [] });
  const d = plan.days.find((x) => x.day === day);
  if (!d) return `Day ${day} is not in the plan.`;
  const slug = slugOf(d.title);
  const run = { id: slug.slice(0, 40), title: d.title, minutes: d.minutes || 22, style: "documentary", slug,
                subject: d.subject || "", day, stage: "research" };
  await kagglePush(env, run);
  const s = await state(env);
  s.running = { id: run.id, day, since: Date.now() };
  s.started[today(s)] = (s.started[today(s)] || 0) + 1;
  await put(env, "state", s);
  await put(env, `job:${run.id}`, { id: run.id, day, title: d.title, status: "running", stage: "research",
                                    started: new Date().toISOString(), feedback: [] });
  return `Day ${day} started on Kaggle: ${d.title}`;
}

// a day that failed: tried once more, then skipped so the plan goes on
function failedDay(s, day) {
  s.tries[day] = (s.tries[day] || 0) + 1;
  if (s.tries[day] < 2) return `It will be tried once more.`;
  if (s.next_day === day) s.next_day = day + 1;
  return `It failed twice, so it is skipped for now (start it again with /day ${day}).`;
}

async function tick(env) {
  const s = await state(env);
  if (s.paused) return;
  if (s.running && Date.now() - s.running.since > STALE) {  // the kernels went quiet: Kaggle stopped them
    const why = failedDay(s, s.running.day);
    await say(env, `Day ${s.running.day} did not finish in 12 hours. ${why}`);
    s.running = null;
    await put(env, "state", s);
  }
  if (s.running || (s.started[today(s)] || 0) >= s.per_day) return;
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
  const msg = await start(env, day).catch((err) => `Could not start day ${day}: ${err}`);
  await say(env, msg);
}

async function kagglePush(env, run) {
  const code = await env.KV.get("kernel");
  if (!code) throw new Error("kernel.py is not in KV, run: python cloud.py worker");
  const me = env.KAGGLE_USERNAME;
  const name = run.stage === "part" ? `studio-render-${run.part}` : `studio-${run.stage}`;
  const gpu = run.stage === "gpu";
  const body = {
    slug: `${me}/${name}`, newTitle: name, text: code.replace("__RUN__", JSON.stringify(run)),
    language: "python", kernelType: "script", isPrivate: true, enableGpu: gpu, enableTpu: false, enableInternet: true,
    datasetDataSources: ["code", "assets", "secrets"].map((x) => `${me}/history-studio-${x}`),
    competitionDataSources: [], kernelDataSources: [], modelDataSources: [], categoryIds: [],
    sessionTimeoutSeconds: ({ join: 2, render: 9 }[run.stage] || 3) * 3600, ...(gpu ? { machineShape: "NvidiaTeslaT4" } : {}),
  };
  const auth = "Basic " + btoa(`${me}:${env.KAGGLE_KEY}`);
  const r = await fetch("https://www.kaggle.com/api/v1/kernels/push", {
    method: "POST", headers: { Authorization: auth, "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const out = await r.json().catch(() => ({}));
  if (!r.ok || out.error) throw new Error(`Kaggle push: ${r.status} ${out.error || out.message || ""}`.trim());
  return out;
}

// ---------- what the kernels report ----------

async function hook(e, env) {
  const job = await get(env, `job:${e.id}`, { id: e.id, title: e.title, day: e.day, feedback: [] });
  const s = await state(env);
  const mine = s.running?.id === e.id;
  if (e.event === "stage") {
    Object.assign(job, { stage: e.stage, status: "running" });
    if (e.stage === "gpu") s.gpu_start[e.id] = Date.now();
  }
  if (e.stage === "gpu" && (e.event === "done" || e.event === "failed") && s.gpu_start[e.id]) {
    const hours = (Date.now() - s.gpu_start[e.id]) / 3600e3;
    s.gpu[week()] = (s.gpu[week()] || 0) + hours;
    if (e.event === "done") s.gpu_runs = { ...(s.gpu_runs || {}), [e.id]: hours };
    delete s.gpu_start[e.id];
  }
  if (e.event === "failed") {
    // a failed stage runs once more from where it stopped (its folder is on Drive); failing again, the day is skipped
    const key = `${e.stage}${e.run?.part ?? ""}`;
    job.retries = { ...(job.retries || {}), [key]: (job.retries?.[key] || 0) + 1 };
    const again = e.run && job.retries[key] === 1 &&
      (await kagglePush(env, e.run).then(() => true).catch(() => false));
    if (again) {
      Object.assign(job, { status: "running", stage: e.stage });
      await say(env, `Day ${job.day ?? "?"}: the ${key} stage failed (${(e.error || "").slice(0, 120)}), running it once more.`);
    } else {
      Object.assign(job, { status: "failed", stage: e.stage, error: e.error });
      if (mine) s.running = null;
      if (job.day) {
        s.tries[job.day] = 1;
        await say(env, `Day ${job.day}: ${failedDay(s, job.day)}`);
      }
    }
  }
  if (e.event === "ready") {
    Object.assign(job, { status: "ready", link: e.link, youtube: e.youtube || null, ready: new Date().toISOString() });
    if (mine) s.running = null;
    if (job.day && job.day >= s.next_day) s.next_day = job.day + 1;
  }
  for (const w of Object.keys(s.gpu)) if (w < week()) delete s.gpu[w];  // keep only this week
  for (const d of Object.keys(s.started)) if (d < today(s)) delete s.started[d];
  await put(env, `job:${e.id}`, job);
  await put(env, "state", s);
  if (e.event === "ready" && !s.paused) await tick(env);  // the next video starts right away, not at the next tick
  return { ok: true };
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
    return say(env, await start(env, day).catch((err) => `Could not start day ${day}: ${err}`));
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
    const lines = jobs.map((j) => `day ${j.day ?? "?"} ${j.status}${j.status === "running" ? ` (${j.stage})` : ""}: ` +
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
