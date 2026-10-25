using System;

namespace DocIntake.BackgroundWorker
{
    public sealed class ClaimRecord
    {
        public string ClaimToken { get; set; }
        public string Owner { get; set; }
        public string Item { get; set; }
        public DateTimeOffset AcquiredAt { get; set; }
        public DateTimeOffset LeaseExpiresAt { get; set; }
        public DateTimeOffset? LastRenewedAt { get; set; }
        public string TakeoverOfOwner { get; set; }
        public DateTimeOffset? TakeoverAt { get; set; }
    }

    public enum ClaimStatus
    {
        Acquired,
        DuplicateContention,
        StaleTakeover,
        StorageFailure
    }

    public sealed class ClaimHandle
    {
        public string Path { get; set; }
        public ClaimRecord Record { get; set; }
    }

    public sealed class ClaimResult
    {
        public ClaimStatus Status { get; set; }
        public ClaimHandle Handle { get; set; }
        public ClaimRecord ExistingRecord { get; set; }
        public string ErrorClassification { get; set; }
    }
}
