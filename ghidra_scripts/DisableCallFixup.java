import ghidra.app.script.GhidraScript;
import ghidra.framework.options.Options;

public class DisableCallFixup extends GhidraScript {

    @Override
    public void run() throws Exception {
        Options opts = currentProgram.getOptions("Analysis");
        String[] targets = {
            "CallFixup",
            "Call-Fixup",
            "Decompiler Switch Analysis",
            "Non-Returning Functions - Discovered",
            "Subroutine References"
        };
        int count = 0;
        for (String name : opts.getOptionNames()) {
            for (String target : targets) {
                if (name.toLowerCase().contains(target.toLowerCase()) && name.endsWith(".enabled")) {
                    try {
                        opts.setBoolean(name, false);
                        println("DisableCallFixup: disabled analyzer -> " + name);
                        count++;
                    } catch (Exception e) {
                        println("DisableCallFixup: could not disable " + name + ": " + e);
                    }
                }
            }
        }
        println("DisableCallFixup: Total problematic analyzers disabled: " + count);
    }
}
