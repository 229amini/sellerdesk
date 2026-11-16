using System;
using System.Collections;
using System.Collections.Generic;
using System.Linq;
using System.Media;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;

namespace SellerDesk
{
    public partial class SettingsWindow : Window
    {
        private readonly Config existing;
        private readonly bool keysOnly;

        /// <param name="keysOnly">Asks only for the Eldorado key, because the dashboard needs nothing else.</param>
        public SettingsWindow(Config existing, bool keysOnly = false)
        {
            this.existing = existing ?? new Config { Topic = "" };   // a shared default topic would ring for strangers' messages
            this.keysOnly = keysOnly;
            Skin.Use(this.existing.Theme);
            InitializeComponent();
            foreach (var skin in Skin.All)
            {
                var choice = new RadioButton
                {
                    Content = skin.Label,
                    GroupName = "theme",
                    Style = (Style)FindResource("Choice"),
                    IsChecked = skin == Skin.Current,
                };
                choice.Checked += (_, __) => PickTheme(skin.Name);
                Themes.Children.Add(choice);
            }
            UrlBox.Text = this.existing.Url;
            TopicBox.Text = this.existing.Topic;
            TokenBox.Password = this.existing.Token;
            SellerIdBox.Text = this.existing.SellerId;
            SellerSecretBox.Password = this.existing.SellerSecret;
            HubUrlBox.Text = this.existing.HubUrl;
            HubTokenBox.Password = this.existing.HubToken;
            VolumeBox.Value = this.existing.Volume;
            (this.existing.Ring ? RingOn : RingOff).IsChecked = true;
            var firstRun = this.existing.Topic.Length == 0 || existing == null;
            if (firstRun) SaveButton.Content = "Save and start";
            Open(keysOnly ? "key" : firstRun ? "alarms" : "general");
            if (keysOnly)
            {
                Tabs.Visibility = Visibility.Collapsed;
                GeneralPart.Visibility = Visibility.Collapsed;
                AlarmPart.Visibility = Visibility.Collapsed;
                AlertsPart.Visibility = Visibility.Collapsed;
            }
            else LoadAlertSwitches();
        }

        // The kinds of alert, in the server's names. The server keeps the choice, so every PC and channel follows it.
        private static readonly (string Id, string Label)[] AlertKinds =
        {
            ("order", "New order"), ("deadline", "Deadline"), ("dispute", "Dispute"), ("boosting", "Boosting"),
            ("news", "Eldorado news"), ("connection", "Connection"), ("other", "Other events"),
        };
        private readonly Dictionary<string, ToggleButton> kindButtons = new Dictionary<string, ToggleButton>();
        private bool alertsLoaded;

        private async void LoadAlertSwitches()
        {
            foreach (var (id, label) in AlertKinds)
            {
                var button = new ToggleButton { Content = label, Style = (Style)FindResource("Choice"), Margin = new Thickness(0, 0, 8, 8) };
                kindButtons[id] = button;
                Kinds.Children.Add(button);
            }
            SetAlertsEnabled(false);
            if (!existing.HasHub)
            {
                AlertsHint.Text = "These switches live on your SellerDesk server. Fill in its address and token under Eldorado key, Shared server, then open Settings again.";
                return;
            }
            var hub = new Hub(existing.HubUrl, existing.HubToken);
            try
            {
                var reply = await Task.Run(() => hub.AlertSwitches());
                var off = reply.TryGetValue("off", out var list) && list is IEnumerable names
                    ? new HashSet<string>(names.Cast<object>().Select(n => n as string)) : new HashSet<string>();
                foreach (var pair in kindButtons) pair.Value.IsChecked = !off.Contains(pair.Key);
                var paused = reply.TryGetValue("paused", out var p) && p is bool b && b;
                (paused ? AlertsOff : AlertsOn).IsChecked = true;
                alertsLoaded = true;
                SetAlertsEnabled(true);
            }
            catch (MarketError problem)
            {
                AlertsHint.Text = "Can't read the alert switches: " + problem.Message;
            }
        }

        /// <summary>For --snapshot: the switches as if the server had answered.</summary>
        public void PreviewAlerts(bool paused, string[] off)
        {
            foreach (var pair in kindButtons) pair.Value.IsChecked = !off.Contains(pair.Key);
            (paused ? AlertsOff : AlertsOn).IsChecked = true;
            alertsLoaded = true;
            AlertsHint.Text = "What your SellerDesk server sends to your phone, chat apps and every PC. An alert server you forward to with WEBHOOK_URL still gets everything; it has its own switches.";
            SetAlertsEnabled(true);
        }

        private void SetAlertsEnabled(bool on)
        {
            AlertsSwitch.IsEnabled = on;
            Kinds.IsEnabled = on && AlertsOn.IsChecked == true;
            KindsLabel.Opacity = Kinds.IsEnabled ? 1 : 0.5;
        }

        private void AlertsChanged(object sender, RoutedEventArgs e)
        {
            if (Kinds == null) return;      // still building the window
            SetAlertsEnabled(alertsLoaded);
        }

        /// <summary>Sends the alert switches to the server. Returns an error, or null.</summary>
        private string SaveAlertSwitches(Config config)
        {
            if (!alertsLoaded || !config.HasHub) return null;
            var paused = false;
            List<string> off = null;
            Dispatcher.Invoke(() =>
            {
                paused = AlertsOff.IsChecked == true;
                off = kindButtons.Where(pair => pair.Value.IsChecked != true).Select(pair => pair.Key).ToList();
            });
            try
            {
                new Hub(config.HubUrl, config.HubToken).SetAlertSwitches(paused, off);
                return null;
            }
            catch (MarketError problem)
            {
                return "Alert switches: " + problem.Message;
            }
        }

        public void Open(string part) =>
            (part == "alarms" ? AlarmTab : part == "key" ? KeyTab : part == "alerts" ? AlertsTab : GeneralTab).IsChecked = true;

        private void PartChanged(object sender, RoutedEventArgs e)
        {
            GeneralPart.Visibility = GeneralTab.IsChecked == true ? Visibility.Visible : Visibility.Hidden;
            AlarmPart.Visibility = AlarmTab.IsChecked == true ? Visibility.Visible : Visibility.Hidden;
            KeyPart.Visibility = KeyTab.IsChecked == true ? Visibility.Visible : Visibility.Hidden;
            if (AlertsPart != null) AlertsPart.Visibility = AlertsTab.IsChecked == true ? Visibility.Visible : Visibility.Hidden;
        }

        /// <summary>A theme needs no connection test, so it saves at once and an open dashboard follows it.</summary>
        private void PickTheme(string name)
        {
            Skin.Use(name);
            KeepTheme(name);
        }

        private static void KeepTheme(string name)
        {
            var saved = Config.Load();
            if (saved == null) return;
            saved.Theme = name;
            saved.Save();
        }

        private void VolumeChanged(object sender, RoutedPropertyChangedEventArgs<double> e) =>
            VolumeText.Text = (int)e.NewValue + "%";

        /// <summary>Plays the critical alarm, the loudest sound the app makes.</summary>
        private void TestSound(object sender, RoutedEventArgs e)
        {
            using (var player = new SoundPlayer(Sounds.Load("alarm.wav", (int)VolumeBox.Value))) player.Play();
        }

        public Config Result { get; private set; }

        private void Drag(object sender, MouseButtonEventArgs e)
        {
            if (e.ButtonState == MouseButtonState.Pressed) DragMove();
        }

        private async void Save(object sender, RoutedEventArgs e)
        {
            var config = new Config
            {
                Url = UrlBox.Text.Trim().TrimEnd('/'),
                Topic = TopicBox.Text.Trim(),
                Token = TokenBox.Password.Trim(),
                Sound = existing.Sound,
                Volume = (int)VolumeBox.Value,
                Ring = RingOff.IsChecked != true,
                Theme = Skin.Current.Name,
                SellerId = SellerIdBox.Text.Trim(),
                SellerSecret = SellerSecretBox.Password.Trim(),
                HubUrl = HubUrlBox.Text.Trim().TrimEnd('/'),
                HubToken = HubTokenBox.Password.Trim(),
                Zoom = existing.Zoom,
            };
            if (!keysOnly && !config.IsSetUp)
            {
                Open("alarms");
                Show("Enter a topic for alarms, or an Eldorado key for the dashboard.");
                return;
            }
            if (!keysOnly && config.HasAlarms && !Regex.IsMatch(config.Topic, "^[-_A-Za-z0-9]{1,64}$"))
            {
                Open("alarms");
                Show("A topic is one word: letters, numbers, - or _. For example: eldorado-7f3kq9x2mz");
                return;
            }
            if (keysOnly && !config.HasSellerKey)
            {
                Show("Enter both the client ID and the client secret.");
                return;
            }
            SaveButton.IsEnabled = false;
            Show("Testing the connection");
            var error = await Task.Run(() => Test(config));
            SaveButton.IsEnabled = true;
            if (error != null)
            {
                Show(error);
                return;
            }
            config.Save();
            Result = config;
            DialogResult = true;
        }

        private string Test(Config config)
        {
            var error = keysOnly || !config.HasAlarms ? null : config.TestConnection();
            if (error == null && config.HasHub)
            {
                try
                {
                    new Hub(config.HubUrl, config.HubToken).Ledger();
                }
                catch (MarketError problem)
                {
                    return "Shared server: " + problem.Message;
                }
            }
            if (error == null && !keysOnly) error = SaveAlertSwitches(config);
            if (error != null || !config.HasSellerKey) return error;
            try
            {
                new Market(config.SellerId, config.SellerSecret).Offers();
                return null;
            }
            catch (MarketError problem)
            {
                return problem.Message;
            }
        }

        private void Show(string text)
        {
            Status.Text = text;
            Status.Visibility = Visibility.Visible;
        }

        private void Cancel(object sender, RoutedEventArgs e)
        {
            PickTheme(existing.Theme);
            DialogResult = false;
        }
    }
}
