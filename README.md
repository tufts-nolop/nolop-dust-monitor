Code to read data from a Dylos air quality monitor and display it in a web browser

Originally written by the noble Alex Beattie in the summer of 2026

### Installation

Install some libraries.

```
sudo apt update
sudo apt install python3-flask matplotlib
```

Make Flask run on port 80 at startup.

```
sudo mv flask.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable flask.service
sudo systemctl start flask.service
```

Add libraries so Firefox can run in kiosk mode.

```
sudo apt install --no-install-recommends xserver-xorg x11-xserver-utils xinit openbox firefox-esr unclutter
```

Set up the Openbox window manager to start Firefox as soon as the Flask app is running.

```
mv ~/nolop-dust-monitor/autostart ~/.config/openbox/autostart
```

Install kiosk.service file to start X and Openbox. 

Create Firefox profile tweaked for kiosk mode.

```
DISPLAY=:0 firefox-esr -CreateProfile kiosk
```

Add some weird stuff to the kiosk profile.

```
cat > ~/.mozilla/firefox/35dxmhxf.kiosk/prefs.js << 'EOF'
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.shell.checkDefaultBrowser", false);
EOF
```

Note that the `35dxmhxf.kiosk` profile directory name is random, so that needs to be changed in the command above.
