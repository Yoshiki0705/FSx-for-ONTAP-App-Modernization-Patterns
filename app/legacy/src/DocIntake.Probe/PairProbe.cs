using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using DocIntake.Core;

namespace DocIntake.Probe
{
    // The coordinated two-client behaviors (file-locking, write-visibility), run against another
    // host. The hosts synchronize through a channel that is NOT the volume under test, so the
    // barrier cannot absorb the visibility being measured: this process raises signals in
    // <sync-dir>\out\<name> and waits for the other host's in <sync-dir>\in\<name>, and the
    // launcher (probe-launch.ps1) bridges those directories through the artifacts S3 bucket.
    //
    // The Probe does not claim a topology. It records its own timeline; only the merge
    // (probe_merge.py) can prove cross-host, from both sides' timelines and a shared sync id.
    //
    // Volume I/O goes through IFileStore where the store offers the operation (OpenExclusive is the
    // planted FileShare.None; Write and List as the app uses them). C# 5: built by the .NET
    // Framework MSBuild that ships with Windows Server.
    public static class PairProbe
    {
        private const int WaitMs = 180000;
        private const int PollMs = 100;
        private const int VisibilityCapMs = 60000;

        public static string Run(ProbeConfig config, IFileStore store)
        {
            var started = Now();
            string observed;
            string errorType = null;
            try
            {
                var channel = new SyncChannel(config.SyncDir);
                string sync;
                if (config.PairBehavior == "file-locking" && config.Role == "holder")
                {
                    sync = LockHolder(config, store, channel);
                }
                else if (config.PairBehavior == "file-locking")
                {
                    sync = LockContender(config, store, channel);
                }
                else if (config.Role == "writer")
                {
                    sync = VisWriter(config, store, channel);
                }
                else
                {
                    sync = VisReader(config, store, channel);
                }
                observed = "{\"topology\": \"pending-merge\", \"sync\": {" + sync + ", "
                    + Pair("sync_id", config.SyncId) + ", " + Pair("role", config.Role) + "}}";
            }
            catch (Exception ex)
            {
                errorType = ex.GetType().Name;
                observed = "{\"topology\": \"pending-merge\", \"sync\": {" + Pair("sync_id", config.SyncId)
                    + ", " + Pair("role", config.Role) + ", " + Pair("message", ex.Message) + "}}";
            }
            var behavior = "{\"id\": \"" + config.PairBehavior + "\", \"started_at\": \"" + started
                + "\", \"outcome\": \"" + (errorType == null ? "measured" : "error") + "\", \"observed\": "
                + observed + ", \"error_type\": " + (errorType == null ? "null" : "\"" + errorType + "\"") + "}";
            return "{\"schema\": \"appmod-probe/1\", " + Pair("run_id", config.RunId) + ", "
                + Pair("started_at", Now()) + ", \"stage\": " + config.Stage + ", " + Pair("role", config.Role)
                + ", \"host\": {\"os\": \"windows\", " + Pair("name", Environment.MachineName)
                + ", \"runtime\": \".NET Framework 4.8 (DocIntake.Probe)\", \"ntp_offset_ms\": "
                + (config.NtpOffsetMs.HasValue
                    ? config.NtpOffsetMs.Value.ToString("0.###", CultureInfo.InvariantCulture) : "null")
                + "}, \"store\": {" + Pair("kind", config.StoreKind) + ", " + Pair("root", config.Root)
                + "}, \"behaviors\": [" + behavior + "]}";
        }

        private static string Dir(ProbeConfig config)
        {
            return "probe/" + config.RunId + "/pairs/" + config.SyncId;
        }

        private static string FullPath(ProbeConfig config, string relative)
        {
            // Same composition SmbFileStore uses, for the operations IFileStore does not offer.
            return StorePaths.Combine(config.Root, relative.Replace("/", "\\"));
        }

        private static string LockHolder(ProbeConfig config, IFileStore store, SyncChannel channel)
        {
            var rel = Dir(config) + "/lock.dat";
            store.Write(rel, new byte[] { 0 });
            string acquired;
            using (var handle = store.OpenExclusive(rel))
            {
                handle.WriteByte(1);
                handle.Flush();
                acquired = Now();
                channel.Signal("lock-acquired", "{" + Pair("at", acquired) + "}");
                channel.Wait("attempt-done");
            }
            var released = Now();
            channel.Signal("released", "{" + Pair("at", released) + "}");
            return Pair("lock", "FileShare.None (IFileStore.OpenExclusive)") + ", "
                + Pair("lock_acquired_at", acquired) + ", " + Pair("lock_released_at", released);
        }

        private static string LockContender(ProbeConfig config, IFileStore store, SyncChannel channel)
        {
            channel.Wait("lock-acquired");
            var rel = Dir(config) + "/lock.dat";
            var full = FullPath(config, rel);
            var started = Now();
            var attempts = new List<string>();
            FileStream reader = null;
            attempts.Add("\"open_read\": " + Attempt(delegate
            {
                reader = new FileStream(full, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            }));
            if (reader != null)
            {
                attempts.Add("\"read\": " + Attempt(delegate { reader.ReadByte(); }));
                reader.Dispose();
            }
            Stream exclusive = null;
            attempts.Add("\"open_exclusive\": " + Attempt(delegate { exclusive = store.OpenExclusive(rel); }));
            if (exclusive != null)
            {
                attempts.Add("\"write\": " + Attempt(delegate { exclusive.WriteByte(2); exclusive.Flush(); }));
                exclusive.Dispose();
            }
            var ended = Now();
            channel.Signal("attempt-done", "{" + Pair("at", ended) + "}");
            channel.Wait("released");
            var after = Attempt(delegate { using (store.OpenExclusive(rel)) { } });
            return Pair("attempt_started_at", started) + ", " + Pair("attempt_ended_at", ended)
                + ", \"attempts\": {" + string.Join(", ", attempts.ToArray()) + "}, \"after_release\": " + after;
        }

        private static string VisWriter(ProbeConfig config, IFileStore store, SyncChannel channel)
        {
            channel.Wait("ready");
            var started = Now();
            store.Write(Dir(config) + "/visible.txt", Encoding.UTF8.GetBytes(config.SyncId));
            var saved = Now();
            channel.Signal("saved", "{" + Pair("write_started_at", started) + ", "
                + Pair("save_completed_at", saved) + "}");
            return Pair("write_started_at", started) + ", " + Pair("save_completed_at", saved);
        }

        private static string VisReader(ProbeConfig config, IFileStore store, SyncChannel channel)
        {
            var dir = Dir(config);
            var rel = dir + "/visible.txt";
            Directory.CreateDirectory(FullPath(config, dir));
            var absentBefore = !File.Exists(FullPath(config, rel));
            var ready = Now();
            channel.Signal("ready", "{" + Pair("at", ready) + "}");
            string firstListed = null, firstRead = null, lastPoll = ready;
            var polls = 0;
            var hardStop = DateTime.UtcNow.AddMilliseconds(WaitMs + VisibilityCapMs);
            DateTime? stopAt = null;
            while (DateTime.UtcNow < hardStop)
            {
                polls++;
                lastPoll = Now();
                if (firstListed == null)
                {
                    try
                    {
                        foreach (var name in store.List(dir))
                        {
                            if (name == "visible.txt")
                            {
                                firstListed = lastPoll;
                            }
                        }
                    }
                    catch (IOException)
                    {
                    }
                }
                try
                {
                    if (Encoding.UTF8.GetString(store.Read(rel)) == config.SyncId)
                    {
                        firstRead = Now();
                        break;
                    }
                }
                catch (IOException)
                {
                }
                catch (UnauthorizedAccessException)
                {
                }
                if (!stopAt.HasValue)
                {
                    var saved = channel.Peek("saved");
                    if (saved != null)
                    {
                        var at = Field(saved, "save_completed_at");
                        stopAt = ParseUtc(at).AddMilliseconds(VisibilityCapMs);
                    }
                }
                if (stopAt.HasValue && DateTime.UtcNow >= stopAt.Value)
                {
                    break;
                }
                Thread.Sleep(PollMs);
            }
            return "\"absent_before\": " + (absentBefore ? "true" : "false") + ", " + Pair("ready_at", ready)
                + ", \"seen\": " + (firstRead != null ? "true" : "false") + ", "
                + PairOrNull("first_listed_at", firstListed) + ", " + PairOrNull("first_read_ok_at", firstRead)
                + ", " + Pair("last_poll_at", lastPoll) + ", \"polls\": " + polls
                + ", \"poll_interval_ms\": " + PollMs;
        }

        private static string Attempt(Action action)
        {
            try
            {
                action();
                return "{\"ok\": true}";
            }
            catch (Exception ex)
            {
                return "{\"ok\": false, " + Pair("error", ex.GetType().Name) + ", "
                    + Pair("hresult", "0x" + ex.HResult.ToString("X8", CultureInfo.InvariantCulture)) + "}";
            }
        }

        internal static string Now()
        {
            return DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture);
        }

        private static DateTime ParseUtc(string value)
        {
            return DateTime.Parse(value, CultureInfo.InvariantCulture,
                DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal);
        }

        private static string Field(string json, string key)
        {
            var match = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*\"([^\"]*)\"");
            if (!match.Success)
            {
                throw new FormatException("signal has no " + key);
            }
            return match.Groups[1].Value;
        }

        private static string Pair(string key, string value)
        {
            return "\"" + key + "\": \"" + Escape(value) + "\"";
        }

        private static string PairOrNull(string key, string value)
        {
            return value == null ? "\"" + key + "\": null" : Pair(key, value);
        }

        private static string Escape(string value)
        {
            var builder = new StringBuilder();
            foreach (var c in value ?? "")
            {
                if (c == '"' || c == '\\')
                {
                    builder.Append('\\').Append(c);
                }
                else if (c < ' ')
                {
                    builder.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                }
                else
                {
                    builder.Append(c);
                }
            }
            return builder.ToString();
        }

        private sealed class SyncChannel
        {
            private readonly string _out;
            private readonly string _in;

            public SyncChannel(string syncDir)
            {
                _out = Path.Combine(syncDir, "out");
                _in = Path.Combine(syncDir, "in");
                Directory.CreateDirectory(_out);
                Directory.CreateDirectory(_in);
            }

            public void Signal(string name, string json)
            {
                var tmp = Path.Combine(_out, name + ".tmp");
                File.WriteAllText(tmp, json, new UTF8Encoding(false));
                File.Move(tmp, Path.Combine(_out, name));
            }

            public string Peek(string name)
            {
                var path = Path.Combine(_in, name);
                return File.Exists(path) ? File.ReadAllText(path) : null;
            }

            public string Wait(string name)
            {
                var deadline = DateTime.UtcNow.AddMilliseconds(WaitMs);
                while (DateTime.UtcNow < deadline)
                {
                    var got = Peek(name);
                    if (got != null)
                    {
                        return got;
                    }
                    Thread.Sleep(50);
                }
                throw new TimeoutException("no '" + name + "' signal from the other host within "
                    + (WaitMs / 1000) + " s");
            }
        }
    }
}
