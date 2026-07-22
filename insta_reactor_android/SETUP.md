# One-time on-device setup

This app is a thin front end. It does not run the automation itself — it
tells Termux (a terminal app) to run the existing `insta_reactor` Python
engine, and shows you the result. Do this setup once per phone, before
tapping Run in the app.

1. Install **Termux** and **Termux:API** from F-Droid (not the Play Store —
   the Play Store build is outdated and RUN_COMMAND won't work reliably).

2. In Termux:
   ```
   pkg install python git
   git clone <your insta_reactor repo url> ~/insta_reactor
   # or copy the D:\reactor BETA\insta_reactor folder onto the phone and
   # move it to ~/insta_reactor via termux-setup-storage
   cd ~/insta_reactor
   pip install uiautomator2
   ```

3. Let Termux drive the phone it's running on (no second PC):
   - Settings → Developer options → Wireless debugging → turn on, pair with
     a pairing code, note the port shown.
   - In Termux: `adb connect 127.0.0.1:<port>` (use the port from Wireless
     debugging's main screen, not the pairing screen, once paired).
   - `python -m uiautomator2 init`
   - Confirm it worked: `adb devices` should list a connected device.

4. Allow this app to trigger commands in Termux:
   - Edit `~/.termux/termux.properties`, add (or uncomment):
     `allow-external-apps=true`
   - `termux-reload-settings`

5. Sanity-check the engine directly in Termux before using the app:
   ```
   python -m insta_reactor setup
   python -m insta_reactor chats add "<some chat name>"
   python -m insta_reactor run --plan-only --json
   ```
   You should see a JSON line back with `"plan_only": true`.

6. Build and install this app (`app` module) from Android Studio onto the
   same phone. Enter the chat name, leave "Dry run" checked, tap Run.

**Leave "Dry run" checked** until the navigation-hardening work on the
engine itself is further along — live sends are still being verified
device-side.
