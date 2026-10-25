using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using DocIntake.BackgroundWorker;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class NfsPromotionBoundaryTests
    {
        [Fact]
        public void Local_temp_evidence_remains_open()
        {
            var result = NfsPromotionGate.Evaluate(new NfsEvidenceInput { Mode = "local_unit" });

            Assert.Equal("open", result.Status);
            Assert.Contains("linux_ec2_nfs_execution", result.MissingControls);
        }

        [Fact]
        public void Unknown_mode_and_placeholder_identity_remain_not_measured()
        {
            var result = NfsPromotionGate.Evaluate(new NfsEvidenceInput
            {
                Mode = "invented_mode",
                CandidateId = "candidate-a-dotnet10-background-nfs",
                RunId = "run-a-dotnet10-background-nfs-test",
                SourceRevision = new string('a', 64),
                OntapVersion = "9.0",
                Region = "us-east-1",
                FsxTopology = "deployment=single-az;svm=svm-a;volume=appdata",
                LinuxEc2Identity = "linux-identity",
                NfsIdentity = "nfs-root",
                SecurityStyle = "security-style",
                Mount = "mount",
                SystemdIdentity = "systemd-unit",
                RawArtifactReferences = new List<string> { "sha256:artifact" },
                Postcondition = "postcondition"
            });

            Assert.Equal("not-measured", result.Status);
            Assert.Contains("supported_mode", result.MissingControls);
            Assert.Contains("ontap_version", result.MissingControls);
            Assert.Contains("linux_ec2_identity", result.MissingControls);
            Assert.Contains("security_style", result.MissingControls);
            Assert.Contains("raw_artifact_references", result.MissingControls);
        }

        [Fact]
        public void Missing_runtime_controls_remain_not_measured()
        {
            var result = NfsPromotionGate.Evaluate(new NfsEvidenceInput
            {
                Mode = "linux_ec2_nfs",
                CandidateId = "candidate-a-dotnet10-background-nfs",
                RunId = "run-a-dotnet10-background-nfs-test",
                SourceRevision = new string('a', 64),
                OntapVersion = "9.0",
                LinuxEc2Identity = "instance:i-0123456789abcdef0",
                NfsIdentity = "nfs:/mnt/appdata",
                SecurityStyle = "unix",
                Mount = "/mnt/appdata",
                SystemdIdentity = "unit:docintake.service;user:docintake",
                RawArtifactReferences = new List<string>(),
                Postcondition = ""
            });

            Assert.Equal("not-measured", result.Status);
            Assert.Contains("raw_artifact_references", result.MissingControls);
            Assert.Contains("postcondition", result.MissingControls);
        }

        [Fact]
        public void Complete_runtime_envelope_requires_existing_hashed_artifacts()
        {
            using (var temp = new TempDirectory())
            {
                var artifact = System.IO.Path.Combine(temp.Path, "runtime.json");
                File.WriteAllText(artifact, "candidate-a runtime artifact\n");
                string hash;
                using (var sha = SHA256.Create())
                using (var stream = File.OpenRead(artifact)) hash = Convert.ToHexString(sha.ComputeHash(stream));

                var result = NfsPromotionGate.Evaluate(new NfsEvidenceInput
                {
                    Mode = "linux_ec2_nfs",
                    CandidateId = "candidate-a-dotnet10-background-nfs",
                    RunId = "run-a-dotnet10-background-nfs-test",
                    SourceRevision = new string('a', 64),
                    OntapVersion = "9.0.1",
                    Region = "us-east-1",
                    FsxTopology = "deployment=single-az;svm=svm-a;volume=appdata",
                    LinuxEc2Identity = "instance:i-0123456789abcdef0",
                    NfsIdentity = "nfs:/mnt/appdata",
                    SecurityStyle = "unix",
                    Mount = "/mnt/appdata",
                    AppDataNfsRoot = "/mnt/appdata",
                    ControlPlaneLocation = "/var/lib/docintake/control/events.jsonl",
                    SystemdIdentity = "unit:docintake.service;user:docintake",
                    RawArtifactReferences = new List<string> { artifact + "|sha256=" + hash },
                    Postcondition = "report_written:/mnt/appdata/out/candidate-a/reports/item.txt.txt"
                });

                Assert.Equal("not-measured", result.Status);
                Assert.Contains("approved_runtime_provenance", result.MissingControls);
            }
        }
    }
}
