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
    /// </summary>
    public static class Requests
    {
        private const int Keep = 1000;
        private static readonly string FilePath = Path.Combine(Config.Home, "requests.json");
        private static readonly HashSet<string> Platforms = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            { "PC", "PlayStation", "Xbox", "Switch", "Mobile", "Android", "iOS" };
        private static readonly object Gate = new object();
        private static Dictionary<string, string> known;

        /// <summary>The buyer's request as one line, or "" when it was never seen.</summary>
        public static string Find(string requestId)
        {
            if (string.IsNullOrEmpty(requestId)) return "";
            lock (Gate)
            {
                Load();
                return known.TryGetValue(requestId, out var text) ? text : "";
            }
        }

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
            var clean = fields.Select(x => Regex.Replace(x ?? "", @"\s+", " ").Trim()).Where(x => x.Length > 0).ToList();
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
            try
            {
                if (!File.Exists(FilePath)) return;
                var saved = new JavaScriptSerializer().Deserialize<Dictionary<string, string>>(File.ReadAllText(FilePath));
                if (saved != null) known = saved;
            }
            catch (Exception error)
            {
                Config.Log("requests.json unreadable: " + error.Message);
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
