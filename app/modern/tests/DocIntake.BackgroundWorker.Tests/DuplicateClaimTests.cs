using System;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class DuplicateClaimTests
    {
        [Fact]
        public async Task Two_workers_contending_for_one_item_have_one_owner_and_one_contention()
        {
            using (var temp = new TempDirectory())
            {
                var first = new FileClaimStore(temp.Path);
                var second = new FileClaimStore(temp.Path);
                using (var barrier = new Barrier(2))
                {
                    var tasks = new[]
                    {
                        Task.Run(() => { barrier.SignalAndWait(); return first.TryClaim("one.txt", "worker-1", DateTimeOffset.UtcNow, TimeSpan.FromMinutes(5)); }),
                        Task.Run(() => { barrier.SignalAndWait(); return second.TryClaim("one.txt", "worker-2", DateTimeOffset.UtcNow, TimeSpan.FromMinutes(5)); })
                    };
                    var results = await Task.WhenAll(tasks);
                    Assert.Equal(1, Count(results, ClaimStatus.Acquired));
                    Assert.Equal(1, Count(results, ClaimStatus.DuplicateContention));
                    var current = first.Read("one.txt");
                    Assert.NotNull(current);
                    Assert.Contains(current.Owner, new[] { "worker-1", "worker-2" });
                }
            }
        }

        private static int Count(ClaimResult[] results, ClaimStatus status)
        {
            var count = 0;
            foreach (var result in results) if (result.Status == status) count++;
            return count;
        }
    }
}
