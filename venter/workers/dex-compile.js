#!/usr/bin/env node
// Venter dex-compile worker — port of the old worker_dex_compile.py.
//  .smali        → smali a  → classes.dex
//  .java/.class/.jar (or zip of sources) → javac + d8 → classes.dex

import fs from "node:fs";
import path from "node:path";
import { exec, runMain, runWorker } from "./lib/run-worker.js";
import { isZip, extractZip, walkFiles } from "./lib/zip.js";

function findAndroidJar() {
  const candidates = [
    process.env.ANDROID_JAR,
    process.env.ANDROID_HOME && path.join(process.env.ANDROID_HOME, "platforms"),
    process.env.ANDROID_SDK_ROOT && path.join(process.env.ANDROID_SDK_ROOT, "platforms"),
    "/usr/local/lib/android/sdk/platforms",
  ].filter(Boolean);
  for (const base of candidates) {
    if (fs.existsSync(base) && fs.statSync(base).isDirectory()) {
      const dirs = fs.readdirSync(base).filter((d) => d.startsWith("android-")).sort().reverse();
      if (dirs.length > 0) {
        const jar = path.join(base, dirs[0], "android.jar");
        if (fs.existsSync(jar)) return jar;
      }
    }
  }
  return null;
}

async function runDexCompile(inputPath, workDir, onProgress) {
  const ext = path.extname(inputPath).toLowerCase();
  const outDex = path.join(workDir, "classes.dex");

  if (ext === ".smali") {
    await onProgress(15, "🛠️ Assembling smali → dex...");
    await exec("smali", ["a", "-o", outDex, inputPath]);
    if (!fs.existsSync(outDex)) throw new Error("smali assembler produced no classes.dex");
    return [{ arcname: "classes.dex", path: outDex }];
  }

  // Java path: gather sources
  const srcDir = path.join(workDir, "src");
  fs.mkdirSync(srcDir, { recursive: true });
  if (isZip(inputPath)) {
    await onProgress(10, "📦 Extracting sources...");
    extractZip(inputPath, srcDir);
  } else if (ext === ".jar") {
    fs.copyFileSync(inputPath, path.join(srcDir, path.basename(inputPath)));
  } else if (ext === ".class") {
    fs.copyFileSync(inputPath, path.join(srcDir, path.basename(inputPath)));
  } else {
    fs.copyFileSync(inputPath, path.join(srcDir, path.basename(inputPath)));
  }

  const javaFiles = walkFiles(srcDir).filter((f) => f.endsWith(".java"));
  const classFiles = walkFiles(srcDir).filter((f) => f.endsWith(".class"));
  const jars = walkFiles(srcDir).filter((f) => f.endsWith(".jar"));

  if (javaFiles.length > 0) {
    await onProgress(20, "☕ Compiling java...");
    await exec("javac", ["-source", "8", "-target", "8", "-d", workDir, ...javaFiles]);
    classFiles.push(...walkFiles(workDir).filter((f) => f.endsWith(".class")));
  }
  if (classFiles.length === 0 && jars.length === 0) {
    throw new Error("Java/smali sources nahi mili — .java/.class/.jar/.smali bhejo");
  }

  await onProgress(50, "☕ Building dex (d8)...");
  const lib = findAndroidJar();
  const args = ["--release", "--output", workDir];
  if (lib) args.push("--lib", lib);
  args.push(...classFiles, ...jars);
  await exec("d8", args);

  let dex = outDex;
  if (!fs.existsSync(dex)) {
    const found = walkFiles(workDir).find((f) => path.basename(f) === "classes.dex");
    if (found) dex = found;
  }
  if (!fs.existsSync(dex)) throw new Error("d8 produced no classes.dex");
  return [{ arcname: "classes.dex", path: dex }];
}

runMain(() =>
  runWorker({
    engine: "DEX-Compile",
    zipSuffix: "dex",
    run: runDexCompile,
    batchExts: [".smali", ".java", ".class", ".jar"],
  })
);
