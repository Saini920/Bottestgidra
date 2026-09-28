import java.io.FileWriter;
import java.io.PrintWriter;
import java.util.ArrayList;
import java.util.List;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressIterator;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.DataIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.mem.Memory;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Symbol;
import ghidra.program.model.symbol.SymbolIterator;

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

        // 1. Direct Disassembly and Function Recovery in executable blocks
        println("FAST_DISASSEMBLY_START");
        int txId = currentProgram.startTransaction("DecompileAll Fast Disassembly");
        try {
            Memory memory = currentProgram.getMemory();
            AddressSet executableSet = new AddressSet();
            for (MemoryBlock block : memory.getBlocks()) {
                if (block.isExecute() && !block.isOverlay()) {
                    executableSet.add(block.getStart(), block.getEnd());
                }
            }

            // A. External Entry Points
            AddressIterator entryPoints = currentProgram.getSymbolTable().getExternalEntryPointIterator();
            while (entryPoints.hasNext()) {
                Address ep = entryPoints.next();
                if (executableSet.contains(ep)) {
                    try {
                        disassemble(ep);
                        if (getFunctionAt(ep) == null) {
                            createFunction(ep, null);
                        }
                    } catch (Exception ignored) {}
                }
            }

            // B. Exported and local symbols in executable memory
            SymbolIterator symIt = currentProgram.getSymbolTable().getAllSymbols(true);
            while (symIt.hasNext()) {
                Symbol s = symIt.next();
                Address addr = s.getAddress();
                if (executableSet.contains(addr)) {
                    try {
                        disassemble(addr);
                        if (getFunctionAt(addr) == null) {
                            createFunction(addr, s.getName());
                        }
                    } catch (Exception ignored) {}
                }
            }

            // C. Fast sweep executable blocks to partition remaining code into functions
            for (MemoryBlock block : memory.getBlocks()) {
                if (!block.isExecute() || block.isOverlay()) continue;
                Address curr = block.getStart();
                Address end = block.getEnd();
                try {
                    disassemble(curr);
                } catch (Exception ignored) {}

                while (curr != null && curr.compareTo(end) <= 0) {
                    Instruction inst = getInstructionAt(curr);
                    if (inst == null) {
                        try {
                            disassemble(curr);
                            inst = getInstructionAt(curr);
                        } catch (Exception ignored) {}
                    }
                    if (inst != null) {
                        Address iAddr = inst.getAddress();
                        Function f = getFunctionAt(iAddr);
                        if (f == null) {
                            try {
                                f = createFunction(iAddr, null);
                            } catch (Exception ignored) {}
                        }
                        if (f != null && f.getBody() != null && f.getBody().getMaxAddress() != null) {
                            curr = f.getBody().getMaxAddress().next();
                        } else {
                            curr = inst.getMaxAddress().next();
                        }
                    } else {
                        curr = curr.next();
                    }
                }
            }
        } finally {
            currentProgram.endTransaction(txId, true);
        }

        // 2. Decompile all discovered functions
        DecompInterface decomp = new DecompInterface();
        decomp.openProgram(currentProgram);

        FunctionManager fm = currentProgram.getFunctionManager();
        List<Function> funcs = new ArrayList<>();
        for (Function f : fm.getFunctions(true)) {
            funcs.add(f);
        }
        funcs.sort((a, b) -> a.getEntryPoint().compareTo(b.getEntryPoint()));

        PrintWriter out = new PrintWriter(new FileWriter(cFile));
        out.println("/*");
        out.println(" * Ghidra decompiled output");
        out.println(" * File: " + currentProgram.getName());
        out.println(" * Functions: " + funcs.size());
        out.println(" */");
        out.println("");

        int total = funcs.size();
        int done = 0;
        int lastPrinted = -1;
        int sinceReset = 0;
        println("DECOMP_PROGRESS 0/" + total);

        for (Function f : funcs) {
            if (sinceReset >= 1500) {
                decomp.dispose();
                decomp = new DecompInterface();
                decomp.openProgram(currentProgram);
                sinceReset = 0;
                System.gc();
            }
            out.println("// ---------- " + f.getName() + " @ " + f.getEntryPoint() + " ----------");
            DecompileResults res = decomp.decompileFunction(f, 45, null);
            if (res != null && res.decompileCompleted()) {
                out.println(res.getDecompiledFunction().getC());
            } else {
                String errMsg = (res != null && res.getErrorMessage() != null) ? " (" + res.getErrorMessage() + ")" : "";
                out.println("/* [FAILED] could not decompile " + f.getName() + errMsg + " */");
            }
            done++;
            sinceReset++;
            if (done % 50 == 0) {
                out.flush();
            }
            int pct = (total == 0) ? 100 : (done * 100) / total;
            if (pct != lastPrinted && pct % 5 == 0) {
                println("DECOMP_PROGRESS " + done + "/" + total);
                lastPrinted = pct;
            }
        }
        out.close();
        decomp.dispose();

        println("DONE: wrote " + cFile + " with " + funcs.size() + " functions");
    }
}
