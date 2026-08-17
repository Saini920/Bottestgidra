#!/usr/bin/env node
// Venter APK build worker — port of the old worker_apk_build.py.
// Builds an APK from a source zip (apktool-format project with apktool.yml).

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";
import { isZip, extractZip } from "./lib/zip.js";

async function runApkBuild(inputPath, workDir, onProgress) {
  const projDir = path.join(workDir, "project");
  fs.mkdirSync(projDir, { recursive: true });

  if (isZip(inputPath)) {
    extractZip(inputPath, projDir);
  } else {
    fs.copyFileSync(inputPath, path.join(projDir, path.basename(inputPath)));
  }
  if (!fs.existsSync(path.join(projDir, "apktool.yml"))) {
    throw new Error("Source zip me apktool.yml nahi mili — apktool-format project bhejo");
  }

  await onProgress(15, "📦 Building APK...");
  const outApk = path.join(workDir, "built.apk");
  await exec("apktool", ["b", "-f", "-o", outApk, projDir]);
  if (!fs.existsSync(outApk)) throw new Error("Build failed — no APK produced");

  return [{ arcname: path.basename(outApk), path: outApk }];
}

runMain(() =>
  runWorker({
    engine: "APK-Build",
    zipSuffix: "apk",
    run: runApkBuild,
  })
);
