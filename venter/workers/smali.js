#!/usr/bin/env node
// Venter smali worker — port of the old worker_smali.py.
// Decodes .dex → smali with baksmali.

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";
import { walkFiles } from "./lib/zip.js";

const MAX_FILES = 2000;

async function runSmali(inputPath, workDir, onProgress) {
  const outDir = path.join(workDir, "out");
  fs.mkdirSync(outDir, { recursive: true });

  await onProgress(15, "🧩 Running baksmali...");
  await exec("baksmali", ["d", "-o", outDir, inputPath]);
  await onProgress(85, "🧩 Packaging...");

  const files = walkFiles(outDir).filter((f) => f.endsWith(".smali"));
  if (files.length === 0) throw new Error("baksmali produced no .smali files");
  if (files.length > MAX_FILES) throw new Error("Too many files in output — aborting");
  return files.map((f) => ({ arcname: path.relative(outDir, f), path: f }));
}

runMain(() =>
  runWorker({
    engine: "Smali",
    zipSuffix: "smali",
    run: runSmali,
    batchExts: [".dex"],
  })
);
