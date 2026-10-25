using System;
using System.Threading;
using System.Threading.Tasks;
using DocIntake.BackgroundWorker;
using Xunit;

namespace DocIntake.BackgroundWorker.Tests
{
    public sealed class StaleClaimTests
    {
        [Fact]
        public void Expired_claim_is_taken_over_and_old_owner_cannot_release_new_claim()
        {
            using (var temp = new TempDirectory())
            {
                var clock = new ManualClock { UtcNow = new DateTimeOffset(2026, 10, 25, 0, 0, 0, TimeSpan.Zero) };
                var store = new FileClaimStore(temp.Path);
                var original = store.TryClaim("stale.txt", "old-owner", clock.UtcNow, TimeSpan.FromMinutes(1));
                clock.UtcNow = clock.UtcNow.AddMinutes(2);
                var takeover = store.TryClaim("stale.txt", "new-owner", clock.UtcNow, TimeSpan.FromMinutes(1));

                Assert.Equal(ClaimStatus.Acquired, original.Status);
                Assert.Equal(ClaimStatus.StaleTakeover, takeover.Status);
                Assert.Equal("old-owner", takeover.ExistingRecord.Owner);
                Assert.Equal("old-owner", takeover.Handle.Record.TakeoverOfOwner);
                Assert.Equal("new-owner", store.Read("stale.txt").Owner);
                Assert.False(store.Release(original.Handle));
                Assert.NotNull(store.Read("stale.txt"));
                Assert.True(store.Release(takeover.Handle));
                Assert.Null(store.Read("stale.txt"));
            }
        }

        [Fact]
        public void Active_owner_renewal_prevents_takeover_after_original_lease_expiry()
        {
            using (var temp = new TempDirectory())
            {
                var start = new DateTimeOffset(2026, 10, 25, 0, 0, 0, TimeSpan.Zero);
                var clock = new ManualClock { UtcNow = start };
                var owner = new FileClaimStore(temp.Path);
                var successor = new FileClaimStore(temp.Path);
                var claim = owner.TryClaim("active.txt", "owner", start, TimeSpan.FromMinutes(1));

                clock.UtcNow = start.AddSeconds(30);
                Assert.True(owner.Renew(claim.Handle, clock.UtcNow, TimeSpan.FromMinutes(1)));
                clock.UtcNow = start.AddSeconds(61);
                var takeover = successor.TryClaim("active.txt", "successor", clock.UtcNow, TimeSpan.FromMinutes(1));

                Assert.Equal(ClaimStatus.DuplicateContention, takeover.Status);
                Assert.Equal("owner", successor.Read("active.txt").Owner);
                Assert.Equal(start.AddSeconds(30), claim.Handle.Record.LastRenewedAt);
                Assert.True(owner.Release(claim.Handle));
            }
        }

        [Fact]
        public async Task Concurrent_release_and_takeover_leave_the_successor_claim_intact()
        {
            using (var temp = new TempDirectory())
            {
                var clock = new DateTimeOffset(2026, 10, 25, 0, 0, 0, TimeSpan.Zero);
                var store = new FileClaimStore(temp.Path);
                var original = store.TryClaim("interleaving.txt", "old-owner", clock, TimeSpan.FromMinutes(1));
                var takeoverClock = clock.AddMinutes(2);

                var release = Task.Run(() => store.Release(original.Handle));
                var takeover = Task.Run(() => store.TryClaim("interleaving.txt", "new-owner", takeoverClock, TimeSpan.FromMinutes(1)));
                await Task.WhenAll(release, takeover);

                var current = store.Read("interleaving.txt");
                Assert.NotNull(current);
                Assert.Equal("new-owner", current.Owner);
                var takeoverResult = await takeover;
                Assert.Contains(takeoverResult.Status, new[] { ClaimStatus.Acquired, ClaimStatus.StaleTakeover });
                Assert.True(store.Release(takeoverResult.Handle));
            }
        }
    }
}
