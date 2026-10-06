using System;
using System.Collections.Generic;

namespace DocIntake.Probe
{
    // Probe configuration parsed from --key value arguments. Validation is strict: a missing
    // required key or an unknown store kind means the Probe exits 2 WITHOUT measuring (acceptance
    // criterion "sample app" 3), rather than producing a misleading result.
    public class ProbeConfig
    {
        public string StoreKind;   // smb | nfs | s3
        public string Root;        // store root (UNC path, mount, or access point)
        public int Stage;          // 0..3
        public string Role;        // writer | reader | holder | contender
        public string RunId;       // s<stage>-<UTC>

        private static readonly HashSet<string> KnownStores =
            new HashSet<string>(new[] { "smb", "nfs", "s3" });

        public static ProbeConfig Parse(string[] args)
        {
            var map = new Dictionary<string, string>();
            for (var i = 0; i + 1 < args.Length; i += 2)
            {
                if (args[i].StartsWith("--"))
                {
                    map[args[i].Substring(2)] = args[i + 1];
                }
            }

            foreach (var required in new[] { "store", "root", "stage", "role", "run-id" })
            {
                if (!map.ContainsKey(required))
                {
                    throw new ArgumentException("missing required key: --" + required);
                }
            }

            if (!KnownStores.Contains(map["store"]))
            {
                throw new ArgumentException("unknown store kind: " + map["store"]);
            }

            int stage;
            if (!int.TryParse(map["stage"], out stage) || stage < 0 || stage > 3)
            {
                throw new ArgumentException("stage must be 0..3");
            }

            return new ProbeConfig
            {
                StoreKind = map["store"],
                Root = map["root"],
                Stage = stage,
                Role = map["role"],
                RunId = map["run-id"]
            };
        }
    }
}
