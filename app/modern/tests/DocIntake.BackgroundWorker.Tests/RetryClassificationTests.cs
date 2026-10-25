using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using DocIntake.Core;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class RetryClassificationTests
    {
        [Fact]
        public async Task Missing_inbox_file_is_permanent_and_not_retryable()
        {
            using (var temp = new TempDirectory())
            {
                var processor = new DocIntakeWorkItemProcessor(new SmbFileStore(temp.Path), "candidate-a");

                var result = await processor.ProcessAsync("missing.txt", CancellationToken.None);

                Assert.False(result.Succeeded);
                Assert.False(result.Retryable);
                Assert.Equal("permanent_missing_inbox", result.ErrorClassification);
            }
        }

        [Fact]
        public async Task Generic_store_io_is_transient_and_retryable()
        {
            var processor = new DocIntakeWorkItemProcessor(new ThrowingFileStore(new IOException("temporary")), "candidate-a");

            var result = await processor.ProcessAsync("item.txt", CancellationToken.None);

            Assert.False(result.Succeeded);
            Assert.True(result.Retryable);
            Assert.Equal("transient_io", result.ErrorClassification);
        }

        private sealed class ThrowingFileStore : IFileStore
        {
            private readonly Exception _exception;

            public ThrowingFileStore(Exception exception) { _exception = exception; }

            public System.Collections.Generic.IEnumerable<string> List(string relativeDir) { throw new NotSupportedException(); }
            public byte[] Read(string relativePath) { throw _exception; }
            public void Write(string relativePath, byte[] content) { throw new NotSupportedException(); }
            public Stream OpenExclusive(string relativePath) { throw new NotSupportedException(); }
            public bool CanWrite(string relativePath) { return false; }
        }
    }
}
