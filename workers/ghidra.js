#!/usr/bin/env node
// Venter Ghidra worker — port of the Python worker.py to TypeScript/Node.
//
// Runs inside a GitHub Actions runner. Reads everything from env:
//   PAYLOAD      — JSON from github.event.client_payload (see workflow yml)
//   API_ID/HASH  — GitHub Actions secrets (Telegram app credentials)
//   SESSION_KEY  — GitHub Actions secret (AES key for the session blob)
//   GHIDRA_HOME  — optional, defaults to /opt/ghidra
//
// Flow: decrypt session → mtcute login → download input from Saved Messages
// → enforce limits → run analyzeHeadless (with crash-retry) → zip results
// → upload zip to Saved Messages → notify completion via ntfy.sh.

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { Ntfy, progressBar } from "./lib/ntfy.js";
import { decryptSessionBlob } from "./lib/crypto.js";
import { Tg } from "./lib/tg.js";
import { Limits } from "./lib/limits.js";
import { isZip, extractZip, createZipFromFiles, listZipEntries } from "./lib/zip.js";
import { runGhidra, applyMemorySettings, extractErrorInfo } from "./lib/ghidra.js";

const SCRIPT_DIR = path.resolve(import.meta.dirname, "..", "ghidra_scripts");

/** Sanitize a filename extension (port of worker.py's ext regex). */
function sanitizeExt(ext) {
  if (ext && /^\.[A-Za-z0-9]{1,10}$/.test(ext)) return ext.toLowerCase();
  return ".bin";
}

/** Sanitize output filename (port of safe_name). */
function safeName(name, fallback = "file") {
  const s = (name || "").replace(/[^A-Za-z0-9._-]+/g, "_").slice(0, 60);
  return s || fallback;
}

/** Optional abuse counter back to the web frontend (kept for parity). */
async function reportExtraCount(reportUrl, token, userId, extra) {
  if (!reportUrl || !token || extra <= 0) return;
  try {
    const resp = await fetch(reportUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Count-Token": token },
      body: JSON.stringify({ user_id: userId, count: extra }),
    });
    if (!resp.ok) console.warn("count report failed:", resp.status);
  } catch (e) {
    console.warn("count report error:", e.message);
  }
}

async function main() {
  const payload = JSON.parse(process.env.PAYLOAD || "{}");
  const {
    file_message_id, // Saved Messages message id of the uploaded input file
    file_id,         // optional fallback (raw document reference)
    filename = "download",
    job_id = "",
    is_admin = false,
    is_premium = false,
    user_id = "",
    report_url = "",
  } = payload;

  const ntfy = new Ntfy(job_id);
  const limits = new Limits({ isAdmin: is_admin, isPremium: is_premium, filename });

  await ntfy.progress(0, "🟢 Job started! Preparing Ghidra engine on cloud runner...");

  const apiId = process.env.API_ID;
  const apiHash = process.env.API_HASH;
  if (!apiId || !apiHash) throw new Error("API_ID / API_HASH env missing on runner");

  // Session is decrypted ONCE, kept only in this process's memory.
  const session = decryptSessionBlob(payload.session, process.env.SESSION_KEY);
  const tg = new Tg({ apiId, apiHash, session });
  const me = await tg.start();
  console.log("Logged in to Telegram as user", me?.id ?? "?");

  applyMemorySettings();
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "venter-ghidra-"));
  try {
    const dest = path.join(workDir, "input" + sanitizeExt(path.extname(filename)));

    // ---- 1. Download input from Saved Messages ----
    let dlLast = -100;
    await ntfy.progress(0, "📥 Downloading file from Saved Messages...");
    await tg.downloadInput(
      { messageId: file_message_id, fileId: file_id },
      dest,
      (pct) => {
        if (pct < dlLast || (pct - dlLast < 2 && pct < 100)) return;
        dlLast = pct;
        ntfy.progress(pct, "📥 Downloading file...", progressBar(pct));
      }
    );

    const size = fs.statSync(dest).size;
    if (size === 0) throw new Error("Downloaded file is empty.");
    limits.checkDownloadSize(size);

    const fileMagic = fs.readFileSync(dest).subarray(0, 16).toString("hex").replace(/(..)(?=.)/g, "$1 ");
    console.log(`Downloaded ${size} bytes, magic: ${fileMagic}`);

    // ---- 2. ZIP abuse checks ----
    const extra = limits.countZipSoDex(dest);
    if (extra) await reportExtraCount(report_url, process.env.TELEGRAM_BOT_TOKEN || "", user_id, extra);
    limits.checkZipLimits(dest);

    await ntfy.progress(0, `📥 Downloaded ${(size / 1024 / 1024).toFixed(1)} MB! Starting Ghidra analysis...`);

    // ---- 3. Run Ghidra (batch ZIP or single file, with crash-retry) ----
    const startT = Date.now();
    let last = { pct: 0, label: "", at: 0 };
    const onProgress = async (pct, label) => {
      const now = Date.now();
      if (pct - last.pct < 5 && label === last.label && now - last.at < 60_000) return;
      last = { pct, label, at: now };
      const mins = Math.floor((now - startT) / 60_000);
      const secs = Math.floor((now - startT) / 1000) % 60;
      const pad = (n) => String(n).padStart(2, "0");
      const elapsed =
        mins >= 60 ? `${Math.floor(mins / 60)}h ${pad(mins % 60)}m` : `${mins}m ${pad(secs)}s`;
      await ntfy.progress(pct, label, `${progressBar(pct)}\n⏱ ${elapsed}`);
    };

    const outFiles = []; // { arcname, path }

    if (isZip(dest)) {
      // Batch decompile — extract and find candidate binaries
      const extractDir = path.join(workDir, "extracted_batch");
      fs.mkdirSync(extractDir, { recursive: true });
      extractZip(dest, extractDir);

      const isApk = filename.toLowerCase().endsWith(".apk");
      const candidates = [];
      const walk = (dir) => {
        for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
          const fp = path.join(dir, entry.name);
          if (entry.isDirectory()) walk(fp);
          else {
            const ext = path.extname(fp).toLowerCase();
            if (isApk) {
              if (ext === ".so") candidates.push(fp);
            } else if (
              [".so", ".dll", ".exe", ".elf", ".apk", ".bin", ".jar", ".o", ".dylib"].includes(ext) ||
              (!ext && fs.statSync(fp).size > 1024)
            ) {
              candidates.push(fp);
            }
          }
        }
      };
      walk(extractDir);

      if (candidates.length > 5 && !is_admin) {
        throw new Error(
          `⚠️ Batch Limit Exceeded!\nArchive contains ${candidates.length} binary files. Maximum batch limit is 5 files per ZIP.`
        );
      }
      if (candidates.length >= 1) {
        await ntfy.progress(0, `📦 Batch / APK Detected! Found ${candidates.length} binary file(s).`);
        for (let i = 0; i < candidates.length; i++) {
          const binPath = candidates[i];
          await ntfy.progress(0, `⚙️ Processing (${i + 1}/${candidates.length}): ${path.basename(binPath)}...`);
          for (const attempt of [1, 2]) {
            try {
              const res = await runGhidra({
                filePath: binPath,
                workDir: path.join(workDir, `analysis_${i + 1}`),
                onProgress,
                disableCallFixup: attempt === 2,
                scriptDir: SCRIPT_DIR,
              });
              const bname = path.basename(binPath, path.extname(binPath));
              if (fs.existsSync(res.outC) && fs.statSync(res.outC).size > 0) {
                outFiles.push({ arcname: `${bname}.c`, path: res.outC });
              }
              if (fs.existsSync(res.outMeta) && fs.statSync(res.outMeta).size > 0) {
                outFiles.push({ arcname: `${bname}_info.txt`, path: res.outMeta });
              }
              break;
            } catch (e) {
              if (attempt === 1) {
                console.warn(`Batch file ${path.basename(binPath)} crashed, retrying without CallFixup:`, e.message);
                continue;
              }
              console.warn(`Batch file ${path.basename(binPath)} failed:`, e.message);
              break;
            }
          }
        }
      }
    }

    if (outFiles.length === 0) {
      // Single-file decompile with crash-retry
      let result = null;
      let firstErr = null;
      for (const attempt of [1, 2]) {
        try {
          result = await runGhidra({
            filePath: dest,
            workDir: path.join(workDir, "analysis"),
            onProgress,
            disableCallFixup: attempt === 2,
            scriptDir: SCRIPT_DIR,
          });
          break;
        } catch (e) {
          if (attempt === 1) {
            firstErr = e.message;
            console.warn("Ghidra crashed, retrying with CallFixupAnalyzer disabled:", e.message);
            continue;
          }
          throw new Error(`Both attempts failed.\n[1st] ${firstErr}\n[2nd] ${e.message}`);
        }
      }

      const bname = path.basename(safeName(filename), path.extname(filename)) || "decompiled";
      if (fs.existsSync(result.outC) && fs.statSync(result.outC).size > 0) {
        outFiles.push({ arcname: `${bname}.c`, path: result.outC });
      }
      if (fs.existsSync(result.outMeta) && fs.statSync(result.outMeta).size > 0) {
        outFiles.push({ arcname: `${bname}_info.txt`, path: result.outMeta });
      }
    }

    if (outFiles.length === 0) {
      const diag =
        extractErrorInfo(result?.lines || []).slice(0, 1400) ||
        (result?.tail || "").slice(-400);
      let msg = "❌ Analysis failed or no output files generated.";
      if (fileMagic) msg += `\n\n📦 File: ${(size / 1024 / 1024).toFixed(1)} MB · magic: ${fileMagic}`;
      if (diag) msg += `\n\n<code>${diag}</code>`;
      throw new Error(msg);
    }

    // ---- 4. Package results ----
    await ntfy.progress(95, "📦 Packaging results...");
    const safe = safeName(filename);
    const origStem = path.basename(safe, path.extname(safe)) || "decompiled";
    const zipPath = path.join(workDir, `${origStem}_decompiled.zip`);
    createZipFromFiles(zipPath, outFiles);

    // ---- 5. Upload result to Saved Messages ----
    await ntfy.progress(96, "✅ Decompilation complete! Uploading ZIP...");
    let upLast = -1;
    const { messageId, size: zipSize } = await tg.uploadFile(
      zipPath,
      `✅ Decompiled ${safe} with Ghidra — Powered By @R3V_X`,
      (pct) => {
        if (pct < upLast || pct - upLast < 2) return;
        upLast = pct;
        ntfy.progress(96 + Math.round(pct * 0.04), "📤 Uploading ZIP...", progressBar(pct));
      }
    );

    // ---- 6. Done ----
    await ntfy.final({
      status: "done",
      message_id: messageId,
      filename: `${origStem}_decompiled.zip`,
      size: zipSize,
      caption: `✅ Decompiled ${safe} with Ghidra — Powered By @R3V_X`,
    });
    console.log("DONE: uploaded result to Saved Messages, message id", messageId);
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
    await tg.close();
  }
}

main().catch(async (err) => {
  console.error("FATAL:", err);
  try {
    const payload = JSON.parse(process.env.PAYLOAD || "{}");
    const ntfy = new Ntfy(payload.job_id || "");
    await ntfy.final({
      status: "error",
      error: String(err?.message || err).slice(0, 1200),
    });
  } catch {
    /* ignore */
  }
  process.exit(1);
});
