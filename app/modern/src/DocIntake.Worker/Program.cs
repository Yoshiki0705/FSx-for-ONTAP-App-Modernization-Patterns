using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.Json;
using DocIntake.Core;

namespace DocIntake.Worker
{
    // Reads the documents under seed/inbox and writes an index plus a per-document report under
    // out/<stage>/. No database and no authentication.
    //
    // Migration notes (DocIntake-modernized, Phase 3 Step 3):
    //
    //   AP-6 (config / MP-25): configuration moved from App.config + System.Configuration
    //   (ConfigurationManager) to environment variables: DOCINTAKE_STORE, DOCINTAKE_ROOT,
    //   DOCINTAKE_STAGE. No App.config. The ".NET configuration provider uses '__' as the hierarchy
    //   separator" trap (MP-25) does not apply to these flat keys.
    //
    //   AP-7 (JSON): the index is serialised with System.Text.Json (compact, STJ default) instead
    //   of hand-built StringBuilder. Per ADR-4 the output bytes differ from the Framework build
    //   (whitespace removed); keys, values and order are unchanged. Comparisons use JSON-equality,
    //   not byte-for-byte.
    //
    //   AP-5 (write-visibility): the Worker still reads the index back after writing it. On a
    //   single client (local temp dir, or one NFS/SMB client) this is consistent. Cross-client over
    //   NFS or an S3 Access Point it may lag (the Probe measures that, AIMF-external). The read-back
    //   is an observation, not a hard precondition: a miss logs to stderr but does not fail the run.
    public static class Program
    {
        public static int Main(string[] args)
        {
            string stage;
            IFileStore store;
            try
            {
                stage = Env("DOCINTAKE_STAGE", "s0");
                store = BuildStore();
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("worker: " + ex.GetType().Name + ": " + ex.Message);
                return 1;
            }
            return Run(store, stage);
        }

        // One pass over the inbox of the given store, writing out/<stage>/. Extracted from Main so the
        // Lambda handler can call it in-process; the console behavior and exit codes are unchanged
        // (0 on success, 1 after printing "worker: <Type>: <message>" to stderr).
        public static int Run(IFileStore store, string stage)
        {
            if (store == null)
            {
                throw new ArgumentNullException("store");
            }
            if (string.IsNullOrEmpty(stage))
            {
                throw new ArgumentException("stage is required", "stage");
            }
            try
            {
                var inbox = store.List(StorePaths.InboxDir);

                var entries = new List<IndexEntry>();
                foreach (var name in inbox)
                {
                    var content = store.Read(StorePaths.InboxDir + "/" + name);
                    var summary = Summarize(content);
                    var reportPath = "out/" + stage + "/reports/" + name + ".txt";
                    store.Write(reportPath, Encoding.UTF8.GetBytes(summary));
                    entries.Add(new IndexEntry { Name = name, Size = content.Length });
                }

                var indexPath = "out/" + stage + "/index.json";
                var indexJson = BuildIndexJson(entries);
                store.Write(indexPath, Encoding.UTF8.GetBytes(indexJson));

                // AP-5 write-visibility: read the just-written index back. An observation, not a hard
                // precondition — a miss is logged but the run still succeeds.
                var readBack = store.List("out/" + stage);
                var found = false;
                foreach (var entry in readBack)
                {
                    if (entry == "index.json")
                    {
                        found = true;
                    }
                }
                if (!found)
                {
                    Console.Error.WriteLine("worker: index.json not visible immediately after write "
                        + "(expected cross-client over NFS/S3; see Probe write-visibility)");
                }

                Console.WriteLine("worker: wrote " + entries.Count + " report(s) and index to "
                    + indexPath);
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("worker: " + ex.GetType().Name + ": " + ex.Message);
                return 1;
            }
        }

        private static string Env(string key, string fallback)
        {
            var v = Environment.GetEnvironmentVariable(key);
            return string.IsNullOrEmpty(v) ? fallback : v;
        }

        private static IFileStore BuildStore()
        {
            // AP-6: store kind and root from environment. "smb"/"local" both use the filesystem store
            // (SmbFileStore is cross-platform after AP-1). "nfs" is added in Step 5. Unknown kinds
            // throw → exit 1.
            var kind = Env("DOCINTAKE_STORE", "smb");
            switch (kind)
            {
                case "smb":
                {
                    var root = Env("DOCINTAKE_ROOT", "\\\\APPMODSVM01\\appdata");
                    return new SmbFileStore(root);
                }
                case "local":
                {
                    var root = Env("DOCINTAKE_ROOT", Directory.GetCurrentDirectory());
                    return new SmbFileStore(root);
                }
                case "nfs":
                {
                    var root = Env("DOCINTAKE_ROOT", "/mnt/appdata");
                    return new NfsFileStore(root);
                }
                case "s3":
                {
                    // AIMF-external path: a real AmazonS3Client against an FSx for ONTAP S3 Access
                    // Point. AIMF does not connect to AWS; the AIMF-internal verification uses a fake
                    // IAmazonS3 in the unit tests. DOCINTAKE_ROOT = access point alias/ARN or bucket;
                    // DOCINTAKE_S3_PREFIX = optional key prefix.
                    var bucket = Env("DOCINTAKE_ROOT", "");
                    var prefix = Env("DOCINTAKE_S3_PREFIX", "");
                    return new S3FileStore(new Amazon.S3.AmazonS3Client(), bucket, prefix);
                }
                default:
                    throw new NotSupportedException("Store '" + kind + "' is not implemented");
            }
        }

        private static string Summarize(byte[] content)
        {
            var text = Encoding.UTF8.GetString(content);
            var length = text.Length;
            var firstLine = text.Split('\n').Length > 0 ? text.Split('\n')[0] : string.Empty;
            return "length=" + length + "\nfirst-line=" + firstLine + "\n";
        }

        // AP-7: serialise with System.Text.Json (compact). Keys/values/order match the Framework
        // build; whitespace differs (ADR-4).
        private static string BuildIndexJson(List<IndexEntry> entries)
        {
            var model = new IndexDocument { Documents = entries };
            return JsonSerializer.Serialize(model, IndexJsonContext.Default.IndexDocument);
        }
    }
}
