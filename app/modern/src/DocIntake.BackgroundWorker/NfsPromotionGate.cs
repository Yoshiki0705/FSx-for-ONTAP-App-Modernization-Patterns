using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace DocIntake.BackgroundWorker
{
    public sealed class NfsEvidenceInput
    {
        public string Mode { get; set; }
        public string CandidateId { get; set; }
        public string RunId { get; set; }
        public string SourceRevision { get; set; }
        public string OntapVersion { get; set; }
        public string Region { get; set; }
        public string FsxTopology { get; set; }
        public string LinuxEc2Identity { get; set; }
        public string NfsIdentity { get; set; }
        public string SecurityStyle { get; set; }
        public string Mount { get; set; }
        public string AppDataNfsRoot { get; set; }
        public string ControlPlaneLocation { get; set; }
        public string SystemdIdentity { get; set; }
        public IList<string> RawArtifactReferences { get; set; }
        public string Postcondition { get; set; }
    }

    public sealed class NfsPromotionResult
    {
        public string Status { get; set; }
        public IList<string> MissingControls { get; set; }
    }

    public static class NfsPromotionGate
    {
        private const string CandidateId = "candidate-a-dotnet10-background-nfs";
        private const string RunPrefix = "run-a-dotnet10-background-nfs-";
        private static readonly Regex Sha256 = new Regex("^[0-9a-fA-F]{64}$", RegexOptions.CultureInvariant);
        private static readonly Regex Version = new Regex("^[0-9]+\\.[0-9]+\\.[0-9]+$", RegexOptions.CultureInvariant);
        private static readonly Regex Instance = new Regex("^instance:i-[0-9a-f]{8,17}$", RegexOptions.CultureInvariant);
        private static readonly Regex Nfs = new Regex("^nfs:[^\\s]+$", RegexOptions.CultureInvariant);
        private static readonly Regex Systemd = new Regex("^unit:[A-Za-z0-9_.@:-]+;user:[A-Za-z_][A-Za-z0-9_-]*$", RegexOptions.CultureInvariant);

        public static NfsPromotionResult Evaluate(NfsEvidenceInput input)
        {
            if (input == null) throw new ArgumentNullException("input");
            if (string.Equals(input.Mode, "local_unit", StringComparison.Ordinal))
            {
                return Result("open", new List<string> { "linux_ec2_nfs_execution" });
            }

            var missing = new List<string>();
            if (!string.Equals(input.Mode, "linux_ec2_nfs", StringComparison.Ordinal)) missing.Add("supported_mode");
            if (!string.Equals(input.CandidateId, CandidateId, StringComparison.Ordinal)) missing.Add("candidate_identity");
            if (string.IsNullOrWhiteSpace(input.RunId) || !input.RunId.StartsWith(RunPrefix, StringComparison.Ordinal)) missing.Add("run_identity");
            // source_revision is a 64-hex SHA-256 content digest of the candidate source tree, NOT a
            // 40-hex Git commit SHA-1. A commit id names a commit; it does not change when the working
            // tree is edited without committing, so it cannot attest to the exact bytes that ran. The
            // digest is derived from the per-file SHA-256 manifest of the source (see the Candidate A
            // guide, docs/{ja,en}/candidate-a-background-nfs.md). The same Sha256 pattern validates the
            // raw-artifact hashes below, so this field and those hashes share one 64-hex contract.
            if (string.IsNullOrWhiteSpace(input.SourceRevision) || !Sha256.IsMatch(input.SourceRevision)) missing.Add("source_revision");
            if (string.IsNullOrWhiteSpace(input.OntapVersion) || !Version.IsMatch(input.OntapVersion)) missing.Add("ontap_version");
            if (string.IsNullOrWhiteSpace(input.Region)) missing.Add("region");
            if (string.IsNullOrWhiteSpace(input.FsxTopology)) missing.Add("fsx_topology");
            if (string.IsNullOrWhiteSpace(input.LinuxEc2Identity) || !Instance.IsMatch(input.LinuxEc2Identity)) missing.Add("linux_ec2_identity");
            if (string.IsNullOrWhiteSpace(input.NfsIdentity) || !Nfs.IsMatch(input.NfsIdentity)) missing.Add("nfs_identity");
            if (!IsSecurityStyle(input.SecurityStyle)) missing.Add("security_style");
            if (string.IsNullOrWhiteSpace(input.Mount) || !Path.IsPathRooted(input.Mount)) missing.Add("mount");
            if (string.IsNullOrWhiteSpace(input.AppDataNfsRoot) || !Path.IsPathRooted(input.AppDataNfsRoot)) missing.Add("appdata_nfs_root");
            if (!IsExternalPath(input.ControlPlaneLocation, input.AppDataNfsRoot)) missing.Add("control_plane_location");
            if (string.IsNullOrWhiteSpace(input.SystemdIdentity) || !Systemd.IsMatch(input.SystemdIdentity)) missing.Add("systemd_identity");
            if (!ValidateArtifacts(input.RawArtifactReferences)) missing.Add("raw_artifact_references");
            if (string.IsNullOrWhiteSpace(input.Postcondition)) missing.Add("postcondition");
            // This local gate deliberately has no trusted attestation source. Caller-supplied
            // fields and local artifacts are validation inputs, not independent runtime provenance.
            missing.Add("approved_runtime_provenance");
            return Result("not-measured", missing);
        }

        private static bool IsExternalPath(string controlPlaneLocation, string appDataNfsRoot)
        {
            if (string.IsNullOrWhiteSpace(controlPlaneLocation) || string.IsNullOrWhiteSpace(appDataNfsRoot)
                || !Path.IsPathRooted(controlPlaneLocation) || !Path.IsPathRooted(appDataNfsRoot)) return false;

            var controlPath = Path.GetFullPath(controlPlaneLocation).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            var appDataPath = Path.GetFullPath(appDataNfsRoot).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            var comparison = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
            return !controlPath.Equals(appDataPath, comparison)
                && !controlPath.StartsWith(appDataPath + Path.DirectorySeparatorChar, comparison);
        }

        private static bool IsSecurityStyle(string value)
        {
            return string.Equals(value, "unix", StringComparison.Ordinal)
                || string.Equals(value, "mixed", StringComparison.Ordinal)
                || string.Equals(value, "ntfs", StringComparison.Ordinal);
        }

        private static bool ValidateArtifacts(IList<string> references)
        {
            if (references == null || references.Count == 0) return false;
            foreach (var reference in references)
            {
                if (string.IsNullOrWhiteSpace(reference)) return false;
                var separator = reference.LastIndexOf("|sha256=", StringComparison.Ordinal);
                if (separator <= 0) return false;
                var path = reference.Substring(0, separator);
                var expected = reference.Substring(separator + 8);
                if (!Sha256.IsMatch(expected) || !File.Exists(path)) return false;
                using (var stream = File.OpenRead(path))
                using (var sha = SHA256.Create())
                {
                    if (!string.Equals(Convert.ToHexString(sha.ComputeHash(stream)), expected, StringComparison.OrdinalIgnoreCase)) return false;
                }
            }
            return true;
        }

        private static NfsPromotionResult Result(string status, IList<string> missing)
        {
            return new NfsPromotionResult { Status = status, MissingControls = missing };
        }
    }
}
