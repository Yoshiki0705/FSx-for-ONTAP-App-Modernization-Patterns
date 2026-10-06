using System;
using System.Collections.Generic;
using System.Configuration;
using System.IO;
using System.Text;
using DocIntake.Core;

namespace DocIntake.Worker
{
    // Reads the documents under seed/inbox and writes an index plus a per-document report under
    // out/<stage>/. No database and no authentication. The storage is chosen by the "Store"
    // appSetting; stage 0 uses the SMB store.
    //
    // The write-visibility pitfall is planted here, exactly once: after writing the index the Worker
    // immediately lists and reads it back assuming the write is visible with no delay. On SMB the
    // read-back succeeds; over NFS (and over an S3 Access Point) a second client may not see the new
    // file for the duration of the attribute cache. The Probe measures the cross-client delay; this
    // line is the in-app assumption that the delay is zero.
    public static class Program
    {
        public static int Main(string[] args)
        {
            try
            {
                var store = BuildStore();
                var stage = ConfigurationManager.AppSettings["Stage"] ?? "s0";
                var inbox = store.List(StorePaths.InboxDir);

                var indexEntries = new List<string>();
                foreach (var name in inbox)
                {
                    var content = store.Read(StorePaths.InboxDir + "/" + name);
                    var summary = Summarize(content);
                    var reportPath = "out/" + stage + "/reports/" + name + ".txt";
                    store.Write(reportPath, Encoding.UTF8.GetBytes(summary));
                    indexEntries.Add(name + "\t" + content.Length);
                }

                var indexPath = "out/" + stage + "/index.json";
                var indexJson = BuildIndexJson(indexEntries);
                store.Write(indexPath, Encoding.UTF8.GetBytes(indexJson));

                // write-visibility pitfall: assume the just-written index is immediately visible to
                // a fresh listing/read. True on SMB; not guaranteed cross-client over NFS or S3.
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
                    Console.Error.WriteLine("worker: index.json not visible immediately after write");
                }

                Console.WriteLine("worker: wrote " + indexEntries.Count + " report(s) and index to "
                    + indexPath);
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("worker: " + ex.GetType().Name + ": " + ex.Message);
                return 1;
            }
        }

        private static IFileStore BuildStore()
        {
            var store = ConfigurationManager.AppSettings["Store"] ?? "smb";
            if (store == "smb")
            {
                var root = ConfigurationManager.AppSettings["SmbRoot"] ?? "\\\\APPMODSVM01\\appdata";
                return new SmbFileStore(root);
            }
            // NfsFileStore and S3FileStore are added by the AIMF work plan in later stages.
            throw new NotSupportedException("Store '" + store + "' is not implemented in stage 0");
        }

        private static string Summarize(byte[] content)
        {
            var text = Encoding.UTF8.GetString(content);
            var length = text.Length;
            var firstLine = text.Split('\n').Length > 0 ? text.Split('\n')[0] : string.Empty;
            return "length=" + length + "\nfirst-line=" + firstLine + "\n";
        }

        private static string BuildIndexJson(List<string> entries)
        {
            var builder = new StringBuilder();
            builder.Append("{\"documents\": [");
            for (var i = 0; i < entries.Count; i++)
            {
                var parts = entries[i].Split('\t');
                if (i > 0)
                {
                    builder.Append(", ");
                }
                builder.Append("{\"name\": \"").Append(parts[0]).Append("\", \"size\": ")
                    .Append(parts[1]).Append("}");
            }
            builder.Append("]}");
            return builder.ToString();
        }
    }
}
