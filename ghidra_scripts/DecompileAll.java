import java.io.BufferedWriter;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileOptions;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Program;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;
import ghidra.program.model.symbol.SymbolType;

public class DecompileAll extends GhidraScript {

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String cFile = args.length > 0 ? args[0] : "decompiled.c";
        String metaFile = args.length > 1 ? args[1] : "info.txt";

        PrintWriter meta = new PrintWriter(new FileWriter(metaFile));
        meta.println("=== FILE INFO ===");
        meta.println("File       : " + currentProgram.getName());
        meta.println("Language   : " + currentProgram.getLanguage().getLanguageID());
        meta.println("Compiler   : " + currentProgram.getCompilerSpec().getCompilerSpecID());
        meta.println("Processor  : " + currentProgram.getLanguage().getProcessor());
        meta.println("");

        List<String> strings = new ArrayList<>();
        DataIterator dataIt = currentProgram.getListing().getDefinedData(true);
        while (dataIt.hasNext() && strings.size() < 20000) {
            Data d = dataIt.next();
            if (d.hasStringValue() && d.getValue() instanceof String) {
                String s = (String) d.getValue();
                if (s.length() >= 4) {
                    strings.add(s);
                }
            }
        }
        meta.println("=== STRINGS (" + strings.size() + ") ===");
        for (String s : strings) {
            meta.println(s.replace("\n", "\\n").replace("\r", "\\r"));
        }
        meta.println("");

        List<String> symbols = new ArrayList<>();
        SymbolIterator it = currentProgram.getSymbolTable().getAllSymbols(true);
        while (it.hasNext() && symbols.size() < 20000) {
            Symbol s = it.next();
            symbols.add(s.getAddress() + "  " + s.getName());
        }
        meta.println("=== SYMBOLS (" + symbols.size() + ") ===");
        for (String s : symbols) {
            meta.println(s);
        }
        meta.close();

        // 1. Ensure all discovered functions and any unmapped entry points / symbols are registered
        FunctionManager fm = currentProgram.getFunctionManager();
        int initialCount = fm.getFunctionCount();
        println("DecompileAll: Functions found by auto-analysis: " + initialCount);

        int txId = currentProgram.startTransaction("DecompileAll Ensure Functions");
        try {
            // A. External Entry Points
            AddressIterator entryPoints = currentProgram.getSymbolTable().getExternalEntryPointIterator();
            while (entryPoints.hasNext()) {
                Address ep = entryPoints.next();
                if (fm.getFunctionAt(ep) == null && fm.getFunctionContaining(ep) == null) {
                    try {
                        createFunction(ep, null);
                    } catch (Exception ignored) {}
                }
            }

            // B. Exported and local function/label symbols in executable memory
            SymbolIterator symIt = currentProgram.getSymbolTable().getAllSymbols(true);
            while (symIt.hasNext()) {
                Symbol s = symIt.next();
                Address addr = s.getAddress();
                MemoryBlock block = currentProgram.getMemory().getBlock(addr);
                if (block != null && block.isExecute() && !block.isOverlay()) {
                    if (s.getSymbolType() == SymbolType.FUNCTION || s.getSymbolType() == SymbolType.LABEL) {
                        if (fm.getFunctionAt(addr) == null && fm.getFunctionContaining(addr) == null) {
                            try {
                                createFunction(addr, s.getName());
                            } catch (Exception ignored) {}
                        }
                    }
                }
            }
        } finally {
            currentProgram.endTransaction(txId, true);
        }

        List<Function> funcs = new ArrayList<>();
        for (Function f : fm.getFunctions(true)) {
            if (!f.isExternal()) {
                funcs.add(f);
            }
        }
        funcs.sort((a, b) -> a.getEntryPoint().compareTo(b.getEntryPoint()));
        final int total = funcs.size();
        println("DecompileAll: Total functions to decompile: " + total);

        if (total == 0) {
            PrintWriter out = new PrintWriter(new FileWriter(cFile));
            out.println("/* No functions found */");
            out.close();
            System.out.println("DECOMP_PROGRESS 0/0");
            System.out.flush();
            println("DONE: wrote empty " + cFile);
            return;
        }

        // Configure base decompiler options (10s per-function timeout)
        final DecompileOptions options = new DecompileOptions();
        try {
            options.grabFromProgram(currentProgram);
        } catch (Exception ignored) {}
        options.setDefaultTimeout(10);

        final Program prog = currentProgram;
        final List<Function> funcsList = funcs;
        final String[] outputs = new String[total];
        final AtomicInteger nextFuncIndex = new AtomicInteger(0);
        final AtomicInteger doneCount = new AtomicInteger(0);

        int numCores = Runtime.getRuntime().availableProcessors();
        final int numThreads = Math.max(1, Math.min(numCores, 4));
        println("DecompileAll: Spawning " + numThreads + " parallel decompiler threads (detected " + numCores + " vCPUs)");
        System.out.println("DECOMP_PROGRESS 0/" + total);
        System.out.flush();

        Thread[] workers = new Thread[numThreads];
        for (int t = 0; t < numThreads; t++) {
            workers[t] = new Thread(() -> {
                DecompInterface decomp = new DecompInterface();
                decomp.setOptions(options);
                synchronized (prog) {
                    decomp.openProgram(prog);
                }
                int sinceReset = 0;

                try {
                    int idx;
                    while ((idx = nextFuncIndex.getAndIncrement()) < total) {
                        Function f = funcsList.get(idx);

                        // Periodically reset DecompInterface to prevent native AST memory bloat
                        if (sinceReset >= 400) {
                            decomp.dispose();
                            decomp = new DecompInterface();
                            decomp.setOptions(options);
                            synchronized (prog) {
                                decomp.openProgram(prog);
                            }
                            sinceReset = 0;
                        }

                        // Fast path for PLT / thunk functions
                        if (f.isThunk()) {
                            Function thunked = f.getThunkedFunction(true);
                            String target = (thunked != null) ? thunked.getName() : "unknown";
                            outputs[idx] = "// ---------- " + f.getName() + " @ " + f.getEntryPoint() + " ----------\n"
                                         + "// [THUNK] jumps to " + target + "\n\n";
                        } else {
                            StringBuilder sb = new StringBuilder();
                            sb.append("// ---------- ").append(f.getName()).append(" @ ").append(f.getEntryPoint()).append(" ----------\n");
                            try {
                                DecompileResults res = decomp.decompileFunction(f, 10, null);
                                if (res != null && res.decompileCompleted()) {
                                    sb.append(res.getDecompiledFunction().getC()).append("\n\n");
                                } else {
                                    String errMsg = (res != null && res.getErrorMessage() != null) ? " (" + res.getErrorMessage() + ")" : "";
                                    sb.append("/* [FAILED] could not decompile ").append(f.getName()).append(errMsg).append(" */\n\n");
                                }
                            } catch (Exception ex) {
                                sb.append("/* [EXCEPTION] ").append(f.getName()).append(": ").append(ex.getMessage()).append(" */\n\n");
                                try {
                                    decomp.dispose();
                                } catch (Exception ignored) {}
                                decomp = new DecompInterface();
                                decomp.setOptions(options);
                                synchronized (prog) {
                                    decomp.openProgram(prog);
                                }
                                sinceReset = 0;
                            }
                            outputs[idx] = sb.toString();
                            sinceReset++;
                        }

                        int done = doneCount.incrementAndGet();
                        if (done % 20 == 0 || done == total) {
                            System.out.println("DECOMP_PROGRESS " + done + "/" + total);
                            System.out.flush();
                        }
                    }
                } catch (Throwable t1) {
                    System.err.println("Decompiler worker error: " + t1.getMessage());
                } finally {
                    try {
                        decomp.dispose();
                    } catch (Exception ignored) {}
                }
            }, "DecompWorker-" + t);
            workers[t].start();
        }

        // Wait for all worker threads to finish
        for (Thread worker : workers) {
            try {
                worker.join();
            } catch (InterruptedException e) {
                println("DecompileAll: Worker interrupted: " + e.getMessage());
            }
        }

        println("DecompileAll: All threads finished. Writing " + cFile + " in sorted order...");
        PrintWriter out = new PrintWriter(new BufferedWriter(new FileWriter(cFile), 65536));
        out.println("/*");
        out.println(" * Ghidra decompiled output");
        out.println(" * File: " + prog.getName());
        out.println(" * Functions: " + total);
        out.println(" */");
        out.println("");

        for (int i = 0; i < total; i++) {
            if (outputs[i] != null) {
                out.print(outputs[i]);
                outputs[i] = null; // free memory immediately
            }
        }
        out.flush();
        out.close();

        println("DONE: wrote " + cFile + " with " + doneCount.get() + " functions");
    }
}
