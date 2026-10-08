using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using DocIntake.Core;

namespace DocIntake.Probe
{
    // Measures the five behaviors and prints a single JSON object (schema appmod-probe/1). The Probe
    // emits observations only; the ok/differs verdict is assigned later by compare-probe.py.
    //
    // Each behavior is measured independently: if one throws, it is recorded with outcome "error"
    // and the type of the exception, and the others still run (acceptance criterion "sample app" 2).
    // A behavior that cannot exist on this path is recorded "skipped" with a reason.
    //
    // An invalid configuration (missing key, unknown store) exits 2 before any measurement
    // (acceptance criterion "sample app" 3).
    public static class Program
    {
        public static int Main(string[] args)
        {
            ProbeConfig config;
            try
            {
                config = ProbeConfig.Parse(args);
            }
            catch (ArgumentException ex)
            {
                Console.Error.WriteLine("probe: invalid configuration: " + ex.Message);
                return 2;
            }

            var store = new SmbFileStore(config.Root);
            if (config.PairBehavior != null)
            {
                Console.WriteLine(PairProbe.Run(config, store));
                return 0;
            }
            var behaviors = new List<string>();
            behaviors.Add(Measure("case-sensitivity", config, () => CaseSensitivity(store)));
            behaviors.Add(Measure("path-separator", config, () => PathSeparator(store)));
            behaviors.Add(Measure("file-locking", config, () => FileLocking(store)));
            behaviors.Add(Measure("acl-evaluation", config, () => AclEvaluation(store)));
            behaviors.Add(Measure("write-visibility", config, () => WriteVisibility(store)));

            var json = BuildJson(config, behaviors);
            Console.WriteLine(json);
            return 0;
        }

        // Behavior body returns an "observed" JSON object body (without the surrounding braces of
        // the behavior record). A throw is captured as outcome "error".
        private static string Measure(string id, ProbeConfig config, Func<string> body)
        {
            var started = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);
            try
            {
                var observed = body();
                return "{\"id\": \"" + id + "\", \"started_at\": \"" + started
                    + "\", \"outcome\": \"measured\", \"observed\": {\"topology\": \"cross-host\""
                    + observed + "}, \"error_type\": null}";
            }
            catch (Exception ex)
            {
                return "{\"id\": \"" + id + "\", \"started_at\": \"" + started
                    + "\", \"outcome\": \"error\", \"observed\": {\"topology\": \"cross-host\"}, "
                    + "\"error_type\": \"" + ex.GetType().Name + "\"}";
            }
        }

        private static string CaseSensitivity(IFileStore store)
        {
            // Reference the index by the mixed-case name and record whether it resolves.
            var resolved = false;
            try
            {
                store.Read(StorePaths.IndexReference);
                resolved = true;
            }
            catch (FileNotFoundException)
            {
                resolved = false;
            }
            catch (DirectoryNotFoundException)
            {
                resolved = false;
            }
            return ", \"reference\": \"" + StorePaths.IndexReference.Replace("\\", "\\\\")
                + "\", \"resolved\": " + (resolved ? "true" : "false");
        }

        private static string PathSeparator(IFileStore store)
        {
            // Build a path with a backslash segment and record the file name that results.
            var relative = StorePaths.Combine("probe", "sep-check.txt");
            store.Write(relative, Encoding.UTF8.GetBytes("x"));
            return ", \"written_relative\": \"" + relative.Replace("\\", "\\\\") + "\"";
        }

        private static string FileLocking(IFileStore store)
        {
            // Hold an exclusive handle and record that it was acquired. The contender side is a
            // separate Probe process (role contender) coordinated by run-probe.sh.
            using (var handle = store.OpenExclusive("probe/lock-check.txt"))
            {
                handle.WriteByte(1);
            }
            return ", \"exclusive_open\": true";
        }

        private static string AclEvaluation(IFileStore store)
        {
            // Record the pre-check result from CanWrite. The actual I/O result is compared against it
            // by compare-probe.py across stages.
            var canWrite = store.CanWrite("probe/acl-check.txt");
            return ", \"precheck_can_write\": " + (canWrite ? "true" : "false");
        }

        private static string WriteVisibility(IFileStore store)
        {
            // Writer side: write a marker and record the write time. The reader side (another Probe)
            // records when it first sees the marker; the delay is computed off-process.
            var marker = "probe/vis-" + Guid.NewGuid().ToString("N") + ".txt";
            store.Write(marker, Encoding.UTF8.GetBytes("v"));
            return ", \"marker\": \"" + marker + "\"";
        }

        private static string BuildJson(ProbeConfig config, List<string> behaviors)
        {
            var started = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);
            var builder = new StringBuilder();
            builder.Append("{\"schema\": \"appmod-probe/1\", \"run_id\": \"").Append(config.RunId)
                .Append("\", \"started_at\": \"").Append(started)
                .Append("\", \"stage\": ").Append(config.Stage)
                .Append(", \"role\": \"").Append(config.Role)
                .Append("\", \"host\": {\"os\": \"windows\", \"runtime\": \".NET Framework 4.8 (DocIntake.Probe)\"}")
                .Append(", \"store\": {\"kind\": \"").Append(config.StoreKind).Append("\", \"root\": \"")
                .Append(config.Root.Replace("\\", "\\\\")).Append("\"}")
                .Append(", \"behaviors\": [");
            for (var i = 0; i < behaviors.Count; i++)
            {
                if (i > 0)
                {
                    builder.Append(", ");
                }
                builder.Append(behaviors[i]);
            }
            builder.Append("]}");
            return builder.ToString();
        }
    }
}
