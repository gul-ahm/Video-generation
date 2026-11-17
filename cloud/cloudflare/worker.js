// History Studio on Cloudflare: the daily start, and the Telegram side of every video.
//
//   every day     starts the next day of plan.json on Kaggle (research, then gpu, then render chain themselves)
//   Yes           the video is approved and gets the next free publish slot, 11 AM New York time by default
//   No            asks what is wrong and keeps the reason and the screenshots for the fix step
//   /settings     the upload settings, asked once with their options; title, description, hashtags and captions
//                 are made by the studio, paid promotion and AI use are always "No"
//   /status /next /day N /pause /resume /help
//
// State lives in KV: "state", "plan", "kernel" (cloud/kernel.py), "settings", "job:<id>", "await".
// The Kaggle kernels report back on /hook with the shared secret.

const FIXED = ["Paid promotion: No", "Altered or synthetic content (AI): No"];

const SETTINGS = [
  ["publish_time", "Publish time (New York)", ["11:00", "14:00", "17:00", "20:00"]],
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
/pause  stop the daily start, /resume  start it again (/resume 2 goes on from day 2)
/settings  choose the upload settings (/settings reset to choose again)`;

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
    ctx.waitUntil(daily(env));
  },
};

// ---------- KV ----------

const get = async (env, key, fallback) => JSON.parse((await env.KV.get(key)) || "null") ?? fallback;
const put = (env, key, value) => env.KV.put(key, JSON.stringify(value));
const state = (env) => get(env, "state", { next_day: 1, paused: true, running: null, last_slot: null });

// ---------- starting a video ----------

function slugOf(title) {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
}

async function start(env, day, who) {
  const plan = await get(env, "plan", { days: [] });
  const d = plan.days.find((x) => x.day === day);
  if (!d) return `Day ${day} is not in the plan.`;
  const s = await state(env);
  const busy = s.running && Date.now() - s.running.since < 20 * 3600e3;
  if (busy && who === "daily") return null;
  const slug = slugOf(d.title);
  const run = { id: slug.slice(0, 40), title: d.title, minutes: d.minutes || 22, style: "documentary", slug,
                subject: d.subject || "", day, stage: "research" };
  await kagglePush(env, run);
  s.running = { id: run.id, day, since: Date.now() };
  await put(env, "state", s);
  await put(env, `job:${run.id}`, { id: run.id, day, title: d.title, status: "running", stage: "research",
                                    started: new Date().toISOString(), feedback: [] });
  return `Day ${day} started on Kaggle: ${d.title}${busy ? "\n(another video was still running)" : ""}`;
}

async function daily(env) {
  const s = await state(env);
  if (s.paused) return;
  const msg = await start(env, s.next_day, "daily").catch((err) => `The daily start failed: ${err}`);
  if (msg) await say(env, msg);
}

async function kagglePush(env, run) {
  const code = await env.KV.get("kernel");
  if (!code) throw new Error("kernel.py is not in KV, run: python cloud.py worker");
  const me = env.KAGGLE_USERNAME;
  const body = {
    slug: `${me}/studio-research`, newTitle: "studio-research", text: code.replace("__RUN__", JSON.stringify(run)),
    language: "python", kernelType: "script", isPrivate: true, enableGpu: false, enableTpu: false, enableInternet: true,
    datasetDataSources: ["code", "assets", "secrets"].map((x) => `${me}/history-studio-${x}`),
    competitionDataSources: [], kernelDataSources: [], modelDataSources: [], categoryIds: [],
    sessionTimeoutSeconds: 3 * 3600,
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
  if (e.event === "stage") Object.assign(job, { stage: e.stage, status: "running" });
  if (e.event === "failed") {
    Object.assign(job, { status: "failed", stage: e.stage, error: e.error });
    if (s.running?.id === e.id) s.running = null;
  }
  if (e.event === "ready") {
    Object.assign(job, { status: "ready", link: e.link, youtube: e.youtube || null, ready: new Date().toISOString() });
    if (s.running?.id === e.id) s.running = null;
    if (job.day && job.day >= s.next_day) s.next_day = job.day + 1;
  }
  await put(env, `job:${e.id}`, job);
  await put(env, "state", s);
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

function nextSlot(last, hhmm) {
  let date = nyDate(Date.now());
  for (let i = 0; i < 400; i++) {
    const t = nyTime(date, hhmm);
    if (t > Date.now() + 2 * 3600e3 && (!last || date > last)) return { date, at: new Date(t).toISOString() };
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
    const slot = nextSlot(s.last_slot, prefs.publish_time || "11:00");
    Object.assign(job, { status: "approved", approved: new Date().toISOString(), publish_at: slot.at });
    s.last_slot = slot.date;
    await put(env, `job:${a}`, job);
    await put(env, "state", s);
    await Promise.all([done("Approved"), unbutton()]);
    const missing = SETTINGS.filter(([k]) => !(k in prefs)).length;
    return say(env, `Approved: ${job.title || a}\nPublish: ${ny(slot.at)} New York (${ist(slot.at)} India)` +
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
    return say(env, s.paused ? "Daily start paused." : `Daily start on. Next: day ${s.next_day}.`);
  }
  if (cmd === "/next" || cmd === "/day") {
    const day = cmd === "/day" ? Number(arg) : s.next_day;
    if (!day) return say(env, "Send it like: /day 5");
    return say(env, await start(env, day, "you").catch((err) => `Could not start day ${day}: ${err}`));
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
    return say(env, `Daily start: ${s.paused ? "paused" : "on"}, next day ${s.next_day}\n` +
      `Running: ${s.running ? `day ${s.running.day}` : "nothing"}\n\n${lines.join("\n") || "No videos yet."}`);
  }
  return say(env, HELP);
}

// ---------- deploy check ----------

async function check(env) {
  const r = await fetch(`https://www.kaggle.com/api/v1/kernels/status?userName=${env.KAGGLE_USERNAME}&kernelSlug=studio-gpu`,
    { headers: { Authorization: "Basic " + btoa(`${env.KAGGLE_USERNAME}:${env.KAGGLE_KEY}`) } });
  const plan = await get(env, "plan", { days: [] });
  return { kaggle: r.status, kaggle_status: (await r.json().catch(() => ({}))).status || null,
           kernel: Boolean(await env.KV.get("kernel")), days: plan.days.length, state: await state(env),
           slot_example: nextSlot(null, "11:00") };
}
