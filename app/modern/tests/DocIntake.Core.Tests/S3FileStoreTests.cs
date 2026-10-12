using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Amazon.S3;
using Amazon.S3.Model;
using DocIntake.Core;
using Moq;
using Xunit;

namespace DocIntake.Core.Tests
{
    // Fake in-memory S3: a Dictionary<key,bytes> wired to the four IAmazonS3 methods S3FileStore
    // uses (ListObjectsV2Async, GetObjectAsync, PutObjectAsync, DeleteObjectAsync). No network, no
    // credentials. A per-key "denyWrite" set lets a test simulate AccessDenied (MP-28).
    internal static class FakeS3
    {
        public static Mock<IAmazonS3> Create(Dictionary<string, byte[]> store, HashSet<string> denyWritePrefixes = null,
            HashSet<string> denyReadPrefixes = null)
        {
            denyWritePrefixes = denyWritePrefixes ?? new HashSet<string>();
            denyReadPrefixes = denyReadPrefixes ?? new HashSet<string>();
            var mock = new Mock<IAmazonS3>(MockBehavior.Strict);

            mock.Setup(s => s.PutObjectAsync(It.IsAny<PutObjectRequest>(), It.IsAny<CancellationToken>()))
                .Returns((PutObjectRequest r, CancellationToken ct) =>
                {
                    if (denyWritePrefixes.Any(p => r.Key.StartsWith(p)))
                    {
                        throw new AmazonS3Exception("AccessDenied") { StatusCode = HttpStatusCode.Forbidden };
                    }
                    using (var ms = new MemoryStream())
                    {
                        r.InputStream.CopyTo(ms);
                        store[r.Key] = ms.ToArray();
                    }
                    return Task.FromResult(new PutObjectResponse());
                });

            mock.Setup(s => s.DeleteObjectAsync(It.IsAny<DeleteObjectRequest>(), It.IsAny<CancellationToken>()))
                .Returns((DeleteObjectRequest r, CancellationToken ct) =>
                {
                    store.Remove(r.Key);
                    return Task.FromResult(new DeleteObjectResponse());
                });

            mock.Setup(s => s.GetObjectAsync(It.IsAny<GetObjectRequest>(), It.IsAny<CancellationToken>()))
                .Returns((GetObjectRequest r, CancellationToken ct) =>
                {
                    if (denyReadPrefixes.Any(p => r.Key.StartsWith(p)))
                    {
                        throw new AmazonS3Exception("AccessDenied") { StatusCode = HttpStatusCode.Forbidden };
                    }
                    byte[] bytes;
                    if (!store.TryGetValue(r.Key, out bytes))
                    {
                        throw new AmazonS3Exception("NoSuchKey") { StatusCode = HttpStatusCode.NotFound };
                    }
                    return Task.FromResult(new GetObjectResponse
                    {
                        BucketName = r.BucketName,
                        Key = r.Key,
                        ResponseStream = new MemoryStream(bytes)
                    });
                });

            mock.Setup(s => s.ListObjectsV2Async(It.IsAny<ListObjectsV2Request>(), It.IsAny<CancellationToken>()))
                .Returns((ListObjectsV2Request r, CancellationToken ct) =>
                {
                    var prefix = r.Prefix ?? "";
                    var objs = new List<S3Object>();
                    foreach (var kv in store)
                    {
                        if (!kv.Key.StartsWith(prefix))
                        {
                            continue;
                        }
                        // Emulate Delimiter="/" : only keys with no further '/' after the prefix.
                        if (r.Delimiter == "/")
                        {
                            var rest = kv.Key.Substring(prefix.Length);
                            if (rest.Contains("/"))
                            {
                                continue;
                            }
                        }
                        objs.Add(new S3Object { Key = kv.Key });
                    }
                    return Task.FromResult(new ListObjectsV2Response { S3Objects = objs, IsTruncated = false });
                });

            return mock;
        }
    }

    public class S3FileStoreTests
    {
        private static S3FileStore NewStore(Dictionary<string, byte[]> store, string prefix = "",
            HashSet<string> deny = null)
        {
            return new S3FileStore(FakeS3.Create(store, deny).Object, "ap-alias", prefix);
        }

        [Fact]
        public void Write_then_Read_round_trips_bytes()
        {
            var store = new Dictionary<string, byte[]>();
            var s3 = NewStore(store);
            s3.Write("out/s0/index.json", Encoding.UTF8.GetBytes("{\"documents\":[]}"));
            var got = Encoding.UTF8.GetString(s3.Read("out/s0/index.json"));
            Assert.Equal("{\"documents\":[]}", got);
            Assert.True(store.ContainsKey("out/s0/index.json"));
        }

        [Fact]
        public void Read_missing_key_throws_FileNotFound()
        {
            var s3 = NewStore(new Dictionary<string, byte[]>());
            Assert.Throws<FileNotFoundException>(() => s3.Read("seed/docs/index.json"));
        }

        [Fact]
        public void List_returns_immediate_names_under_dir()
        {
            var store = new Dictionary<string, byte[]>
            {
                { "seed/inbox/doc-001.txt", new byte[] { 1 } },
                { "seed/inbox/doc-002.txt", new byte[] { 2 } },
                { "seed/other/x.txt", new byte[] { 3 } }
            };
            var s3 = NewStore(store);
            var names = s3.List("seed/inbox").OrderBy(n => n).ToList();
            Assert.Equal(new[] { "doc-001.txt", "doc-002.txt" }, names);
        }

        [Fact]
        public void Keys_are_case_sensitive_matching_s3b()
        {
            // s3b (verified): NTFS-volume keys are case-sensitive via S3 Access Points. The wrong-case
            // reference does not resolve; the lowercase real control does.
            var store = new Dictionary<string, byte[]> { { "seed/docs/index.json", new byte[] { 0 } } };
            var s3 = NewStore(store);
            Assert.Throws<FileNotFoundException>(() => s3.Read("seed/Docs/Index.json")); // wrong case
            Assert.NotNull(s3.Read("seed/docs/index.json"));                              // control
        }

        [Fact]
        public void OpenExclusive_is_not_supported_on_s3()
        {
            var s3 = NewStore(new Dictionary<string, byte[]>());
            Assert.Throws<NotSupportedException>(() => s3.OpenExclusive("probe/lock.txt"));
        }

        [Fact]
        public void CanWrite_true_when_put_succeeds_and_cleans_up_probe()
        {
            var store = new Dictionary<string, byte[]>();
            var s3 = NewStore(store);
            Assert.True(s3.CanWrite("out/s0/index.json"));
            // the probe object was deleted; no residue
            Assert.DoesNotContain(store.Keys, k => k.Contains(".docintake-canwrite-"));
        }

        [Fact]
        public void CanWrite_false_on_AccessDenied_MP28()
        {
            // Simulate the AD-joined SVM denying data ops (AccessDenied), where HeadBucket would be a
            // false positive. CanWrite must report not-writable.
            var store = new Dictionary<string, byte[]>();
            var deny = new HashSet<string> { "out/" };
            var s3 = NewStore(store, deny: deny);
            Assert.False(s3.CanWrite("out/s0/index.json"));
        }

        [Fact]
        public void Backslash_stays_inside_the_key()
        {
            // S3 keys are '/'-separated; a backslash is an ordinary character and must reach the client
            // unchanged (the Probe path-separator behavior depends on it).
            var store = new Dictionary<string, byte[]>();
            var s3 = NewStore(store);
            s3.Write("probe/sep\\check-s3.txt", new byte[] { 7 });
            Assert.True(store.ContainsKey("probe/sep\\check-s3.txt"));
            Assert.False(store.ContainsKey("probe/sep/check-s3.txt"));
            Assert.Equal(new byte[] { 7 }, s3.Read("probe/sep\\check-s3.txt"));
        }

        [Fact]
        public void Write_AccessDenied_throws_UnauthorizedAccess()
        {
            var s3 = NewStore(new Dictionary<string, byte[]>(), deny: new HashSet<string> { "probe/" });
            Assert.Throws<UnauthorizedAccessException>(() => s3.Write("probe/x.txt", new byte[] { 1 }));
        }

        [Fact]
        public void Read_AccessDenied_throws_UnauthorizedAccess()
        {
            var store = new Dictionary<string, byte[]> { { "seed/x.txt", new byte[] { 1 } } };
            var mock = FakeS3.Create(store, denyReadPrefixes: new HashSet<string> { "seed/" });
            var s3 = new S3FileStore(mock.Object, "ap-alias", "");
            Assert.Throws<UnauthorizedAccessException>(() => s3.Read("seed/x.txt"));
        }

        [Fact]
        public void Prefix_is_prepended_to_keys()
        {
            var store = new Dictionary<string, byte[]>();
            var s3 = NewStore(store, prefix: "tenant1");
            s3.Write("a.txt", new byte[] { 9 });
            Assert.True(store.ContainsKey("tenant1/a.txt"));
        }
    }
}
