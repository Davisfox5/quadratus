# The monitor as a phone app

`quadratus --monitor --serve` serves the run status as one self-reloading
page on `127.0.0.1:7861`. It only reads the run directory. It has no
controls, calls no model, and binds to loopback with no flag to change
that. Three steps make it an icon on a phone that is always up.

## 1. Start it at login (Mac)

Edit the two paths in `com.quadratus.monitor.plist` (the `quadratus`
binary in your venv, and the series or project to watch by default), then:

```
cp tools/monitor/com.quadratus.monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.quadratus.monitor.plist
```

`launchctl unload` the same path stops it. The log is
`/tmp/quadratus-monitor.log`. The page takes `?project=` and `?series=`
and has a form for both, so the defaults in the plist are a starting
point, not a restart every series.

## 2. Reach it from your own devices only

With Tailscale on the Mac and the phone:

```
tailscale serve --bg 7861
```

This prints an `https://<mac>.<tailnet>.ts.net` address that only devices
on your tailnet can open, with a real certificate. The server itself stays
on loopback; Tailscale is the only way in. `tailscale serve --bg off`
removes it.

## 3. Make it an app

Open that address in Safari on the phone, tap Share, then Add to Home
Screen. The page declares itself a standalone web app, so it opens
full-screen from the icon and reloads every five seconds while open.

## Why not share the Gradio GUI the same way

The GUI's Project tab can read local files and run check commands. That
is the reason `resolve_share` refuses to enable Gradio sharing, and it is
just as true over a tailnet. The monitor page exists so the thing that is
exposed has nothing on it that can act.
