import { useEffect, useMemo, useState } from "react";
import Editor from "@monaco-editor/react";
import { unzipSync, strFromU8 } from "fflate";

interface Props {
  blob: Blob;
  fileName: string;
}

interface TreeFile {
  path: string;
  content: string;
}

const LANG_BY_EXT: Record<string, string> = {
  ".c": "c",
  ".h": "c",
  ".cpp": "cpp",
  ".java": "java",
  ".smali": "plaintext",
  ".txt": "plaintext",
  ".xml": "xml",
  ".json": "json",
  ".js": "javascript",
  ".ts": "typescript",
  ".md": "markdown",
};

export function MonacoViewer({ blob, fileName }: Props) {
  const [files, setFiles] = useState<TreeFile[]>([]);
  const [active, setActive] = useState<string>("");
  const [error, setError] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const buf = new Uint8Array(await blob.arrayBuffer());
        const unzipped = unzipSync(buf); // fflate
        const list: TreeFile[] = [];
        for (const [name, data] of Object.entries(unzipped)) {
          if (name.endsWith("/")) continue;
          const text = strFromU8(data);
          if (text.length > 4_000_000) continue; // skip monster files
          list.push({ path: name, content: text });
        }
        list.sort((a, b) => a.path.localeCompare(b.path));
        setFiles(list);
        setActive(list[0]?.path ?? "");
      } catch (e: any) {
        setError(`ZIP extract failed: ${e?.message || e}`);
      }
    })();
  }, [blob]);

  const activeFile = useMemo(() => files.find((f) => f.path === active), [files, active]);

  if (error) return <p className="text-sm text-red-400">❌ {error}</p>;

  if (files.length === 0) {
    return (
      <p className="text-sm text-zinc-400">
        Result me koi readable text file nahi mili — ZIP download karke dekho.
      </p>
    );
  }

  const lang = (() => {
    const ext = "." + (active.split(".").pop() || "");
    return LANG_BY_EXT[ext] || "plaintext";
  })();

  return (
    <div className="grid grid-cols-[220px_1fr] gap-0 rounded-xl overflow-hidden border border-zinc-800">
      <div className="bg-zinc-900 p-2 overflow-auto max-h-[70vh]">
        <p className="px-2 py-1 text-xs text-zinc-500 font-semibold">📁 {fileName}</p>
        {files.map((f) => (
          <button
            key={f.path}
            className={`block w-full truncate rounded px-2 py-1 text-left text-xs ${
              f.path === active ? "bg-blue-600 text-white" : "text-zinc-300 hover:bg-zinc-800"
            }`}
            onClick={() => setActive(f.path)}
          >
            {f.path}
          </button>
        ))}
      </div>
      <div className="h-[70vh] bg-zinc-950">
        <Editor
          height="100%"
          language={lang}
          theme="vs-dark"
          value={activeFile?.content || ""}
          options={{ readOnly: true, minimap: { enabled: false }, fontSize: 13 }}
        />
      </div>
    </div>
  );
}
