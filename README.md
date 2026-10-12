# aws-cli-credential-helper — Build & Deploy Guide

## What this tool does

This tool manages exactly one thing: the `aws-cli-credential-helper` AWS CLI
profile. It checks whether the AWS CLI is installed, drives sign-in via
either AWS Identity Center (`aws sso login`) or direct SAML federation
(`aws login`), shows credential status, and validates the signed-in
account matches what's expected.

It does not launch any other application. Any tool that wants to use
these credentials — an AWS SDK, another CLI, a desktop app — reads its
own settings to decide which AWS profile to trust, so once this helper
keeps the profile above signed in, point that tool's own settings at the
same profile name. That's a one-time, per-tool, manual step outside what
this helper can safely automate on another application's behalf.


---

## Prerequisites

- Python 3.10+ installed (python.org) — only needed to build, not to run
  the compiled binary
- AWS CLI v2 installed and in PATH on the machine running the tool
  - Direct SAML mode additionally requires AWS CLI **v2.32.0+** (uses the
    `aws login` command)

---

## Step 1: Install dependencies

```bash
pip install pyinstaller
```

That's it — tkinter is built into Python, no other packages needed.

If you're using `uv`:
```bash
uv venv --python 3.12
# Windows: .venv\Scripts\activate      Mac: source .venv/bin/activate
uv pip install pyinstaller
```

---

## Step 2: Test before compiling (optional but recommended)

```bash
python aws_cli_credential_helper.py
```

The app opens, creates a default config if none exists, and walks you
through the first-run setup wizard. Config file location:
- Windows: `%APPDATA%\aws-cli-credential-helper\config.json`
- Mac:     `~/.config/aws-cli-credential-helper/config.json`

Verify the AWS profile was written correctly:
```bash
aws sts get-caller-identity --profile aws-cli-credential-helper
```

---

## App icon

`aws-cli-credential-helper.ico` (Windows) and the
`aws-cli-credential-helper.iconset` folder (Mac — run
`iconutil -c icns aws-cli-credential-helper.iconset` on an actual Mac to
produce `aws-cli-credential-helper.icns`, since `iconutil` is a Mac-only
tool) ship alongside the script. Keep both in the same folder as
`aws_cli_credential_helper.py` before compiling — the commands below
bundle the `.ico` into the executable so the running app's
titlebar/taskbar icon resolves correctly, not just the file icon shown
in Explorer.

---

## Step 3: Compile — Windows

```bash
pyinstaller --onefile --windowed --name "aws-cli-credential-helper" ^
    --icon=aws-cli-credential-helper.ico ^
    --add-data "aws-cli-credential-helper.ico;." ^
    aws_cli_credential_helper.py
```

Output: `dist\aws-cli-credential-helper.exe`

### Code signing (required for MDM deployment)
```bash
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td sha256 ^
    /f "YourCert.pfx" /p "CertPassword" dist\aws-cli-credential-helper.exe
```

### Create a desktop shortcut via Intune / PowerShell
```powershell
$exe = "C:\Program Files\aws-cli-credential-helper\aws-cli-credential-helper.exe"
$WshShell = New-Object -ComObject WScript.Shell
$sc = $WshShell.CreateShortcut("$env:PUBLIC\Desktop\AWS Sign-In.lnk")
$sc.TargetPath = $exe
$sc.Save()
```

Only one shortcut is needed — this tool does one job.

---

## Step 4: Compile — Mac

First build the `.icns` from the provided iconset (one-time, on a Mac):
```bash
iconutil -c icns aws-cli-credential-helper.iconset
```

```bash
pyinstaller --onedir --windowed --name "aws-cli-credential-helper" \
    --icon=aws-cli-credential-helper.icns \
    aws_cli_credential_helper.py
```

Output: `dist/aws-cli-credential-helper.app`. Use `--onedir` (not
`--onefile`) on Mac so the result is a proper `.app` bundle rather than
a bare binary.

### Code signing (required for Gatekeeper / Jamf)
```bash
codesign --deep --force --verify --verbose \
    --sign "Developer ID Application: Your Company (TEAMID)" \
    dist/aws-cli-credential-helper.app

xcrun notarytool submit dist/aws-cli-credential-helper.app \
    --apple-id "you@company.com" --team-id "TEAMID" \
    --password "@keychain:AC_PASSWORD" --wait
```

---

## Distributing via MDM

### Intune (Windows)
1. Package `aws-cli-credential-helper.exe` as a Win32 app (.intunewin)
2. Install command copies the signed exe into Program Files
3. Push a pre-filled `%APPDATA%\aws-cli-credential-helper\config.json` as
   a separate deployment step (see below) so users never see the setup
   wizard

### Jamf (Mac)
1. Package `aws-cli-credential-helper.app` as a signed, notarized `.pkg`
2. Pre-seed `~/.config/aws-cli-credential-helper/config.json` via a Jamf
   script

---

## Pre-seeding config for users (MDM mode)

Push a pre-filled `config.json` to the path above so the app skips the
setup wizard and opens ready to sign in:

**IDC example:**
```json
{
  "auth": {
    "method": "idc",
    "profileName": "aws-cli-credential-helper",
    "idc": {
      "ssoStartUrl": "https://yourcompany.awsapps.com/start",
      "ssoRegion": "us-east-1",
      "accountId": "123456789012",
      "permissionSetName": "PowerUserAccess"
    },
    "saml": { "idpAppUrl": "", "region": "us-east-1", "expectedAccountId": "" }
  }
}
```

**Direct SAML example:**
```json
{
  "auth": {
    "method": "saml",
    "profileName": "aws-cli-credential-helper",
    "idc": { "ssoStartUrl": "", "ssoRegion": "us-east-1", "accountId": "", "permissionSetName": "" },
    "saml": {
      "idpAppUrl": "https://yourcompany.okta.com/app/amazon_aws/exxxxxx/sso/saml",
      "region": "us-east-1",
      "expectedAccountId": "123456789012"
    }
  }
}
```

`permissionSetName` maps to the `sso_role_name` key in `~/.aws/config` —
it's labeled "Permission Set Name" in the UI because that's what it
actually is in IAM Identity Center, not an IAM role.

---

## After deployment: per-tool configuration (do this once, per tool, per user)

This tool only keeps the `aws-cli-credential-helper` profile signed in.
For each application, SDK, or CLI tool that should use these
credentials, open that tool's own settings and set its AWS profile field
to exactly:

```
aws-cli-credential-helper
```

If a tool was previously configured with a different profile name (e.g.
`default`, or a profile from earlier testing), it will keep using that
profile regardless of whether `aws-cli-credential-helper` is valid — this
helper cannot override another tool's own stored settings.

---

## Why there's no "refresh now" button

Both auth modes have a hard floor below which nothing can be done early:

- **IDC**: the profile style this tool writes is what AWS's own CLI docs
  call the "legacy non-refreshable configuration." AWS states plainly
  that automated token refresh is NOT supported for this style — no
  early renewal is possible. `aws sso logout` exists but signs out *all*
  SSO profiles on the machine, not just this one, so it's deliberately
  not used here — too broad a side effect for other profiles/developers
  sharing the machine.
- **SAML**: `aws login` credentials auto-rotate every ~15 minutes in the
  background (per AWS), up to the signed-in principal's session duration
  (max 12 hours) — this happens automatically and isn't something the
  app needs to do, but it also can't be triggered early.

Because of this, the app intentionally shows only two credential states —
**Signed in** or **Credentials expired** — rather than a countdown. An
earlier version showed a live countdown for both modes, but SAML's
~15-minute auto-rotation made that number misleading (it would look
alarmingly low yet reset itself constantly), so it was removed in favor
of the binary status that's actually actionable.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "AWS CLI Not Found" dialog on startup | Install/upgrade AWS CLI v2, restart computer, relaunch |
| "AWS CLI Upgrade Required" in SAML mode | `aws login` needs CLI v2.32.0+; upgrade and retry |
| IDC: browser doesn't open on Sign In | Check `ssoStartUrl` in Settings → Authentication is correct |
| SAML: Step 2 asks for root/IAM credentials instead of showing active session | Step 1 and Step 2 used different browsers — redo both in the same browser (use Copy Link if your SSO session lives in a non-default browser) |
| "Signed in to the wrong AWS account" | Credentials are valid but don't match the expected account ID in config — click Sign In / Step 2 again and pick the correct account |
| Credential check hangs / network error | VPN may be blocking the STS or sign-in endpoint |
| A tool still uses old/wrong AWS identity after signing in here | That tool has its own AWS profile setting — open its settings and point it at `aws-cli-credential-helper` (see section above) |
| Config lost after reinstalling the tool | Config lives in `%APPDATA%`/`~/.config`, not next to the exe — safe across reinstalls |
| IDC: no countdown shown next to "Signed in" | Requires `aws configure export-credentials` to succeed; older CLI versions or unusual credential setups may not support it — the app degrades gracefully and just shows "Signed in" with no number |
| IDC: countdown shows a huge number (hours in the thousands) | That would mean the app is reading the IAM Identity Center *portal* session instead of actual credential expiry — should not happen in this version, but if seen, check for a stale/old build |
| SAML never shows a countdown number | By design — see "Why there's no refresh now button" above |
| User wants to "refresh now" before actual expiry | Not possible for either mode — see "Why there's no refresh now button" above |
