using System;

namespace DocIntake.BackgroundWorker
{
    public sealed class SystemClock : IClock
    {
        public DateTimeOffset UtcNow { get { return DateTimeOffset.UtcNow; } }
    }
}
