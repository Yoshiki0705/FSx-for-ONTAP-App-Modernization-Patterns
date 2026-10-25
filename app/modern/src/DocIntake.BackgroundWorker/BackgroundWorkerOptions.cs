using System;

namespace DocIntake.BackgroundWorker
{
    public sealed class BackgroundWorkerOptions
    {
        public string AppDataRoot { get; set; }
        public string ControlPlanePath { get; set; }
        public string Stage { get; set; }
        public string OwnerId { get; set; }
        public TimeSpan ClaimLease { get; set; }
        public int MaxAttempts { get; set; }
        public TimeSpan RetryDelay { get; set; }
        public TimeSpan PollInterval { get; set; }
        public TimeSpan ControlPlaneTimeout { get; set; }

        public void Validate()
        {
            Require(AppDataRoot, "AppDataRoot");
            Require(ControlPlanePath, "ControlPlanePath");
            Require(Stage, "Stage");
            Require(OwnerId, "OwnerId");
            if (ClaimLease <= TimeSpan.Zero) throw new ArgumentOutOfRangeException("ClaimLease");
            if (MaxAttempts < 1) throw new ArgumentOutOfRangeException("MaxAttempts");
            if (RetryDelay < TimeSpan.Zero) throw new ArgumentOutOfRangeException("RetryDelay");
            if (PollInterval < TimeSpan.Zero) throw new ArgumentOutOfRangeException("PollInterval");
            if (ControlPlaneTimeout <= TimeSpan.Zero) throw new ArgumentOutOfRangeException("ControlPlaneTimeout");
        }

        public static BackgroundWorkerOptions FromEnvironment()
        {
            var options = new BackgroundWorkerOptions
            {
                AppDataRoot = Required("DOCINTAKE_ROOT"),
                ControlPlanePath = Required("DOCINTAKE_CONTROL_PLANE_PATH"),
                Stage = Required("DOCINTAKE_STAGE"),
                OwnerId = Required("DOCINTAKE_OWNER_ID"),
                ClaimLease = TimeSpan.FromSeconds(RequiredInt("DOCINTAKE_CLAIM_LEASE_SECONDS")),
                MaxAttempts = RequiredInt("DOCINTAKE_MAX_ATTEMPTS"),
                RetryDelay = TimeSpan.FromMilliseconds(RequiredInt("DOCINTAKE_RETRY_DELAY_MS")),
                PollInterval = TimeSpan.FromMilliseconds(RequiredInt("DOCINTAKE_POLL_INTERVAL_MS")),
                ControlPlaneTimeout = TimeSpan.FromMilliseconds(RequiredInt("DOCINTAKE_CONTROL_PLANE_TIMEOUT_MS"))
            };
            options.Validate();
            return options;
        }

        private static string Required(string name)
        {
            var value = Environment.GetEnvironmentVariable(name);
            if (string.IsNullOrWhiteSpace(value)) throw new InvalidOperationException(name + " is required");
            return value;
        }

        private static int RequiredInt(string name)
        {
            int value;
            if (!int.TryParse(Required(name), out value)) throw new InvalidOperationException(name + " must be an integer");
            return value;
        }

        private static void Require(string value, string name)
        {
            if (string.IsNullOrWhiteSpace(value)) throw new ArgumentException(name + " is required", name);
        }
    }
}
