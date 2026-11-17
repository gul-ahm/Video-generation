// node remotion/render.mjs jobs.json
// jobs.json: [{"id": "Join", "props": {...}, "out": "/abs/path.mp4"}, ...]
// The project is bundled once and every job renders from that bundle, so ten transitions cost one start up.
import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition, ensureBrowser} from '@remotion/renderer';
import fs from 'node:fs';
import path from 'node:path';

const jobs = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const here = path.dirname(new URL(import.meta.url).pathname);
const browserExecutable = process.env.FX_BROWSER || null;
if (!browserExecutable) await ensureBrowser();

const serveUrl = await bundle({entryPoint: path.join(here, 'index.jsx'), publicDir: process.env.FX_PUBLIC || here});
for (const job of jobs) {
  if (fs.existsSync(job.out)) continue;
  const t = Date.now();
  try {
    const composition = await selectComposition({serveUrl, id: job.id, inputProps: job.props, browserExecutable});
    const alpha = job.out.endsWith('.mov');
    await renderMedia({
      serveUrl, composition, inputProps: job.props, browserExecutable,
      codec: alpha ? 'prores' : 'h264',
      proResProfile: alpha ? '4444' : undefined,
      pixelFormat: alpha ? 'yuva444p10le' : 'yuv420p',
      imageFormat: alpha ? 'png' : 'jpeg',
      crf: alpha ? undefined : 16,
      outputLocation: job.out + '.part' + path.extname(job.out),
      chromiumOptions: {gl: process.env.FX_GL || undefined},
      concurrency: process.env.FX_THREADS ? Number(process.env.FX_THREADS) : null,
      logLevel: 'error',
      timeoutInMilliseconds: Number(process.env.FX_TIMEOUT || 20000),
    });
    fs.renameSync(job.out + '.part' + path.extname(job.out), job.out);
    console.log(`fx ${job.id} ${job.props.kind || ''} ${((Date.now() - t) / 1000).toFixed(1)}s`);
  } catch (err) {
    // one transition that cannot render (an old Chrome without HTML in canvas) must not stop the others
    console.log(`fail ${job.id} ${job.props.kind || ''}: ${String(err.message).split('\n')[0].slice(0, 120)}`);
  }
}
