import { ENGINE_LABELS } from "../types";

interface Props {
  filename: string;
  onPick: (engine: string) => void;
}

const APK_ENGINES = ["jadx", "dex2jar", "apktool", "apkSign", "ghidra"];
const DEX_ENGINES = ["jadx", "smali", "ghidra"];
const ZIP_ENGINES = ["ghidra", "jadx", "smali"];
const BIN_ENGINES = ["ghidra"];

function suggest(filename: string): string[] {
  const f = filename.toLowerCase();
  if (f.endsWith(".apk")) return APK_ENGINES;
  if (f.endsWith(".dex")) return DEX_ENGINES;
  if (f.endsWith(".zip")) return ZIP_ENGINES;
  if (f.endsWith(".smali")) return ["jadx", "dexCompileSmali"];
  if (f.endsWith(".pdf")) return ["pdfTxt"];
  if (f.endsWith((".c")) || f.endsWith(".cpp")) return ["ccCompile"];
  if (/\.(exe|dll|so|elf|bin|o|dylib|jar)$/.test(f)) return BIN_ENGINES;
  return ["ghidra"];
}

export function EnginePicker({ filename, onPick }: Props) {
  const engines = suggest(filename);

  return (
    <div className="space-y-2">
      <p className="text-sm text-zinc-400">
        <b>{filename}</b> ke liye engine chuno:
      </p>
      <div className="grid gap-2">
        {engines.map((e) => (
          <button
            key={e}
            className="rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-3 text-left text-sm hover:border-blue-500 hover:bg-zinc-700"
            onClick={() => onPick(e)}
          >
            {ENGINE_LABELS[e] || e}
          </button>
        ))}
      </div>
    </div>
  );
}
