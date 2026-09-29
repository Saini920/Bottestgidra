import java.io.FileWriter;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
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
        println("DecompileAll: Total functions to decompile: " + funcs.size());

        // 2. Configure decompiler with 10s timeout per function
        DecompInterface decomp = new DecompInterface();
        ghidra.app.decompiler.DecompileOptions options = new ghidra.app.decompiler.DecompileOptions();
        try {
            options.grabFromProgram(currentProgram);
        } catch (Exception ignored) {}
        options.setDefaultTimeout(10);
        decomp.setOptions(options);
        decomp.openProgram(currentProgram);

        PrintWriter out = new PrintWriter(new FileWriter(cFile));
        out.println("/*");
        out.println(" * Ghidra decompiled output");
        out.println(" * File: " + currentProgram.getName());
        out.println(" * Functions: " + funcs.size());
        out.println(" */");
        out.println("");

        int total = funcs.size();
        int done = 0;
        int sinceReset = 0;
        println("DECOMP_PROGRESS 0/" + total);

        for (Function f : funcs) {
            // Reset decompiler interface periodically to prevent memory leaks / high RSS on large binaries
            if (sinceReset >= 800) {
                decomp.dispose();
                decomp = new DecompInterface();
                decomp.setOptions(options);
                decomp.openProgram(currentProgram);
                sinceReset = 0;
                System.gc();
            }

            out.println("// ---------- " + f.getName() + " @ " + f.getEntryPoint() + " ----------");

            // Fast path for PLT / thunk functions
            if (f.isThunk()) {
                Function thunked = f.getThunkedFunction(true);
                String target = (thunked != null) ? thunked.getName() : "unknown";
                out.println("// [THUNK] jumps to " + target);
                out.println("");
                done++;
                sinceReset++;
                if (done % 20 == 0 || done == total) {
                    out.flush();
                    println("DECOMP_PROGRESS " + done + "/" + total);
                }
                continue;
            }

            DecompileResults res = decomp.decompileFunction(f, 10, null);
            if (res != null && res.decompileCompleted()) {
                out.println(res.getDecompiledFunction().getC());
            } else {
                String errMsg = (res != null && res.getErrorMessage() != null) ? " (" + res.getErrorMessage() + ")" : "";
                out.println("/* [FAILED] could not decompile " + f.getName() + errMsg + " */");
            }
            done++;
            sinceReset++;

            if (done % 20 == 0 || done == total) {
                out.flush();
                println("DECOMP_PROGRESS " + done + "/" + total);
            }
        }
        out.close();
        decomp.dispose();

        println("DONE: wrote " + cFile + " with " + done + " functions");
    }
}
