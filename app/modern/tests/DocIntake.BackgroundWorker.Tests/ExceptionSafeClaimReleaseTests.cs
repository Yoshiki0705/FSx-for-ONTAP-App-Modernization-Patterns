using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class ExceptionSafeClaimReleaseTests
    {
        [Fact]
        public async Task Processor_exception_releases_claim_and_records_failure()
        {
            using (var temp = new TempDirectory())
            {
                var recorder = new RecordingControlPlaneRecorder();
                var processor = new ScriptedProcessor((item, token) => throw new InvalidOperationException("processor failed"));
                var service = TestFactory.Service(temp.Path, System.IO.Path.Combine(temp.Path, "events.jsonl"), processor, recorder);

                var result = await service.ProcessItemAsync("item.txt", CancellationToken.None);

                Assert.Equal("failed", result.Status);
                Assert.True(result.ClaimReleased);
                Assert.Equal("processor_exception:InvalidOperationException", result.ErrorClassification);
                Assert.Null(new FileClaimStore(temp.Path).Read("item.txt"));
            }
        }

        [Fact]
        public async Task Invalid_processor_result_releases_claim()
        {
            using (var temp = new TempDirectory())
            {
                var recorder = new RecordingControlPlaneRecorder();
                var processor = new ScriptedProcessor((item, token) => Task.FromResult<WorkItemResult>(null));
                var service = TestFactory.Service(temp.Path, System.IO.Path.Combine(temp.Path, "events.jsonl"), processor, recorder);

                var result = await service.ProcessItemAsync("item.txt", CancellationToken.None);

                Assert.Equal("failed", result.Status);
                Assert.True(result.ClaimReleased);
                Assert.Equal("invalid_processor_result", result.ErrorClassification);
                Assert.Null(new FileClaimStore(temp.Path).Read("item.txt"));
            }
        }

        [Fact]
        public void Claim_store_io_failure_is_not_duplicate_contention()
        {
            using (var temp = new TempDirectory())
            {
                var store = new FileClaimStore(temp.Path);
                Directory.Delete(System.IO.Path.Combine(temp.Path, ".claims"), true);

                var result = store.TryClaim("item.txt", "owner", DateTimeOffset.UtcNow, TimeSpan.FromMinutes(1));

                Assert.Equal(ClaimStatus.StorageFailure, result.Status);
                Assert.NotEqual("duplicate_contention", result.ErrorClassification);
                Assert.Contains("claim_store_io", result.ErrorClassification);
            }
        }
    }
}
