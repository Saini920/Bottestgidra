#!/usr/bin/env node
// Venter JADX worker — port of the old worker_jadx.py.

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";
import { walkFiles } from "./lib/zip.js";

const MAX_FILES = 200;

async function runJadx(inputPath, workDir, onProgress) {
  const outDir = path.join(workDir, "out");
  fs.mkdirSync(outDir, { recursive: true });

  await onProgress(15, "☕ Starting JADX...");
  await exec("jadx", ["--no-res", "--deobf", "-j", "4", "-d", outDir, inputPath], (line) => {
    const m = line.match(/progress\s+(\d+)/i) || line.match(/(\d+)\s*%$/);
    if (m) onProgress(15 + Math.min(75, Number(m[1]) * 0.75), "☕ Decompiling with JADX...");
  });
  await onProgress(90, "☕ JADX done — packaging...");

  const javaFiles = walkFiles(outDir).filter((f) => f.endsWith(".java"));
  if (javaFiles.length === 0) throw new Error("JADX produced no .java files — unsupported input?");
  if (javaFiles.length > MAX_FILES) throw new Error("Too many files in output — aborting");

  return javaFiles.map((f) => ({ arcname: path.relative(outDir, f), path: f }));
}

runMain(() =>
  runWorker({
    engine: "JADX",
    zipSuffix: "jadx",
    run: runJadx,
    batchExts: [".apk", ".dex", ".jar", ".class", ".zip", ".smali"],
  })
);
