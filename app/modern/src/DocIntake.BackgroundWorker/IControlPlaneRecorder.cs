using System;

namespace DocIntake.BackgroundWorker
{
    public sealed class ControlPlaneRecord
    {
        public string Event { get; set; }
        public DateTimeOffset OccurredAt { get; set; }
        public string Item { get; set; }
        public string ErrorClassification { get; set; }
        public string ClaimState { get; set; }
        public string Postcondition { get; set; }
        public string OutputState { get; set; }
        public string ReconciliationRule { get; set; }
        public string Detail { get; set; }
    }

    public interface IControlPlaneRecorder
    {
        void Record(ControlPlaneRecord record);
    }
}
