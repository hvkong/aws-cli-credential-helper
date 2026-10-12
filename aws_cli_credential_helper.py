#!/usr/bin/env python3
"""
aws-cli-credential-helper  v1.1
A focused credential-management helper for AWS CLI.  This tool does
exactly one thing: keep the `aws-cli-credential-helper` AWS profile signed in
and show the user how much credential time is left.

It does NOT launch any other application.  Any tool that wants to use
these credentials — an AWS SDK, another CLI, a desktop app — has its
own idea of which AWS profile to trust (set in that tool's own
settings/config), so once this helper keeps the profile below signed
in, point that tool's own settings at the SAME profile name.  That's a
one-time, per-tool, manual step outside what this helper can safely
automate on another application's behalf.

Auth modes
──────────
  IDC  : AWS Identity Center.  Uses `aws sso login`.  Deterministic —
         the SSO start URL drives the whole flow from inside the CLI.
  SAML : Direct SAML federation (e.g. an Okta app tile pointed at an
         IAM SAML Identity Provider, not Identity Center).  There is
         no AWS-hosted endpoint that routes to an arbitrary IdP, so
         the user must manually visit their IdP's AWS app link first
         to establish a federated Console session; only then can
         `aws login` (CLI >= 2.32.0) pick that session up.  This is a
         two-step, user-driven flow — see the sign-in panel below.

Credential time shown
──────────────────────
The countdown in the main window reflects the credentials AWS CLI will
actually use right now for API calls (read via `aws configure
export-credentials`), not the deeper portal/login session — and the
two auth modes behave differently underneath, per AWS's own docs:

  IDC  : the profile block this tool writes is what AWS's CLI docs
         call the "legacy non-refreshable configuration" for IAM
         Identity Center. AWS states plainly that automated token
         refresh is NOT supported for this style — once the portal
         session (which an admin can set up to 90 days) expires, there
         is no silent renewal of any kind; an interactive browser
         sign-in (the Sign In button) is required every time that
         happens, with no way to proactively extend it early.

  SAML : credentials from `aws login` are, per AWS, "automatically
         rotated by the AWS CLI... every 15 minutes," valid up to the
         signed-in IAM principal's session duration (max 12 hours).
         So the countdown here is normally much shorter than IDC's
         (often under 15 minutes) and renews itself in the background
         via the CLI's own built-in rotation — not something this app
         does. Once that 12-hour ceiling is hit, the two-step sign-in
         flow is required again, same as IDC's hard stop.

Startup modes
─────────────
  MDM mode        : config.json is pre-seeded (by Intune/Jamf) before
                    first launch.  App writes the AWS profile and
                    opens the main window ready to go.
  Self-config mode: no pre-seeded config found.  A first-run wizard
                    guides the user through setup first.

Config location
───────────────
  Windows : %APPDATA%\\aws-cli-credential-helper\\config.json
  Mac     : ~/.config/aws-cli-credential-helper/config.json

Prerequisites (never installed by this app)
─────────────────────────────────────────────
  • AWS CLI v2       https://aws.amazon.com/cli/
  • AWS CLI >=2.32.0 additionally required for SAML mode (uses `aws login`)
"""

import json
import os
import re
import sys
import subprocess
import threading
import platform
import webbrowser
from pathlib import Path
from datetime import datetime, timezone
import tkinter as tk
from tkinter import messagebox

# ── Platform ──────────────────────────────────────────────────────────────────
IS_WINDOWS = platform.system() == "Windows"
IS_MAC     = platform.system() == "Darwin"

def resource_path(relative: str) -> str:
    """Resolves a bundled file's path whether running as a plain script or
    as a PyInstaller-frozen executable (which extracts data files to a
    temp folder exposed via sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)

def set_window_icon(window: tk.Misc):
    """Best-effort — sets the titlebar/taskbar icon on Windows. Toplevel
    windows inherit this from the root automatically. No-op on Mac, where
    the Dock icon comes from the .app bundle's .icns resource set at
    build time instead.

    Note: on Windows, a withdraw()/deiconify() cycle on a window can drop
    a previously-set iconbitmap before the window is ever shown. Callers
    that withdraw a window after creating it should call this again right
    before/after deiconify() — see main()."""
    if not IS_WINDOWS:
        return
    try:
        window.iconbitmap(resource_path("aws-cli-credential-helper.ico"))
    except Exception:
        pass

AWS_CLI_DOWNLOAD          = "https://aws.amazon.com/cli/"
MIN_VERSION_FOR_AWS_LOGIN = (2, 32, 0)

# ── Palette ───────────────────────────────────────────────────────────────────
# Dark theme — AWS Squid Ink navy backgrounds with AWS orange accents.
CLR_BG         = "#1b2430"   # main window background (dark navy-slate)
CLR_FG         = "#e3e6ea"   # default body text (light gray)
CLR_HEADER_BG  = "#11151c"   # header strip — darker than body for depth
CLR_HEADER_FG  = "#ffffff"
CLR_STATUS_BG  = "#242e3b"   # card/panel background, lighter than CLR_BG
CLR_STEP_BG    = "#1f2b3d"   # SAML step boxes
CLR_ENTRY_BG   = "#2a3542"   # text entry fields
CLR_ENTRY_FG   = "#e3e6ea"
CLR_BORDER     = "#3a4654"   # subtle outline for entries
CLR_BTN        = "#FF9900"   # AWS orange — primary action buttons
CLR_BTN_FG     = "#1b2430"   # dark navy text on orange (contrast)
CLR_BTN_HOVER  = "#e68a00"   # darker orange for hover state
CLR_BTN_NEUTRAL       = "#2a3542"  # secondary/neutral buttons (Cancel, Close, etc.)
CLR_BTN_NEUTRAL_HOVER = "#374251"
CLR_GREEN      = "#3fb950"
CLR_RED        = "#ff6b6b"
CLR_AMBER      = "#f0a030"
CLR_MUTED      = "#9aa5b1"   # secondary/hint text
CLR_BANNER_BG  = "#3a2f12"   # warning banner (amber-tinted)
CLR_BANNER_FG  = "#f0c674"
CLR_INFO_BG    = "#1f2b3d"   # informational banner (blue-tinted)
CLR_INFO_FG    = "#8fb8e8"

def styled_entry(parent: tk.Widget, textvariable: tk.StringVar, width: int = 36,
                  placeholder: str = "") -> tk.Entry:
    """Consistent dark-themed text entry — plain tk.Entry defaults to a
    white background with a black cursor, which needs overriding on every
    instance to fit a dark window.

    If `placeholder` is given, a faint example string is overlaid while the
    field is empty (e.g. "https://your-org.awsapps.com/start"). It's purely
    visual — drawn with a separate Label on top of the Entry — so it never
    gets written into textvariable or saved as a real value."""
    entry = tk.Entry(
        parent, textvariable=textvariable, width=width,
        bg=CLR_ENTRY_BG, fg=CLR_ENTRY_FG, insertbackground=CLR_ENTRY_FG,
        relief="flat", highlightthickness=1,
        highlightbackground=CLR_BORDER, highlightcolor=CLR_BTN
    )
    if placeholder:
        hint = tk.Label(parent, text=placeholder, bg=CLR_ENTRY_BG, fg=CLR_MUTED,
                        anchor="w", font=("", 8))
        def refresh_hint(*_):
            if textvariable.get():
                hint.place_forget()
            else:
                hint.place(in_=entry, x=6, y=0, relheight=1.0,
                          width=-6, relwidth=1.0)
        textvariable.trace_add("write", refresh_hint)
        refresh_hint()
    return entry

DEFAULT_CONFIG = {
    "_readme": (
        "Edit this file to configure the tool. "
        "Relaunch after saving changes. "
        "This tool only manages the AWS CLI profile below — it does "
        "not launch any other application. Point whatever tool or app "
        "you want to use these credentials with at this same profile "
        "name, in that tool's own settings."
    ),
    "auth": {
        "method":      "idc",
        "profileName": "aws-cli-credential-helper",
        "idc": {
            "ssoStartUrl":       "",
            "ssoRegion":         "us-east-1",
            "accountId":         "",
            "permissionSetName": ""
        },
        "saml": {
            "idpAppUrl":         "",
            "region":            "us-east-1",
            "expectedAccountId": ""
        }
    }
}

# ── Config helpers ────────────────────────────────────────────────────────────
def get_config_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", Path.home())) / "aws-cli-credential-helper"
    else:
        base = Path.home() / ".config" / "aws-cli-credential-helper"
    base.mkdir(parents=True, exist_ok=True)
    return base

def get_config_path() -> Path:
    return get_config_dir() / "config.json"

def load_config() -> dict:
    path = get_config_path()
    if not path.exists():
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        _blocking_error(
            "Config File Error",
            f"The config file could not be read (JSON syntax error).\n\n"
            f"File: {path}\n"
            f"Error: {e}\n\n"
            f"The file will open so you can fix or delete it, then relaunch.",
            action_label="Open Config File",
            action=open_config_file
        )
        sys.exit(1)

def save_config(cfg: dict):
    try:
        with open(get_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        _blocking_error(
            "Cannot Save Config",
            f"Could not write the config file:\n{get_config_path()}\n\nError: {e}\n\n"
            f"Check that the folder is not read-only or locked by another process."
        )

def open_config_file():
    path = str(get_config_path())
    try:
        if IS_WINDOWS:
            os.startfile(path)
        else:
            subprocess.Popen(["open", "-t", path])
    except Exception:
        messagebox.showinfo("Config Location", f"Config file is at:\n{path}")

def delete_config_file() -> tuple[bool, str]:
    try:
        path = get_config_path()
        if path.exists():
            path.unlink()
        return True, ""
    except OSError as e:
        return False, str(e)

def is_config_complete(cfg: dict) -> bool:
    """True when required auth fields are filled — indicates MDM pre-seed."""
    auth   = cfg.get("auth", {})
    method = auth.get("method", "idc")
    if method == "idc":
        idc = auth.get("idc", {})
        return all([
            idc.get("ssoStartUrl", "").strip(),
            idc.get("accountId",   "").strip(),
            idc.get("permissionSetName", "").strip(),
        ])
    else:
        saml = auth.get("saml", {})
        return bool(saml.get("idpAppUrl", "").strip())

# ── Shared dialog helpers ─────────────────────────────────────────────────────
def _blocking_error(title: str, message: str,
                    action_label: str = None, action=None):
    root = tk.Tk()
    root.withdraw()

    dlg = tk.Toplevel(root)
    dlg.title(title)
    dlg.resizable(False, False)
    dlg.configure(bg=CLR_BG)
    dlg.grab_set()
    set_window_icon(dlg)

    tk.Label(dlg, text=title, bg=CLR_BG,
             font=("", 12, "bold"), fg=CLR_RED).pack(padx=20, pady=(18, 4))
    tk.Label(dlg, text=message, bg=CLR_BG, fg=CLR_FG, justify="left",
             wraplength=420).pack(padx=20, pady=(0, 16))

    btn_row = tk.Frame(dlg, bg=CLR_BG)
    btn_row.pack(pady=(0, 16))

    def close():
        dlg.destroy()
        root.destroy()

    if action_label and action:
        tk.Button(btn_row, text=action_label, bg=CLR_BTN, fg=CLR_BTN_FG,
                  activebackground=CLR_BTN_HOVER, activeforeground=CLR_BTN_FG,
                  relief="flat", cursor="hand2", padx=12, pady=6,
                  command=action).pack(side="left", padx=(0, 8))

    tk.Button(btn_row, text="Close", bg=CLR_BTN_NEUTRAL, fg=CLR_FG,
              activebackground=CLR_BTN_NEUTRAL_HOVER, activeforeground=CLR_FG,
              relief="flat", cursor="hand2", padx=12, pady=6,
              command=close).pack(side="left")

    dlg.update_idletasks()
    sw = dlg.winfo_screenwidth()
    sh = dlg.winfo_screenheight()
    dlg.geometry(f"+{(sw - dlg.winfo_width()) // 2}+{(sh - dlg.winfo_height()) // 2}")

    root.mainloop()

def _info_banner(parent: tk.Widget, message: str, bg=CLR_BANNER_BG, fg=CLR_BANNER_FG):
    f = tk.Frame(parent, bg=bg, pady=8, padx=12, relief="flat", bd=1)
    tk.Label(f, text=message, bg=bg, fg=fg,
             wraplength=440, justify="left").pack(anchor="w")
    return f

def copy_to_clipboard(widget: tk.Widget, text: str):
    widget.clipboard_clear()
    widget.clipboard_append(text)
    widget.update()

# ── AWS CLI version checks ────────────────────────────────────────────────────
def get_aws_cli_version() -> tuple | None:
    """Returns (major, minor, patch) or None if AWS CLI is not found/unparseable."""
    try:
        r = subprocess.run(["aws", "--version"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    text = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"aws-cli/(\d+)\.(\d+)\.(\d+)", text)
    if not m:
        return None
    return tuple(int(x) for x in m.groups())

def check_aws_cli_installed() -> bool:
    """Basic prerequisite gate — shown once at startup regardless of auth mode."""
    version = get_aws_cli_version()
    if version is None:
        _blocking_error(
            "AWS CLI Not Found",
            "AWS CLI v2 is required but was not found on this machine.\n\n"
            "Please install the latest AWS CLI v2, then relaunch this app.\n\n"
            "Download:  https://aws.amazon.com/cli/\n\n"
            "Note: after installing, you may need to restart your computer\n"
            "before the app can detect it.",
            action_label="Open Download Page",
            action=lambda: webbrowser.open(AWS_CLI_DOWNLOAD)
        )
        return False
    return True

def check_aws_cli_version_for_saml(show_dialog=True) -> bool:
    """SAML mode uses `aws login`, which requires AWS CLI >= 2.32.0."""
    version = get_aws_cli_version()
    if version is None or version < MIN_VERSION_FOR_AWS_LOGIN:
        current = ".".join(map(str, version)) if version else "not found"
        if show_dialog:
            messagebox.showerror(
                "AWS CLI Upgrade Required",
                f"Direct SAML sign-in uses the 'aws login' command, which requires\n"
                f"AWS CLI v2.32.0 or later.\n\n"
                f"Detected version: {current}\n\n"
                f"Please upgrade AWS CLI, then try again."
            )
        return False
    return True

# ── AWS ~/.aws/config management ──────────────────────────────────────────────
def write_aws_profile(cfg: dict) -> tuple[bool, str]:
    """
    Writes/updates only the [profile aws-cli-credential-helper] block.
    All other profiles are left exactly as they are.
    """
    aws_cfg = Path.home() / ".aws" / "config"
    aws_cfg.parent.mkdir(exist_ok=True)

    profile = cfg["auth"]["profileName"]
    method  = cfg["auth"]["method"]

    if method == "idc":
        idc   = cfg["auth"]["idc"]
        block = (
            f"[profile {profile}]\n"
            f"sso_start_url  = {idc['ssoStartUrl']}\n"
            f"sso_region     = {idc['ssoRegion']}\n"
            f"sso_account_id = {idc['accountId']}\n"
            f"sso_role_name  = {idc['permissionSetName']}\n"
            f"region         = {idc['ssoRegion']}\n"
            f"output         = json\n"
        )
    else:
        saml  = cfg["auth"]["saml"]
        # aws login determines account/role via the browser session the user
        # picks manually — nothing SAML-specific belongs in this file.
        block = (
            f"[profile {profile}]\n"
            f"region = {saml['region']}\n"
            f"output = json\n"
        )

    try:
        existing  = aws_cfg.read_text(encoding="utf-8") if aws_cfg.exists() else ""
        lines     = existing.splitlines(keepends=True)
        out, skip = [], False
        for line in lines:
            if line.strip() == f"[profile {profile}]":
                skip = True
                continue
            if skip and line.startswith("["):
                skip = False
            if not skip:
                out.append(line)

        result = "".join(out).rstrip("\n") + "\n\n" + block
        aws_cfg.write_text(result.lstrip("\n"), encoding="utf-8")
        return True, ""
    except OSError as e:
        return False, str(e)

# ── Session management ────────────────────────────────────────────────────────
SESSION_ACTIVE        = "active"
SESSION_WRONG_ACCOUNT = "wrong_account"
SESSION_EXPIRED       = "expired"
SESSION_NO_PROFILE    = "no_profile"
SESSION_NETWORK       = "network_error"
SESSION_UNKNOWN       = "unknown"

def check_session(profile: str, expected_account_id: str | None, callback):
    """
    Non-blocking session check.
    Calls callback(state, expiry_str|None, actual_account_id|None).
    """
    def _run():
        try:
            r = subprocess.run(
                ["aws", "sts", "get-caller-identity", "--profile", profile, "--output", "json"],
                capture_output=True, text=True, timeout=10
            )
        except subprocess.TimeoutExpired:
            callback(SESSION_NETWORK, None, None)
            return
        except FileNotFoundError:
            callback(SESSION_UNKNOWN, None, None)
            return

        if r.returncode == 0:
            account = None
            try:
                account = json.loads(r.stdout).get("Account")
            except Exception:
                pass

            countdown = _get_credential_countdown(profile)

            if expected_account_id and account and str(account) != str(expected_account_id):
                callback(SESSION_WRONG_ACCOUNT, countdown, account)
            else:
                callback(SESSION_ACTIVE, countdown, account)
            return

        err = (r.stderr or "").lower()
        if "could not load credentials" in err or "profile" in err:
            callback(SESSION_NO_PROFILE, None, None)
        elif "connect" in err or "endpoint" in err or "network" in err:
            callback(SESSION_NETWORK, None, None)
        else:
            callback(SESSION_EXPIRED, None, None)

    threading.Thread(target=_run, daemon=True).start()

def get_credential_expiry(profile: str) -> datetime | None:
    """
    Returns the Expiration of the credentials AWS CLI will actually use
    right now for API calls under this profile — the real "CLI session"
    ceiling, not the IAM Identity Center portal session (which can last
    up to 90 days and is a separate, much longer-lived thing). Works the
    same way for both IDC and direct SAML (`aws login`) profiles, since
    it reads whatever credentials are currently resolved regardless of
    which provider produced them.
    """
    try:
        r = subprocess.run(
            ["aws", "configure", "export-credentials",
             "--profile", profile, "--format", "process"],
            capture_output=True, text=True, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        data    = json.loads(r.stdout)
        exp_str = data.get("Expiration")
        if not exp_str:
            return None
        return datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
    except Exception:
        return None

def _format_countdown(expiry: datetime) -> str | None:
    delta = expiry - datetime.now(timezone.utc)
    secs  = int(delta.total_seconds())
    if secs <= 0:
        return None
    m = secs // 60
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"

def _get_credential_countdown(profile: str) -> str | None:
    """Best-effort — returns None (no countdown shown) if the CLI version
    doesn't support `export-credentials` or the call otherwise fails."""
    expiry = get_credential_expiry(profile)
    return _format_countdown(expiry) if expiry else None

def trigger_idc_auth(cfg: dict, callback):
    """IDC mode: `aws sso login` drives the whole flow deterministically."""
    profile = cfg["auth"]["profileName"]

    def _run():
        try:
            r = subprocess.run(
                ["aws", "sso", "login", "--profile", profile],
                capture_output=True, text=True
            )
        except FileNotFoundError:
            callback(False, "'aws' command not found. Is the AWS CLI installed and in PATH?")
            return
        if r.returncode == 0:
            callback(True, "")
        else:
            err = (r.stderr or r.stdout or "").strip()
            callback(False, err or "Authentication did not complete.")

    threading.Thread(target=_run, daemon=True).start()

def trigger_aws_login(cfg: dict, callback):
    """
    SAML mode, Step 2: `aws login` picks up whatever Console session is
    active in the user's default browser (ideally the one they just used
    for Step 1) and lets them pick the role/session.
    """
    profile = cfg["auth"]["profileName"]

    def _run():
        try:
            r = subprocess.run(
                ["aws", "login", "--profile", profile],
                capture_output=True, text=True
            )
        except FileNotFoundError:
            callback(False, "'aws' command not found. Is the AWS CLI installed and in PATH?")
            return
        if r.returncode == 0:
            callback(True, "")
        else:
            err = (r.stderr or r.stdout or "").strip()
            callback(False, err or "Sign-in did not complete.")

    threading.Thread(target=_run, daemon=True).start()

# ── First-run Setup Wizard ────────────────────────────────────────────────────
class SetupWizard(tk.Toplevel):
    def __init__(self, parent: tk.Tk, cfg: dict, on_complete):
        super().__init__(parent)
        self.title("AWS CLI Credential Helper — First-time Setup")
        self.resizable(False, False)
        self.configure(bg=CLR_BG)
        set_window_icon(self)
        self.cfg        = cfg
        self.on_complete = on_complete
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.grab_set()

        banner = _info_banner(
            self,
            "Welcome!  No configuration was found on this machine.\n"
            "Please fill in your AWS authentication details below, then click Save.\n"
            "You can change these settings at any time from the main window."
        )
        banner.pack(fill="x", padx=12, pady=(12, 0))

        self._sw = SettingsContent(self, cfg, on_save=self._on_save, on_cancel=self._on_cancel)
        self._sw.pack(fill="both", expand=True)

        self.update_idletasks()
        self.geometry(f"{max(520, self.winfo_reqwidth())}x{self.winfo_reqheight()}")
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{(sw - self.winfo_width()) // 2}+{(sh - self.winfo_height()) // 2}")

    def _on_save(self, new_cfg: dict):
        self.destroy()
        self.on_complete(new_cfg)

    def _on_cancel(self):
        if messagebox.askyesno(
            "Exit Setup",
            "Setup is not complete. The app cannot run without configuration.\n\nExit?"
        ):
            self.destroy()
            sys.exit(0)

# ── Settings content (Authentication only — shared by Settings window and Wizard) ─
class SettingsContent(tk.Frame):
    def __init__(self, parent, cfg: dict, on_save, on_cancel=None):
        super().__init__(parent, bg=CLR_BG)
        self.cfg       = cfg
        self.on_save   = on_save
        self.on_cancel = on_cancel

        self._build_auth_section()

        foot = tk.Frame(self, bg=CLR_BG)
        foot.pack(fill="x", padx=16, pady=(0, 14))

        tk.Label(foot, text=f"Config file: {get_config_path()}",
                 bg=CLR_BG, fg=CLR_MUTED, font=("", 7),
                 wraplength=480, justify="left").pack(anchor="w", pady=(0, 6))

        btn_row = tk.Frame(foot, bg=CLR_BG)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="Open Config File", bg=CLR_BG, relief="flat", fg=CLR_MUTED,
                  activebackground=CLR_BG, activeforeground=CLR_FG,
                  cursor="hand2", command=open_config_file).pack(side="left")
        tk.Button(btn_row, text="Delete Config…", bg=CLR_BG, relief="flat", fg=CLR_RED,
                  activebackground=CLR_BG, activeforeground=CLR_RED,
                  cursor="hand2", command=self._delete_config).pack(side="left", padx=(10, 0))
        if on_cancel:
            tk.Button(btn_row, text="Cancel", width=9, bg=CLR_BTN_NEUTRAL, fg=CLR_FG,
                      activebackground=CLR_BTN_NEUTRAL_HOVER, activeforeground=CLR_FG,
                      relief="flat", command=on_cancel).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Save", width=9, bg=CLR_BTN, fg=CLR_BTN_FG,
                  activebackground=CLR_BTN_HOVER, activeforeground=CLR_BTN_FG,
                  relief="flat", cursor="hand2", command=self._save).pack(side="right")

    def _delete_config(self):
        if not messagebox.askyesno(
            "Delete Config File",
            "This permanently deletes the config file and resets all settings.\n\n"
            "The app will close — relaunch it to go through first-time setup again.\n\n"
            "Continue?"
        ):
            return
        ok, err = delete_config_file()
        if not ok:
            messagebox.showerror("Delete Failed", f"Could not delete config file:\n{err}")
            return
        messagebox.showinfo("Config Deleted", "Config file deleted. The app will now close.")
        sys.exit(0)

    def _build_auth_section(self):
        outer = tk.Frame(self, bg=CLR_BG, padx=16, pady=14)
        outer.pack(fill="both")
        auth = self.cfg.get("auth", {})

        tk.Label(outer, text="Authentication Method", bg=CLR_BG, fg=CLR_FG,
                 font=("", 10, "bold")).pack(anchor="w", pady=(0, 6))

        self._auth_method = tk.StringVar(value=auth.get("method", "idc"))
        rb = tk.Frame(outer, bg=CLR_BG)
        rb.pack(anchor="w", pady=(0, 6))
        # indicatoron=0 turns this into a flat chip whose own background IS
        # the selected/unselected color — deterministic across platforms.
        # (The classic circular indicator's selectcolor is only *supposed*
        # to apply when selected, but rendering this is inconsistent across
        # Tk builds, which is why both chips could show the same color.)
        tk.Radiobutton(rb, text="AWS Identity Center (IDC)", indicatoron=0,
                       bg=CLR_BTN_NEUTRAL, fg=CLR_FG,
                       selectcolor=CLR_GREEN, activebackground=CLR_BTN_NEUTRAL_HOVER,
                       activeforeground=CLR_FG, relief="flat", highlightthickness=0,
                       padx=10, pady=5,
                       variable=self._auth_method, value="idc",
                       command=self._toggle_auth).pack(side="left", padx=(0, 8))
        tk.Radiobutton(rb, text="Direct SAML federation", indicatoron=0,
                       bg=CLR_BTN_NEUTRAL, fg=CLR_FG,
                       selectcolor=CLR_GREEN, activebackground=CLR_BTN_NEUTRAL_HOVER,
                       activeforeground=CLR_FG, relief="flat", highlightthickness=0,
                       padx=10, pady=5,
                       variable=self._auth_method, value="saml",
                       command=self._toggle_auth).pack(side="left")

        # ── IDC frame ─────────────────────────────────────────────────────────
        self._idc_frame = tk.LabelFrame(outer, text="Identity Center Settings",
                                         bg=CLR_BG, fg=CLR_FG, padx=12, pady=8)
        idc = auth.get("idc", {})
        self._idc = {}

        tk.Label(self._idc_frame, text="SSO Start URL", width=18, anchor="w",
                 bg=CLR_BG, fg=CLR_FG).grid(row=0, column=0, sticky="w", pady=3)
        v = tk.StringVar(value=idc.get("ssoStartUrl", ""))
        styled_entry(self._idc_frame, v,
                     placeholder="https://your-org.awsapps.com/start"
                    ).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self._idc["ssoStartUrl"] = v

        tk.Label(self._idc_frame, text="SSO Region", width=18, anchor="w",
                 bg=CLR_BG, fg=CLR_FG).grid(row=1, column=0, sticky="w", pady=3)
        v = tk.StringVar(value=idc.get("ssoRegion", "us-east-1"))
        styled_entry(self._idc_frame, v).grid(row=1, column=1, sticky="ew", padx=(6, 0))
        self._idc["ssoRegion"] = v

        tk.Label(self._idc_frame, text="Account ID", width=18, anchor="w",
                 bg=CLR_BG, fg=CLR_FG).grid(row=2, column=0, sticky="w", pady=3)
        v = tk.StringVar(value=idc.get("accountId", ""))
        styled_entry(self._idc_frame, v,
                     placeholder="123456789012"
                    ).grid(row=2, column=1, sticky="ew", padx=(6, 0))
        self._idc["accountId"] = v
        tk.Label(self._idc_frame, text="AWS account number this permission set grants access to",
                 bg=CLR_BG, fg=CLR_MUTED, font=("", 8)).grid(row=3, column=1, sticky="w", padx=(6, 0))

        tk.Label(self._idc_frame, text="Permission Set Name", width=18, anchor="w",
                 bg=CLR_BG, fg=CLR_FG).grid(row=4, column=0, sticky="w", pady=(8, 3))
        v = tk.StringVar(value=idc.get("permissionSetName", ""))
        styled_entry(self._idc_frame, v,
                     placeholder="PowerUserAccess"
                    ).grid(row=4, column=1, sticky="ew", padx=(6, 0))
        self._idc["permissionSetName"] = v
        tk.Label(self._idc_frame,
                 text="As shown in your AWS access portal, e.g. PowerUserAccess\n"
                      "(this is the Permission Set, not an IAM role name)",
                 bg=CLR_BG, fg=CLR_MUTED, font=("", 8), justify="left").grid(
                     row=5, column=1, sticky="w", padx=(6, 0), pady=(0, 6))

        # ── SAML frame ────────────────────────────────────────────────────────
        self._saml_frame = tk.LabelFrame(outer, text="Direct SAML Federation Settings",
                                          bg=CLR_BG, fg=CLR_FG, padx=12, pady=8)
        saml = auth.get("saml", {})
        self._saml = {}

        tk.Label(self._saml_frame, text="IdP App URL", width=18, anchor="w",
                 bg=CLR_BG, fg=CLR_FG).grid(row=0, column=0, sticky="w", pady=3)
        v = tk.StringVar(value=saml.get("idpAppUrl", ""))
        styled_entry(self._saml_frame, v,
                     placeholder="https://your-org.okta.com/app/amazon_aws/exxxxxx/sso/saml"
                    ).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self._saml["idpAppUrl"] = v
        tk.Label(self._saml_frame,
                 text="The SSO app link from your identity provider (e.g. an\n"
                      "Okta app tile URL) that signs the user into this AWS account",
                 bg=CLR_BG, fg=CLR_MUTED, font=("", 8), justify="left").grid(
                     row=1, column=1, sticky="w", padx=(6, 0), pady=(0, 6))

        tk.Label(self._saml_frame, text="AWS Region", width=18, anchor="w",
                 bg=CLR_BG, fg=CLR_FG).grid(row=2, column=0, sticky="w", pady=3)
        v = tk.StringVar(value=saml.get("region", "us-east-1"))
        styled_entry(self._saml_frame, v).grid(row=2, column=1, sticky="ew", padx=(6, 0))
        self._saml["region"] = v

        tk.Label(self._saml_frame, text="Expected Account ID", width=18, anchor="w",
                 bg=CLR_BG, fg=CLR_FG).grid(row=3, column=0, sticky="w", pady=3)
        v = tk.StringVar(value=saml.get("expectedAccountId", ""))
        styled_entry(self._saml_frame, v,
                     placeholder="123456789012"
                    ).grid(row=3, column=1, sticky="ew", padx=(6, 0))
        self._saml["expectedAccountId"] = v
        tk.Label(self._saml_frame,
                 text="Used to verify the user picked the right account after signing\n"
                      "in — if it doesn't match, the app will ask them to try again",
                 bg=CLR_BG, fg=CLR_MUTED, font=("", 8), justify="left").grid(
                     row=4, column=1, sticky="w", padx=(6, 0), pady=(0, 6))

        note = _info_banner(
            self._saml_frame,
            "Requires AWS CLI v2.32.0+ (uses the 'aws login' command).",
            bg=CLR_INFO_BG, fg=CLR_INFO_FG
        )
        note.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        # Profile name
        prow = tk.Frame(outer, bg=CLR_BG)
        prow.pack(fill="x", pady=(10, 0))
        tk.Label(prow, text="AWS Profile Name", bg=CLR_BG, fg=CLR_FG,
                 width=17, anchor="w").pack(side="left")
        self._profile = tk.StringVar(value=auth.get("profileName", "aws-cli-credential-helper"))
        styled_entry(prow, self._profile, width=28).pack(side="left", padx=(6, 0))

        reminder = _info_banner(
            outer,
            "Remember: this tool only manages the AWS profile above. Open the "
            "settings of whatever tool or application should use these "
            "credentials and point it at this same profile name — that step "
            "has to be done once, per tool, inside that tool's own "
            "configuration.",
            bg=CLR_INFO_BG, fg=CLR_INFO_FG
        )
        reminder.pack(fill="x", pady=(14, 0))

        self._toggle_auth()

    def _toggle_auth(self):
        if self._auth_method.get() == "idc":
            self._saml_frame.pack_forget()
            self._idc_frame.pack(fill="x", pady=(0, 8))
        else:
            self._idc_frame.pack_forget()
            self._saml_frame.pack(fill="x", pady=(0, 8))
        # The IDC and SAML panels aren't the same height, and the window is
        # non-resizable with a size fixed once at creation — without this,
        # switching to whichever panel is taller clips its Save button
        # below the window edge, same failure mode as the original bug.
        top = self.winfo_toplevel()
        top.update_idletasks()
        top.geometry(f"{max(520, top.winfo_reqwidth())}x{top.winfo_reqheight()}")

    # ── Save ──────────────────────────────────────────────────────────────────
    def _save(self):
        cfg = json.loads(json.dumps(self.cfg))
        method = self._auth_method.get()

        if method == "saml" and not check_aws_cli_version_for_saml(show_dialog=True):
            return   # block save until CLI is upgraded, dialog already shown

        cfg["auth"]["method"]      = method
        cfg["auth"]["profileName"] = self._profile.get().strip()
        cfg["auth"]["idc"]         = {k: v.get().strip() for k, v in self._idc.items()}
        cfg["auth"]["saml"]        = {k: v.get().strip() for k, v in self._saml.items()}

        save_config(cfg)

        ok, err = write_aws_profile(cfg)
        if not ok:
            messagebox.showwarning(
                "AWS Config Warning",
                f"Settings were saved but the AWS profile could not be written to "
                f"~/.aws/config:\n\n{err}\n\n"
                f"You can add the profile block manually or check folder permissions."
            )

        self.on_save(cfg)

# ── Settings Window (standalone) ──────────────────────────────────────────────
class SettingsWindow(tk.Toplevel):
    def __init__(self, parent, cfg: dict, on_save):
        super().__init__(parent)
        self.title("Settings — AWS CLI Credential Helper")
        self.resizable(False, False)
        self.configure(bg=CLR_BG)
        set_window_icon(self)
        self.grab_set()

        SettingsContent(self, cfg,
                        on_save=lambda c: (on_save(c), self.destroy()),
                        on_cancel=self.destroy).pack(fill="both", expand=True)

        self.update_idletasks()
        self.geometry(f"{max(520, self.winfo_reqwidth())}x{self.winfo_reqheight()}")
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

# ── Main Window ───────────────────────────────────────────────────────────────
SESSION_MESSAGES = {
    SESSION_ACTIVE:        (CLR_GREEN, "Signed in"),
    SESSION_WRONG_ACCOUNT: (CLR_RED,   "Signed in to the wrong AWS account"),
    SESSION_EXPIRED:       (CLR_RED,   "Credentials expired — sign in to continue"),
    SESSION_NO_PROFILE:    (CLR_RED,   "AWS profile not found — check Settings → Authentication"),
    SESSION_NETWORK:       (CLR_AMBER, "Cannot reach AWS — check your network or VPN"),
    SESSION_UNKNOWN:       (CLR_AMBER, "Credential status unknown"),
}

SAML_TIP_TEXT = (
    "Clicking \"Sign in via SSO\" will launch your default browser, where you "
    "are expected to log in to your organization's SSO portal (the same way "
    "you normally sign in to AWS today). If you are currently using Incognito "
    "or a browser other than your default, use Copy Link instead and paste it "
    "into the Incognito window or browser you are using."
)

class MainWindow:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg  = cfg
        root.title("AWS CLI Credential Helper")
        root.resizable(False, False)
        root.configure(bg=CLR_BG)
        self._session_state = SESSION_UNKNOWN
        self._account       = None
        self._build()
        self._check_session()

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build(self):
        hdr = tk.Frame(self.root, bg=CLR_HEADER_BG, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="  AWS CLI Credential Helper", bg=CLR_HEADER_BG,
                 fg=CLR_HEADER_FG, font=("", 12, "bold")).pack(side="left")
        tk.Button(hdr, text="⚙  Settings", bg=CLR_HEADER_BG, fg="#aaaaaa",
                  relief="flat", cursor="hand2", activebackground=CLR_HEADER_BG,
                  activeforeground="#ffffff", font=("", 9),
                  command=self._open_settings).pack(side="right", padx=10)

        # Session status card
        sf = tk.Frame(self.root, bg=CLR_STATUS_BG, padx=14, pady=10)
        sf.pack(fill="x", padx=10, pady=(10, 4))

        row1 = tk.Frame(sf, bg=CLR_STATUS_BG)
        row1.pack(fill="x")
        self._dot = tk.Label(row1, text="●", fg=CLR_AMBER,
                              bg=CLR_STATUS_BG, font=("", 16))
        self._dot.pack(side="left")
        self._status_lbl = tk.Label(row1, text="Checking credentials…",
                                     bg=CLR_STATUS_BG, fg=CLR_FG, font=("", 10))
        self._status_lbl.pack(side="left", padx=(6, 0))
        tk.Button(row1, text="↻  Refresh", relief="flat", bg=CLR_STATUS_BG,
                  activebackground=CLR_STATUS_BG, activeforeground=CLR_FG,
                  fg=CLR_MUTED, cursor="hand2", font=("", 9),
                  command=self._check_session).pack(side="right")

        self._profile_lbl = tk.Label(
            sf, bg=CLR_STATUS_BG, fg=CLR_MUTED, font=("", 9),
            text=f"Profile: {self.cfg['auth']['profileName']}"
        )
        self._profile_lbl.pack(anchor="w", padx=(22, 0))

        self._account_lbl = tk.Label(
            sf, bg=CLR_STATUS_BG, fg=CLR_MUTED, font=("", 9), text=""
        )
        self._account_lbl.pack(anchor="w", padx=(22, 0))

        # Sign-in area (contents depend on auth mode + session state)
        self._signin_frame = tk.Frame(self.root, bg=CLR_BG, padx=14)
        self._signin_frame.pack(fill="x", pady=(8, 10))
        self._rebuild_signin_area()

    def _is_saml(self) -> bool:
        return self.cfg["auth"]["method"] == "saml"

    def _expected_account(self) -> str | None:
        if self._is_saml():
            return self.cfg["auth"]["saml"].get("expectedAccountId") or None
        return self.cfg["auth"]["idc"].get("accountId") or None

    # ── Sign-in area ──────────────────────────────────────────────────────────
    def _rebuild_signin_area(self):
        for w in self._signin_frame.winfo_children():
            w.destroy()

        signed_in = self._session_state == SESSION_ACTIVE

        if self._is_saml():
            if signed_in:
                row = tk.Frame(self._signin_frame, bg=CLR_BG)
                row.pack(fill="x")
                tk.Label(row, text="✓ Signed in", bg=CLR_BG,
                         fg=CLR_GREEN, font=("", 10, "bold")).pack(side="left")
                tk.Button(row, text="Switch Account / Re-authenticate", bg=CLR_BG, relief="flat",
                          fg=CLR_MUTED, activebackground=CLR_BG, activeforeground=CLR_FG,
                          cursor="hand2", font=("", 9),
                          command=self._force_show_saml_steps).pack(side="right")
                tk.Label(self._signin_frame,
                         text="You can close the Credential Helper app now. "
                              "You'll only need to come back and sign in again "
                              "once your full sign-in session ends (your "
                              "organization sets this) — this app will show "
                              "'Credentials expired' when that happens, and "
                              "the two-step sign-in will be required again.",
                         bg=CLR_BG, fg=CLR_MUTED, font=("", 8),
                         wraplength=300, justify="left").pack(anchor="w", pady=(6, 0))
            else:
                self._build_saml_steps(self._signin_frame)
        else:
            btn = tk.Button(
                self._signin_frame, text="Sign In", font=("", 11), padx=10, pady=9,
                bg=CLR_BTN, fg=CLR_BTN_FG, activebackground=CLR_BTN_HOVER,
                activeforeground=CLR_BTN_FG, relief="flat", cursor="hand2",
                command=self._idc_sign_in
            )
            btn.pack(fill="x")
            if signed_in:
                tk.Label(self._signin_frame,
                         text="You can close the Credential Helper app now. Take "
                              "note that the AWS CLI credential will only be "
                              "usable until the expiration time indicated. Open "
                              "this app again and click Sign In above to refresh "
                              "your credential when needed.",
                         bg=CLR_BG, fg=CLR_MUTED, font=("", 8),
                         wraplength=300, justify="left").pack(anchor="w", pady=(6, 0))
                tk.Label(self._signin_frame,
                         text="Please note that the AWS CLI Credential expiration "
                              "is configurable and set by your organization.",
                         bg=CLR_BG, fg=CLR_MUTED, font=("", 8),
                         wraplength=300, justify="left").pack(anchor="w", pady=(6, 0))

    def _force_show_saml_steps(self):
        for w in self._signin_frame.winfo_children():
            w.destroy()
        self._build_saml_steps(self._signin_frame)

    def _build_saml_steps(self, parent):
        banner = _info_banner(parent, "⚠ Sign-In Required — complete both steps below.")
        banner.pack(fill="x", pady=(0, 6))

        tip = tk.Label(parent, text=SAML_TIP_TEXT, bg=CLR_BG,
                        fg=CLR_MUTED, font=("", 8), justify="left", wraplength=320)
        tip.pack(anchor="w", pady=(0, 8))

        step1 = tk.Frame(parent, bg=CLR_STEP_BG, padx=10, pady=8)
        step1.pack(fill="x", pady=(0, 6))
        tk.Label(step1, text="Step 1", bg=CLR_STEP_BG, font=("", 8, "bold"),
                 fg=CLR_MUTED).pack(anchor="w")
        row = tk.Frame(step1, bg=CLR_STEP_BG)
        row.pack(fill="x", pady=(2, 0))
        tk.Button(row, text="Sign in via SSO", bg=CLR_BTN, fg=CLR_BTN_FG,
                  activebackground=CLR_BTN_HOVER, activeforeground=CLR_BTN_FG,
                  relief="flat", cursor="hand2", padx=8, pady=5,
                  command=self._saml_step1).pack(side="left")
        self._copy_btn = tk.Button(row, text="⎘ Copy Link", bg=CLR_STEP_BG, fg=CLR_FG,
                  activebackground=CLR_STEP_BG, activeforeground=CLR_FG,
                  relief="flat", cursor="hand2", padx=8, pady=5,
                  command=self._saml_copy_link)
        self._copy_btn.pack(side="left", padx=(6, 0))

        step2 = tk.Frame(parent, bg=CLR_STEP_BG, padx=10, pady=8)
        step2.pack(fill="x", pady=(0, 4))
        tk.Label(step2, text="Step 2", bg=CLR_STEP_BG, font=("", 8, "bold"),
                 fg=CLR_MUTED).pack(anchor="w")
        tk.Button(step2, text="Finish Sign-In", bg=CLR_BTN, fg=CLR_BTN_FG,
                  activebackground=CLR_BTN_HOVER, activeforeground=CLR_BTN_FG,
                  relief="flat", cursor="hand2", padx=8, pady=5,
                  command=self._saml_step2).pack(anchor="w", pady=(2, 0))

    # ── Session ───────────────────────────────────────────────────────────────
    def _check_session(self):
        self._status_lbl.config(text="Checking credentials…")
        check_session(self.cfg["auth"]["profileName"], self._expected_account(),
                      self._on_session_result)

    def _on_session_result(self, state: str, expiry: str | None, account: str | None):
        colour, text = SESSION_MESSAGES.get(state, (CLR_AMBER, "Credential status unknown"))
        if state == SESSION_ACTIVE:
            # SAML/aws login credentials auto-rotate every ~15 min in the
            # background (per AWS), so a short-lived countdown here would be
            # stale almost immediately and easy to misread as a problem.
            # IDC's countdown is a single, stable deadline — safe to show.
            if expiry and not self._is_saml():
                text = f"Signed in — credentials valid for {expiry}"
            else:
                text = "Signed in"
        if state == SESSION_WRONG_ACCOUNT:
            expected = self._expected_account()
            text = f"Signed in to account {account} — expected {expected}"

        def update():
            self._session_state = state
            self._account        = account
            self._dot.config(fg=colour)
            self._status_lbl.config(text=text)
            self._account_lbl.config(text=f"Account: {account}" if account else "")
            self._rebuild_signin_area()
        self.root.after(0, update)

    # ── IDC sign-in ───────────────────────────────────────────────────────────
    def _idc_sign_in(self):
        self._dot.config(fg=CLR_AMBER)
        self._status_lbl.config(text="Signing in via browser…")

        def on_done(success: bool, err_msg: str):
            def after():
                if success:
                    self._check_session()
                else:
                    self._dot.config(fg=CLR_RED)
                    self._status_lbl.config(text="Sign-in did not complete")
                    messagebox.showerror(
                        "Authentication Failed",
                        f"Sign-in did not complete successfully.\n\n"
                        f"Details: {err_msg}\n\n"
                        f"Please try again. If the problem persists, check your\n"
                        f"Settings → Authentication details."
                    )
            self.root.after(0, after)

        trigger_idc_auth(self.cfg, on_done)

    # ── SAML two-step flow ───────────────────────────────────────────────────
    def _saml_step1(self):
        url = self.cfg["auth"]["saml"].get("idpAppUrl", "").strip()
        if not url:
            messagebox.showerror(
                "Not Configured",
                "No IdP App URL is set.\n\nOpen Settings → Authentication and fill in the IdP App URL."
            )
            return
        webbrowser.open(url)
        self._status_lbl.config(text="Complete sign-in in your browser, then click Step 2.")

    def _saml_copy_link(self):
        url = self.cfg["auth"]["saml"].get("idpAppUrl", "").strip()
        if not url:
            messagebox.showerror(
                "Not Configured",
                "No IdP App URL is set.\n\nOpen Settings → Authentication and fill in the IdP App URL."
            )
            return
        copy_to_clipboard(self.root, url)
        orig = self._copy_btn.cget("text")
        self._copy_btn.config(text="Copied!")
        self.root.after(1500, lambda: self._copy_btn.config(text=orig))

    def _saml_step2(self):
        if not check_aws_cli_version_for_saml(show_dialog=True):
            return
        self._dot.config(fg=CLR_AMBER)
        self._status_lbl.config(text="Finishing sign-in — check your browser…")

        def on_done(success, err_msg):
            def after():
                if success:
                    self._check_session()
                else:
                    self._dot.config(fg=CLR_RED)
                    self._status_lbl.config(text="Sign-in did not complete")
                    messagebox.showerror(
                        "Sign-In Failed",
                        f"'aws login' did not complete successfully.\n\n"
                        f"Details: {err_msg}\n\n"
                        f"If you used Copy Link with a non-default browser, make sure "
                        f"you complete Step 2 in that SAME browser."
                    )
            self.root.after(0, after)

        trigger_aws_login(self.cfg, on_done)

    # ── Settings ──────────────────────────────────────────────────────────────
    def _open_settings(self):
        def on_save(new_cfg):
            self.cfg = new_cfg
            self._profile_lbl.config(
                text=f"Profile: {new_cfg['auth']['profileName']}")
            self._check_session()
        SettingsWindow(self.root, self.cfg, on_save)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    if not check_aws_cli_installed():
        return

    cfg  = load_config()
    root = tk.Tk()
    root.configure(bg=CLR_BG)
    set_window_icon(root)

    def open_main(final_cfg):
        set_window_icon(root)   # re-apply: withdraw()/deiconify() can drop it on Windows
        root.geometry("380x400")
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(
            f"+{(sw - root.winfo_width()) // 2}+{(sh - root.winfo_height()) // 2}"
        )
        MainWindow(root, final_cfg)

    if is_config_complete(cfg):
        if cfg["auth"]["method"] == "saml":
            check_aws_cli_version_for_saml(show_dialog=True)  # warn but don't block startup

        ok, err = write_aws_profile(cfg)
        if not ok:
            messagebox.showwarning(
                "AWS Config Warning",
                f"Could not write the AWS profile to ~/.aws/config:\n\n{err}\n\n"
                f"Authentication may fail until this is resolved.\n"
                f"Check file permissions on your ~/.aws directory."
            )
        root.withdraw()
        root.after(0, lambda: (root.deiconify(), open_main(cfg)))
    else:
        root.withdraw()
        SetupWizard(root, cfg, on_complete=lambda c: (root.deiconify(), open_main(c)))

    root.mainloop()

if __name__ == "__main__":
    main()
