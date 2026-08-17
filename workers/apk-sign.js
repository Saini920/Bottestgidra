#!/usr/bin/env node
// Venter APK sign worker — port of the old worker_apk_sign.py.
// Signs an APK (v1+v2) with apksigner using a throwaway generated keystore.

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";

const KS_PASS = "venter_sign_2026";

async function runApkSign(inputPath, workDir, onProgress) {
  const ks = path.join(workDir, "release.keystore");

  await onProgress(15, "🔏 Generating signing key...");
  await exec("keytool", [
    "-genkeypair", "-v",
    "-keystore", ks,
    "-alias", "venter",
    "-keyalg", "RSA",
    "-keysize", "2048",
    "-validity", "10000",
    "-storepass", KS_PASS,
    "-keypass", KS_PASS,
    "-dname", "CN=Venter, OU=RE, O=Venter, L=Mumbai, ST=MH, C=IN",
  ]);

  await onProgress(40, "🔏 Signing APK (v1+v2)...");
  const outApk = path.join(workDir, "signed.apk");
  await exec("apksigner", [
    "sign",
    "--ks", ks,
    "--ks-pass", `pass:${KS_PASS}`,
    "--key-pass", `pass:${KS_PASS}`,
    "--out", outApk,
    inputPath,
  ], (line) => {
    const m = line.match(/(\d+)%/);
    if (m) onProgress(40 + Math.min(50, Number(m[1]) * 0.5), "🔏 Signing...");
  });
  if (!fs.existsSync(outApk)) throw new Error("apksigner produced no output");

  return [{ arcname: path.basename(outApk), path: outApk }];
}

runMain(() =>
  runWorker({
    engine: "APK-Sign",
    zipSuffix: "signed",
    run: runApkSign,
  })
);
