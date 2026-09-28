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

        String[] disabledKeywords = {
            "constant propagation",
            "symbolic propagator",
            "decompiler parameter id",
            "parameter id",
            "decompiler switch analysis",
            "switch analysis",
            "call convention id",
            "call convention",
            "call-fixup",
            "callfixup",
            "non-returning",
            "subroutine references",
            "subroutine",
            "stack",
            "dwarf",
            "exception handling",
            "gcc exception",
            "aggressive instruction",
            "variadic function",
            "create address tables",
            "shared return calls",
            "condense filler bytes",
            "embedded media",
            "rtti",
            "class analyzer"
        };

        int count = 0;

        // 1. Dynamically search all registered analyzers in Ghidra and disable hang-prone ones
        try {
            List<Analyzer> analyzers = ClassSearcher.getInstances(Analyzer.class);
            for (Analyzer a : analyzers) {
                String aName = a.getName();
                String lower = aName.toLowerCase();
                for (String kw : disabledKeywords) {
                    if (lower.contains(kw)) {
                        try {
                            opts.setBoolean(aName, false);
                            println("DisableCallFixup: disabled analyzer -> " + aName);
                            count++;
                        } catch (Exception e) {
                            // ignore non-boolean option
                        }
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
                        count++;
                    } catch (Exception e) {
                        // ignore non-boolean option
                    }
                    break;
                }
            }
        }

        println("DisableCallFixup: Total problematic analyzers disabled: " + count);

        // 3. Propagate updated options to AutoAnalysisManager tasks
        if (mgr != null) {
            try {
                mgr.initializeOptions(opts);
                println("DisableCallFixup: AutoAnalysisManager options re-initialized successfully.");
            } catch (Throwable t) {
                println("DisableCallFixup: could not initializeOptions: " + t);
            }
        }
    }
}
