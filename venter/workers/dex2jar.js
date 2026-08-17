#!/usr/bin/env node
// Venter dex2jar worker — port of the old worker_dex2jar.py.
// Converts .apk/.dex → .jar, then JADX for Java sources.

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";
import { walkFiles } from "./lib/zip.js";

const MAX_JAVA = 500;

async function runDex2Jar(inputPath, workDir, onProgress) {
  const jarPath = path.join(workDir, "output.jar");

  await onProgress(15, "🧬 Running dex2jar...");
  await exec("d2j-dex2jar.sh", ["-f", "-o", jarPath, inputPath], (line) => {
    if (/dex2jar/i.test(line)) onProgress(30, "🧬 Converting dex → jar...");
  });
  if (!fs.existsSync(jarPath) || fs.statSync(jarPath).size === 0) {
    throw new Error("dex2jar produced no jar — unsupported input?");
  }

  const outFiles = [{ arcname: path.basename(jarPath), path: jarPath }];

  // Bonus: Java sources via JADX on the jar
  try {
    const javaDir = path.join(workDir, "src");
    fs.mkdirSync(javaDir, { recursive: true });
    await onProgress(50, "☕ Extracting Java sources (JADX)...");
    await exec("jadx", ["--no-res", "-j", "4", "-d", javaDir, jarPath]);
    const javas = walkFiles(javaDir).filter((f) => f.endsWith(".java")).slice(0, MAX_JAVA);
    javas.forEach((f) => outFiles.push({ arcname: `src/${path.relative(javaDir, f)}`, path: f }));
  } catch (e) {
    console.warn("JADX java-extract skipped:", e.message);
  }

  return outFiles;
}

runMain(() =>
  runWorker({
    engine: "dex2jar",
    zipSuffix: "dex2jar",
    run: runDex2Jar,
    batchExts: [".apk", ".dex"],
  })
);
