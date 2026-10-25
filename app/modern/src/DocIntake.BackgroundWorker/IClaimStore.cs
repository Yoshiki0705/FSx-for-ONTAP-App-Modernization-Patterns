using System;

namespace DocIntake.BackgroundWorker
{
    public interface IClaimStore
    {
        ClaimResult TryClaim(string item, string owner, DateTimeOffset now, TimeSpan lease);
        bool Renew(ClaimHandle handle, DateTimeOffset now, TimeSpan lease);
        bool IsCurrent(ClaimHandle handle, DateTimeOffset now);
        bool Release(ClaimHandle handle);
        ClaimRecord Read(string item);
    }
}
