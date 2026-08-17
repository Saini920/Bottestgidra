// Generic worker flow for the "simple" engines (JADX, apktool, dex2jar,
// smali, dex-compile, cc-compile, apk build/sign, pdf→txt).
//
// Each engine file only implements `run(inputPath, workDir, onProgress)` and
// calls runWorker() with a small config. Everything else (session decrypt,
// Telegram login, download, limits, zip packaging, upload, ntfy progress) is
// shared here.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

import { Ntfy, progressBar } from "./ntfy.js";
import { decryptSessionBlob } from "./crypto.js";
import { Tg } from "./tg.js";
import { Limits } from "./limits.js";
import { isZip, extractZip, createZipFromFiles, walkFiles } from "./zip.js";

export function sanitizeExt(ext) {
  if (ext && /^\.[A-Za-z0-9]{1,10}$/.test(ext)) return ext.toLowerCase();
  return ".bin";
}

export function safeName(name, fallback = "file") {
  const s = (name || "").replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 60);
  return s || fallback;
}

/** Spawn a command, capture output, call onLine per line (for progress parsing). */
export function exec(cmd, args, onLine) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { stdio: ["ignore", "pipe", "pipe"] });
    let err = "";
    const onData = (chunk) => {
      const text = chunk.toString();
      err += text;
      for (const line of text.split("\n")) {
        if (line.trim()) onLine?.(line);
      }
    };
    p.stdout.on("data", onData);
    p.stderr.on("data", onData);
    p.on("error", (e) => reject(new Error(`Failed to start ${cmd}: ${e.message}`)));
    p.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`${cmd} exited with code ${code}:\n${err.slice(-800)}`));
    });
  });
}

/**
 * Run a worker end-to-end.
 * @param {object} cfg
 * @param {string} cfg.engine        human label, e.g. "JADX"
 * @param {string} cfg.zipSuffix     output zip suffix, e.g. "jadx"
 * @param {(inputPath: string, workDir: string, onProgress: (pct:number,label:string)=>Promise<void>) => Promise<{arcname:string,path:string}[]>} cfg.run
 * @param {string[]} [cfg.batchExts] if set, ZIP inputs are extracted and each
 *                                   matching file is run separately (max 5)
 */
export async function runWorker(cfg) {
  const payload = JSON.parse(process.env.PAYLOAD || "{}");
  const {
    file_message_id,
    file_id,
    filename = "download",
    job_id = "",
    is_admin = false,
    is_premium = false,
    user_id = "",
    report_url = "",
  } = payload;

  const ntfy = new Ntfy(job_id);
  const limits = new Limits({ isAdmin: is_admin, isPremium: is_premium, filename });

  await ntfy.progress(0, `🟢 Job started! Preparing ${cfg.engine} engine on cloud runner...`);

  const apiId = process.env.API_ID;
  const apiHash = process.env.API_HASH;
  if (!apiId || !apiHash) throw new Error("API_ID / API_HASH env missing on runner");

  const session = decryptSessionBlob(payload.session, process.env.SESSION_KEY);
  const tg = new Tg({ apiId, apiHash, session });
  const me = await tg.start();
  console.log("Logged in as", me?.id ?? "?");

  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), `venter-${cfg.zipSuffix}-`));
  try {
    const dest = path.join(workDir, "input" + sanitizeExt(path.extname(filename)));

    await ntfy.progress(0, "📥 Downloading file from Saved Messages...");
    let dlLast = -100;
    await tg.downloadInput({ messageId: file_message_id, fileId: file_id }, dest, (pct) => {
      if (pct < dlLast || (pct - dlLast < 2 && pct < 100)) return;
      dlLast = pct;
      ntfy.progress(pct, "📥 Downloading file...", progressBar(pct));
    });

    const size = fs.statSync(dest).size;
    if (size === 0) throw new Error("Downloaded file is empty.");
    limits.checkDownloadSize(size);
    if (report_url) limits.countZipSoDex(dest);

    await ntfy.progress(4, `📥 Downloaded ${(size / 1024 / 1024).toFixed(1)} MB!`);

    const startT = Date.now();
    let last = { pct: 0, label: "", at: 0 };
    const onProgress = async (pct, label) => {
      const now = Date.now();
      if (pct - last.pct < 5 && label === last.label && now - last.at < 60_000) return;
      last = { pct, label, at: now };
      const mins = Math.floor((now - startT) / 60_000);
      await ntfy.progress(pct, label, `${progressBar(pct)}\n⏱ ${mins}m`);
    };

    const outFiles = [];
    if (cfg.batchExts && isZip(dest)) {
      const extractDir = path.join(workDir, "batch");
      fs.mkdirSync(extractDir, { recursive: true });
      extractZip(dest, extractDir);
      const candidates = walkFiles(extractDir).filter((f) =>
        cfg.batchExts.includes(path.extname(f).toLowerCase())
      );
      if (candidates.length > 5 && !is_admin) {
        throw new Error(`Batch Limit Exceeded! Max 5 files per ZIP for ${cfg.engine}.`);
      }
      if (candidates.length === 0) {
        throw new Error(`ZIP me koi ${cfg.batchExts.join(" / ")} file nahi mili.`);
      }
      for (let i = 0; i < candidates.length; i++) {
        const c = candidates[i];
        await ntfy.progress(5, `📦 Processing (${i + 1}/${candidates.length}): ${path.basename(c)}...`);
        const res = await cfg.run(c, path.join(workDir, `a${i + 1}`), onProgress);
        const prefix = path.basename(c, path.extname(c));
        res.forEach((f) => outFiles.push({ arcname: `${prefix}/${f.arcname}`, path: f.path }));
      }
    } else {
      const res = await cfg.run(dest, path.join(workDir, "analysis"), onProgress);
      outFiles.push(...res);
    }

    if (outFiles.length === 0) throw new Error(`${cfg.engine} analysis produced no output files.`);

    await ntfy.progress(95, "📦 Packaging results...");
    const safe = safeName(filename);
    const origStem = path.basename(safe, path.extname(safe)) || "decompiled";
    const zipPath = path.join(workDir, `${origStem}_${cfg.zipSuffix}.zip`);
    createZipFromFiles(zipPath, outFiles);

    await ntfy.progress(96, "✅ Done! Uploading ZIP...");
    let upLast = -1;
    const { messageId, size: zipSize } = await tg.uploadFile(
      zipPath,
      `✅ ${cfg.engine} result — Powered By Venter`,
      (pct) => {
        if (pct < upLast || pct - upLast < 2) return;
        upLast = pct;
        ntfy.progress(96 + Math.round(pct * 0.04), "📤 Uploading ZIP...", progressBar(pct));
      }
    );

    await ntfy.final({
      status: "done",
      message_id: messageId,
      filename: `${origStem}_${cfg.zipSuffix}.zip`,
      size: zipSize,
      caption: `✅ ${cfg.engine} result — Powered By Venter`,
    });
    console.log("DONE:", messageId);
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
    await tg.close();
  }
}

/** Standard process.exit wrapper for worker entrypoints. */
export function runMain(fn) {
  fn().catch(async (err) => {
    console.error("FATAL:", err);
    try {
      const payload = JSON.parse(process.env.PAYLOAD || "{}");
      const ntfy = new Ntfy(payload.job_id || "");
      await ntfy.final({ status: "error", error: String(err?.message || err).slice(0, 1200) });
    } catch {
      /* ignore */
    }
    process.exit(1);
  });
}
