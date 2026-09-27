// Windows GUI subsystem helper: no console is allocated at process creation.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;

[DataContract]
public class EnvironmentEntry {
    [DataMember] public string name;
    [DataMember] public string value;
}
[DataContract]
public class LaunchRequest {
    [DataMember] public string exe;
    [DataMember] public string args;
    [DataMember] public List<EnvironmentEntry> env;
}
[DataContract]
public class LaunchResult {
    [DataMember] public int pid;
    [DataMember] public string error;
}
public static class PackageLauncher {
    [STAThread]
    public static void Main(string[] args) {
        if (args.Length != 2) return;
        var result = new LaunchResult();
        try {
            LaunchRequest request;
            using (var input = File.OpenRead(args[0])) {
                request = (LaunchRequest)new DataContractJsonSerializer(typeof(LaunchRequest)).ReadObject(input);
            }
            var info = new ProcessStartInfo(request.exe, request.args);
            info.WorkingDirectory = Path.GetDirectoryName(request.exe);
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.EnvironmentVariables.Clear();
            foreach (var entry in request.env) info.EnvironmentVariables[entry.name] = entry.value;
            using (var child = Process.Start(info)) result.pid = child.Id;
        } catch (Exception error) {
            result.error = error.Message;
        }
        try {
            using (var output = File.Create(args[1] + ".tmp")) {
                new DataContractJsonSerializer(typeof(LaunchResult)).WriteObject(output, result);
            }
            File.Move(args[1] + ".tmp", args[1]);
        } catch { /* Caller reports a missing result instead of showing a dialog. */ }
    }
}
