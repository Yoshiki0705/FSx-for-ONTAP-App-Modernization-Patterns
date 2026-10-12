using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using DocIntake.Core;
using DocIntake.Probe;
using Xunit;

namespace DocIntake.Core.Tests
{
    // ADR-3: the acl-evaluation record must tell a Windows ACL pre-check from a Linux real-I/O
    // attempt. Each test parses the fragment as JSON so key names, not substrings, are asserted.
    public class ProbeAclEvaluationTests
    {
        private const string Reason =
            "CanWrite on this platform is a real I/O attempt (ADR-3); there is no ACL pre-check";

        private sealed class StubStore : IFileStore
        {
            private readonly bool _canWrite;

            public StubStore(bool canWrite)
            {
                _canWrite = canWrite;
            }

            public bool CanWrite(string relativePath)
            {
                return _canWrite;
            }

            public IEnumerable<string> List(string relativeDir) { throw new NotSupportedException(); }
            public byte[] Read(string relativePath) { throw new NotSupportedException(); }
            public void Write(string relativePath, byte[] content) { throw new NotSupportedException(); }
            public Stream OpenExclusive(string relativePath) { throw new NotSupportedException(); }
        }

        // The Probe returns the body of an observed object that starts with ", ".
        private static JsonElement Parse(string fragment)
        {
            Assert.StartsWith(", ", fragment);
            using (var doc = JsonDocument.Parse("{" + fragment.Substring(1) + "}"))
            {
                return doc.RootElement.Clone();
            }
        }

        private static string[] Keys(JsonElement obj)
        {
            return obj.EnumerateObject().Select(p => p.Name).OrderBy(n => n, StringComparer.Ordinal).ToArray();
        }

        [Theory]
        [InlineData(true)]
        [InlineData(false)]
        public void NonWindows_SkipsPrecheckAndRecordsRealIo(bool canWrite)
        {
            var observed = Parse(Program.AclEvaluation(new StubStore(canWrite), false));

            Assert.Equal("skipped", observed.GetProperty("precheck").GetString());
            Assert.Equal(Reason, observed.GetProperty("precheck_reason").GetString());
            Assert.Equal(canWrite, observed.GetProperty("io_can_write").GetBoolean());
            Assert.False(observed.TryGetProperty("precheck_can_write", out _));
            Assert.Equal(new[] { "io_can_write", "precheck", "precheck_reason" }, Keys(observed));
        }

        [Theory]
        [InlineData(true)]
        [InlineData(false)]
        public void Windows_KeepsPrecheckCanWriteOnly(bool canWrite)
        {
            var observed = Parse(Program.AclEvaluation(new StubStore(canWrite), true));

            Assert.Equal(canWrite, observed.GetProperty("precheck_can_write").GetBoolean());
            Assert.Equal(new[] { "precheck_can_write" }, Keys(observed));
        }

        [Fact]
        public void NonWindows_RealIoOnThisHost_LeavesNoTrialFile()
        {
            if (OperatingSystem.IsWindows())
            {
                return;
            }
            var dir = Path.Combine(Path.GetTempPath(), "docintake-acl-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(dir);
            try
            {
                var observed = Parse(Program.AclEvaluation(new SmbFileStore(dir), false));

                Assert.True(observed.GetProperty("io_can_write").GetBoolean());
                Assert.Equal("skipped", observed.GetProperty("precheck").GetString());
                Assert.Empty(Directory.GetFiles(dir, ".docintake-canwrite-*", SearchOption.AllDirectories));
            }
            finally
            {
                Directory.Delete(dir, true);
            }
        }
    }
}
