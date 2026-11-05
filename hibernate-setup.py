#!/usr/bin/env python3
"""Hibernate Setup: configure suspend-then-hibernate on Fedora GNOME.

Every system change is a systemd drop-in with a marker comment, written as
root through pkexec. Remove deletes every file Apply can create. Export
writes equivalent install.sh / uninstall.sh for version control.

  hibernate-setup.py                 run the app
  hibernate-setup.py --install-app   add it to the app grid (~/.local)
  hibernate-setup.py --remove-app    take it out again
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date

APP_ID = "local.sth.HibernateSetup"
APP_NAME = "Hibernate Setup"
MARK = "# Managed by suspend-then-hibernate setup; remove with uninstall.sh"
SLEEP = "/etc/systemd/sleep.conf.d/80-suspend-then-hibernate.conf"
TEST = "/etc/systemd/sleep.conf.d/99-test.conf"
SVC = "/etc/systemd/system/systemd-suspend.service.d/80-suspend-then-hibernate.conf"
LOGIND = "/etc/systemd/logind.conf.d/80-suspend-then-hibernate.conf"
LEGACY = ("/etc/systemd/system/systemd-suspend.service.d/override.conf",
          "/etc/systemd/sleep.conf.d/hibernate-delay.conf")
SCHEMA = "org.gnome.settings-daemon.plugins.power"
PB_KEY = "power-button-action"

SCOPES = [("Every suspend (menu, idle, lid)", "all"), ("Lid close only", "lid")]
LID_AC = [("Suspend, then hibernate", "suspend-then-hibernate"),
          ("Suspend", "suspend"), ("Do nothing", "ignore")]
DELAYS = [("15 minutes", "15min"), ("30 minutes", "30min"), ("1 hour", "1h"),
          ("2 hours", "2h"), ("3 hours", "3h"), ("6 hours", "6h"), ("12 hours", "12h")]
ESTIMATES = [("systemd default", ""), ("30 minutes", "30min"),
             ("1 hour", "1h"), ("2 hours", "2h")]
PBTN = [("Suspend", "suspend"), ("Hibernate", "hibernate"),
        ("Show power menu", "interactive"), ("Do nothing", "nothing")]
SCOPE_TEXT = {"all": "Suspend, then hibernate (every suspend)",
              "lid": "Suspend, then hibernate (lid close only)",
              "none": "Plain suspend"}
DEFAULTS = dict(scope="all", lid_ac="suspend-then-hibernate", delay_on=True,
                delay="1h", ac=False, est="", pbtn=None, migrate=True)


# ---------------------------------------------------------------- state ----

def parse_conf(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("# power-button:"):
                    v = line.split(":", 1)[1].strip()
                    out["#pbtn"] = None if v == "unchanged" else v
                elif "=" in line and not line.startswith(("#", ";")):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
    except FileNotFoundError:
        return None
    return out


def read_state():
    """Return (config, installed) reconstructed from the files on disk."""
    c = dict(DEFAULTS)
    s = parse_conf(SLEEP)
    if s is None:
        return c, False
    c["delay_on"] = "HibernateDelaySec" in s
    if c["delay_on"]:
        c["delay"] = s["HibernateDelaySec"]
        c["ac"] = s.get("HibernateOnACPower", "yes") == "yes"
    c["est"] = s.get("SuspendEstimationSec", "")
    c["pbtn"] = s.get("#pbtn")
    lg = parse_conf(LOGIND)
    if lg is not None:
        c["scope"] = "lid"
        c["lid_ac"] = lg.get("HandleLidSwitchExternalPower", c["lid_ac"])
    return c, True


def disk_swaps():
    try:
        with open("/proc/swaps") as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        return []
    return [r.split()[0] for r in rows if r and not r.startswith("/dev/zram")]


def systemd_version():
    try:
        out = subprocess.run(["systemctl", "--version"], capture_output=True,
                             text=True).stdout
        return int(out.split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def run_out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True).stdout
    except OSError:
        return ""


def cat_config(name):
    """Effective settings as systemd merges them: {key: (value, source file)}.

    Parses `systemd-analyze cat-config`, which prints every file in precedence
    order with a "# /path" header; the last assignment wins and an empty
    assignment resets the key to the built-in default."""
    vals, src = {}, None
    for line in run_out(["systemd-analyze", "cat-config", name]).splitlines():
        s = line.strip()
        if s.startswith("# /"):
            src = s[2:].strip()
        elif s and not s.startswith(("#", ";", "[")) and "=" in s:
            k, v = (x.strip() for x in s.split("=", 1))
            if v:
                vals[k] = (v, src)
            else:
                vals.pop(k, None)
    return vals


def suspend_override_source():
    """Drop-in that turns systemd-suspend.service into suspend-then-hibernate."""
    unit = "systemd-suspend.service"
    if "suspend-then-hibernate" not in run_out(
            ["systemctl", "show", unit, "-p", "ExecStart", "--value"]):
        return None
    found = "the unit file"
    for p in run_out(["systemctl", "show", unit, "-p", "DropInPaths", "--value"]).split():
        try:
            with open(p) as f:
                if "suspend-then-hibernate" in f.read():
                    found = p
        except OSError:
            pass
    return found


def gsettings_get():
    out = run_out(["gsettings", "get", SCHEMA, PB_KEY]).strip().strip("'")
    return out or None


def truthy(v):
    return v.lower() in ("yes", "true", "1", "on")


def read_effective():
    """Return (form config, info) describing what the system does right now,
    whoever configured it. info maps each setting to (value, source)."""
    c = dict(DEFAULTS)
    own, managed = read_state()
    c["pbtn"] = own["pbtn"] if managed else None
    c["migrate"] = True

    sl = cat_config("systemd/sleep.conf")
    delay, ac, est = (sl.get(k) for k in
                      ("HibernateDelaySec", "HibernateOnACPower", "SuspendEstimationSec"))
    c["delay_on"] = delay is not None
    if delay:
        c["delay"] = delay[0]
    c["ac"] = truthy(ac[0]) if ac else True          # systemd default: yes
    c["est"] = est[0] if est else ""

    lg = cat_config("systemd/logind.conf")
    lid, lid_ac = lg.get("HandleLidSwitch"), lg.get("HandleLidSwitchExternalPower")
    c["lid_ac"] = (lid_ac or lid or ("suspend", None))[0]  # unset: follows HandleLidSwitch

    svc = suspend_override_source()
    if svc:
        scope = ("all", svc)
    elif lid and lid[0] == "suspend-then-hibernate":
        scope = ("lid", lid[1])
    else:
        scope = ("none", None)
    c["scope"] = scope[0] if scope[0] != "none" else "all"

    info = dict(managed=managed, scope=scope, delay=delay, ac=ac,
                pb_live=gsettings_get())
    return c, info


def source_text(src):
    if src is None:
        return "systemd default"
    if src in (SLEEP, SVC, LOGIND):
        return "set by this app"
    return f"from {src}"


def battery_wh():
    for p in sorted(glob.glob("/sys/class/power_supply/BAT*/energy_full")):
        try:
            with open(p) as f:
                return int(f.read()) / 1e6
        except (OSError, ValueError):
            pass
    return None


# ----------------------------------------------------------- generators ----
# Root-side bash, shared by the app (via pkexec) and the exported scripts.

PUT = ("MARK='" + MARK + "'\n"
       'put() { mkdir -p "$(dirname "$1")"; { echo "$MARK"; cat; } > "$1"; '
       'chmod 0644 "$1"; restorecon -F "$1" 2>/dev/null || true; echo "wrote $1"; }')


def sleep_body(c):
    lines = [f"# power-button: {c['pbtn'] or 'unchanged'}", "[Sleep]"]
    if c["delay_on"]:
        lines += [f"HibernateDelaySec={c['delay']}",
                  f"HibernateOnACPower={'yes' if c['ac'] else 'no'}"]
    if c["est"]:
        lines.append(f"SuspendEstimationSec={c['est']}")
    return "\n".join(lines)


def core_install(c):
    L = ["set -euo pipefail", PUT, ""]
    if c["migrate"]:
        L += ["# Retire hand-made files from earlier setups",
              f"for f in {' '.join(LEGACY)}; do",
              '  if [[ -f $f ]] && ! grep -qF "$MARK" "$f"; then mv -v "$f" "$f.bak"; fi',
              "done", ""]
    L += [f"put {SLEEP} <<'EOF'", sleep_body(c), "EOF", ""]
    if c["scope"] == "all":
        L += [f"put {SVC} <<'EOF'", "[Service]", "ExecStart=",
              "ExecStart=/usr/lib/systemd/systemd-sleep suspend-then-hibernate", "EOF",
              f"if [[ -f {LOGIND} ]]; then rm -v {LOGIND}; systemctl kill -s HUP systemd-logind; fi",
              "systemctl daemon-reload"]
    else:
        L += [f"put {LOGIND} <<'EOF'", "[Login]",
              "HandleLidSwitch=suspend-then-hibernate",
              f"HandleLidSwitchExternalPower={c['lid_ac']}", "EOF",
              f"if [[ -f {SVC} ]]; then rm -v {SVC}; fi",
              "systemctl daemon-reload",
              "systemctl kill -s HUP systemd-logind"]
    return "\n".join(L) + "\n"


def core_uninstall():
    return "\n".join([
        "set -euo pipefail",
        f"for f in {SLEEP} {TEST} {SVC}; do",
        '  if [[ -f $f ]]; then rm -v "$f"; fi',
        "done",
        "rmdir --ignore-fail-on-non-empty /etc/systemd/system/systemd-suspend.service.d 2>/dev/null || true",
        "systemctl daemon-reload",
        f"if [[ -f {LOGIND} ]]; then rm -v {LOGIND}; systemctl kill -s HUP systemd-logind; fi",
    ]) + "\n"


def core_test(scope):
    trigger = "systemctl suspend" if scope == "all" else ": # waiting for the lid to close"
    return "\n".join([
        "set -uo pipefail",
        f"TEST={TEST}",
        'trap \'rm -f "$TEST"\' EXIT',
        "start=$(date +%s)",
        'mkdir -p "$(dirname "$TEST")"',
        "printf '[Sleep]\\nHibernateDelaySec=2min\\nHibernateOnACPower=yes\\n' > \"$TEST\"",
        'chmod 0644 "$TEST"; restorecon -F "$TEST" 2>/dev/null || true',
        "eff=$(systemd-analyze cat-config systemd/sleep.conf | grep -E '^HibernateDelaySec=' | tail -1)",
        '[[ $eff == HibernateDelaySec=2min ]] || { echo "ERROR=Test value not in effect ($eff)"; exit 1; }',
        "coproc MON { exec gdbus monitor --system --dest org.freedesktop.login1 --object-path /org/freedesktop/login1; }",
        "sleep 1",
        trigger,
        "resumed=0 sleeping=0",
        "while :; do",
        '  if IFS= read -r -t 60 line <&"${MON[0]}"; then',
        "    case $line in",
        '      *"PrepareForSleep (true"*) sleeping=1 ;;',
        '      *"PrepareForSleep (false"*) resumed=1; break ;;',
        "    esac",
        "  else",
        "    (( $? > 128 )) || break   # monitor exited, not a timeout",
        "  fi",
        "  (( sleeping || SECONDS < 1800 )) || break   # give up if never suspended",
        "done",
        'kill "$MON_PID" 2>/dev/null || true',
        'echo "RESUMED=$resumed"',
        "sleep 2",
        "if journalctl -k --since \"@$start\" -q --no-pager -g 'hibernation entry' | grep -q .; then echo RESULT=hibernated; else echo RESULT=not-hibernated; fi",
        "if journalctl --since \"@$start\" -q --no-pager -g 'avc:.*systemd-sleep' | grep -q .; then echo AVC=yes; else echo AVC=no; fi",
    ]) + "\n"


def export_install(c):
    L = ["#!/usr/bin/env bash",
         f"# Suspend-then-hibernate setup. Generated by {APP_NAME} on {date.today()}.",
         "# Re-runnable: each file is rewritten in full. Undo with uninstall.sh.",
         "set -euo pipefail",
         '[[ $EUID -ne 0 ]] || { echo "Run as your normal user; sudo is called where needed." >&2; exit 1; }',
         "tail -n +2 /proc/swaps | grep -qv '^/dev/zram' || { echo \"No disk swap active; hibernation would fail.\" >&2; exit 1; }"]
    if c["delay_on"] and not c["ac"]:
        L += ["SDV=$(systemctl --version | awk 'NR==1{print $2}')",
              '(( SDV >= 257 )) || { echo "HibernateOnACPower needs systemd 257+, found $SDV" >&2; exit 1; }']
    L += ["", "sudo bash -s <<'ROOT'", core_install(c).rstrip("\n"), "ROOT"]
    if c["pbtn"]:
        L.append(f"gsettings set {SCHEMA} {PB_KEY} '{c['pbtn']}'")
    L.append('echo "Done."')
    return "\n".join(L) + "\n"


def export_uninstall(c):
    L = ["#!/usr/bin/env bash",
         f"# Reverts suspend-then-hibernate setup. Generated by {APP_NAME} on {date.today()}.",
         "# Safe to run more than once. Leaves the swapfile in place.",
         "set -euo pipefail",
         "sudo bash -s <<'ROOT'", core_uninstall().rstrip("\n"), "ROOT"]
    if c["pbtn"]:
        L.append(f"gsettings reset {SCHEMA} {PB_KEY}")
    L.append('echo "Reverted."')
    return "\n".join(L) + "\n"


# ---------------------------------------------------- app grid install ----

DESKTOP = """[Desktop Entry]
Type=Application
Name={name}
Comment=Suspend, then hibernate after a delay
Exec="{exec}"
Icon=weather-clear-night-symbolic
Categories=GTK;GNOME;Settings;HardwareSettings;
Keywords=hibernate;suspend;sleep;battery;
StartupNotify=true
"""


def desktop_paths():
    home = os.path.expanduser("~")
    return (os.path.join(home, ".local/bin/hibernate-setup"),
            os.path.join(home, ".local/share/applications", APP_ID + ".desktop"))


def install_app():
    target, desktop = desktop_paths()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.makedirs(os.path.dirname(desktop), exist_ok=True)
    if os.path.abspath(__file__) != target:
        shutil.copy(__file__, target)
    os.chmod(target, 0o755)
    with open(desktop, "w") as f:
        f.write(DESKTOP.format(name=APP_NAME, exec=target))
    print(f"Installed {target}\nInstalled {desktop}")


def remove_app():
    for p in desktop_paths():
        if os.path.exists(p):
            os.remove(p)
            print(f"Removed {p}")


# ------------------------------------------------------------------- UI ----

def run_ui():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    def combo(title, subtitle, items, current):
        labels, values = [l for l, _ in items], [v for _, v in items]
        if current not in values:  # keep a hand-edited value selectable
            labels.append(str(current))
            values.append(current)
        row = Adw.ComboRow(title=title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_model(Gtk.StringList.new(labels))
        row.set_selected(values.index(current))
        row.values = values
        return row

    def val(row):
        return row.values[row.get_selected()]

    def pb_settings():
        src = Gio.SettingsSchemaSource.get_default()
        return Gio.Settings.new(SCHEMA) if src and src.lookup(SCHEMA, True) else None

    def logind_can(method):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            v = bus.call_sync("org.freedesktop.login1", "/org/freedesktop/login1",
                              "org.freedesktop.login1.Manager", method, None,
                              GLib.VariantType("(s)"), Gio.DBusCallFlags.NONE, -1, None)
            return v.unpack()[0]
        except GLib.Error:
            return "unknown"

    class Window(Adw.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title=APP_NAME,
                             default_width=580, default_height=820)
            self.toasts = Adw.ToastOverlay()
            view = Adw.ToolbarView()
            header = Adw.HeaderBar()
            reload_btn = Gtk.Button(icon_name="view-refresh-symbolic",
                                    tooltip_text="Reload from disk")
            reload_btn.connect("clicked", lambda *_: self.reload())
            export_btn = Gtk.Button(icon_name="document-save-symbolic",
                                    tooltip_text="Export install.sh and uninstall.sh")
            export_btn.connect("clicked", self.on_export)
            header.pack_start(reload_btn)
            header.pack_end(export_btn)
            view.add_top_bar(header)
            self.page = None
            self.view = view
            self.toasts.set_child(view)
            self.set_content(self.toasts)
            self.reload()

        # -- layout ---------------------------------------------------------
        def reload(self):
            c, info = read_effective()
            self.info = info
            page = Adw.PreferencesPage()

            g = Adw.PreferencesGroup(title="In effect now",
                                     description="What this system does today, "
                                                 "whoever configured it.")
            self.st_scope = Adw.ActionRow(title="When you suspend")
            self.st_delay = Adw.ActionRow(title="Hibernate after")
            self.st_ac = Adw.ActionRow(title="On AC power")
            self.st_pb = Adw.ActionRow(title="Power button")
            self.st_notes = Adw.ActionRow(title="Notes")
            for r in (self.st_scope, self.st_delay, self.st_ac, self.st_pb, self.st_notes):
                r.add_css_class("property")
                r.set_subtitle_selectable(True)
                g.add(r)
            page.add(g)

            g = Adw.PreferencesGroup(title="System")
            self.st_swap = Adw.ActionRow(title="Disk swap")
            self.st_can = Adw.ActionRow(title="Suspend-then-hibernate available")
            self.st_bat = Adw.ActionRow(title="Battery capacity")
            for r in (self.st_swap, self.st_can, self.st_bat):
                r.add_css_class("property")
                g.add(r)
            page.add(g)

            g = Adw.PreferencesGroup(title="Applies to",
                                     description="Starts from the settings in effect. "
                                                 "Apply makes this app manage them.")
            self.scope_row = combo("Trigger", None, SCOPES, c["scope"])
            self.lid_row = combo("Lid closed on AC power", None, LID_AC, c["lid_ac"])
            g.add(self.scope_row)
            g.add(self.lid_row)
            page.add(g)

            g = Adw.PreferencesGroup(title="When to hibernate",
                                     description="Below 5% battery it always hibernates.")
            self.delay_sw = Adw.SwitchRow(title="Hibernate after a delay",
                                          subtitle="Off: only at low battery",
                                          active=c["delay_on"])
            self.delay_row = combo("Delay", None, DELAYS, c["delay"])
            self.ac_sw = Adw.SwitchRow(title="Count down while plugged in",
                                       subtitle="Off: stays suspended on AC power",
                                       active=c["ac"])
            self.est_row = combo("Battery check interval",
                                 "Fallback if firmware has no low-battery alarm",
                                 ESTIMATES, c["est"])
            for r in (self.delay_sw, self.delay_row, self.ac_sw, self.est_row):
                g.add(r)
            page.add(g)

            g = Adw.PreferencesGroup(title="Other")
            self.pb_live = info["pb_live"]
            self.pb_record = c["pbtn"]
            self.pb_row = combo("Power button", "Per-user GNOME setting", PBTN,
                                self.pb_live or "suspend")
            self.pb_row.set_visible(self.pb_live is not None)
            self.mig_sw = Adw.SwitchRow(title="Retire hand-made config files",
                                        subtitle="Renames override.conf and hibernate-delay.conf to .bak",
                                        active=c["migrate"])
            g.add(self.pb_row)
            g.add(self.mig_sw)
            page.add(g)

            g = Adw.PreferencesGroup()
            box = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6)
            self.apply_btn = Gtk.Button(label="Apply")
            self.apply_btn.add_css_class("suggested-action")
            self.test_btn = Gtk.Button(label="Run 2-minute test")
            self.remove_btn = Gtk.Button(label="Remove")
            self.remove_btn.add_css_class("destructive-action")
            for b, cb in ((self.apply_btn, self.on_apply), (self.test_btn, self.on_test),
                          (self.remove_btn, self.on_remove)):
                b.add_css_class("pill")
                b.connect("clicked", cb)
                box.append(b)
            g.add(box)
            page.add(g)

            self.scope_row.connect("notify::selected", self.update_visibility)
            self.delay_sw.connect("notify::active", self.update_visibility)
            self.update_visibility()
            self.view.set_content(page)
            self.page = page
            self.refresh_status()

        def update_visibility(self, *_):
            self.lid_row.set_visible(val(self.scope_row) == "lid")
            on = self.delay_sw.get_active()
            self.delay_row.set_visible(on)
            self.ac_sw.set_visible(on)

        def refresh_status(self):
            info = self.info
            scope, src = info["scope"]
            self.st_scope.set_subtitle(SCOPE_TEXT[scope]
                                       + ("" if scope == "none" else f" · {source_text(src)}"))
            d = info["delay"]
            self.st_delay.set_subtitle(f"{d[0]} or low battery · {source_text(d[1])}" if d
                                       else "Only at low battery (no delay set)")
            ac = info["ac"]
            if d:
                stays = ac is not None and not truthy(ac[0])
                self.st_ac.set_subtitle(("Stays suspended" if stays else "Keeps counting down")
                                        + f" · {source_text(ac[1] if ac else None)}")
            else:
                self.st_ac.set_subtitle("No timer, so it stays suspended")
            labels = {v: l for l, v in PBTN}
            pb = info["pb_live"]
            self.st_pb.set_subtitle("Not a GNOME session" if pb is None else
                                    labels.get(pb, pb) + (" · set by this app"
                                                          if self.pb_record == pb else ""))
            notes = [f"Hand-made file: {p}" for p in LEGACY if os.path.exists(p)]
            if os.path.exists(TEST):
                notes.append(f"Leftover test file: {TEST}")
            if scope != "none" and not info["managed"]:
                notes.append("Configured outside this app")
            self.st_notes.set_subtitle("\n".join(notes))
            self.st_notes.set_visible(bool(notes))
            swaps = disk_swaps()
            self.st_swap.set_subtitle(", ".join(swaps) if swaps
                                      else "None: hibernation will fail")
            self.st_can.set_subtitle(logind_can("CanSuspendThenHibernate"))
            wh = battery_wh()
            self.st_bat.set_subtitle(f"{wh:.1f} Wh" if wh else "No battery found")
            self.test_btn.set_sensitive(scope != "none")
            self.remove_btn.set_sensitive(info["managed"] or os.path.exists(TEST))

        def current(self):
            return dict(scope=val(self.scope_row), lid_ac=val(self.lid_row),
                        delay_on=self.delay_sw.get_active(), delay=val(self.delay_row),
                        ac=self.ac_sw.get_active(), est=val(self.est_row),
                        pbtn=self.pb_choice(), migrate=self.mig_sw.get_active())

        def pb_choice(self):
            """Power-button value this app should manage, or None to leave it alone.
            Managed if the user changed it here, or this app already set it."""
            if not self.pb_row.get_visible():
                return None
            new = val(self.pb_row)
            return new if new != self.pb_live or new == self.pb_record else None

        # -- helpers --------------------------------------------------------
        def toast(self, msg):
            self.toasts.add_toast(Adw.Toast(title=msg))

        def alert(self, heading, body):
            d = Adw.AlertDialog(heading=heading, body=body)
            d.add_response("ok", "OK")
            d.present(self)

        def busy(self, on):
            self.page.set_sensitive(not on)

        def run_root(self, script, done):
            fd, path = tempfile.mkstemp(prefix="sth-", suffix=".sh",
                                        dir=GLib.get_user_runtime_dir())
            with os.fdopen(fd, "w") as f:
                f.write(script)
            proc = Gio.Subprocess.new(["pkexec", "/usr/bin/bash", path],
                                      Gio.SubprocessFlags.STDOUT_PIPE
                                      | Gio.SubprocessFlags.STDERR_MERGE)

            def finish(p, res):
                try:
                    _, out, _ = p.communicate_utf8_finish(res)
                except GLib.Error as e:
                    out = e.message
                os.unlink(path)
                code = p.get_exit_status() if p.get_if_exited() else -1
                done(code, out or "")
            proc.communicate_utf8_async(None, None, finish)

        def failed(self, code, out):
            if code in (126, 127):
                self.toast("Authentication cancelled")
            else:
                self.alert("That didn't work", out.strip()[-1500:] or f"Exit code {code}")

        def set_power_button(self, new):
            s = pb_settings()
            if s and new and new != self.pb_live:
                s.set_string(PB_KEY, new)

        def reset_power_button(self):
            """Undo only a value this app set and nobody has changed since."""
            s = pb_settings()
            if s and self.pb_record and gsettings_get() == self.pb_record:
                s.reset(PB_KEY)

        # -- actions --------------------------------------------------------
        def on_apply(self, *_):
            c = self.current()
            if not disk_swaps():
                self.alert("No disk swap", "Hibernation needs a disk-backed swapfile. "
                           "zram does not count. Set up the swapfile first.")
                return
            if c["delay_on"] and not c["ac"] and systemd_version() < 257:
                self.alert("systemd too old", "Staying suspended on AC power needs "
                           "systemd 257 or later. Turn on “Count down while plugged in”.")
                return
            self.busy(True)

            def done(code, out):
                self.busy(False)
                if code == 0:
                    self.set_power_button(c["pbtn"])
                    self.reload()
                    self.toast("Applied")
                else:
                    self.failed(code, out)
            self.run_root(core_install(c), done)

        def on_remove(self, *_):
            d = Adw.AlertDialog(heading="Remove suspend-then-hibernate?",
                                body="Deletes the files this app wrote. Suspend falls back "
                                     "to any other configuration, usually plain suspend. "
                                     "Your swapfile and hibernation setup stay in place.")
            d.add_response("cancel", "Cancel")
            d.add_response("remove", "Remove")
            d.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
            d.connect("response", self._remove_response)
            d.present(self)

        def _remove_response(self, _d, resp):
            if resp != "remove":
                return
            self.busy(True)

            def done(code, out):
                self.busy(False)
                if code == 0:
                    self.reset_power_button()
                    self.reload()
                    self.toast("Removed")
                else:
                    self.failed(code, out)
            self.run_root(core_uninstall(), done)

        def on_test(self, *_):
            scope = self.info["scope"][0]
            how = ("After you authenticate, the laptop suspends."
                   if scope == "all" else
                   "After you authenticate, close the lid within 30 minutes.")
            d = Adw.AlertDialog(
                heading="Run a 2-minute test?",
                body=how + " It should hibernate after about 2 minutes, when the "
                     "lid logo light stops pulsing and goes dark. Then press the "
                     "power button. The test setting is removed automatically.")
            d.add_response("cancel", "Cancel")
            d.add_response("run", "Start test")
            d.set_response_appearance("run", Adw.ResponseAppearance.SUGGESTED)
            d.connect("response", lambda _d, r: r == "run" and self._start_test(scope))
            d.present(self)

        def _start_test(self, scope):
            self.busy(True)
            self.test_btn.set_label("Waiting for resume…")

            def done(code, out):
                self.busy(False)
                self.reload()
                if code in (126, 127):
                    self.toast("Authentication cancelled")
                    return
                kv = {}
                for line in out.splitlines():
                    k, sep, v = line.partition("=")
                    if sep and k.isupper():
                        kv[k] = v
                if "ERROR" in kv:
                    self.alert("Test couldn't start", kv["ERROR"])
                elif kv.get("RESUMED") != "1":
                    self.alert("No suspend detected", "Nothing happened within 30 minutes.")
                elif kv.get("AVC") == "yes":
                    self.alert("SELinux blocked systemd-sleep",
                               "Check with: journalctl -b -g avc -u systemd-suspend.service")
                elif kv.get("RESULT") == "hibernated":
                    self.alert("It works", "The laptop suspended, woke itself after "
                               "2 minutes and hibernated. Your normal settings are back.")
                else:
                    self.alert("It didn't hibernate",
                               "The laptop resumed from plain suspend. If you woke it "
                               "within 2 minutes, run the test again and wait longer.")
            self.run_root(core_test(scope), done)

        def on_export(self, *_):
            dlg = Gtk.FileDialog(title="Choose a folder for install.sh and uninstall.sh")
            dlg.select_folder(self, None, self._export_done)

        def _export_done(self, dlg, res):
            try:
                folder = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            path, c = folder.get_path(), self.current()
            for name, text in (("install.sh", export_install(c)),
                               ("uninstall.sh", export_uninstall(c))):
                p = os.path.join(path, name)
                with open(p, "w") as f:
                    f.write(text)
                os.chmod(p, 0o755)
            self.toast(f"Saved to {path}")

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID,
                             flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

        def do_activate(self):
            (self.props.active_window or Window(self)).present()

    return App().run([sys.argv[0]])


if __name__ == "__main__":
    if "--install-app" in sys.argv:
        install_app()
    elif "--remove-app" in sys.argv:
        remove_app()
    else:
        sys.exit(run_ui())
