#!/usr/bin/env node
// Venter apktool worker — port of the old worker_apktool.py.
// Decompiles an APK to smali + resources with apktool.

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";
import { walkFiles } from "./lib/zip.js";

const MAX_FILES = 2000;

async function runApktool(inputPath, workDir, onProgress) {
  const outDir = path.join(workDir, "out");
  fs.mkdirSync(outDir, { recursive: true });

  await onProgress(15, "📱 Running apktool d...");
  await exec("apktool", ["d", "-f", "-o", outDir, inputPath], (line) => {
    const m = line.match(/(\d+)%\s*$/);
    if (m) onProgress(15 + Math.min(75, Number(m[1]) * 0.75), "📱 Decompiling APK...");
  });
  await onProgress(90, "📱 Packaging...");

  const files = walkFiles(outDir).filter((f) => !path.basename(f).startsWith("."));
  if (files.length === 0) throw new Error("apktool produced no files");
  if (files.length > MAX_FILES) throw new Error("Too many files in output — aborting");
  return files.map((f) => ({ arcname: path.relative(outDir, f), path: f }));
}

runMain(() =>
  runWorker({
    engine: "Apktool",
    zipSuffix: "apktool",
    run: runApktool,
    batchExts: [".apk"],
  })
);
