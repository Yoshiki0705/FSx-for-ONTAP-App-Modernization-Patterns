using System;
using System.IO;
using System.Linq;
using System.Text.Json;
using DocIntake.Core;
using DocIntake.Probe;
using Xunit;

namespace DocIntake.Core.Tests
{
    // Worker.Program.Run and Probe.Program.RunFiveBehaviors, the in-process entries the Lambda handler
    // calls. Console behavior (exit code, stderr line) is asserted so the extraction stays a no-op.
    [Collection("Console")]
    public class WorkerRunTests
    {
        private const string Bucket = "example-ap";

        private static BucketFakeS3 Seeded()
        {
            var fake = new BucketFakeS3();
            fake.Seed(Bucket, "seed/inbox/doc-001.txt", "first\nsecond");
            fake.Seed(Bucket, "seed/inbox/doc-002.txt", "alpha");
            return fake;
        }

        [Fact]
        public void Run_returns_0_and_writes_reports_and_index()
        {
            var fake = Seeded();
            var code = DocIntake.Worker.Program.Run(new S3FileStore(fake.Client, Bucket, ""), "s3");
            Assert.Equal(0, code);
            Assert.Equal("length=12\nfirst-line=first\n", fake.Text(Bucket, "out/s3/reports/doc-001.txt.txt"));
            using (var index = JsonDocument.Parse(fake.Text(Bucket, "out/s3/index.json")))
            {
                var docs = index.RootElement.GetProperty("documents").EnumerateArray()
                    .Select(d => d.GetProperty("name").GetString() + ":" + d.GetProperty("size").GetInt32())
                    .OrderBy(n => n, StringComparer.Ordinal).ToArray();
                Assert.Equal(new[] { "doc-001.txt:12", "doc-002.txt:5" }, docs);
            }
        }

        [Fact]
        public void Run_returns_1_and_prints_the_exception_when_the_store_fails()
        {
            var fake = Seeded();
            fake.FailPut = key => true;
            var original = Console.Error;
            var captured = new StringWriter();
            Console.SetError(captured);
            try
            {
                var code = DocIntake.Worker.Program.Run(new S3FileStore(fake.Client, Bucket, ""), "s3");
                Assert.Equal(1, code);
            }
            finally
            {
                Console.SetError(original);
            }
            Assert.StartsWith("worker: AmazonS3Exception: ", captured.ToString());
        }

        [Fact]
        public void Run_rejects_a_null_store_and_an_empty_stage()
        {
            var fake = Seeded();
            Assert.Throws<ArgumentNullException>(() => DocIntake.Worker.Program.Run(null, "s3"));
            Assert.Throws<ArgumentException>(
                () => DocIntake.Worker.Program.Run(new S3FileStore(fake.Client, Bucket, ""), ""));
        }

        [Fact]
        public void Main_exits_1_for_an_unknown_store()
        {
            var original = Console.Error;
            var captured = new StringWriter();
            var previous = Environment.GetEnvironmentVariable("DOCINTAKE_STORE");
            Environment.SetEnvironmentVariable("DOCINTAKE_STORE", "bogus");
            Console.SetError(captured);
            try
            {
                Assert.Equal(1, DocIntake.Worker.Program.Main(new string[0]));
            }
            finally
            {
                Console.SetError(original);
                Environment.SetEnvironmentVariable("DOCINTAKE_STORE", previous);
            }
            Assert.StartsWith("worker: NotSupportedException: ", captured.ToString());
        }

        [Fact]
        public void RunFiveBehaviors_returns_the_envelope_for_a_filesystem_store()
        {
            var root = Path.Combine(Path.GetTempPath(), "docintake-run5-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path.Combine(root, "seed", "docs"));
            File.WriteAllText(Path.Combine(root, "seed", "docs", "index.json"), "{}");
            try
            {
                var config = new ProbeConfig { StoreKind = "smb", Root = root, Stage = 0, Role = "writer", RunId = "s0-test" };
                var json = DocIntake.Probe.Program.RunFiveBehaviors(config, new SmbFileStore(root));
                using (var doc = JsonDocument.Parse(json))
                {
                    var r = doc.RootElement;
                    Assert.Equal("appmod-probe/1", r.GetProperty("schema").GetString());
                    Assert.Equal("s0-test", r.GetProperty("run_id").GetString());
                    Assert.Equal(0, r.GetProperty("stage").GetInt32());
                    Assert.Equal("writer", r.GetProperty("role").GetString());
                    Assert.Equal("smb", r.GetProperty("store").GetProperty("kind").GetString());
                    Assert.Equal(
                        new[] { "case-sensitivity", "path-separator", "file-locking", "acl-evaluation", "write-visibility" },
                        r.GetProperty("behaviors").EnumerateArray().Select(b => b.GetProperty("id").GetString()).ToArray());
                }
            }
            finally
            {
                Directory.Delete(root, true);
            }
        }

        [Fact]
        public void RunFiveBehaviors_rejects_null_arguments()
        {
            var config = new ProbeConfig { StoreKind = "smb", Root = "x", Stage = 0, Role = "writer", RunId = "s0-test" };
            Assert.Throws<ArgumentNullException>(() => DocIntake.Probe.Program.RunFiveBehaviors(null, new SmbFileStore("x")));
            Assert.Throws<ArgumentNullException>(() => DocIntake.Probe.Program.RunFiveBehaviors(config, null));
        }
    }
}
