import java.util.List;

import ghidra.app.plugin.core.analysis.AutoAnalysisManager;
import ghidra.app.script.GhidraScript;
import ghidra.app.services.Analyzer;
import ghidra.framework.options.Options;
import ghidra.util.classfinder.ClassSearcher;

public class DisableCallFixup extends GhidraScript {

    @Override
    public void run() throws Exception {
        AutoAnalysisManager mgr = null;
        try {
            mgr = AutoAnalysisManager.getAnalysisManager(currentProgram);
            mgr.registerOptions();
        } catch (Throwable t) {
            println("DisableCallFixup: could not call registerOptions: " + t);
        }

        Options opts = currentProgram.getOptions("Analysis");

        // Specific analyzers that cause infinite loops, OOM, or hours of redundant decompilation during auto-analysis:
        // DO NOT disable Constant Propagation, Subroutine References, Stack, Function Start Search,
        // GCC Exception Handling, or Address Tables, as they are essential to discover functions in stripped .so binaries.
        String[] exactAnalyzers = {
            "Decompiler Parameter ID",
            "Decompiler Switch Analysis",
            "Call-Fixup",
            "CallFixupAnalyzer",
            "Non-Returning Functions - Discovered",
            "DWARF"
        };

        String[] disabledKeywords = {
            "decompiler parameter id",
            "decompiler switch analysis",
            "call-fixup",
            "callfixup",
            "non-returning",
            "dwarf"
        };

        int count = 0;

        for (String exact : exactAnalyzers) {
            try {
                opts.setBoolean(exact, false);
                setAnalysisOption(currentProgram, exact, "false");
                println("DisableCallFixup: explicitly disabled -> " + exact);
                count++;
            } catch (Exception ignored) {}
        }

        // 1. Dynamically search all registered analyzers in Ghidra and disable matching ones
        try {
            List<Analyzer> analyzers = ClassSearcher.getInstances(Analyzer.class);
            for (Analyzer a : analyzers) {
                String aName = a.getName();
                String lower = aName.toLowerCase();
                for (String kw : disabledKeywords) {
                    if (lower.contains(kw)) {
                        try {
                            opts.setBoolean(aName, false);
                            setAnalysisOption(currentProgram, aName, "false");
                            println("DisableCallFixup: disabled analyzer class -> " + aName);
                            count++;
                        } catch (Exception ignored) {}
                        break;
                    }
                }
            }
        } catch (Throwable t) {
            println("DisableCallFixup: ClassSearcher failed: " + t);
        }

        // 2. Also check any options directly registered on the program
        for (String name : opts.getOptionNames()) {
            String lower = name.toLowerCase();
            for (String kw : disabledKeywords) {
                if (lower.contains(kw)) {
                    try {
                        opts.setBoolean(name, false);
                        setAnalysisOption(currentProgram, name, "false");
                        count++;
                    } catch (Exception ignored) {}
                    break;
                }
            }
        }

        println("DisableCallFixup: Total problematic analyzers disabled: " + count);

        // 3. Propagate updated options to AutoAnalysisManager tasks
        if (mgr != null) {
            try {
                mgr.initializeOptions();
                println("DisableCallFixup: AutoAnalysisManager options re-initialized successfully.");
            } catch (Throwable t) {
                println("DisableCallFixup: could not initializeOptions: " + t);
            }
        }
    }
}
