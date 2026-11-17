using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace SellerDesk
{
    /// <summary>
    /// What buyers wrote in their boosting and custom requests. A RequestedBoosting order carries only the
    /// category ("Custom Request"); the buyer's own words come in the BoostingRequestCreated notification,
    /// whose detailsId is the order's boostingRequestId. Kept on disk, because notifications age out.
    /// The notification leaves out some fields (Completion Method, "Provide any additional information"), so
    /// each order's request is also read whole, with its labels, from the request's details page.
    /// </summary>
    public static class Requests
    {
        private const int Keep = 1000;
        private static readonly string FilePath = Path.Combine(Config.Home, "requests.json");
        private static readonly string DetailsPath = Path.Combine(Config.Home, "request-details.json");
        private static readonly HashSet<string> Platforms = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { "PC", "PlayStation", "Xbox", "Switch", "Mobile", "Android", "iOS" };
        // Shown on the order, but left out of its one-line title: nearly every request says "Solo".
        private static readonly HashSet<string> NotInTitle = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { "Completion Method" };
        private static readonly object Gate = new object();
        private static Dictionary<string, string> known;
        private static Dictionary<string, List<string[]>> details;   // request id -> [label, value] rows; empty = gave up

        /// <summary>The buyer's request as one line, or "" when it was never seen. The full details win over the notification.</summary>
        public static string Find(string requestId)
        {
            if (string.IsNullOrEmpty(requestId)) return "";
            lock (Gate)
            {
                Load();
                if (details.TryGetValue(requestId, out var rows) && rows.Count > 0)
                    return Format(rows.Where(r => !NotInTitle.Contains(r[0])).Select(r => r[1]).ToList());
                return known.TryGetValue(requestId, out var text) ? text : "";
            }
        }

        /// <summary>Every field of the request with its label, as the order page shows it; empty when not read yet.</summary>
        public static List<string[]> Details(string requestId)
        {
            if (string.IsNullOrEmpty(requestId)) return new List<string[]>();
            lock (Gate)
            {
                Load();
                return details.TryGetValue(requestId, out var rows) ? rows.ToList() : new List<string[]>();
            }
        }

        /// <summary>True once the details page was read, or found missing, for this request.</summary>
        public static bool HasDetails(string requestId)
        {
            lock (Gate)
            {
                Load();
                return details.ContainsKey(requestId);
            }
        }

        /// <summary>Keeps one request's details. rows null means the page is gone: it isn't asked for again.</summary>
        public static void Learn(string requestId, List<string[]> rows)
        {
            if (string.IsNullOrEmpty(requestId)) return;
            var clean = (rows ?? new List<string[]>())
                .Select(r => new[] { Clean(r[0]), Clean(r[1]) })
                .Where(r => r[1].Length > 0)
                .Select(r => new[] { r[0], r[1].Length > 500 ? r[1].Substring(0, 499) + "…" : r[1] })
                .ToList();
            lock (Gate)
            {
                Load();
                details[requestId] = clean;
                SaveDetails();
            }
        }

        private static string Clean(string text) => Regex.Replace(text ?? "", @"\s+", " ").Trim();

        public static bool Has(string requestId)
        {
            lock (Gate)
            {
                Load();
                return known.ContainsKey(requestId);
            }
        }

        /// <summary>Takes every BoostingRequestCreated from a notifications reply. True when something new was learned.</summary>
        public static bool Harvest(IEnumerable<Dictionary<string, object>> results)
        {
            var learned = false;
            lock (Gate)
            {
                Load();
                foreach (var row in results)
                {
                    var plain = row.TryGetValue("notification", out var p) ? p as Dictionary<string, object> : null;
                    if (plain == null || Str(plain, "event") != "BoostingRequestCreated") continue;
                    var details = plain.TryGetValue("details", out var d) ? d as Dictionary<string, object> : null;
                    var data = plain.TryGetValue("customNotificationData", out var c) ? c as Dictionary<string, object> : null;
                    var id = details == null ? "" : Str(details, "detailsId");
                    if (id.Length == 0 || data == null || (known.TryGetValue(id, out var had) && had.Length > 0)) continue;
                    var fields = data.TryGetValue("requestFields", out var f) && f is IEnumerable list && !(f is string)
                        ? list.Cast<object>().Select(x => Convert.ToString(x, CultureInfo.InvariantCulture) ?? "").ToList()
                        : new List<string>();
                    var text = Format(fields);
                    if (text.Length == 0) continue;
                    known[id] = text;
                    learned = true;
                }
                if (learned) Save();
            }
            return learned;
        }

        /// <summary>Requests a full search didn't find: stored empty, so later passes don't read for them again.</summary>
        public static void GiveUp(IEnumerable<string> requestIds)
        {
            lock (Gate)
            {
                Load();
                var added = false;
                foreach (var id in requestIds.Where(i => !string.IsNullOrEmpty(i) && !known.ContainsKey(i)))
                {
                    known[id] = "";
                    added = true;
                }
                if (added) Save();
            }
        }

        /// <summary>["PC", "100 herbals"] -> "100 herbals | PC"; ["1", "75", "PC"] -> "1 -> 75 | PC".</summary>
        public static string Format(IList<string> fields)
        {
            var clean = fields.Select(Clean).Where(x => x.Length > 0).ToList();
            var platforms = clean.Where(Platforms.Contains).ToList();
            var rest = clean.Where(x => !Platforms.Contains(x)).ToList();
            if (rest.Count >= 2 && IsNumber(rest[0]) && IsNumber(rest[1]))
            {
                rest[0] = rest[0] + " → " + rest[1];
                rest.RemoveAt(1);
            }
            var line = string.Join(" | ", rest.Concat(platforms));
            return line.Length > 240 ? line.Substring(0, 239) + "…" : line;
        }

        private static bool IsNumber(string text) => Regex.IsMatch(text, @"^\d+$");

        private static string Str(Dictionary<string, object> map, string key) =>
            map.TryGetValue(key, out var value) ? Convert.ToString(value, CultureInfo.InvariantCulture) ?? "" : "";

        private static void Load()
        {
            if (known != null) return;
            known = new Dictionary<string, string>();
            details = new Dictionary<string, List<string[]>>();
            try
            {
                if (File.Exists(FilePath))
                {
                    var saved = new JavaScriptSerializer().Deserialize<Dictionary<string, string>>(File.ReadAllText(FilePath));
                    if (saved != null) known = saved;
                }
            }
            catch (Exception error)
            {
                Config.Log("requests.json unreadable: " + error.Message);
            }
            try
            {
                if (File.Exists(DetailsPath))
                {
                    var saved = new JavaScriptSerializer().Deserialize<Dictionary<string, List<List<string>>>>(File.ReadAllText(DetailsPath));
                    if (saved != null)
                        foreach (var pair in saved)
                            details[pair.Key] = (pair.Value ?? new List<List<string>>()).Where(r => r != null && r.Count == 2)
                                .Select(r => new[] { r[0] ?? "", r[1] ?? "" }).ToList();
                }
            }
            catch (Exception error)
            {
                Config.Log("request-details.json unreadable: " + error.Message);
            }
        }

        private static void SaveDetails()
        {
            try
            {
                if (details.Count > Keep) details = details.Skip(details.Count - Keep).ToDictionary(k => k.Key, k => k.Value);
                Directory.CreateDirectory(Config.Home);
                var temp = DetailsPath + ".tmp";
                File.WriteAllText(temp, new JavaScriptSerializer().Serialize(details));
                if (File.Exists(DetailsPath)) File.Replace(temp, DetailsPath, null);
                else File.Move(temp, DetailsPath);
            }
            catch (Exception error)
            {
                Config.Log("request-details.json not saved: " + error.Message);
            }
        }

        private static void Save()
        {
            try
            {
                // Insertion order is kept by Dictionary in practice; drop the oldest past the cap.
                var rows = known.Count > Keep ? known.Skip(known.Count - Keep).ToDictionary(k => k.Key, k => k.Value) : known;
                known = rows;
                Directory.CreateDirectory(Config.Home);
                var temp = FilePath + ".tmp";
                File.WriteAllText(temp, new JavaScriptSerializer().Serialize(rows));
                if (File.Exists(FilePath)) File.Replace(temp, FilePath, null);
                else File.Move(temp, FilePath);
            }
            catch (Exception error)
            {
                Config.Log("requests.json not saved: " + error.Message);
            }
        }
    }
}
