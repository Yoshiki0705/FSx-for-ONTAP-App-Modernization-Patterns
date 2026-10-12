using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using Amazon.S3.Model;
using DocIntake.Core;
using DocIntake.Probe;
using Moq;
using Xunit;

namespace DocIntake.Core.Tests
{
    // --store s3: the five behaviors run against S3FileStore over the in-memory fake S3 client.
    // No AWS call. Each record is parsed as JSON so key names and values are asserted, not substrings.
    public class ProbeS3BehaviorTests
    {
        private const string AccessPoint = "arn:aws:s3:ap-northeast-1:123456789012:accesspoint/example-ap";
        private const string RunId = "s3-test";

        private static ProbeConfig S3Config()
        {
            return new ProbeConfig { StoreKind = "s3", Root = AccessPoint, Stage = 3, Role = "writer", RunId = RunId };
        }

        private static Dictionary<string, byte[]> SeededStore()
        {
            return new Dictionary<string, byte[]> { { "seed/docs/index.json", new byte[] { 1 } } };
        }

        private static List<JsonElement> Run(Mock<Amazon.S3.IAmazonS3> fake)
        {
            var store = new S3FileStore(fake.Object, AccessPoint, "");
            return Program.BuildBehaviors(S3Config(), store).Select(Parse).ToList();
        }

        private static JsonElement Parse(string record)
        {
            using (var doc = JsonDocument.Parse(record))
            {
                return doc.RootElement.Clone();
            }
        }

        private static JsonElement Find(List<JsonElement> records, string id)
        {
            return records.Single(r => r.GetProperty("id").GetString() == id);
        }

        [Fact]
        public void Five_behaviors_in_order_each_with_topology()
        {
            var records = Run(FakeS3.Create(SeededStore()));
            Assert.Equal(
                new[] { "case-sensitivity", "path-separator", "file-locking", "acl-evaluation", "write-visibility" },
                records.Select(r => r.GetProperty("id").GetString()).ToArray());
            foreach (var r in records)
            {
                Assert.Equal("cross-host", r.GetProperty("observed").GetProperty("topology").GetString());
            }
        }

        [Fact]
        public void CaseSensitivity_measured_when_control_resolves()
        {
            var r = Find(Run(FakeS3.Create(SeededStore())), "case-sensitivity");
            Assert.Equal("measured", r.GetProperty("outcome").GetString());
            var o = r.GetProperty("observed");
            Assert.Equal("seed/Docs/Index.json", o.GetProperty("reference").GetString());
            Assert.False(o.GetProperty("resolved").GetBoolean());
            Assert.Equal("seed/docs/index.json", o.GetProperty("real").GetString());
            Assert.True(o.GetProperty("real_resolved").GetBoolean());
        }

        [Fact]
        public void CaseSensitivity_is_error_when_control_is_absent()
        {
            // The index exists only outside seed/, so neither key resolves: nothing can be concluded.
            var store = new Dictionary<string, byte[]> { { "docs/index.json", new byte[] { 1 } } };
            var r = Find(Run(FakeS3.Create(store)), "case-sensitivity");
            Assert.Equal("error", r.GetProperty("outcome").GetString());
            Assert.Equal("PositiveControlAbsentException", r.GetProperty("error_type").GetString());
            var o = r.GetProperty("observed");
            Assert.False(o.GetProperty("resolved").GetBoolean());
            Assert.False(o.GetProperty("real_resolved").GetBoolean());
            Assert.False(string.IsNullOrEmpty(o.GetProperty("reason").GetString()));
        }

        [Fact]
        public void PathSeparator_writes_and_reads_back_the_literal_backslash_key()
        {
            var keys = SeededStore();
            var r = Find(Run(FakeS3.Create(keys)), "path-separator");
            Assert.Equal("measured", r.GetProperty("outcome").GetString());
            var o = r.GetProperty("observed");
            // After JSON parsing the key holds exactly one backslash (the wire form is escaped).
            Assert.Equal("probe/sep\\check-s3.txt", o.GetProperty("written_key").GetString());
            Assert.True(o.GetProperty("read_back").GetBoolean());
            Assert.Contains("probe/sep\\check-s3.txt", keys.Keys);
            Assert.DoesNotContain("probe/sep/check-s3.txt", keys.Keys);
        }

        [Fact]
        public void FileLocking_is_skipped_with_reason()
        {
            var r = Find(Run(FakeS3.Create(SeededStore())), "file-locking");
            Assert.Equal("skipped", r.GetProperty("outcome").GetString());
            Assert.Equal("S3 API has no exclusive open", r.GetProperty("observed").GetProperty("reason").GetString());
            Assert.Equal(JsonValueKind.Null, r.GetProperty("error_type").ValueKind);
        }

        [Fact]
        public void AclEvaluation_records_skipped_precheck_and_real_put()
        {
            var keys = SeededStore();
            var r = Find(Run(FakeS3.Create(keys)), "acl-evaluation");
            Assert.Equal("measured", r.GetProperty("outcome").GetString());
            var o = r.GetProperty("observed");
            Assert.Equal("skipped", o.GetProperty("precheck").GetString());
            Assert.Equal("S3 API has no ACL pre-check", o.GetProperty("precheck_reason").GetString());
            Assert.Equal("probe/s3-test/deny-appsvc.txt", o.GetProperty("io_key").GetString());
            Assert.True(o.GetProperty("io_put_allowed").GetBoolean());
            Assert.Contains("probe/s3-test/deny-appsvc.txt", keys.Keys);
        }

        [Fact]
        public void AclEvaluation_denied_put_is_measured_false_not_error()
        {
            var fake = FakeS3.Create(SeededStore(), new HashSet<string> { "probe/s3-test/" });
            var records = Run(fake);
            var acl = Find(records, "acl-evaluation");
            Assert.Equal("measured", acl.GetProperty("outcome").GetString());
            Assert.False(acl.GetProperty("observed").GetProperty("io_put_allowed").GetBoolean());
            // The other behaviors are unaffected by the denied prefix.
            Assert.Equal("measured", Find(records, "write-visibility").GetProperty("outcome").GetString());
            Assert.Equal("measured", Find(records, "path-separator").GetProperty("outcome").GetString());
        }

        [Fact]
        public void WriteVisibility_writes_a_marker_under_probe()
        {
            var keys = SeededStore();
            var r = Find(Run(FakeS3.Create(keys)), "write-visibility");
            Assert.Equal("measured", r.GetProperty("outcome").GetString());
            var marker = r.GetProperty("observed").GetProperty("marker").GetString();
            Assert.Matches(new Regex("^probe/vis-[0-9a-f]{32}\\.txt$"), marker);
            Assert.Contains(marker, keys.Keys);
        }

        [Fact]
        public void No_object_is_deleted()
        {
            // The Lambda role has no s3:DeleteObject; the S3 variants must not use CanWrite.
            var keys = SeededStore();
            var fake = FakeS3.Create(keys);
            Run(fake);
            fake.Verify(s => s.DeleteObjectAsync(It.IsAny<DeleteObjectRequest>(), It.IsAny<System.Threading.CancellationToken>()),
                Times.Never);
            Assert.DoesNotContain(keys.Keys, k => k.Contains(".docintake-canwrite-"));
        }

        [Fact]
        public void Filesystem_path_is_unchanged()
        {
            var dir = Path.Combine(Path.GetTempPath(), "docintake-probe-fs-" + Guid.NewGuid().ToString("N"));
            try
            {
                Directory.CreateDirectory(Path.Combine(dir, "seed", "docs"));
                File.WriteAllText(Path.Combine(dir, "seed", "docs", "index.json"), "{}");
                var config = new ProbeConfig { StoreKind = "smb", Root = dir, Stage = 0, Role = "writer", RunId = "s0-x" };
                var records = Program.BuildBehaviors(config, Program.BuildStore(config)).Select(Parse).ToList();
                Assert.Equal(5, records.Count);
                var locking = Find(records, "file-locking");
                Assert.Equal("measured", locking.GetProperty("outcome").GetString());
                Assert.True(locking.GetProperty("observed").GetProperty("exclusive_open").GetBoolean());
                if (!OperatingSystem.IsWindows())
                {
                    var acl = Find(records, "acl-evaluation").GetProperty("observed");
                    Assert.Equal("skipped", acl.GetProperty("precheck").GetString());
                    Assert.True(acl.GetProperty("io_can_write").GetBoolean());
                }
            }
            finally
            {
                if (Directory.Exists(dir))
                {
                    Directory.Delete(dir, true);
                }
            }
        }

        [Fact]
        public void BuildStore_selects_by_kind()
        {
            var dir = Path.GetTempPath();
            Assert.IsType<SmbFileStore>(Program.BuildStore(new ProbeConfig { StoreKind = "smb", Root = dir }));
            Assert.IsType<NfsFileStore>(Program.BuildStore(new ProbeConfig { StoreKind = "nfs", Root = dir }));
            Assert.Throws<NotSupportedException>(() => Program.BuildStore(new ProbeConfig { StoreKind = "bogus", Root = dir }));
            // The s3 branch builds an AmazonS3Client (needs region/credential configuration), so it is
            // not constructed here; BuildBehaviors covers the s3 behaviors over the fake.
        }
    }
}
