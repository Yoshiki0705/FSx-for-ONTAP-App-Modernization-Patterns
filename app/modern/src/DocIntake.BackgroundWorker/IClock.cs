using System;

namespace DocIntake.BackgroundWorker
{
    public interface IClock
    {
        DateTimeOffset UtcNow { get; }
    }
}
