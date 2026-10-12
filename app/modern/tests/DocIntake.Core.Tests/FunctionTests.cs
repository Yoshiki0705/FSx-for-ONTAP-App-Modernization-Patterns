using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;
using DocIntake.Lambda;
using Xunit;

namespace DocIntake.Core.Tests
{
    // The stage-3 handler over the in-memory fake S3 client. No AWS call.
    public class FunctionTests
    {
        private const string AccessPoint = "arn:aws:s3:ap-northeast-1:123456789012:accesspoint/example-ap";
        private const string Artifacts = "example-artifacts";
        private const string RunId = "s3-20261012-test";

        private static BucketFakeS3 SeededFake()
        {
            var fake = new BucketFakeS3();
            fake.Seed(AccessPoint, "seed/inbox/doc-001.txt", "first\nsecond");
            fake.Seed(AccessPoint, "seed/inbox/doc-002.txt", "alpha");
            fake.Seed(AccessPoint, "seed/docs/index.json", "{}");
            return fake;
        }

        private static Function NewFunction(BucketFakeS3 fake)
        {
            return new Function(fake.Client, AccessPoint, Artifacts);
        }

        private static Invocation Payload(string mode, string runId, string stage)
        {
            return new Invocation { Mode = mode, RunId = runId, Stage = stage };
        }

        [Fact]
        public void Worker_valid_run_returns_status_and_writes_under_out_s3()
        {
            var fake = SeededFake();
            var result = NewFunction(fake).Handler(Payload("worker", RunId, "s3"), null);

            Assert.Equal(
                new[] { "exit_code", "mode", "output_prefix", "run_id", "stage" },
                result.EnumerateObject().Select(p => p.Name).OrderBy(n => n, StringComparer.Ordinal).ToArray());
            Assert.Equal("worker", result.GetProperty("mode").GetString());
            Assert.Equal(RunId, result.GetProperty("run_id").GetString());
            Assert.Equal("s3", result.GetProperty("stage").GetString());
            Assert.Equal(0, result.GetProperty("exit_code").GetInt32());
            Assert.Equal("out/s3/", result.GetProperty("output_prefix").GetString());

            Assert.True(fake.Has(AccessPoint, "out/s3/index.json"));
            Assert.True(fake.Has(AccessPoint, "out/s3/reports/doc-001.txt.txt"));
            Assert.True(fake.Has(AccessPoint, "out/s3/reports/doc-002.txt.txt"));
            Assert.All(fake.Calls.Where(c => c.StartsWith("put ")), c => Assert.StartsWith("put " + AccessPoint + " out/s3/", c));
            Assert.Empty(fake.Keys(Artifacts));
            Assert.Equal(0, fake.CallsStarting("delete"));

            using (var index = JsonDocument.Parse(fake.Text(AccessPoint, "out/s3/index.json")))
            {
                var names = index.RootElement.GetProperty("documents").EnumerateArray()
                    .Select(d => d.GetProperty("name").GetString()).OrderBy(n => n, StringComparer.Ordinal).ToArray();
                Assert.Equal(new[] { "doc-001.txt", "doc-002.txt" }, names);
            }
        }

        [Fact]
        public void Worker_whose_pass_fails_throws()
        {
            var fake = SeededFake();
            fake.FailPut = key => key.StartsWith("out/s3/", StringComparison.Ordinal);
            Assert.Throws<InvalidOperationException>(() => NewFunction(fake).Handler(Payload("worker", RunId, "s3"), null));
        }

        [Theory]
        [InlineData("s4")]
        [InlineData("S3")]
        [InlineData("")]
        [InlineData(null)]
        public void Invalid_stage_throws_before_any_s3_call(string stage)
        {
            foreach (var mode in new[] { "worker", "probe" })
            {
                var fake = SeededFake();
                Assert.Throws<ArgumentException>(() => NewFunction(fake).Handler(Payload(mode, RunId, stage), null));
                Assert.Empty(fake.Calls);
            }
        }

        public static IEnumerable<object[]> InvalidRunIds()
        {
            foreach (var id in new string[] { null, "", "../x", "-a", ".a", "a/b", "a b", "a\\b", new string('a', 65) })
            {
                yield return new object[] { id };
            }
        }

        [Theory]
        [MemberData(nameof(InvalidRunIds))]
        public void Invalid_run_id_throws_before_any_s3_call(string runId)
        {
            foreach (var mode in new[] { "worker", "probe" })
            {
                var fake = SeededFake();
                Assert.Throws<ArgumentException>(() => NewFunction(fake).Handler(Payload(mode, runId, "s3"), null));
                Assert.Empty(fake.Calls);
            }
        }

        [Fact]
        public void Run_id_of_64_characters_is_accepted()
        {
            var fake = SeededFake();
            var id = new string('a', 64);
            var result = NewFunction(fake).Handler(Payload("worker", id, "s3"), null);
            Assert.Equal(id, result.GetProperty("run_id").GetString());
        }

        [Fact]
        public void Invalid_mode_and_null_payload_throw_before_any_s3_call()
        {
            foreach (var payload in new[] { Payload("scan", RunId, "s3"), Payload(null, RunId, "s3"), null })
            {
                var fake = SeededFake();
                Assert.Throws<ArgumentException>(() => NewFunction(fake).Handler(payload, null));
                Assert.Empty(fake.Calls);
            }
        }

        [Fact]
        public void Probe_returns_the_schema_and_stores_the_same_json_in_the_artifacts_bucket()
        {
            var fake = SeededFake();
            var result = NewFunction(fake).Handler(Payload("probe", RunId, "s3"), null);

            Assert.Equal("appmod-probe/1", result.GetProperty("schema").GetString());
            Assert.Equal(RunId, result.GetProperty("run_id").GetString());
            Assert.Equal(3, result.GetProperty("stage").GetInt32());
            Assert.Equal("s3", result.GetProperty("store").GetProperty("kind").GetString());
            Assert.Equal(AccessPoint, result.GetProperty("store").GetProperty("root").GetString());

            var behaviors = result.GetProperty("behaviors").EnumerateArray().ToList();
            Assert.Equal(
                new[] { "case-sensitivity", "path-separator", "file-locking", "acl-evaluation", "write-visibility" },
                behaviors.Select(b => b.GetProperty("id").GetString()).ToArray());
            Assert.All(behaviors, b => Assert.Contains(
                b.GetProperty("outcome").GetString(), new[] { "measured", "error", "skipped" }));
            var locking = behaviors.Single(b => b.GetProperty("id").GetString() == "file-locking");
            Assert.Equal("skipped", locking.GetProperty("outcome").GetString());
            Assert.Equal("S3 API has no exclusive open", locking.GetProperty("observed").GetProperty("reason").GetString());
            var acl = behaviors.Single(b => b.GetProperty("id").GetString() == "acl-evaluation");
            Assert.Equal("skipped", acl.GetProperty("observed").GetProperty("precheck").GetString());

            var key = Function.ProbeResultKey(RunId);
            Assert.Equal("probe/" + RunId + "/lambda-s3.json", key);
            Assert.True(fake.Has(Artifacts, key));
            Assert.False(fake.Has(AccessPoint, key));
            using (var stored = JsonDocument.Parse(fake.Text(Artifacts, key)))
            {
                Assert.Equal(result.GetRawText(), stored.RootElement.GetRawText());
            }
            Assert.Equal(0, fake.CallsStarting("delete"));
        }

        [Fact]
        public void Probe_with_the_control_absent_records_error_for_case_sensitivity_only()
        {
            var fake = new BucketFakeS3();
            fake.Seed(AccessPoint, "docs/index.json", "{}");
            var result = NewFunction(fake).Handler(Payload("probe", RunId, "s3"), null);
            var behaviors = result.GetProperty("behaviors").EnumerateArray().ToList();
            var cs = behaviors.Single(b => b.GetProperty("id").GetString() == "case-sensitivity");
            Assert.Equal("error", cs.GetProperty("outcome").GetString());
            Assert.Equal("PositiveControlAbsentException", cs.GetProperty("error_type").GetString());
            var sep = behaviors.Single(b => b.GetProperty("id").GetString() == "path-separator");
            Assert.Equal("measured", sep.GetProperty("outcome").GetString());
        }

        [Fact]
        public void Constructor_validates_its_arguments()
        {
            var fake = new BucketFakeS3();
            Assert.Throws<ArgumentNullException>(() => new Function(null, AccessPoint, Artifacts));
            Assert.Throws<ArgumentException>(() => new Function(fake.Client, "", Artifacts));
            Assert.Throws<ArgumentException>(() => new Function(fake.Client, null, Artifacts));
            Assert.Throws<ArgumentException>(() => new Function(fake.Client, AccessPoint, ""));
            Assert.Throws<ArgumentException>(() => new Function(fake.Client, AccessPoint, null));
        }

        [Fact]
        public void Environment_variable_names_match_the_template()
        {
            Assert.Equal("S3_ACCESS_POINT_ARN", Function.AccessPointVariable);
            Assert.Equal("ARTIFACTS_BUCKET", Function.ArtifactsBucketVariable);
        }
    }
}
