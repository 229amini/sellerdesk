using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace SellerDesk
{
    /// <summary>
    /// The Requests tab: the boosting and custom requests buyers send, as your server reads and prices them.
    /// The server sends offers (by itself when you switch that on), so this window only shows and asks.
    /// </summary>
    public partial class DashboardWindow
    {
        private const int RequestsOpenSeconds = 5;     // while the tab is on screen
        private const int RequestsAwaySeconds = 30;    // otherwise, for the tab's count

        private Dictionary<string, object> requestsData;
        private string requestsNote = "";
        private bool requestsBusy;
        private volatile bool requestsShown;
        private readonly AutoResetEvent requestsWake = new AutoResetEvent(false);
        private bool requestsStarted;

        private Dictionary<string, object> requestsTrial;
        private string requestsTrialNote = "";
        private bool requestsTrying;

        private Dictionary<string, object> RequestsToWeb() => new Dictionary<string, object>
        {
            ["ready"] = requestsData != null,
            ["note"] = requestsNote,
            ["busy"] = requestsBusy,
            ["data"] = requestsData,
            ["trial"] = requestsTrial,
            ["trialNote"] = requestsTrialNote,
            ["trying"] = requestsTrying,
        };

        /// <summary>Try: the server judges recent requests with the settings on screen. Nothing is saved or sent.</summary>
        private async void RequestTry(Dictionary<string, object> settings, Dictionary<string, object> sample)
        {
            if (hub == null || requestsTrying) return;
            requestsTrying = true;
            requestsTrialNote = "";
            Later();
            try
            {
                requestsTrial = await Task.Run(() => hub.TryRequests(settings, sample));
            }
            catch (MarketError error)
            {
                requestsTrialNote = error.Message;
            }
            requestsTrying = false;
            Later();
        }

        /// <summary>Its own loop: requests go stale in minutes, far sooner than the market pass comes round.</summary>
        private void StartRequests()
        {
            if (hub == null || requestsStarted) return;
            requestsStarted = true;
            Task.Run(() =>
            {
                while (!closing)
                {
                    try
                    {
                        var fresh = hub.Requests();
                        Post(() =>
                        {
                            requestsData = fresh;
                            if (!requestsBusy && requestsNote.StartsWith("Can't", StringComparison.Ordinal)) requestsNote = "";
                            Later();
                        });
                    }
                    catch (MarketError error)
                    {
                        Post(() =>
                        {
                            if (!requestsBusy) requestsNote = error.Message;
                            Later();
                        });
                    }
                    requestsWake.WaitOne(TimeSpan.FromSeconds(requestsShown ? RequestsOpenSeconds : RequestsAwaySeconds));
                }
            });
        }

        private bool HandleRequests(string type, Func<string, string> text, Dictionary<string, object> message)
        {
            switch (type)
            {
                case "tab":
                    requestsShown = text("value") == "requests";
                    if (requestsShown) requestsWake.Set();
                    return false;   // the page keeps its own tab; this only sets the pace
                case "reqSettings":
                    var settings = message.TryGetValue("settings", out var raw) ? raw as Dictionary<string, object> : null;
                    if (settings == null) return true;
                    RequestChange(() => hub.SaveRequestSettings(settings), Said(settings));
                    return true;
                case "reqTry":
                    var trySettings = message.TryGetValue("settings", out var rawTry) ? rawTry as Dictionary<string, object> : null;
                    var sample = message.TryGetValue("sample", out var rawSample) ? rawSample as Dictionary<string, object> : null;
                    RequestTry(trySettings, sample);
                    return true;
                case "reqTryClear":
                    requestsTrial = null;
                    requestsTrialNote = "";
                    Later();
                    return true;
                case "reqOffer":
                    if (!double.TryParse(text("price").Trim().TrimStart('$'), NumberStyles.Float, CultureInfo.InvariantCulture, out var price) || price <= 0)
                        return RequestTold("Type your price, like 2.40.");
                    var id = text("id");
                    var delivery = text("delivery");
                    string words = message.ContainsKey("message") ? text("message") : null;
                    RequestChange(() => hub.SendRequestOffer(id, Math.Round(price, 2), delivery, words), $"Offer sent: ${price:0.00}");
                    return true;
                case "reqWithdraw":
                    var withdrawId = text("id");
                    RequestChange(() => hub.WithdrawRequestOffer(withdrawId), "Offer withdrawn");
                    return true;
                case "reqHide":
                    var hideId = text("id");
                    var hide = text("hidden") != "False" && text("hidden") != "false";
                    RequestChange(() => hub.HideRequest(hideId, hide), hide ? "Hidden" : "Shown again");
                    return true;
                case "reqSkip":
                    var skipId = text("id");
                    var skip = text("skipped") != "False" && text("skipped") != "false";
                    RequestChange(() => hub.SkipRequest(skipId, skip), skip ? "Skipped" : "Back in To answer");
                    return true;
                default:
                    return false;
            }
        }

        private static string Said(Dictionary<string, object> settings)
        {
            if (settings.TryGetValue("mode", out var mode))
                switch (mode as string)
                {
                    case "live": return "Live: offers go out by themselves";
                    case "simulate": return "Simulate: prices requests, sends nothing";
                    case "off": return "Auto offers off";
                }
            return "Saved";
        }

        private bool RequestTold(string note)
        {
            requestsNote = note;
            Later();
            return true;
        }

        /// <summary>Every change is followed by a fresh read, so the page shows the server's word, never a guess.</summary>
        private async void RequestChange(Func<Dictionary<string, object>> call, string done)
        {
            if (hub == null)
            {
                RequestTold("Requests need your shared server. Add it in Settings, Eldorado key.");
                return;
            }
            if (requestsBusy) return;
            requestsBusy = true;
            requestsNote = "Sending";
            Later();
            try
            {
                var fresh = await Task.Run(() =>
                {
                    call();
                    return hub.Requests();
                });
                requestsData = fresh;
                requestsNote = done;
            }
            catch (MarketError error)
            {
                Config.Log("requests: " + error.Message);
                requestsNote = error.Message;
            }
            requestsBusy = false;
            Later();
        }
    }
}
