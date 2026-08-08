import ghidra.app.script.GhidraScript;
import ghidra.framework.options.Options;

public class DisableCallFixup extends GhidraScript {

    @Override
    public void run() throws Exception {
        Options opts = currentProgram.getOptions("Analysis");
        boolean found = false;
        for (String name : opts.getOptionNames()) {
            if (name.contains("CallFixupAnalyzer") && name.endsWith(".enabled")) {
                opts.setBoolean(name, false);
                found = true;
            }
        }
        println("DisableCallFixup: " +
            (found ? "disabled CallFixupAnalyzer" : "CallFixupAnalyzer option not found"));
    }
}
