#!/usr/bin/env node
// Venter apktool-build worker — port of the old worker_apktool_build.py.
// Rebuilds an APK from an apktool project (folder with apktool.yml).

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";
import { isZip, extractZip } from "./lib/zip.js";

async function runApktoolBuild(inputPath, workDir, onProgress) {
  const projDir = path.join(workDir, "project");
  fs.mkdirSync(projDir, { recursive: true });

  if (isZip(inputPath)) {
    extractZip(inputPath, projDir);
  } else {
    fs.copyFileSync(inputPath, path.join(projDir, path.basename(inputPath)));
  }
  if (!fs.existsSync(path.join(projDir, "apktool.yml"))) {
    throw new Error("Input me apktool.yml nahi mili — sahi apktool project zip bhejo");
  }

  await onProgress(20, "📦 Building APK (apktool b)...");
  const outApk = path.join(workDir, "built.apk");
  await exec("apktool", ["b", "-f", "-o", outApk, projDir], (line) => {
    const m = line.match(/(\d+)%\s*$/);
    if (m) onProgress(20 + Math.min(70, Number(m[1]) * 0.7), "📦 Building resources...");
  });
  if (!fs.existsSync(outApk)) throw new Error("apktool b failed — no APK produced");

  return [{ arcname: path.basename(outApk), path: outApk }];
}

runMain(() =>
  runWorker({
    engine: "Apktool-Build",
    zipSuffix: "apk",
    run: runApktoolBuild,
  })
);
