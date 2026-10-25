using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;

namespace DocIntake.BackgroundWorker
{
    public sealed class FileClaimStore : IClaimStore
    {
        private readonly string _claimsRoot;

        public FileClaimStore(string appDataRoot)
        {
            if (string.IsNullOrWhiteSpace(appDataRoot)) throw new ArgumentException("appDataRoot is required", "appDataRoot");
            _claimsRoot = Path.Combine(Path.GetFullPath(appDataRoot), ".claims");
            Directory.CreateDirectory(_claimsRoot);
        }

        public ClaimResult TryClaim(string item, string owner, DateTimeOffset now, TimeSpan lease)
        {
            if (string.IsNullOrWhiteSpace(item)) throw new ArgumentException("item is required", "item");
            if (string.IsNullOrWhiteSpace(owner)) throw new ArgumentException("owner is required", "owner");
            if (lease <= TimeSpan.Zero) throw new ArgumentOutOfRangeException("lease");

            var path = ClaimPath(item);
            var lockPath = LockPath(item);
            try
            {
                using (AcquireClaimLock(lockPath))
                {
                    if (!File.Exists(path))
                    {
                        var record = NewRecord(item, owner, now, lease, null, null);
                        WriteNew(path, record);
                        return new ClaimResult { Status = ClaimStatus.Acquired, Handle = Handle(path, record) };
                    }

                    var current = ReadRecord(path);
                    if (current == null)
                    {
                        return StorageFailure("claim_record_invalid");
                    }
                    if (current.LeaseExpiresAt > now)
                    {
                        return new ClaimResult { Status = ClaimStatus.DuplicateContention, ExistingRecord = current };
                    }

                    var takeover = NewRecord(item, owner, now, lease, current.Owner, now);
                    WriteReplacement(path, takeover);
                    return new ClaimResult
                    {
                        Status = ClaimStatus.StaleTakeover,
                        ExistingRecord = current,
                        Handle = Handle(path, takeover)
                    };
                }
            }
            catch (IOException ex)
            {
                return ClassifyClaimIoFailure(path, ex);
            }
            catch (UnauthorizedAccessException ex)
            {
                return StorageFailure("claim_store_access_denied:" + ex.GetType().Name);
            }
            catch (JsonException ex)
            {
                return StorageFailure("claim_record_read_failed:" + ex.GetType().Name);
            }
        }

        public bool Renew(ClaimHandle handle, DateTimeOffset now, TimeSpan lease)
        {
            if (handle == null || handle.Record == null || string.IsNullOrWhiteSpace(handle.Path) || lease <= TimeSpan.Zero) return false;
            try
            {
                using (AcquireClaimLock(LockPath(handle.Record.Item)))
                {
                    var current = ReadRecord(handle.Path);
                    if (current == null || current.ClaimToken != handle.Record.ClaimToken || current.Owner != handle.Record.Owner)
                    {
                        return false;
                    }
                    if (current.LeaseExpiresAt <= now) return false;

                    current.LeaseExpiresAt = now.Add(lease);
                    current.LastRenewedAt = now;
                    WriteReplacement(handle.Path, current);
                    handle.Record.LeaseExpiresAt = current.LeaseExpiresAt;
                    handle.Record.LastRenewedAt = now;
                    return true;
                }
            }
            catch (FileNotFoundException)
            {
                return false;
            }
            catch (DirectoryNotFoundException)
            {
                return false;
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
            catch (JsonException)
            {
                return false;
            }
        }

        public bool IsCurrent(ClaimHandle handle, DateTimeOffset now)
        {
            if (handle == null || handle.Record == null || string.IsNullOrWhiteSpace(handle.Path)) return false;
            try
            {
                using (AcquireClaimLock(LockPath(handle.Record.Item)))
                {
                    var current = ReadRecord(handle.Path);
                    return current != null
                        && current.ClaimToken == handle.Record.ClaimToken
                        && current.Owner == handle.Record.Owner
                        && current.LeaseExpiresAt > now;
                }
            }
            catch (FileNotFoundException)
            {
                return false;
            }
            catch (DirectoryNotFoundException)
            {
                return false;
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
            catch (JsonException)
            {
                return false;
            }
        }

        public ClaimRecord Read(string item)
        {
            var path = ClaimPath(item);
            if (!File.Exists(path)) return null;
            try
            {
                return ReadRecord(path);
            }
            catch (FileNotFoundException)
            {
                return null;
            }
            catch (DirectoryNotFoundException)
            {
                return null;
            }
        }

        public bool Release(ClaimHandle handle)
        {
            if (handle == null || handle.Record == null || string.IsNullOrWhiteSpace(handle.Path)) return false;
            try
            {
                using (AcquireClaimLock(LockPath(handle.Record.Item)))
                {
                    var current = ReadRecord(handle.Path);
                    if (current == null || current.ClaimToken != handle.Record.ClaimToken || current.Owner != handle.Record.Owner)
                    {
                        return false;
                    }

                    // The stable per-item lock is held through the comparison and delete. A stale
                    // owner therefore cannot delete a successor created through this store.
                    File.Delete(handle.Path);
                    return true;
                }
            }
            catch (FileNotFoundException)
            {
                return false;
            }
            catch (DirectoryNotFoundException)
            {
                return false;
            }
            catch (IOException)
            {
                return false;
            }
            catch (UnauthorizedAccessException)
            {
                return false;
            }
            catch (JsonException)
            {
                return false;
            }
        }

        private ClaimResult ClassifyClaimIoFailure(string path, IOException exception)
        {
            // An existing, readable record proves contention. An absent record or an unreadable
            // record is a storage failure and must not be reported as another worker's claim.
            if (!File.Exists(path)) return StorageFailure("claim_store_io:" + exception.GetType().Name);
            try
            {
                var current = ReadRecord(path);
                if (current == null) return StorageFailure("claim_record_read_failed");
                return new ClaimResult { Status = ClaimStatus.DuplicateContention, ExistingRecord = current };
            }
            catch (Exception readException) when (readException is IOException || readException is UnauthorizedAccessException || readException is JsonException)
            {
                return StorageFailure("claim_record_read_failed:" + readException.GetType().Name);
            }
        }

        private static ClaimResult StorageFailure(string classification)
        {
            return new ClaimResult
            {
                Status = ClaimStatus.StorageFailure,
                ErrorClassification = classification
            };
        }

        private static FileStream AcquireClaimLock(string path)
        {
            IOException last = null;
            for (var attempt = 0; attempt < 100; attempt++)
            {
                try
                {
                    return new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
                }
                catch (IOException ex)
                {
                    last = ex;
                    Thread.Sleep(1);
                }
            }
            throw last ?? new IOException("claim lock could not be acquired");
        }

        private static void WriteNew(string path, ClaimRecord record)
        {
            using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                WriteRecord(stream, record);
                stream.Flush(true);
            }
        }

        private static void WriteReplacement(string path, ClaimRecord record)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
            {
                stream.SetLength(0);
                WriteRecord(stream, record);
                stream.Flush(true);
            }
        }

        private static void WriteRecord(Stream stream, ClaimRecord record)
        {
            var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(record));
            stream.Write(bytes, 0, bytes.Length);
        }

        private static ClaimRecord ReadRecord(string path)
        {
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            {
                return ReadRecord(stream);
            }
        }

        private static ClaimRecord ReadRecord(Stream stream)
        {
            return JsonSerializer.Deserialize<ClaimRecord>(stream);
        }

        private static ClaimRecord NewRecord(string item, string owner, DateTimeOffset now, TimeSpan lease, string takeoverOf, DateTimeOffset? takeoverAt)
        {
            return new ClaimRecord
            {
                ClaimToken = Guid.NewGuid().ToString("N"),
                Owner = owner,
                Item = item,
                AcquiredAt = now,
                LeaseExpiresAt = now.Add(lease),
                TakeoverOfOwner = takeoverOf,
                TakeoverAt = takeoverAt
            };
        }

        private static ClaimHandle Handle(string path, ClaimRecord record)
        {
            return new ClaimHandle { Path = path, Record = record };
        }

        private string ClaimPath(string item)
        {
            using (var sha = SHA256.Create())
            {
                var hash = sha.ComputeHash(Encoding.UTF8.GetBytes(item));
                var name = Convert.ToHexString(hash).ToLowerInvariant() + ".json";
                return Path.Combine(_claimsRoot, name);
            }
        }

        private string LockPath(string item)
        {
            return ClaimPath(item) + ".lock";
        }
    }
}
