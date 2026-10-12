using System;
using System.Collections.Generic;
using System.Globalization;

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
        public string PairBehavior; // file-locking | write-visibility, or null for the 5-behavior run
        public string SyncId;      // shared with the other host for one coordinated pair
        public string SyncDir;     // local signal directory the launcher bridges to S3
        public double? NtpOffsetMs; // measured by the launcher (w32tm), recorded in host

        private static readonly HashSet<string> KnownStores =
            new HashSet<string>(new[] { "smb", "nfs", "s3" });

        private static readonly Dictionary<string, string[]> PairRoles = new Dictionary<string, string[]>
        {
            { "file-locking", new[] { "holder", "contender" } },
            { "write-visibility", new[] { "writer", "reader" } }
        };

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

            var config = new ProbeConfig
            {
                StoreKind = map["store"],
                Root = map["root"],
                Stage = stage,
                Role = map["role"],
                RunId = map["run-id"]
            };
            string value;
            if (map.TryGetValue("ntp-offset-ms", out value))
            {
                double offset;
                if (!double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out offset))
                {
                    throw new ArgumentException("--ntp-offset-ms must be a number");
                }
                config.NtpOffsetMs = offset;
            }
            // Coordinated two-client mode (file-locking / write-visibility against another host).
            if (map.TryGetValue("pair-behavior", out value))
            {
                string[] roles;
                if (!PairRoles.TryGetValue(value, out roles))
                {
                    throw new ArgumentException("unknown pair behavior: " + value);
                }
                if (Array.IndexOf(roles, config.Role) < 0)
                {
                    throw new ArgumentException("role for " + value + " must be " + string.Join(" or ", roles));
                }
                foreach (var required in new[] { "sync-id", "sync-dir" })
                {
                    if (!map.ContainsKey(required))
                    {
                        throw new ArgumentException("--" + required + " is required with --pair-behavior");
                    }
                }
                config.PairBehavior = value;
                config.SyncId = map["sync-id"];
                config.SyncDir = map["sync-dir"];
            }
            return config;
        }
    }
}
