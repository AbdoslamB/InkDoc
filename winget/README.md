# Windows Package Manager (winget) Manifests

This directory contains the official [Windows Package Manager (`winget`)](https://learn.microsoft.com/en-us/windows/package-manager/) manifest files for **InkDoc v1.0.0**.

## Local Installation Test

You can test installing InkDoc on your local Windows PC using these manifests:

```powershell
winget install --manifest winget/manifests/a/AbdoslamB/InkDoc/1.0.0/
```

## Submitting to Microsoft's Official `winget-pkgs` Repository

To make InkDoc installable by anyone globally via:

```powershell
winget install AbdoslamB.InkDoc
```

You can submit these manifests to Microsoft in either of two ways:

### Option 1: Automated via `wingetcreate` CLI (Recommended)

1. Install the official Microsoft Winget Create tool:
   ```powershell
   winget install Microsoft.WingetCreate
   ```
2. Submit the release:
   ```powershell
   wingetcreate submit https://github.com/AbdoslamB/inkdoc/releases/download/v1.0.0/inkdoc-setup.exe
   ```
   Follow the interactive prompts and enter your GitHub token to submit a Pull Request to [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs).

### Option 2: Manual Fork & Pull Request

1. Fork [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs).
2. Copy the folder `winget/manifests/a/AbdoslamB/InkDoc/1.0.0/` into the `manifests/a/AbdoslamB/InkDoc/1.0.0/` path of your fork.
3. Open a Pull Request on `microsoft/winget-pkgs`. Microsoft's automated validation bots will scan the installer, verify the SHA-256 hash, and merge it.
