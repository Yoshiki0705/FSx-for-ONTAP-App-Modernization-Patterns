using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using DocIntake.Core;

namespace DocIntake.Probe
{
    // Measures the five behaviors and prints a single JSON object (schema appmod-probe/1). The Probe
    // emits observations only; the ok/differs verdict is assigned later by compare-probe.py.
    //
    // Migration notes (DocIntake-modernized, Phase 3 Step 4):
    //
    //   edc67a0 port (case-sensitivity): read the real file (positive control) and its wrong-case
    //   spelling, both under seed/. Without the control, "wrong case did not resolve" cannot be told
    //   apart from "absent"; the behavior is then recorded as error (PositiveControlAbsentException)
    //   with a reason, carrying "real"/"real_resolved".
    //
    //   host os/runtime: reported from the actual environment (was hard-coded "windows" / ".NET
    //   Framework 4.8"). The Probe now runs on Linux/.NET 10 as well.
    //
    //   --store s3 (post-AIMF, reference ceb732c): runs the S3 variants against S3FileStore.
    //   case-sensitivity keeps the positive control; path-separator writes probe/sep\check-s3.txt
    //   and reads it back; file-locking is skipped (no exclusive open in the S3 API); acl-evaluation
    //   records precheck=skipped and the real PutObject result (a 403 maps to
    //   UnauthorizedAccessException in S3FileStore); write-visibility is shared with the filesystem.
    //
    //   AP-7 is NOT applied here: the appmod-probe/1 output is a wire format compared against the
    //   s0/s1/s4c/s3b baselines, so it stays hand-built and byte-stable (adaptation-ledger AP-7).
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

            var store = BuildStore(config);
            if (config.PairBehavior != null)
            {
                Console.WriteLine(PairProbe.Run(config, store));
                return 0;
            }
            Console.WriteLine(RunFiveBehaviors(config, store));
            return 0;
        }

        // The five-behavior run as a JSON string (schema appmod-probe/1). Extracted from Main so the
        // Lambda handler can call it in-process; Main prints exactly this string.
        public static string RunFiveBehaviors(ProbeConfig config, IFileStore store)
        {
            if (config == null)
            {
                throw new ArgumentNullException("config");
            }
            if (store == null)
            {
                throw new ArgumentNullException("store");
            }
            return BuildJson(config, BuildBehaviors(config, store));
        }

        // smb and nfs keep the filesystem store (nfs is the same semantics, mounted). s3 talks to an S3
        // Access Point: --root is its ARN or alias, and the prefix is empty because the access point
        // root is the volume root.
        internal static IFileStore BuildStore(ProbeConfig config)
        {
            switch (config.StoreKind)
            {
                case "smb":
                    return new SmbFileStore(config.Root);
                case "nfs":
                    return new NfsFileStore(config.Root);
                case "s3":
                    return new S3FileStore(new Amazon.S3.AmazonS3Client(), config.Root, "");
                default:
                    throw new NotSupportedException("unsupported store kind: " + config.StoreKind);
            }
        }

        // The five behaviors in their fixed order. Filesystem stores run the original bodies; s3 runs
        // the S3 variants (same ids, same order, same record shape).
        internal static List<string> BuildBehaviors(ProbeConfig config, IFileStore store)
        {
            var behaviors = new List<string>();
            if (config.StoreKind == "s3")
            {
                behaviors.Add(Measure("case-sensitivity", config, () => CaseSensitivityS3(store)));
                behaviors.Add(Measure("path-separator", config, () => PathSeparatorS3(store)));
                behaviors.Add(Skip("file-locking", "S3 API has no exclusive open"));
                behaviors.Add(Measure("acl-evaluation", config, () => AclEvaluationS3(store, config.RunId)));
                behaviors.Add(Measure("write-visibility", config, () => WriteVisibility(store)));
                return behaviors;
            }
            behaviors.Add(Measure("case-sensitivity", config, () => CaseSensitivity(store)));
            behaviors.Add(Measure("path-separator", config, () => PathSeparator(store)));
            behaviors.Add(Measure("file-locking", config, () => FileLocking(store)));
            behaviors.Add(Measure("acl-evaluation", config, () => AclEvaluation(store)));
            behaviors.Add(Measure("write-visibility", config, () => WriteVisibility(store)));
            return behaviors;
        }

        // A behavior that cannot be exercised on this store. outcome "skipped" is not a measurement and
        // carries the reason; topology stays so the merge step can read the record.
        private static string Skip(string id, string reason)
        {
            return "{\"id\": \"" + id + "\", \"started_at\": \"" + Now()
                + "\", \"outcome\": \"skipped\", \"observed\": {\"topology\": \"cross-host\", \"reason\": \""
                + Esc(reason) + "\"}, \"error_type\": null}";
        }

        // Behavior body returns an "observed" JSON object body (without the surrounding braces of
        // the behavior record). A throw is captured as outcome "error". A failed positive control
        // (PositiveControlAbsentException) keeps what was observed and why it is not a measurement.
        private static string Measure(string id, ProbeConfig config, Func<string> body)
        {
            var started = Now();
            try
            {
                var observed = body();
                return "{\"id\": \"" + id + "\", \"started_at\": \"" + started
                    + "\", \"outcome\": \"measured\", \"observed\": {\"topology\": \"cross-host\""
                    + observed + "}, \"error_type\": null}";
            }
            catch (Exception ex)
            {
                var control = ex as PositiveControlAbsentException;
                var kept = control != null ? control.Observed : string.Empty;
                return "{\"id\": \"" + id + "\", \"started_at\": \"" + started
                    + "\", \"outcome\": \"error\", \"observed\": {\"topology\": \"cross-host\"" + kept
                    + "}, \"error_type\": \"" + ex.GetType().Name + "\"}";
            }
        }

        // Thrown when the control of a measurement is absent, so the result must not be recorded as
        // measured. Observed carries the JSON fragment (leading comma, no braces) to keep.
        internal sealed class PositiveControlAbsentException : Exception
        {
            public PositiveControlAbsentException(string observed)
                : base("positive control did not resolve")
            {
                Observed = observed;
            }

            public string Observed { get; private set; }
        }

        private static bool Resolves(IFileStore store, string relative)
        {
            try
            {
                store.Read(relative);
                return true;
            }
            catch (FileNotFoundException)
            {
                return false;
            }
            catch (DirectoryNotFoundException)
            {
                return false;
            }
        }

        private static string CaseSensitivity(IFileStore store)
        {
            // edc67a0: read the real file (positive control) and its wrong-case spelling, both under
            // seed/. Without the control, "wrong case did not resolve" cannot be told apart from
            // "absent".
            var resolved = Resolves(store, StorePaths.IndexReference);
            var realResolved = Resolves(store, StorePaths.IndexReal);
            var observed = ", \"reference\": \"" + Esc(StorePaths.IndexReference)
                + "\", \"resolved\": " + (resolved ? "true" : "false")
                + ", \"real\": \"" + Esc(StorePaths.IndexReal)
                + "\", \"real_resolved\": " + (realResolved ? "true" : "false");
            if (!realResolved)
            {
                throw new PositiveControlAbsentException(observed);
            }
            return observed;
        }

        private static string PathSeparator(IFileStore store)
        {
            // AP-1: the measurement intentionally constructs a literal-backslash relative name to see
            // whether the environment treats it as a separator or as part of the file name. This is
            // explicit here (not via StorePaths.Combine, which now emits the OS-native separator).
            var relative = "probe" + Path.DirectorySeparatorChar + "sep-check.txt";
            store.Write(relative, Encoding.UTF8.GetBytes("x"));
            return ", \"written_relative\": \"" + Esc(relative) + "\"";
        }

        private static string FileLocking(IFileStore store)
        {
            using (var handle = store.OpenExclusive("probe/lock-check.txt"))
            {
                handle.WriteByte(1);
            }
            return ", \"exclusive_open\": true";
        }

        private static string AclEvaluation(IFileStore store)
        {
            return AclEvaluation(store, OperatingSystem.IsWindows());
        }

        // Seam for tests: the platform decision is a parameter so both branches run on any host.
        //
        // ADR-3: on Windows CanWrite is an NTFS ACL pre-check; elsewhere it is a real I/O attempt.
        // The two must not share a key, or a Linux trial reads as a Windows pre-check. The Linux
        // record therefore carries precheck=skipped with a reason, and the I/O result in io_can_write.
        internal static string AclEvaluation(IFileStore store, bool isWindows)
        {
            var canWrite = store.CanWrite("probe/acl-check.txt");
            if (isWindows)
            {
                // Windows: key and shape unchanged.
                return ", \"precheck_can_write\": " + (canWrite ? "true" : "false");
            }
            return ", \"precheck\": \"skipped\""
                + ", \"precheck_reason\": \"CanWrite on this platform is a real I/O attempt (ADR-3); there is no ACL pre-check\""
                + ", \"io_can_write\": " + (canWrite ? "true" : "false");
        }

        private static string WriteVisibility(IFileStore store)
        {
            var marker = "probe/vis-" + Guid.NewGuid().ToString("N") + ".txt";
            store.Write(marker, Encoding.UTF8.GetBytes("v"));
            return ", \"marker\": \"" + marker + "\"";
        }

        // S3 variant of case-sensitivity (reference ceb732c): the wrong-case key and the real key (the
        // positive control), both under seed/ at the access point root. Without the control, "wrong
        // case did not resolve" cannot be told apart from "the key is absent", so the behavior is
        // recorded as error (PositiveControlAbsentException) with the observed fields and a reason.
        private static string CaseSensitivityS3(IFileStore store)
        {
            var resolved = Resolves(store, StorePaths.IndexReference);
            var realResolved = Resolves(store, StorePaths.IndexReal);
            var observed = ", \"reference\": \"" + Esc(StorePaths.IndexReference)
                + "\", \"resolved\": " + (resolved ? "true" : "false")
                + ", \"real\": \"" + Esc(StorePaths.IndexReal)
                + "\", \"real_resolved\": " + (realResolved ? "true" : "false");
            if (!realResolved)
            {
                throw new PositiveControlAbsentException(
                    observed + ", \"reason\": \"positive control " + StorePaths.IndexReal
                    + " did not resolve; case sensitivity cannot be concluded\"");
            }
            return observed;
        }

        // S3 variant of path-separator: a key with a literal backslash inside the file name, under
        // probe/ (not at the volume root, so the top-level listing is unchanged). S3FileStore sends
        // the key verbatim, so this records whether S3 treats the backslash as an ordinary character.
        private const string S3SeparatorKey = "probe/sep\\check-s3.txt";

        private static string PathSeparatorS3(IFileStore store)
        {
            store.Write(S3SeparatorKey, Encoding.UTF8.GetBytes("x"));
            var readBack = Resolves(store, S3SeparatorKey);
            return ", \"written_key\": \"" + Esc(S3SeparatorKey)
                + "\", \"read_back\": " + (readBack ? "true" : "false");
        }

        // S3 variant of acl-evaluation: no pre-check exists in the S3 API, so it is recorded skipped
        // and the observation is a real PutObject on the key Windows denies to appsvc. A 403 is
        // UnauthorizedAccessException from S3FileStore; any other failure stays an error. CanWrite is
        // not used: it deletes the probe object, and the Lambda role has no s3:DeleteObject.
        private static string AclEvaluationS3(IFileStore store, string runId)
        {
            var key = "probe/" + runId + "/deny-appsvc.txt";
            bool allowed;
            try
            {
                store.Write(key, Encoding.UTF8.GetBytes("a"));
                allowed = true;
            }
            catch (UnauthorizedAccessException)
            {
                allowed = false;
            }
            return ", \"precheck\": \"skipped\", \"precheck_reason\": \"S3 API has no ACL pre-check\""
                + ", \"io_key\": \"" + Esc(key) + "\", \"io_put_allowed\": " + (allowed ? "true" : "false");
        }

        private static string BuildJson(ProbeConfig config, List<string> behaviors)
        {
            var started = Now();
            var builder = new StringBuilder();
            builder.Append("{\"schema\": \"appmod-probe/1\", \"run_id\": \"").Append(config.RunId)
                .Append("\", \"started_at\": \"").Append(started)
                .Append("\", \"stage\": ").Append(config.Stage)
                .Append(", \"role\": \"").Append(config.Role)
                .Append("\", \"host\": {\"os\": \"").Append(OsLabel())
                .Append("\", \"runtime\": \"").Append(RuntimeLabel()).Append("\"}")
                .Append(", \"store\": {\"kind\": \"").Append(config.StoreKind).Append("\", \"root\": \"")
                .Append(Esc(config.Root)).Append("\"}")
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

        private static string OsLabel()
        {
            if (RuntimeInformation.IsOSPlatform(OSPlatform.Windows)) return "windows";
            if (RuntimeInformation.IsOSPlatform(OSPlatform.Linux)) return "linux";
            if (RuntimeInformation.IsOSPlatform(OSPlatform.OSX)) return "macos";
            return "unknown";
        }

        private static string RuntimeLabel()
        {
            // e.g. ".NET 10.0.11 (DocIntake.Probe)" — reflects the actual runtime (was hard-coded).
            var v = Environment.Version.ToString();
            return ".NET " + v + " (DocIntake.Probe)";
        }

        private static string Esc(string value)
        {
            return value.Replace("\\", "\\\\");
        }

        internal static string Now()
        {
            return DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture);
        }
    }
}
