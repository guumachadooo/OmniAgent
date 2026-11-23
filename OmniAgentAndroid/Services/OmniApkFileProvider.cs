using Android.Content;
using Android.Database;
using Android.OS;
using Android.Provider;

using Path = System.IO.Path;

namespace OmniAgentAndroid.Services;

/// <summary>
/// Platform ContentProvider serving downloaded APKs safely via <c>content://</c> URIs
/// under <c>hu.sbence.cavern.omniagent.fileprovider</c>.
/// </summary>
[ContentProvider(["hu.sbence.cavern.omniagent.fileprovider"], Exported = false, GrantUriPermissions = true)]
public sealed class OmniApkFileProvider : ContentProvider {
    public const string Authority = "hu.sbence.cavern.omniagent.fileprovider";

    public static string GetApkDirectory(Context context) {
        string dir = Path.Combine(context.CacheDir.AbsolutePath, "apks");
        Directory.CreateDirectory(dir);
        return dir;
    }

    public static Android.Net.Uri GetApkUri(Context context, string fileName) {
        string safe = Path.GetFileName(fileName);
        string auth = !string.IsNullOrEmpty(context?.PackageName) ? $"{context.PackageName}.fileprovider" : Authority;
        return new Android.Net.Uri.Builder()
            .Scheme("content")
            .Authority(auth)
            .AppendPath("apks")
            .AppendPath(safe)
            .Build();
    }

    public override bool OnCreate() => true;

    public override string GetType(Android.Net.Uri uri) => "application/vnd.android.package-archive";

    public override ParcelFileDescriptor OpenFile(Android.Net.Uri uri, string mode) {
        string full = ResolveSafePath(Context, uri);
        if (string.IsNullOrEmpty(full) || !File.Exists(full)) {
            throw new Java.IO.FileNotFoundException($"APK not found: {uri}");
        }
        return ParcelFileDescriptor.Open(new Java.IO.File(full), ParcelFileMode.ReadOnly);
    }

    public override ICursor Query(Android.Net.Uri uri, string[] projection, string selection, string[] selectionArgs, string sortOrder) {
        string full = ResolveSafePath(Context, uri);
        if (string.IsNullOrEmpty(full)) {
            return null;
        }
        string safe = Path.GetFileName(full);
        long size = File.Exists(full) ? new FileInfo(full).Length : 0;
        string[] cols = projection is { Length: > 0 } ? projection : [IOpenableColumns.DisplayName, IOpenableColumns.Size];
        MatrixCursor cursor = new(cols, 1);
        Java.Lang.Object[] row = new Java.Lang.Object[cols.Length];
        for (int i = 0; i < cols.Length; i++) {
            if (cols[i] == IOpenableColumns.DisplayName) {
                row[i] = new Java.Lang.String(safe);
            } else if (cols[i] == IOpenableColumns.Size) {
                row[i] = Java.Lang.Long.ValueOf(size);
            }
        }
        cursor.AddRow(row);
        return cursor;
    }

    static string ResolveSafePath(Context context, Android.Net.Uri uri) {
        if (context == null || uri == null || uri.PathSegments == null || uri.PathSegments.Count != 2) {
            return null;
        }
        if (!string.Equals(uri.PathSegments[0], "apks", StringComparison.Ordinal)) {
            return null;
        }
        string name = uri.PathSegments[1] ?? "";
        if (string.IsNullOrEmpty(name) || name.Contains('/') || name.Contains('\\') || name.Contains("..")) {
            return null;
        }
        return Path.Combine(GetApkDirectory(context), name);
    }

    public override int Delete(Android.Net.Uri uri, string selection, string[] selectionArgs) => 0;
    public override Android.Net.Uri Insert(Android.Net.Uri uri, ContentValues values) => null;
    public override int Update(Android.Net.Uri uri, ContentValues values, string selection, string[] selectionArgs) => 0;
}
