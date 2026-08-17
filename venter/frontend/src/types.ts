export interface Settings {
  githubToken: string;
  githubRepo: string; // owner/name
  apiId: string;
  apiHash: string;
  sessionKey: string; // passphrase used to encrypt the Telegram session (also the worker SESSION_KEY secret)
}

export interface StoredSession {
  blob: string; // AES-256-GCM encrypted mtcute session string
  me: { id: number; name: string } | null;
}

export interface SavedFile {
  messageId: number;
  fileName: string;
  size: number;
  date: number;
  caption?: string;
}

export interface JobInfo {
  jobId: string;
  filename: string;
  engine: string;
  fileMessageId: number;
  startedAt: number;
}

export type ProgressEvent =
  | { type: "progress"; pct: number; label: string; bar: string }
  | { type: "final"; status: "done" | "error"; message_id?: number; filename?: string; size?: number; caption?: string; error?: string };

export const ENGINE_EVENTS: Record<string, string> = {
  ghidra: "decompile-ghidra",
  jadx: "decompile-jadx",
  dex2jar: "decompile-dex2jar",
  apktool: "decompile-apktool",
  apktoolBuild: "compile-apktool",
  smali: "decompile-smali",
  smaliExtract: "decompile-smali-extract",
  dexCompileSmali: "dex-compile-smali",
  dexCompileJava: "dex-compile-java",
  ccCompile: "cc-compile",
  apkBuild: "apk-source-build",
  apkSign: "apk-sign",
  pdfTxt: "pdf-to-txt",
};

export const ENGINE_LABELS: Record<string, string> = {
  ghidra: "⚙️ Ghidra (C Code)",
  jadx: "☕ JADX (Java Source)",
  dex2jar: "🧬 dex2jar (JAR+Java)",
  apktool: "📱 Apktool (XML/Smali)",
  apktoolBuild: "📱 Apktool Build",
  smali: "🧩 Smali Decode",
  smaliExtract: "🧩 Smali Extract (com/)",
  dexCompileSmali: "🛠️ Smali → .dex",
  dexCompileJava: "☕ Java → .dex",
  ccCompile: "⚙️ C/C++ → .so",
  apkBuild: "📦 APK Build",
  apkSign: "🔏 APK Sign",
  pdfTxt: "📄 PDF → TXT",
};
