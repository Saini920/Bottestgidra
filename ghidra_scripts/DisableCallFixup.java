import ghidra.app.script.GhidraScript;
import ghidra.framework.options.Options;

public class DisableCallFixup extends GhidraScript {

    @Override
    public void run() throws Exception {
        try {
            ghidra.app.plugin.core.analysis.AutoAnalysisManager.getAnalysisManager(currentProgram).initializeOptions();
        } catch (Throwable t) {
            // ignore if not available
        }

        Options opts = currentProgram.getOptions("Analysis");

        String[] exactAnalyzers = {
            "Decompiler Parameter ID",
            "Decompiler Switch Analysis",
            "Call Convention ID",
            "Call-Fixup",
            "CallFixupAnalyzer",
            "Non-Returning Functions - Discovered",
            "Subroutine References"
        };

        int count = 0;
        for (String exact : exactAnalyzers) {
            try {
                opts.setBoolean(exact, false);
                println("DisableCallFixup: explicitly disabled -> " + exact);
                count++;
            } catch (Exception e) {
                // analyzer might not be registered yet
            }
        }

        String[] wildcardTargets = {
            "parameter id",
            "switch",
            "convention",
            "call-fixup",
            "callfixup",
            "non-returning",
            "subroutine"
        };

        for (String name : opts.getOptionNames()) {
            String lower = name.toLowerCase();
            for (String target : wildcardTargets) {
                if (lower.contains(target)) {
                    try {
                        opts.setBoolean(name, false);
                        println("DisableCallFixup: wildcard disabled -> " + name);
                        count++;
                    } catch (Exception e) {
                        // ignore non-boolean options
                    }
                    break;
                }
            }
        }
        println("DisableCallFixup: Total problematic analyzers disabled: " + count);
    }
}
