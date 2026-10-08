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

## Or: a public address on dfconsulting.tech

This replaces step 2 when you want `https://monitor.dfconsulting.tech`
instead of a Tailscale address: no VPN app on the phone, any network. The
server still binds to loopback only. The tunnel is the only way in, and
Cloudflare Access in front of it is the whole lock, so set it up before
the hostname goes live.

The domain has to be on Cloudflare DNS. If it isn't, add it in the
Cloudflare dashboard (free plan) and switch the registrar's nameservers to
the two Cloudflare gives you. Everything else on the domain keeps working
once its records are copied over, which Cloudflare does on import.

1. Lock it first. In the Cloudflare dashboard open Zero Trust, then Access,
   Applications, Add an application, Self-hosted. Domain
   `monitor.dfconsulting.tech`. Policy: Allow, Include, Emails, your
   address only. Login method: One-time PIN. Session duration: 1 month, so
   the home-screen app doesn't ask for a code every day.

2. Create the tunnel on the Mac:

   ```
   brew install cloudflared
   cloudflared tunnel login
   cloudflared tunnel create quadratus-monitor
   cloudflared tunnel route dns quadratus-monitor monitor.dfconsulting.tech
   ```

3. Copy `cloudflared-config.yml` to `~/.cloudflared/config.yml`, put the
   tunnel id from step 2 in both places, then try it in the foreground:

   ```
   cloudflared tunnel run quadratus-monitor
   ```

   With the monitor running (step 1 above), open
   `https://monitor.dfconsulting.tech` on the phone. It should ask for the
   email code, then show the page. If it shows the page without asking,
   stop the tunnel: Access isn't in front of it yet.

4. Keep it up across restarts:

   ```
   sudo cloudflared service install
   ```

   This installs a launch daemon that reads `~/.cloudflared/config.yml`.
   `sudo cloudflared service uninstall` removes it.

5. Safari, Share, Add to Home Screen. The code from step 1 is asked once
   inside the home-screen app; iOS keeps its cookies apart from Safari's.

What passes through Cloudflare: task text, file paths, model names, stop
reasons, token counts. Nothing that can act on a run. Anyone past the
Access policy can read it, so keep the policy to your own address.

## Why not share the Gradio GUI the same way

The GUI's Project tab can read local files and run check commands. That
is the reason `resolve_share` refuses to enable Gradio sharing, and it is
just as true over a tailnet. The monitor page exists so the thing that is
exposed has nothing on it that can act.
